"""Backtester: replays historical HL candles through the full
models -> aggregator -> risk pipeline with fees, funding and walk-forward
splits. Book/OI-dependent models degrade gracefully to FLAT (no historical
L2 data), which is reported honestly in per-model stats."""
import math
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from hyperliquid.info import Info

from reaper.aggregator import SignalAggregator
from reaper.config import PROJECT_ROOT
from reaper.logger import get_logger
from reaper.models import FLAT, LONG, atr_from_candles
from reaper.models.funding_rate import FundingRateModel
from reaper.models.liquidation_heatmap import LiquidationHeatmapModel
from reaper.models.mean_reversion import MeanReversionModel
from reaper.models.ml_forecast import MLForecastModel
from reaper.models.orderbook_imbalance import OrderbookImbalanceModel
from reaper.models.regime_detector import RegimeDetectorModel
from reaper.models.ta_model import TAModel
from reaper.models.vwap_model import VWAPModel

log = get_logger("backtester")

INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000,
               "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
TAKER_FEE = 0.00035          # 0.035% per side
MAKER_FEE = 0.00010          # 0.010% per side (base tier, post-only entry)
HOUR_MS = 3_600_000


# ----------------------------------------------------------------------
# historical data loading
# ----------------------------------------------------------------------
def load_historical(coin: str, interval: str, start_ms: int, end_ms: int,
                    api_url: str) -> pd.DataFrame:
    """Chunked candles_snapshot loading into a [t,o,h,l,c,v] frame.

    The API caps each response at 5000 candles AND only retains the most
    recent ~5000 candles per interval (1m ≈ 3.5 days, 5m ≈ 17 days,
    1h ≈ 208 days), so requests are chunked to never exceed the cap and a
    warning fires when retention truncates the requested range."""
    info = Info(api_url, skip_ws=True)
    step = INTERVAL_MS[interval]
    chunk = 5000 * step
    rows: list[dict] = []
    cur = start_ms
    while cur < end_ms:
        chunk_end = min(cur + chunk - step, end_ms)
        batch = info.candles_snapshot(coin, interval, cur, chunk_end)
        if batch:
            rows.extend(batch)
        cur = chunk_end + step
        time.sleep(0.1)  # be polite to the public endpoint
    if not rows:
        return pd.DataFrame(columns=["t", "o", "h", "l", "c", "v"])
    df = pd.DataFrame(rows)
    df = pd.DataFrame({
        "t": df["t"].astype("int64"),
        "o": df["o"].astype(float), "h": df["h"].astype(float),
        "l": df["l"].astype(float), "c": df["c"].astype(float),
        "v": df["v"].astype(float),
    })
    df = df.drop_duplicates("t").sort_values("t").reset_index(drop=True)
    got_days = (int(df["t"].iloc[-1]) - int(df["t"].iloc[0])) / 86_400_000
    req_days = (end_ms - start_ms) / 86_400_000
    log.info("loaded %d %s candles for %s (%.1f days)",
             len(df), interval, coin, got_days)
    if got_days < req_days * 0.9:
        log.warning("requested %.1f days but the exchange only retains "
                    "~5000 %s candles (%.1f days) — use a coarser interval "
                    "for longer lookbacks", req_days, interval, got_days)
    return df


DEFAULT_HISTORY_DIR = PROJECT_ROOT / "data" / "history"

_AGG = {"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"}


