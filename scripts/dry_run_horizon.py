#!/usr/bin/env python3
"""TASK 3 — Longer-horizon config shadow run (NO real orders).

Runs the SAME live data feed + same 8 models + same aggregator + same entry
gate as the live aggressive bot, but applies the Phase 4.6 "best training"
horizon config — 15m bars for ATR, ATR x1.5 stop, 2R take-profit, trailing at
1.5R, a long hold cap, and NO maker-timeout skip (hypothetical fill at mid).

It places NO orders. Every would-be trade and its simulated SL/TP/trailing
outcome is logged to a `shadow_trades` table so we can compare, on the SAME
market conditions over the same days:
  * does the longer horizon catch ETH's downtrend in fewer/larger wins?
  * does it avoid the ARB/SOL/WIF noise losses aggressive mode is taking?

Full isolation: uses its OWN SQLite (data/shadow.db) for pollers + shadow_trades
so it cannot touch the live bot's database. Read-only market data only.
"""
import argparse
import json
import signal as _signal
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.aggregator import SignalAggregator
from reaper.config import PROJECT_ROOT, Config
from reaper.data.buffer import MarketBuffer
from reaper.data.rest_pollers import RestPollers
from reaper.data.websocket_feed import WebSocketFeed
from reaper.db import DB
from hyperliquid.info import Info
from reaper.logger import get_logger
from reaper.models import FLAT, LONG, SHORT
from reaper.models.funding_rate import FundingRateModel
from reaper.models.liquidation_heatmap import LiquidationHeatmapModel
from reaper.models.mean_reversion import MeanReversionModel
from reaper.models.ml_forecast import MLForecastModel
from reaper.models.orderbook_imbalance import OrderbookImbalanceModel
from reaper.models.regime_detector import RegimeDetectorModel
from reaper.models.ta_model import TAModel
from reaper.models.vwap_model import VWAPModel
from reaper.risk.manager import PAPER_AGGRESSIVE_GATES

log = get_logger("shadow")
_running = True


def _stop(_s, _f):
    global _running
    _running = False


SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin TEXT, direction TEXT,
    entry_ts INTEGER, entry_px REAL,
    sl REAL, tp REAL, atr15 REAL, r_px REAL,
    conf REAL, agreement INTEGER, regime TEXT,
    status TEXT,                         -- OPEN | CLOSED
    exit_ts INTEGER, exit_px REAL, exit_reason TEXT,
    pnl_pct REAL, net_pnl_pct REAL, r_multiple REAL, max_hold_s REAL,
    -- per-model ticket breakdown at entry (TASK C: OB-confirmation experiment)
    ob_direction TEXT, ob_conf REAL, tickets_json TEXT
);
"""

# columns added after the table first shipped — ALTER idempotently so an
# existing shadow.db picks them up without a wipe.
_MIGRATIONS = [
    ("ob_direction", "TEXT"),
    ("ob_conf", "REAL"),
    ("tickets_json", "TEXT"),
]


def migrate(conn):
    have = {r[1] for r in conn.execute("PRAGMA table_info(shadow_trades)")}
    for col, typ in _MIGRATIONS:
        if col not in have:
            conn.execute(f"ALTER TABLE shadow_trades ADD COLUMN {col} {typ}")
    conn.commit()


def prime_buffer(api_url, coins, intervals, buf, lookback):
    """Backfill recent candles via REST so ATR/indicators are ready at start
    instead of waiting hours for the WS stream to fill the buffer cold."""
    info = Info(api_url, skip_ws=True)
    span_ms = {"1m": 60000, "5m": 300000, "15m": 900000, "1h": 3600000}
    now = int(time.time() * 1000)
    for coin in coins:
        for iv in intervals:
            start = now - lookback.get(iv, 300) * span_ms.get(iv, 60000)
            try:
                snap = info.candles_snapshot(coin, iv, start, now) or []
            except Exception as e:
                log.warning("prime %s/%s failed: %s", coin, iv, e)
                continue
            for c in snap:
                buf.on_candle(coin, iv, c)
        log.info("primed %s: %s", coin,
                 {iv: len(buf.latest_candles(coin, iv, 99999))
                  for iv in intervals})


def resample_15m_atr(candles_1m: list, period: int = 14) -> float | None:
    """Build 15m OHLC from 1m buffer candles and return SMA-ATR over `period`."""
    if not candles_1m or len(candles_1m) < (period + 1) * 15:
        return None
    buckets: dict[int, dict] = {}
    for c in candles_1m:
        t = int(c["t"]); m = (t // 900000) * 900000
        h, l, cl = float(c["h"]), float(c["l"]), float(c["c"])
        b = buckets.get(m)
        if b is None:
            buckets[m] = {"h": h, "l": l, "c": cl}
        else:
            b["h"] = max(b["h"], h); b["l"] = min(b["l"], l); b["c"] = cl
    bars = [buckets[k] for k in sorted(buckets)]
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        trs.append(max(bars[i]["h"] - bars[i]["l"],
                       abs(bars[i]["h"] - bars[i - 1]["c"]),
                       abs(bars[i]["l"] - bars[i - 1]["c"])))
    w = trs[-period:]
    return sum(w) / len(w)


class ShadowBook:
    """In-memory tracker for open simulated positions (mirrors RiskManager
    Layer 2: SL, 2R TP, trailing at 1.5R / 1*ATR, time expiry)."""

    ATR_SL_MULT = 1.5
    TRAIL_ACT_R = 1.5
    TRAIL_ATR_MULT = 1.0

    def __init__(self, conn, max_hold_s, fee_rt_pct):
        self.conn = conn
        self.max_hold_s = max_hold_s
        self.fee_rt_pct = fee_rt_pct
        self.open: dict[str, dict] = {}

    def has(self, coin):
        return coin in self.open

    def enter(self, coin, direction, px, atr15, conf, agreement, regime,
              tickets=None):
        is_long = direction == LONG
        r = atr15 * self.ATR_SL_MULT
        sl = px - r if is_long else px + r
        tp = px + 2 * r if is_long else px - 2 * r
        ts = int(time.time() * 1000)
        # full per-model ticket snapshot at entry — feeds the later
        # OB-confirmation experiment (split shadow trades by whether OB agreed)
        tj = {t.model: {"dir": t.direction, "conf": round(t.confidence, 4)}
              for t in (tickets or [])}
        ob = tj.get("OrderbookImbalanceModel", {})
        cur = self.conn.execute(
            "INSERT INTO shadow_trades (coin,direction,entry_ts,entry_px,sl,tp,"
            "atr15,r_px,conf,agreement,regime,status,max_hold_s,"
            "ob_direction,ob_conf,tickets_json) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,'OPEN',?,?,?,?)",
            (coin, direction, ts, px, sl, tp, atr15, r, conf, agreement,
             regime, self.max_hold_s, ob.get("dir"), ob.get("conf"),
             json.dumps(tj)))
        self.conn.commit()
        self.open[coin] = {
            "id": cur.lastrowid, "is_long": is_long, "entry_px": px,
            "sl": sl, "tp": tp, "r_px": r, "opened_ts": time.time(),
            "trailing": False, "atr15": atr15}
        log.warning("SHADOW OPEN %s %s @ %.6f sl=%.6f tp=%.6f conf=%.2f "
                    "votes=%d regime=%s", direction, coin, px, sl, tp, conf,
                    agreement, regime)

    def manage(self, coin, mid):
        tr = self.open.get(coin)
        if tr is None or mid is None:
            return
        is_long = tr["is_long"]
        hit_sl = mid <= tr["sl"] if is_long else mid >= tr["sl"]
        hit_tp = mid >= tr["tp"] if is_long else mid <= tr["tp"]
        reason = None
        if hit_sl:
            reason = ("trailing stop" if tr["trailing"] else "stop loss")
            exit_px = tr["sl"]
        elif hit_tp:
            reason = "take profit"; exit_px = tr["tp"]
        elif time.time() - tr["opened_ts"] > self.max_hold_s:
            reason = "max hold expired"; exit_px = mid
        if reason:
            self._close(coin, exit_px, reason)
            return
        # trailing activation at >= 1.5R, trail at 1*ATR15
        unreal_r = ((mid - tr["entry_px"]) if is_long
                    else (tr["entry_px"] - mid)) / tr["r_px"]
        if unreal_r >= self.TRAIL_ACT_R:
            new_sl = (mid - tr["atr15"] * self.TRAIL_ATR_MULT if is_long
                      else mid + tr["atr15"] * self.TRAIL_ATR_MULT)
            improved = (new_sl > tr["sl"]) if is_long else (new_sl < tr["sl"])
            if improved:
                tr["sl"] = new_sl; tr["trailing"] = True

    def _close(self, coin, exit_px, reason):
        tr = self.open.pop(coin)
        sign = 1.0 if tr["is_long"] else -1.0
        pnl_pct = (exit_px - tr["entry_px"]) / tr["entry_px"] * sign
        net = pnl_pct - self.fee_rt_pct
        r_mult = ((exit_px - tr["entry_px"]) * sign) / tr["r_px"] \
            if tr["r_px"] else 0.0
        self.conn.execute(
            "UPDATE shadow_trades SET status='CLOSED',exit_ts=?,exit_px=?,"
            "exit_reason=?,pnl_pct=?,net_pnl_pct=?,r_multiple=? WHERE id=?",
            (int(time.time() * 1000), exit_px, reason, pnl_pct, net, r_mult,
             tr["id"]))
        self.conn.commit()
        log.warning("SHADOW CLOSE %s %s @ %.6f  pnl=%.3f%% net=%.3f%% (%.2fR)",
                    coin, reason, exit_px, pnl_pct * 100, net * 100, r_mult)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(PROJECT_ROOT / "data" / "shadow.db"))
    ap.add_argument("--min-conf", type=float, default=None,
                    help="override entry confidence gate (default: live mode)")
    ap.add_argument("--min-agreement", type=int, default=None)
    ap.add_argument("--max-hold-hours", type=float, default=8.0)
    ap.add_argument("--fee-rt-pct", type=float, default=0.00045,
                    help="assumed maker round-trip fee for net pnl (0.045%)")
    ap.add_argument("--loop-seconds", type=float, default=10.0)
    args = ap.parse_args()

    cfg = Config()
    t_raw = cfg._raw.get("trading", {}) or {}
    m_raw = cfg._raw.get("models", {}) or {}
    mode = t_raw.get("mode", "conservative")
    coins_active = t_raw.get("coins_active", cfg.coins)
    ml_dir = str((PROJECT_ROOT / m_raw.get("ml_model_dir", "models/")).resolve())

    # gate: match the live bot's effective gate unless overridden, so the only
    # variable under test is the 15m horizon / stop geometry.
    if mode == "paper_aggressive":
        gate_conf = PAPER_AGGRESSIVE_GATES["min_confidence"]
        gate_agree = PAPER_AGGRESSIVE_GATES["min_model_agreement"]
    else:
        risk_raw = cfg._raw.get("risk", {}) or {}
        gate_conf = float(risk_raw.get("min_confidence", 0.62))
        gate_agree = int(risk_raw.get("min_model_agreement", 5))
    if args.min_conf is not None:
        gate_conf = args.min_conf
    if args.min_agreement is not None:
        gate_agree = args.min_agreement
    max_hold_s = args.max_hold_hours * 3600

    log.info("SHADOW horizon run — 15m ATR x1.5, 2R TP, trail 1.5R, hold<=%.0fh"
             " | gate conf>=%.2f agree>=%d | live mode=%s | db=%s | NO ORDERS",
             args.max_hold_hours, gate_conf, gate_agree, mode, args.db)

    db = DB(args.db)
    conn = sqlite3.connect(args.db, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    migrate(conn)

    buf = MarketBuffer(cfg.coins, cfg.candle_intervals, cfg.candle_buffer_size)
    feed = WebSocketFeed(cfg.api_url, buf, cfg.candle_intervals,
                         cfg.stale_feed_seconds)
    pollers = RestPollers(cfg.api_url, cfg, buf, db)
    feed.start()
    pollers.start()

    models = [
        RegimeDetectorModel(),
        TAModel(),
        MeanReversionModel(),
        FundingRateModel(db),
        OrderbookImbalanceModel(
            top_levels=int(m_raw.get("ob_top_levels", 10)),
            min_imbalance=float(m_raw.get("ob_min_imbalance", 0.30))),
        VWAPModel(),
        LiquidationHeatmapModel(),
        MLForecastModel(model_dir=ml_dir,
                        min_confidence=float(m_raw.get("ml_min_confidence",
                                                       0.55))),
    ]
    aggregator = SignalAggregator()
    book = ShadowBook(conn, max_hold_s, args.fee_rt_pct)

    _signal.signal(_signal.SIGINT, _stop)
    _signal.signal(_signal.SIGTERM, _stop)

    log.info("priming buffer with recent candles via REST...")
    prime_buffer(cfg.api_url, cfg.coins, cfg.candle_intervals, buf,
                 lookback={"1m": 400, "5m": 300, "1h": 300})
    log.info("warming up — letting feed/pollers fill book/ctx (15s)")
    time.sleep(15)

    last_status = 0.0
    while _running:
        cycle = time.time()
        for coin in coins_active:
            mid = buf.mid(coin)
            # manage any open shadow position first
            if book.has(coin):
                book.manage(coin, mid)
                continue
            tickets = [m.compute(coin, buf) for m in models]
            sig = aggregator.aggregate(coin, tickets)
            if sig.direction == FLAT:
                continue
            agreement = (sig.long_votes if sig.direction == LONG
                         else sig.short_votes)
            if sig.confidence < gate_conf or agreement < gate_agree:
                continue
            atr15 = resample_15m_atr(
                [{"t": c["t"], "h": c["h"], "l": c["l"], "c": c["c"]}
                 for c in buf.latest_candles(coin, "1m", cfg.candle_buffer_size)])
            if not atr15 or not mid:
                log.debug("skip %s: no 15m ATR / mid yet", coin)
                continue
            book.enter(coin, sig.direction, mid, atr15, sig.confidence,
                       agreement, sig.regime, tickets=tickets)

        if time.time() - last_status > 300:
            n_open = len(book.open)
            n_closed = conn.execute(
                "SELECT COUNT(*) FROM shadow_trades WHERE status='CLOSED'"
            ).fetchone()[0]
            log.info("shadow status: %d open, %d closed | feed %.1fs old",
                     n_open, n_closed, buf.seconds_since_msg())
            last_status = time.time()

        time.sleep(max(0.0, args.loop_seconds - (time.time() - cycle)))

    log.info("shadow run stopping — %d positions left open", len(book.open))
    feed.stop() if hasattr(feed, "stop") else None
    conn.close()


if __name__ == "__main__":
    main()