def load_local_history(coin: str, interval: str, start_ms: int, end_ms: int,
                       data_dir: Path = DEFAULT_HISTORY_DIR
                       ) -> pd.DataFrame | None:
    """Deep 1m history from data/history/ (scripts/download_history.py),
    resampled to the requested interval. None if no local file."""
    path = Path(data_dir) / f"{coin}_1m.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    df = df[(df["t"] >= start_ms) & (df["t"] <= end_ms)]
    if df.empty:
        return None
    if interval != "1m":
        idx = pd.to_datetime(df["t"], unit="ms", utc=True)
        res = (df.set_index(idx)[["o", "h", "l", "c", "v"]]
               .resample(interval.replace("m", "min")).agg(_AGG).dropna())
        res["t"] = (res.index.astype("int64") // 10 ** 6)
        df = res[["t", "o", "h", "l", "c", "v"]]
    return df.reset_index(drop=True)


def load_local_funding(coin: str, start_ms: int, end_ms: int,
                       data_dir: Path = DEFAULT_HISTORY_DIR
                       ) -> list[tuple[int, float]] | None:
    path = Path(data_dir) / f"{coin}_funding.csv"
    if not path.exists():
        return None
    out = []
    df = pd.read_csv(path)
    for ts, rate in zip(df["ts"], df["rate"]):
        if start_ms <= int(ts) <= end_ms:
            out.append((int(ts), float(rate)))
    return out or None


def get_history(coin: str, interval: str, start_ms: int, end_ms: int,
                api_url: str,
                data_dir: Path = DEFAULT_HISTORY_DIR) -> pd.DataFrame:
    """Local deep history when available, exchange API otherwise."""
    df = load_local_history(coin, interval, start_ms, end_ms, data_dir)
    if df is not None and len(df):
        days = (int(df["t"].iloc[-1]) - int(df["t"].iloc[0])) / 86_400_000
        log.info("using local history: %d %s candles for %s (%.1f days)",
                 len(df), interval, coin, days)
        return df
    return load_historical(coin, interval, start_ms, end_ms, api_url)


def get_funding(coin: str, start_ms: int, end_ms: int, api_url: str,
                data_dir: Path = DEFAULT_HISTORY_DIR
                ) -> list[tuple[int, float]]:
    rows = load_local_funding(coin, start_ms, end_ms, data_dir)
    if rows:
        log.info("using local funding history: %d points for %s",
                 len(rows), coin)
        return rows
    return load_funding(coin, start_ms, end_ms, api_url)


def load_funding(coin: str, start_ms: int, end_ms: int,
                 api_url: str) -> list[tuple[int, float]]:
    """Paginate funding_history into [(ts, hourly_rate), ...]."""
    info = Info(api_url, skip_ws=True)
    out: list[tuple[int, float]] = []
    cur = start_ms
    while cur < end_ms:
        batch = info.funding_history(coin, cur)
        if not batch:
            break
        for r in batch:
            ts = int(r["time"])
            if ts <= end_ms:
                out.append((ts, float(r["fundingRate"])))
        last = int(batch[-1]["time"])
        if last <= cur:
            break
        cur = last + 1
        if len(batch) < 500:
            break
        time.sleep(0.1)
    out.sort()
    log.info("loaded %d funding points for %s", len(out), coin)
    return out


# ----------------------------------------------------------------------
# simulation doubles for MarketBuffer / DB
# ----------------------------------------------------------------------
class SimBuffer:
    """Read-compatible MarketBuffer fed from historical candles. No L2 book
    or trades are available, so book-driven models return FLAT.

    Working bars (whatever interval the backtest runs at) are exposed under
    "1m" — the key the candle-driven models hardcode — and aliased under
    their true interval name so interval-aware models (ML) line up."""

    def __init__(self, coin: str, interval: str = "1m", maxlen: int = 1500):
        self.coins = [coin]
        self._coin = coin
        self._interval = interval
        dq = deque(maxlen=maxlen)
        self.candles = {coin: {"1m": dq}}
        if interval != "1m":
            self.candles[coin][interval] = dq  # alias, same deque
        if interval == "1m":
            # true 5m aggregate for the regime detector
            self.candles[coin]["5m"] = deque(maxlen=maxlen // 5 + 10)
        self.books = {coin: None}
        self.trades = {coin: deque(maxlen=10)}
        self.ctx = {coin: {"funding": 0.0, "open_interest": 0.0,
                           "mark_px": 0.0, "oracle_px": 0.0}}

    def push(self, candle: dict):
        c = self._coin
        self.candles[c]["1m"].append(candle)
        if self._interval == "1m":
            bucket_t = candle["t"] - candle["t"] % 300_000
            dq5 = self.candles[c]["5m"]
            if dq5 and dq5[-1]["t"] == bucket_t:
                agg = dq5[-1]
                agg["h"] = max(float(agg["h"]), float(candle["h"]))
                agg["l"] = min(float(agg["l"]), float(candle["l"]))
                agg["c"] = candle["c"]
                agg["v"] = float(agg["v"]) + float(candle["v"])
                agg["T"] = candle["T"]
            else:
                dq5.append({**candle, "t": bucket_t,
                            "T": bucket_t + 300_000})
        self.ctx[c]["mark_px"] = float(candle["c"])

    def mid(self, coin: str) -> float | None:
        dq = self.candles[coin]["1m"]
        return float(dq[-1]["c"]) if dq else None

    def latest_candles(self, coin: str, interval: str, n: int = 100) -> list:
        dq = self.candles[coin].get(interval)
        return list(dq)[-n:] if dq else []

    def seconds_since_msg(self) -> float:
        return 0.0


class SimFundingDB:
    """funding_window() over preloaded historical rows, sliced at sim time."""

    def __init__(self, rows: list[tuple[int, float]]):
        self.rows = rows
        self.now_ms = 0

    def funding_window(self, coin: str, since_ms: int) -> list[tuple]:
        return [(ts, r) for ts, r in self.rows
                if since_ms <= ts <= self.now_ms]


# ----------------------------------------------------------------------
# results
# ----------------------------------------------------------------------
@dataclass
class BacktestResults:
    total_return_pct: float
    sharpe_ratio: float           # annualized, risk-free = 0
    max_drawdown_pct: float
    win_rate: float
    profit_factor: float          # gross profit / gross loss
    total_trades: int
    avg_hold_minutes: float
    per_model_contribution: dict  # model -> net USD attributed
    equity_curve: list[float]
    label: str = ""
    trades: list = field(default_factory=list)
    exit_reasons: dict = field(default_factory=dict)  # reason -> (count, net USD)
    gross_pnl_usd: float = 0.0    # before fees and funding
    total_fees_usd: float = 0.0
    total_funding_usd: float = 0.0

    def summary(self) -> str:
        lines = [
            f"--- {self.label or 'backtest'} ---",
            f"total return:    {self.total_return_pct:+.2f}%",
            f"sharpe (ann.):   {self.sharpe_ratio:.2f}",
            f"max drawdown:    {self.max_drawdown_pct:.2f}%",
            f"win rate:        {self.win_rate:.1%}",
            f"profit factor:   {self.profit_factor:.2f}",
            f"trades:          {self.total_trades}",
            f"avg hold:        {self.avg_hold_minutes:.1f} min",
            "per-model contribution (net USD):",
        ]
        for m, v in sorted(self.per_model_contribution.items(),
                           key=lambda kv: -kv[1]):
            lines.append(f"  {m:<26s} {v:+10.2f}")
        if self.exit_reasons:
            lines.append("exit reasons (count, net USD):")
            for reason, (n, usd) in sorted(self.exit_reasons.items(),
                                           key=lambda kv: -kv[1][0]):
                lines.append(f"  {reason:<26s} {n:>4d}  {usd:+10.2f}")
        if self.total_trades:
            lines.append(f"cost structure: gross {self.gross_pnl_usd:+.2f}  "
                         f"fees -{self.total_fees_usd:.2f}  "
                         f"funding {-self.total_funding_usd:+.2f}")
        return "\n".join(lines)


# ----------------------------------------------------------------------
# the engine
# ----------------------------------------------------------------------
class Backtester:
    def __init__(self, cfg, min_confidence: float | None = None,
                 min_agreement: int = 3, notional_frac: float = 0.3,
                 signal_step: int = 5, start_equity: float = 10_000.0,
                 warmup: int = 120, interval: str = "5m",
                 data_api_url: str | None = None,
                 atr_sl_mult: float | None = None, rr: float = 2.0,
                 data_dir: Path = DEFAULT_HISTORY_DIR,
                 entry_style: str = "taker", entry_ttl_bars: int = 2):
        self.cfg = cfg
        self.interval = interval
        self.bar_ms = INTERVAL_MS[interval]
        self.data_api_url = data_api_url or cfg.api_url
        self.data_dir = data_dir
        self.rr = rr  # take-profit at rr × initial risk
        # maker mode: entry rests at the signal bar's close, fills only if a
        # later bar trades strictly through it (no queue-position optimism),
        # expires after entry_ttl_bars. Exits stay taker (stops are market).
        self.entry_style = entry_style
        self.entry_ttl_bars = entry_ttl_bars
        r = (getattr(cfg, "_raw", {}) or {}).get("risk", {}) or {}
        m = (getattr(cfg, "_raw", {}) or {}).get("models", {}) or {}
        self._gate_override = min_confidence
        self._live_gate = float(r.get("min_confidence", 0.62))
        self.min_confidence = (min_confidence if min_confidence is not None
                               else self._live_gate)  # resolved per-coin in run()
        # NOTE: live quorum is 5/8, but book/OI models have no historical
        # data and always vote FLAT in replay, so the backtest quorum is
        # taken over the candle-driven models only (default 3).
        self.min_agreement = min_agreement
        self.notional_frac = notional_frac
        self.signal_step = signal_step
        self.start_equity = start_equity
        self.warmup = warmup
        self.daily_dd = float(r.get("daily_drawdown_limit", 0.05))
        self.severe_dd = float(r.get("severe_drawdown_limit", 0.10))
        self.atr_sl_mult = (atr_sl_mult if atr_sl_mult is not None
                            else float(r.get("atr_sl_multiplier", 1.5)))
        # hold cap scales with bar size (48 bars), bounded by the scalp
        # floor and swing ceiling — a 4h cap on 1h bars would force-expire
        # every position after 4 bars before stops/targets can resolve
        scalp_ms = float(r.get("max_hold_hours_scalp", 4)) * HOUR_MS
        swing_ms = float(r.get("max_hold_hours_swing", 48)) * HOUR_MS
        self.max_hold_ms = int(max(scalp_ms, min(48 * self.bar_ms, swing_ms)))
        self.ml_dir = str((PROJECT_ROOT /
                           m.get("ml_model_dir", "models/")).resolve())
        self._cache: dict = {}

    # typical per-model vote confidence (models emit 0.45-0.65 in normal
    # conditions; 1.0 never happens). Measured on 17d of BTC 5m data the
    # resulting gate sits at the ~p95 of aggregated signal confidence.
    NOMINAL_CONF = 0.6

    def _resolve_gate(self, coin: str):
        """The book/OI models can never vote in replay (no historical L2
        data) and the ML model can't vote without a trained pkl, so the live
        confidence gate is scaled by the weight that CAN vote times the
        nominal per-model confidence — otherwise the gate sits above the
        maximum achievable score and 0 trades result."""
        if self._gate_override is not None:
            self.min_confidence = self._gate_override
            return
        from reaper.aggregator import BASE_WEIGHTS
        capable = (1.0
                   - BASE_WEIGHTS["OrderbookImbalanceModel"]
                   - BASE_WEIGHTS["LiquidationHeatmapModel"])
        if not (Path(self.ml_dir) / f"xgb_{coin}.pkl").exists():
            capable -= BASE_WEIGHTS["MLForecastModel"]
            log.warning("no trained ML model for %s — it votes FLAT in "
                        "this replay", coin)
        self.min_confidence = self._live_gate * capable * self.NOMINAL_CONF
        log.info("replay confidence gate %.3f (live %.2f × %.2f votable "
                 "weight × %.1f nominal confidence)", self.min_confidence,
                 self._live_gate, capable, self.NOMINAL_CONF)

    # -------------- data --------------
    def _data(self, coin: str, start_ms: int, end_ms: int):
        key = (coin, start_ms, end_ms)
        if key not in self._cache:
            df = get_history(coin, self.interval, start_ms, end_ms,
                             self.data_api_url, self.data_dir)
            funding = get_funding(coin, start_ms - 24 * HOUR_MS, end_ms,
                                  self.data_api_url, self.data_dir)
            self._cache[key] = (df, funding)
        return self._cache[key]

    def run(self, coin: str, start_ms: int, end_ms: int) -> BacktestResults:
        self._resolve_gate(coin)
        df, funding = self._data(coin, start_ms, end_ms)
        return self._simulate(coin, df, funding, label="full")

    def walk_forward(self, coin: str, start_ms: int, end_ms: int) -> dict:
        """70% train / 15% validation / 15% out-of-sample test splits.
        Returns {"train":…, "validation":…, "test":…, "oos_degraded":bool}."""
        self._resolve_gate(coin)
        df, funding = self._data(coin, start_ms, end_ms)
        n = len(df)
        i70, i85 = int(n * 0.70), int(n * 0.85)
        res = {
            "train": self._simulate(coin, df.iloc[:i70], funding, "train (70%)"),
            "validation": self._simulate(coin, df.iloc[i70:i85].reset_index(drop=True),
                                         funding, "validation (15%)"),
            "test": self._simulate(coin, df.iloc[i85:].reset_index(drop=True),
                                   funding, "out-of-sample test (15%)"),
        }
        tr, oos = res["train"].total_return_pct, res["test"].total_return_pct
        degraded = (tr > 0 and oos < tr * 0.70) or (tr > 0 and oos <= 0)
        res["oos_degraded"] = degraded
        return res

    # -------------- core loop --------------
    def _simulate(self, coin: str, df: pd.DataFrame,
                  funding_rows: list[tuple[int, float]],
                  label: str = "") -> BacktestResults:
        buf = SimBuffer(coin, self.interval)
        sim_db = SimFundingDB(funding_rows)
        models = [
            RegimeDetectorModel(),       # must run first: sets ctx regime
            TAModel(),
            MeanReversionModel(),
            FundingRateModel(sim_db),
            OrderbookImbalanceModel(),
            VWAPModel(),
            LiquidationHeatmapModel(),
            MLForecastModel(model_dir=self.ml_dir),
        ]
        agg = SignalAggregator()

        equity = self.start_equity
        curve: list[float] = []
        trades: list[dict] = []
        pos: dict | None = None
        pending: dict | None = None
        f_idx = 0
        cur_rate = 0.0
        day = ""
        day_open = 0.0
        blocked_day = ""

        def close_pos(px: float, ts: int, reason: str):
            nonlocal equity, pos
            sign = 1 if pos["dir"] == LONG else -1
            gross = pos["sz"] * (px - pos["entry"]) * sign
            fee = pos["sz"] * px * TAKER_FEE
            net = gross - fee - pos["entry_fee"] - pos["funding"]
            equity += net
            trades.append({**pos, "exit": px, "exit_t": ts, "gross": gross,
                           "net": net, "exit_fee": fee, "reason": reason})
            pos = None

        for row in df.itertuples(index=True):
            i, t = row.Index, int(row.t)
            o, h, l, c = float(row.o), float(row.h), float(row.l), float(row.c)
            buf.push({"t": t, "T": t + self.bar_ms, "o": o, "h": h, "l": l,
                      "c": c, "v": float(row.v), "n": 0})

            # advance funding clock
            while f_idx < len(funding_rows) and funding_rows[f_idx][0] <= t:
                cur_rate = funding_rows[f_idx][1]
                f_idx += 1
            buf.ctx[coin]["funding"] = cur_rate
            sim_db.now_ms = t

            cand_day = time.strftime("%Y-%m-%d", time.gmtime(t / 1000))

            # 1. execute pending entry on this candle
            if pending is not None and pos is None:
                if cand_day == blocked_day:
                    pending = None
                else:
                    entry_px, fee_rate = None, TAKER_FEE
                    if self.entry_style == "maker":
                        lp = pending["limit_px"]
                        touched = ((l < lp) if pending["dir"] == LONG
                                   else (h > lp))
                        if touched:
                            entry_px, fee_rate = lp, MAKER_FEE
                        else:
                            pending["ttl"] -= 1
                            if pending["ttl"] <= 0:
                                pending = None
                    else:
                        entry_px = o
                    if entry_px is not None:
                        notional = equity * self.notional_frac
                        sz = notional / entry_px
                        entry_fee = notional * fee_rate
                        r_px = pending["atr"] * self.atr_sl_mult
                        sign = 1 if pending["dir"] == LONG else -1
                        pos = {"dir": pending["dir"], "entry": entry_px,
                               "sz": sz, "entry_t": t,
                               "entry_fee": entry_fee, "funding": 0.0,
                               "sl": entry_px - sign * r_px,
                               "tp": entry_px + sign * self.rr * r_px,
                               "contrib": pending["contrib"],
                               "confidence": pending["confidence"]}
                        pending = None

            # 2. manage open position on this candle
            if pos is not None:
                sign = 1 if pos["dir"] == LONG else -1
                if t % HOUR_MS == 0:
                    # hourly funding: longs pay positive rates
                    pos["funding"] += pos["sz"] * c * cur_rate * sign
                if (sign > 0 and l <= pos["sl"]) or (sign < 0 and h >= pos["sl"]):
                    close_pos(pos["sl"], t, "stop_loss")
                elif (sign > 0 and h >= pos["tp"]) or (sign < 0 and l <= pos["tp"]):
                    close_pos(pos["tp"], t, "take_profit")
                elif t - pos["entry_t"] >= self.max_hold_ms:
                    close_pos(c, t, "time_expiry")

            # 3. mark-to-market equity + daily drawdown guard
            unreal = 0.0
            if pos is not None:
                unreal = pos["sz"] * (c - pos["entry"]) * \
                    (1 if pos["dir"] == LONG else -1)
            mtm = equity + unreal
            curve.append(mtm)
            if cand_day != day:
                day, day_open = cand_day, mtm
            if day_open > 0:
                dd = 1 - mtm / day_open
                if dd >= self.severe_dd and pos is not None:
                    close_pos(c, t, "severe_drawdown")
                    blocked_day = cand_day
                elif dd >= self.daily_dd:
                    blocked_day = cand_day

            # 4. new signal at candle close
            if (pos is None and i >= self.warmup
                    and i % self.signal_step == 0
                    and cand_day != blocked_day):
                tickets = [m.compute(coin, buf) for m in models]
                sig = agg.aggregate(coin, tickets)
                votes = (sig.long_votes if sig.direction == LONG
                         else sig.short_votes)
                if (sig.direction != FLAT
                        and sig.confidence >= self.min_confidence
                        and votes >= self.min_agreement):
                    atr = atr_from_candles(buf.latest_candles(coin, "1m", 60))
                    if atr and atr > 0:
                        contrib = {
                            tk.model: sig.weights.get(tk.model, 0) * tk.confidence
                            for tk in tickets
                            if tk.direction == sig.direction}
                        total_w = sum(contrib.values()) or 1.0
                        contrib = {k: v / total_w for k, v in contrib.items()}
                        pending = {"dir": sig.direction,
                                   "confidence": sig.confidence,
                                   "atr": atr, "contrib": contrib,
                                   "limit_px": c,
                                   "ttl": self.entry_ttl_bars}

        if pos is not None and len(df):
            close_pos(float(df["c"].iloc[-1]), int(df["t"].iloc[-1]),
                      "end_of_data")

        return self._metrics(curve, trades, label)

    # -------------- metrics --------------
    def _metrics(self, curve: list[float], trades: list[dict],
                 label: str) -> BacktestResults:
        final = curve[-1] if curve else self.start_equity
        total_ret = (final / self.start_equity - 1) * 100

        hourly = curve[::60] or [self.start_equity]
        rets = [hourly[i] / hourly[i - 1] - 1 for i in range(1, len(hourly))
                if hourly[i - 1] > 0]
        sharpe = 0.0
        if len(rets) > 2:
            mean = sum(rets) / len(rets)
            var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
            std = math.sqrt(var)
            if std > 0:
                sharpe = mean / std * math.sqrt(24 * 365)

        peak, max_dd = -1e18, 0.0
        for v in curve:
            peak = max(peak, v)
            if peak > 0:
                max_dd = max(max_dd, (peak - v) / peak)

        wins = [tr for tr in trades if tr["net"] > 0]
        losses = [tr for tr in trades if tr["net"] <= 0]
        gross_p = sum(tr["net"] for tr in wins)
        gross_l = abs(sum(tr["net"] for tr in losses))
        pf = gross_p / gross_l if gross_l > 0 else (
            float("inf") if gross_p > 0 else 0.0)
        win_rate = len(wins) / len(trades) if trades else 0.0
        avg_hold = (sum((tr["exit_t"] - tr["entry_t"]) for tr in trades)
                    / len(trades) / 60_000 if trades else 0.0)

        contrib: dict[str, float] = {}
        for tr in trades:
            for model, share in tr.get("contrib", {}).items():
                contrib[model] = contrib.get(model, 0.0) + tr["net"] * share

        reasons: dict[str, list] = {}
        for tr in trades:
            slot = reasons.setdefault(tr["reason"], [0, 0.0])
            slot[0] += 1
            slot[1] += tr["net"]
        exit_reasons = {k: (n, round(usd, 2)) for k, (n, usd)
                        in reasons.items()}
        gross = sum(tr["gross"] for tr in trades)
        fees = sum(tr["entry_fee"] + tr["exit_fee"] for tr in trades)
        funding_cost = sum(tr["funding"] for tr in trades)

        return BacktestResults(
            total_return_pct=total_ret,
            sharpe_ratio=sharpe,
            max_drawdown_pct=max_dd * 100,
            win_rate=win_rate,
            profit_factor=pf,
            total_trades=len(trades),
            avg_hold_minutes=avg_hold,
            per_model_contribution={k: round(v, 2)
                                    for k, v in contrib.items()},
            equity_curve=curve,
            label=label,
            trades=trades,
            exit_reasons=exit_reasons,
            gross_pnl_usd=round(gross, 2),
            total_fees_usd=round(fees, 2),
            total_funding_usd=round(funding_cost, 2),
        )
