#!/usr/bin/env python3
"""Full trading loop: data -> models -> aggregator -> risk -> execution.

Replaces run_phase1.py as the live entrypoint once Phases 2-4 are verified.
Every entry passes through RiskManager.can_open(); every open position is
managed by RiskManager.check_open_positions() each cycle."""
import fcntl
import json
import signal
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper import alerts
from reaper.aggregator import (REGIME_NAMES, SCALP_WEIGHTS, TREND_WEIGHTS,
                               SignalAggregator, apply_regime_bias)
from reaper.config import PROJECT_ROOT, Config
from reaper.data.buffer import MarketBuffer
from reaper.data.rest_pollers import RestPollers
from reaper.data.spot_poller import SpotPoller
from reaper.data.websocket_feed import WebSocketFeed
from reaper.db import DB
from reaper.execution.exchange_client import ExchangeClient
from reaper.logger import get_logger
from reaper.data import liquidation_store
from reaper.models import FLAT, LONG, SHORT, atr_from_candles
from reaper.models.cascade_bounce import CascadeBounceModel
from reaper.models.funding_rate import FundingRateModel
from reaper.models.liquidation_heatmap import LiquidationHeatmapModel
from reaper.models.mean_reversion import MeanReversionModel
from reaper.models.ml_forecast import MLForecastModel
from reaper.models.orderbook_imbalance import OrderbookImbalanceModel
from reaper.models.regime_detector import RegimeDetectorModel
from reaper.models.ta_model import TAModel
from reaper.models.vwap_model import VWAPModel
from reaper.risk.manager import (CLOSE_PENDING_TIMEOUT_S, CONF_GATE_EPS,
                                  RiskManager, with_retry)
from reaper.risk.state import BotState

log = get_logger("bot")
_running = True


def _sig(_s, _f):
    global _running
    _running = False


class SignalWriter:
    """Writes aggregated signals to the existing `signals` table over its
    own connection (db.py is frozen Phase 1 code and has no insert helper)."""

    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path, timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")

    def log(self, coin: str, model: str, direction: str, confidence: float,
            meta: dict):
        with self._conn:
            self._conn.execute(
                "INSERT INTO signals (ts,coin,model,direction,confidence,meta)"
                " VALUES (?,?,?,?,?,?)",
                (int(time.time() * 1000), coin, model, direction, confidence,
                 json.dumps(meta, default=str)))


def long_confirmation_count(model_votes: dict, models: set) -> int:
    """How many of `models` are actively voting LONG (legacy Change B gate).
    Superseded by long_structural_gate() for live entries; retained for the
    dashboard SHORT-mirror display and the regression suite."""
    return sum(1 for t in model_votes.values()
               if t.model in models and t.direction == LONG)


def _momentum_cooldown_ok(coin: str, buf, params: dict) -> tuple[bool, str, dict]:
    """Signal 4 — momentum cooldown (no recent sharp pump).

    Blocks LONG entry if price moved sharply UPWARD over a recent short window.
    Even with all three structural signals green, those signals ARE the
    consequence of a pump (spot leads + OI rises + book turns bid-heavy as the
    move runs); by the time all three confirm the move is largely done and the
    entry catches the top. The losing-LONG signature in 374 round-trips is a
    4.6-minute MEDIAN hold — enter near the top, stopped on the retrace within
    minutes. Letting the post-pump consolidation happen first fixes the TIMING;
    the structural signals themselves stay valid.

    Checks three lookback windows on 5m candles (5m / 10m / 15m) and blocks if
    ANY exceeds its threshold. Returns (ok, reason, moves) where `moves` carries
    the three % moves for logging + the dashboard. Fail-OPEN during warmup
    (too few candles): momentum can't be judged, so it must not block.

    NOTE: the symmetric SHORT dump-cooldown (block SHORT after a sharp DROP —
    catching the bottom) is deliberately NOT added here. The SHORT side is
    working (57% win, +$21.46 net); mirror this with inverted moves only if
    SHORT entry quality degrades.
    """
    moves = {"move_5m": None, "move_10m": None, "move_15m": None}
    if not params.get("pump_cooldown_enabled", True):
        return True, "cooldown_disabled", moves
    candles = buf.latest_candles(coin, "5m", 6)  # last ~30 minutes
    if len(candles) < 3:
        return True, "insufficient_candles", moves  # fail-open during warmup

    thr_1 = float(params.get("pump_threshold_1", 0.005))   # 0.5% in 5m
    thr_2 = float(params.get("pump_threshold_2", 0.008))   # 0.8% in 10m
    thr_3 = float(params.get("pump_threshold_3", 0.012))   # 1.2% in 15m

    current = float(candles[-1]["c"])
    prev_1 = float(candles[-2]["c"])                       # 5m ago
    prev_2 = float(candles[-3]["c"])                       # 10m ago
    prev_3 = float(candles[-4]["c"]) if len(candles) >= 4 else prev_2  # 15m ago

    move_1 = (current - prev_1) / prev_1 if prev_1 else 0.0
    move_2 = (current - prev_2) / prev_2 if prev_2 else 0.0
    move_3 = (current - prev_3) / prev_3 if prev_3 else 0.0
    moves["move_5m"], moves["move_10m"], moves["move_15m"] = (
        move_1, move_2, move_3)

    if move_1 > thr_1:
        return (False, f"pump_5m:+{move_1*100:.3f}%>{thr_1*100:.1f}%", moves)
    if move_2 > thr_2:
        return (False, f"pump_10m:+{move_2*100:.3f}%>{thr_2*100:.1f}%", moves)
    if move_3 > thr_3:
        return (False, f"pump_15m:+{move_3*100:.3f}%>{thr_3*100:.1f}%", moves)
    return (True, f"momentum_ok:5m={move_1*100:.3f}% 10m={move_2*100:.3f}% "
            f"15m={move_3*100:.3f}%", moves)


def long_structural_gate(coin: str, buf, params: dict) -> tuple[bool, dict]:
    """Strong structural LONG gate (2026-06-17), supersedes Change B.

    A LONG entry must clear ALL of these signals, each independently confirmed
    in this project's research as pointing the right way:
      1. SPOT LEADING perp  — real demand, not leverage-driven (spot return
         over the lookback exceeds perp return and is itself positive).
      2. OI RISING with price — fresh longs entering, structural participation
         (not short-covering / liquidation exhaustion).
      3. ORDERBOOK BID-HEAVY — live microstructure confirms buy pressure now.
      4. MOMENTUM COOLDOWN — no recent sharp pump (2026-06-18, anti-pump-top):
         the three signals above flash green AS a pump runs; entering then
         catches the top. Block if price ran up past threshold in the last
         5m/10m/15m and let the consolidation happen first.

    Fail-safe: if any of signals 1-3 can't be computed (missing/stale history,
    no book) that signal FAILS and the LONG is blocked. Better to miss a good
    LONG than take a bad one — matters most during startup before history
    accumulates. Signal 4 instead fails OPEN during warmup: momentum can't be
    judged with too few candles, so it must not block on its own.

    Returns (allowed, detail) where detail carries each signal's pass/fail and
    the underlying numbers for logging + the dashboard. SHORTs never call this.
    """
    spot_lead_thr = float(params.get("spot_lead_threshold", 0.0002))
    oi_rise_thr = float(params.get("oi_rise_threshold", 0.001))
    ob_bid_thr = float(params.get("ob_bid_threshold", 0.20))
    spot_lookback = float(params.get("spot_lookback_minutes", 5))
    oi_lookback = float(params.get("oi_lookback_minutes", 5))
    top_n = int(params.get("ob_top_levels", 10))

    detail = {
        "spot_leading": False, "oi_rising": False, "ob_bid_heavy": False,
        "momentum_ok": False,
        "spot_ret": None, "perp_ret": None, "oi_change": None,
        "imbalance": None,
        "move_5m": None, "move_10m": None, "move_15m": None, "pump_detail": "",
        "allowed": False, "block_reason": "",
    }

    # --- Signal 1: spot leadership over the lookback window -----------------
    spot_now = buf.spot_price(coin)
    spot_then = buf.spot_price_n_minutes_ago(coin, spot_lookback)
    perp_now = buf.mid(coin)
    c5 = buf.latest_candles(coin, "5m", 2)
    perp_then = float(c5[-2]["c"]) if len(c5) >= 2 else None
    if all(v is not None and v > 0 for v in
           (spot_now, spot_then, perp_now, perp_then)):
        spot_ret = (spot_now - spot_then) / spot_then
        perp_ret = (perp_now - perp_then) / perp_then
        detail["spot_ret"] = spot_ret
        detail["perp_ret"] = perp_ret
        detail["spot_leading"] = spot_ret > perp_ret and spot_ret > spot_lead_thr

    # --- Signal 2: OI rising (fresh buying) ---------------------------------
    oi_now = (buf.ctx.get(coin) or {}).get("open_interest")
    oi_then = buf.oi_n_minutes_ago(coin, oi_lookback)
    if oi_now and oi_then:
        detail["oi_change"] = (oi_now - oi_then) / oi_then
        detail["oi_rising"] = oi_now > oi_then * (1 + oi_rise_thr)

    # --- Signal 3: orderbook bid-heavy --------------------------------------
    book = buf.books.get(coin)
    if book and book.get("bids") and book.get("asks"):
        bid_vol = sum(sz for _px, sz in book["bids"][:top_n])
        ask_vol = sum(sz for _px, sz in book["asks"][:top_n])
        total = bid_vol + ask_vol
        if total > 0:
            imb = (bid_vol - ask_vol) / total
            detail["imbalance"] = imb
            detail["ob_bid_heavy"] = imb >= ob_bid_thr

    # --- Signal 4: momentum cooldown (no recent sharp pump) -----------------
    m_ok, m_reason, m_moves = _momentum_cooldown_ok(coin, buf, params)
    detail["momentum_ok"] = m_ok
    detail["pump_detail"] = m_reason
    detail.update(m_moves)

    if not detail["spot_leading"]:
        detail["block_reason"] = "spot_not_leading"
    elif not detail["oi_rising"]:
        detail["block_reason"] = "oi_not_rising"
    elif not detail["ob_bid_heavy"]:
        detail["block_reason"] = "book_not_bid_heavy"
    elif not detail["momentum_ok"]:
        detail["block_reason"] = "recent_pump"
    else:
        detail["allowed"] = True
    return detail["allowed"], detail


def _dump_cooldown_ok(coin: str, buf, params: dict) -> tuple[bool, str, dict]:
    """SHORT Signal 4 — dump cooldown (no recent sharp DROP).

    Mirror of _momentum_cooldown_ok for the SHORT side (2026-06-19). The three
    structural SHORT signals (spot lagging + OI rising with falling price + book
    ask-heavy) all flash green AS a dump runs; entering then catches the BOTTOM
    and gets stopped on the bounce — the SHORT analogue of the losing-LONG
    pump-top signature. Block SHORT entry if price dropped sharply over a recent
    short window and let the dump exhaust first; the structural signals stay
    valid, only the TIMING is corrected.

    Checks three lookback windows on 5m candles (5m / 10m / 15m) and blocks if
    ANY exceeds its (downward) threshold. Returns (ok, reason, moves) where
    `moves` carries the three % moves for logging + the dashboard. Fail-OPEN
    during warmup (too few candles): momentum can't be judged, so it must not
    block.
    """
    moves = {"move_5m": None, "move_10m": None, "move_15m": None}
    if not params.get("dump_cooldown_enabled", True):
        return True, "cooldown_disabled", moves
    candles = buf.latest_candles(coin, "5m", 6)  # last ~30 minutes
    if len(candles) < 3:
        return True, "insufficient_candles", moves  # fail-open during warmup

    thr_1 = float(params.get("dump_threshold_1", 0.005))   # 0.5% in 5m
    thr_2 = float(params.get("dump_threshold_2", 0.008))   # 0.8% in 10m
    thr_3 = float(params.get("dump_threshold_3", 0.012))   # 1.2% in 15m

    current = float(candles[-1]["c"])
    prev_1 = float(candles[-2]["c"])                       # 5m ago
    prev_2 = float(candles[-3]["c"])                       # 10m ago
    prev_3 = float(candles[-4]["c"]) if len(candles) >= 4 else prev_2  # 15m ago

    move_1 = (current - prev_1) / prev_1 if prev_1 else 0.0
    move_2 = (current - prev_2) / prev_2 if prev_2 else 0.0
    move_3 = (current - prev_3) / prev_3 if prev_3 else 0.0
    moves["move_5m"], moves["move_10m"], moves["move_15m"] = (
        move_1, move_2, move_3)

    if move_1 < -thr_1:
        return (False, f"dump_5m:{move_1*100:.3f}%<-{thr_1*100:.1f}%", moves)
    if move_2 < -thr_2:
        return (False, f"dump_10m:{move_2*100:.3f}%<-{thr_2*100:.1f}%", moves)
    if move_3 < -thr_3:
        return (False, f"dump_15m:{move_3*100:.3f}%<-{thr_3*100:.1f}%", moves)
    return (True, f"momentum_ok:5m={move_1*100:.3f}% 10m={move_2*100:.3f}% "
            f"15m={move_3*100:.3f}%", moves)


def short_structural_gate(coin: str, buf, params: dict) -> tuple[bool, dict]:
    """Strong structural SHORT gate (2026-06-19), mirror of long_structural_gate.

    A SHORT entry must clear ALL of these signals, each the inverse of the LONG
    gate's and each pointing the right way for genuine downside participation:
      1. SPOT LAGGING perp — real selling, not a leverage-driven bounce (spot
         return over the lookback is below perp return AND itself negative; spot
         falling faster than perp, or perp trying to bounce while spot still
         falls).
      2. OI RISING with FALLING price — fresh shorts entering, structural
         participation (not short-covering / long-liquidation exhaustion). This
         is the new_shorts signal from the Phase 4.6 OI decomposition.
      3. ORDERBOOK ASK-HEAVY — live microstructure confirms sell pressure now.
      4. DUMP COOLDOWN — no recent sharp drop: the three signals above flash
         green AS a dump runs; entering then catches the bottom. Block if price
         fell past threshold in the last 5m/10m/15m and let the dump exhaust.

    Fail-safe (mirror of the LONG gate): if any of signals 1-3 can't be computed
    (missing/stale spot or OI history, no book) that signal FAILS and the SHORT
    is blocked. Signal 4 fails OPEN during warmup. The drought this targets
    (33 SHORTs on 6/17 -> 0 on 6/19) comes from the regime detector lagging into
    TRENDING_UP; this gate reads live microstructure instead and fires SHORTs on
    confirmed downside structure regardless of the regime/TA bias.

    Returns (allowed, detail) — same shape as long_structural_gate — so the
    dashboard, logging and tests treat both gates identically. LONGs never call
    this.
    """
    spot_lag_thr = float(params.get("spot_lag_threshold", 0.0002))
    oi_rise_thr = float(params.get("oi_rise_threshold", 0.001))
    ob_ask_thr = float(params.get("ob_ask_threshold", 0.20))
    spot_lookback = float(params.get("spot_lookback_minutes", 5))
    oi_lookback = float(params.get("oi_lookback_minutes", 5))
    top_n = int(params.get("ob_top_levels", 10))

    detail = {
        "spot_lagging": False, "oi_rising": False, "ob_ask_heavy": False,
        "momentum_ok": False,
        "spot_ret": None, "perp_ret": None, "oi_change": None,
        "imbalance": None,
        "move_5m": None, "move_10m": None, "move_15m": None, "dump_detail": "",
        "allowed": False, "block_reason": "",
    }

    # --- Signal 1: spot lagging perp over the lookback window ---------------
    spot_now = buf.spot_price(coin)
    spot_then = buf.spot_price_n_minutes_ago(coin, spot_lookback)
    perp_now = buf.mid(coin)
    c5 = buf.latest_candles(coin, "5m", 2)
    perp_then = float(c5[-2]["c"]) if len(c5) >= 2 else None
    if all(v is not None and v > 0 for v in
           (spot_now, spot_then, perp_now, perp_then)):
        spot_ret = (spot_now - spot_then) / spot_then
        perp_ret = (perp_now - perp_then) / perp_then
        detail["spot_ret"] = spot_ret
        detail["perp_ret"] = perp_ret
        # spot falling faster than perp (real selling) OR perp bouncing while
        # spot still falls (leverage-driven bounce) — both are spot < perp with
        # spot itself negative past the threshold.
        detail["spot_lagging"] = (spot_ret < perp_ret
                                  and spot_ret < -spot_lag_thr)

    # --- Signal 2: OI rising WITH falling price (fresh shorts) --------------
    oi_now = (buf.ctx.get(coin) or {}).get("open_interest")
    oi_then = buf.oi_n_minutes_ago(coin, oi_lookback)
    if oi_now and oi_then and detail["perp_ret"] is not None:
        detail["oi_change"] = (oi_now - oi_then) / oi_then
        detail["oi_rising"] = (oi_now > oi_then * (1 + oi_rise_thr)
                               and detail["perp_ret"] < -0.0001)

    # --- Signal 3: orderbook ask-heavy -------------------------------------
    book = buf.books.get(coin)
    if book and book.get("bids") and book.get("asks"):
        bid_vol = sum(sz for _px, sz in book["bids"][:top_n])
        ask_vol = sum(sz for _px, sz in book["asks"][:top_n])
        total = bid_vol + ask_vol
        if total > 0:
            imb = (bid_vol - ask_vol) / total
            detail["imbalance"] = imb
            detail["ob_ask_heavy"] = imb <= -ob_ask_thr

    # --- Signal 4: dump cooldown (no recent sharp drop) --------------------
    m_ok, m_reason, m_moves = _dump_cooldown_ok(coin, buf, params)
    detail["momentum_ok"] = m_ok
    detail["dump_detail"] = m_reason
    detail.update(m_moves)

    if not detail["spot_lagging"]:
        detail["block_reason"] = "spot_not_lagging"
    elif not detail["oi_rising"]:
        detail["block_reason"] = "oi_not_rising"
    elif not detail["ob_ask_heavy"]:
        detail["block_reason"] = "book_not_ask_heavy"
    elif not detail["momentum_ok"]:
        detail["block_reason"] = "recent_dump"
    else:
        detail["allowed"] = True
    return detail["allowed"], detail


def long_structural_params(t_raw: dict, m_raw: dict) -> dict:
    """Collect the LONG structural-gate tunables (config + live overrides) so
    the initial read and the per-loop hot-reload stay in sync."""
    return {
        "enabled": bool(t_raw.get("long_structural_gate_enabled", True)),
        "spot_lead_threshold": float(
            t_raw.get("long_spot_lead_threshold", 0.0002)),
        "oi_rise_threshold": float(t_raw.get("long_oi_rise_threshold", 0.001)),
        "ob_bid_threshold": float(t_raw.get("long_ob_bid_threshold", 0.20)),
        "spot_lookback_minutes": float(
            t_raw.get("long_spot_lookback_minutes", 5)),
        "oi_lookback_minutes": float(t_raw.get("long_oi_lookback_minutes", 5)),
        "ob_top_levels": int(m_raw.get("ob_top_levels", 10)),
        # Signal 4 — momentum cooldown (anti-pump-top, 2026-06-18)
        "pump_cooldown_enabled": bool(
            t_raw.get("long_pump_cooldown_enabled", True)),
        "pump_threshold_1": float(t_raw.get("long_pump_threshold_1", 0.005)),
        "pump_threshold_2": float(t_raw.get("long_pump_threshold_2", 0.008)),
        "pump_threshold_3": float(t_raw.get("long_pump_threshold_3", 0.012)),
    }


def short_structural_params(t_raw: dict, m_raw: dict) -> dict:
    """Collect the SHORT structural-gate tunables (config + live overrides) so
    the initial read and the per-loop hot-reload stay in sync. Mirror of
    long_structural_params (2026-06-19)."""
    return {
        "enabled": bool(t_raw.get("short_structural_gate_enabled", True)),
        "spot_lag_threshold": float(
            t_raw.get("short_spot_lag_threshold", 0.0002)),
        "oi_rise_threshold": float(t_raw.get("short_oi_rise_threshold", 0.001)),
        "ob_ask_threshold": float(t_raw.get("short_ob_ask_threshold", 0.20)),
        "spot_lookback_minutes": float(
            t_raw.get("short_spot_lookback_minutes", 5)),
        "oi_lookback_minutes": float(t_raw.get("short_oi_lookback_minutes", 5)),
        "ob_top_levels": int(m_raw.get("ob_top_levels", 10)),
        # Signal 4 — dump cooldown (anti-dump-bottom, 2026-06-19)
        "dump_cooldown_enabled": bool(
            t_raw.get("short_dump_cooldown_enabled", True)),
        "dump_threshold_1": float(t_raw.get("short_dump_threshold_1", 0.005)),
        "dump_threshold_2": float(t_raw.get("short_dump_threshold_2", 0.008)),
        "dump_threshold_3": float(t_raw.get("short_dump_threshold_3", 0.012)),
    }


# Throttle high-frequency skip logging. The structural gate and the direction
# switches reject the same coin every loop (~10s); without this the trades table
# grows ~15k near-duplicate skip rows/day. Persist at most one skip row per
# (coin, category) per SKIP_LOG_THROTTLE_S — enough to audit that a side is being
# held without flooding the table. In-memory, resets on restart.
SKIP_LOG_THROTTLE_S = 300
_last_skip_log: dict = {}


def _should_log_skip(coin: str, category: str) -> bool:
    now = time.time()
    key = (coin, category)
    if now - _last_skip_log.get(key, 0.0) >= SKIP_LOG_THROTTLE_S:
        _last_skip_log[key] = now
        return True
    return False


def parse_fill(res: dict) -> tuple[float | None, str]:
    """Extract (avg_px, status_note) from an exchange order response."""
    try:
        statuses = res["response"]["data"]["statuses"]
        for st in statuses:
            if "filled" in st:
                return float(st["filled"]["avgPx"]), "filled"
            if "error" in st:
                return None, f"error: {st['error']}"
        return None, f"unfilled: {statuses}"
    except Exception:
        return None, f"unparsed: {res}"


class MakerTimeoutTracker:
    """Per-coin maker-timeout streak tracker for the intelligent taker
    fallback. A streak is consecutive maker non-fills on the same coin +
    direction within a rolling window; the mid at the FIRST timeout anchors
    the exhaustion check. The streak resets on a direction flip, after the
    window elapses, or explicitly via reset() (called on any fill or skip)."""

    def __init__(self, n: int, window_s: float):
        self.n = n
        self.window_s = window_s
        self._streaks: dict[str, dict] = {}

    def record_timeout(self, coin: str, direction: str, mid: float | None,
                       now: float | None = None) -> dict:
        """Register a maker non-fill; returns the (possibly fresh) streak dict
        {'direction','count','first_ts','start_mid'}."""
        now = time.time() if now is None else now
        s = self._streaks.get(coin)
        if (s is None or s["direction"] != direction
                or now - s["first_ts"] > self.window_s):
            s = {"direction": direction, "count": 0, "first_ts": now,
                 "start_mid": mid}
            self._streaks[coin] = s
        s["count"] += 1
        return s

    def reset(self, coin: str):
        self._streaks.pop(coin, None)


class ArmedSignalTracker:
    """Per-coin arm-time tracker for the armed-signal TTL (2026-06-22).

    A signal "arms" the moment it first clears every entry gate (can_open) on a
    coin+band. The maker order may then sit unfilled and retry across several
    10s cycles before it fills via taker fallback or is dropped. This tracker
    anchors the arm time so the loop can bound that retry window: once an armed
    setup ages past its band TTL the original votes/conf no longer reflect
    current microstructure, so the entry is abandoned instead of filled stale.

    Distinct from MakerTimeoutTracker — that one COUNTS consecutive maker
    non-fills to drive the taker fallback; this one TIMES how long a setup has
    been armed. They coexist: the TTL drop sits upstream of the fallback. The
    arm time resets on a direction flip, a band change, or explicitly via
    reset() (called on any fill, drop, or TTL expiry)."""

    def __init__(self):
        self._armed: dict[str, dict] = {}

    def age(self, coin: str, band: str, direction: str,
            now: float | None = None) -> float | None:
        """Seconds since the signal armed, or None if this exact coin+band+
        direction is not currently armed (a flip or fresh setup is not yet
        armed, so it has no age to test against the TTL)."""
        now = time.time() if now is None else now
        s = self._armed.get(coin)
        if s is None or s["band"] != band or s["direction"] != direction:
            return None
        return now - s["armed_at"]

    def arm(self, coin: str, band: str, direction: str,
            now: float | None = None) -> None:
        """Stamp the arm time on the FIRST armed attempt; a no-op while the same
        setup stays armed so the TTL always measures from the original arm, not
        the latest retry."""
        now = time.time() if now is None else now
        s = self._armed.get(coin)
        if s is None or s["band"] != band or s["direction"] != direction:
            self._armed[coin] = {"band": band, "direction": direction,
                                 "armed_at": now}

    def is_armed(self, coin: str, band: str) -> bool:
        s = self._armed.get(coin)
        return s is not None and s["band"] == band

    def reset(self, coin: str):
        self._armed.pop(coin, None)


def run_taker_fallback(coin, is_long, usd_size, *, models, aggregator, buf, xc,
                       db, min_confidence, min_model_agreement,
                       exhaustion_atr_mult, start_mid, interval=None,
                       weights=None, regime_routing=True, band=None) -> dict:
    """Maker-timeout streak hit N: re-validate the signal on the CURRENT
    buffer (never the cached one from streak start) and confirm the move is
    still live, then take the market only if both hold. Every decision —
    fired or skipped — is logged to the trades table for later audit.

    Re-aggregates on the band's resolution + weight set so the re-validation
    matches the band that requested the entry.

    Returns {"status": taker_fallback|taker_skipped_degraded|
    taker_skipped_exhausted|taker_failed, "fill_px", "sig", "agreement"}."""
    direction = LONG if is_long else SHORT

    # Step 2 — re-validate the signal on current buffer state (band resolution)
    tickets = [m.compute(coin, buf, interval=interval) for m in models]
    sig = aggregator.aggregate(coin, tickets, weights=weights,
                               regime_routing=regime_routing)
    agreement = (sig.long_votes if sig.direction == LONG else sig.short_votes)
    degraded = None
    if sig.direction != direction:
        degraded = f"direction {sig.direction} != {direction}"
    elif sig.confidence < min_confidence - CONF_GATE_EPS:
        degraded = f"conf {sig.confidence:.3f} < {min_confidence:.3f}"
    elif agreement < min_model_agreement:
        degraded = f"agreement {agreement} < {min_model_agreement}"
    if degraded:
        log.info("taker fallback %s %s SKIP — signal degraded: %s",
                 direction, coin, degraded)
        db.log_trade(coin, direction, "OPEN", status="taker_skipped_degraded",
                     band=band,
                     note=f"maker:taker_skipped_degraded ({degraded})")
        return {"status": "taker_skipped_degraded", "fill_px": None,
                "sig": sig, "agreement": agreement}

    # Step 3 — exhaustion: price already ran our way, or book flipped against us
    cur_mid = buf.mid(coin)
    atr = atr_from_candles(buf.latest_candles(coin, "1m", 60))
    exhausted = None
    if atr and atr > 0 and start_mid and cur_mid:
        moved = (cur_mid - start_mid) if is_long else (start_mid - cur_mid)
        if moved > exhaustion_atr_mult * atr:
            exhausted = (f"moved {moved:.4f} > {exhaustion_atr_mult}xATR "
                         f"({exhaustion_atr_mult * atr:.4f}) since streak start")
    if exhausted is None:
        book = buf.books.get(coin)
        if book and book.get("bids") and book.get("asks"):
            bid_sz = sum(sz for _, sz in book["bids"][:10])
            ask_sz = sum(sz for _, sz in book["asks"][:10])
            tot = bid_sz + ask_sz
            if tot > 0:
                bid_frac = bid_sz / tot
                if not is_long and bid_frac > 0.60:
                    exhausted = f"book bid-heavy {bid_frac:.0%} — SHORT reversal"
                elif is_long and bid_frac < 0.40:
                    exhausted = (f"book ask-heavy {(1 - bid_frac):.0%} — "
                                 f"LONG reversal")
    if exhausted:
        log.info("taker fallback %s %s SKIP — move exhausted: %s",
                 direction, coin, exhausted)
        db.log_trade(coin, direction, "OPEN", status="taker_skipped_exhausted",
                     band=band,
                     note=f"maker:taker_skipped_exhausted ({exhausted})")
        return {"status": "taker_skipped_exhausted", "fill_px": None,
                "sig": sig, "agreement": agreement}

    # Step 4 — signal live + move not exhausted -> take the market
    log.warning("TAKER FALLBACK %s %s conf=%.2f votes=%d — converting maker "
                "timeout to market", direction, coin, sig.confidence, agreement)
    res = with_retry(lambda: xc.market_open(coin, is_long, usd_size),
                     f"taker_fallback_market_open({coin})")
    if not res:
        db.log_trade(coin, direction, "OPEN", status="taker_failed",
                     note="maker:taker_fallback order failed")
        return {"status": "taker_failed", "fill_px": None, "sig": sig,
                "agreement": agreement}
    fill_px, st_note = parse_fill(res)
    fmap = "smooth" if next(
        (m.smooth_mapping for m in models if m.name == "FundingRateModel"),
        False) else "binary"
    db.log_trade(coin, direction, "OPEN",
                 size=usd_size / (fill_px or cur_mid or 1),
                 price=fill_px, band=band,
                 status="taker_fallback" if fill_px else "taker_failed",
                 note=(f"maker:taker_fallback conf={sig.confidence:.2f} "
                       f"votes={agreement} active={sig.long_votes + sig.short_votes} "
                       f"fmap={fmap} {st_note}"))
    return {"status": "taker_fallback" if fill_px else "taker_failed",
            "fill_px": fill_px, "sig": sig, "agreement": agreement}


def _pack_band(tickets: list, sig) -> dict:
    """Serialize one band's model tickets + aggregated verdict for the
    dashboard (single overwritten live_tickets key, zero table growth)."""
    return {
        "tickets": [{"model": t.model, "direction": t.direction,
                     "confidence": round(t.confidence, 3), "meta": t.meta}
                    for t in tickets],
        "direction": sig.direction,
        "confidence": round(sig.confidence, 3),
        "regime": sig.regime,
        "long": sig.long_votes, "short": sig.short_votes,
        "flat": sig.flat_votes, "meta": sig.meta,
    }


def acquire_singleton_lock() -> object:
    """Exclusive flock — two trading loops placing orders on the same
    account would double every position. Held for process lifetime."""
    lock_path = PROJECT_ROOT / "data" / "run_bot.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("another run_bot.py already holds %s — exiting", lock_path)
        sys.exit(1)
    fh.write(str(Path("/proc/self").resolve().name))
    fh.flush()
    return fh


def main():
    _lock = acquire_singleton_lock()
    cfg = Config()
    log.setLevel(cfg.log_level)
    t_raw = (cfg._raw.get("trading", {}) or {})
    m_raw = (cfg._raw.get("models", {}) or {})
    r_raw = (cfg._raw.get("risk", {}) or {})
    loop_s = float(t_raw.get("loop_interval_seconds", 10))
    usd_size = float(t_raw.get("default_usd_size", 50))
    default_lev = float(t_raw.get("default_leverage", 3.0))
    coins_active = t_raw.get("coins_active", cfg.coins)
    entry_style = t_raw.get("entry_style", "maker")
    entry_timeout = float(t_raw.get("entry_timeout_seconds", 30))
    fallback_enabled = bool(t_raw.get("maker_timeout_fallback_enabled", True))
    fallback_n = int(t_raw.get("maker_timeout_fallback_n", 3))
    fallback_window_s = float(t_raw.get("maker_timeout_fallback_window_s", 180))
    fallback_exhaustion_mult = float(
        t_raw.get("maker_timeout_exhaustion_atr_mult", 1.5))
    # armed-signal TTL ceilings (2026-06-22) — drop a setup that has stayed
    # armed (clearing the gate but not filling) longer than this; per-band.
    scalp_armed_ttl = float(t_raw.get("scalp_armed_ttl_seconds", 20))
    trend_armed_ttl = float(t_raw.get("trend_armed_ttl_seconds", 45))
    # LONG structural gate (2026-06-17): supersedes the old Change B OB/VWAP
    # confirmation. A LONG must clear ALL of {spot leading, OI rising, book
    # bid-heavy}, else skip. SHORTs (the working side) are never gated.
    long_struct = long_structural_params(t_raw, m_raw)
    # SHORT structural gate (2026-06-19): mirror of the LONG gate for the SHORT
    # side. A SHORT must clear ALL of {spot lagging, OI rising with falling
    # price, book ask-heavy} plus the dump cooldown. Targets the SHORT drought
    # (33->0 SHORTs as the regime detector lagged into TRENDING_UP).
    short_struct = short_structural_params(t_raw, m_raw)
    # Dual-band (2026-06-20): two aggregations per coin per cycle — SCALP on the
    # fast resolution, TREND on the slow one. Each band has its own weight set,
    # entry gate, risk geometry and concurrency (see RiskManager.bands). One-way
    # exchange nets per coin, so a coin is owned by at most one band at a time.
    scalp_band_enabled = bool(t_raw.get("scalp_band_enabled", True))
    trend_band_enabled = bool(t_raw.get("trend_band_enabled", True))
    scalp_interval = str(t_raw.get("scalp_interval", "5m"))
    trend_interval = str(t_raw.get("trend_interval", "1h"))
    # Legacy SHORT OB/VWAP mirror — OFF by default; superseded by short_struct
    # above but still read for the dashboard / regression suite.
    short_conf_enabled = bool(t_raw.get("short_confirmation_enabled", False))
    short_conf_models = set(t_raw.get(
        "short_confirmation_models",
        ["OrderbookImbalanceModel", "VWAPModel"]))
    short_conf_min = int(t_raw.get("short_confirmation_min", 1))
    ml_dir = str((PROJECT_ROOT / m_raw.get("ml_model_dir", "models/")).resolve())

    log.info("HL Reaper FULL LOOP starting — network=%s coins=%s size=$%.0f",
             cfg.network, coins_active, usd_size)

    # 1. data layer (same as Phase 1)
    db = DB(cfg.db_path)
    db.set_state("phase", "5")
    db.set_state("status", "starting")
    buf = MarketBuffer(cfg.coins, cfg.candle_intervals, cfg.candle_buffer_size)
    feed = WebSocketFeed(cfg.api_url, buf, cfg.candle_intervals,
                         cfg.stale_feed_seconds)
    pollers = RestPollers(cfg.api_url, cfg, buf, db)
    # in-process Binance spot poller feeding THIS buffer (record=False — the
    # standalone hl-spot-poller.service owns the disk recordings). The LONG
    # structural gate reads buf.spot_price() / spot_history from here; without
    # it spot data never reaches the bot and every LONG fails the gate's
    # spot-leading check. Best-effort: a spot outage just fails LONGs safe.
    spot_poller = SpotPoller(cfg.coins, PROJECT_ROOT / "data" / "recorded",
                             buf=buf, poll_s=5.0, record=False)
    # prime candle buffers over REST so TA / MeanReversion / VWAP work
    # immediately after a (re)start instead of waiting hours for the 5m/1h
    # deques to fill live (HL's candle WS sends no history).
    try:
        feed.backfill(per_interval=cfg.candle_buffer_size)
    except Exception as e:
        log.warning("candle backfill skipped: %s", e)
    feed.start()
    pollers.start()
    spot_poller.start()

    # 2-4. models, aggregator, risk
    xc = ExchangeClient(cfg)
    funding_model = FundingRateModel(
        db, smooth_mapping=bool(r_raw.get("funding_smooth_mapping_enabled",
                                          False)))
    models = [
        RegimeDetectorModel(),   # first: publishes regime for the others
        TAModel(),
        MeanReversionModel(),
        funding_model,
        OrderbookImbalanceModel(
            top_levels=int(m_raw.get("ob_top_levels", 10)),
            min_imbalance=float(m_raw.get("ob_min_imbalance", 0.30))),
        VWAPModel(),
        LiquidationHeatmapModel(),
        MLForecastModel(model_dir=ml_dir,
                        min_confidence=float(m_raw.get("ml_min_confidence",
                                                       0.55))),
    ]
    aggregator = SignalAggregator(
        funding_hard_block_enabled=bool(
            r_raw.get("funding_hard_block_enabled", True)),
        funding_hard_block_conf=float(
            r_raw.get("funding_hard_block_conf", 0.75)),
        funding_hard_block_short_enabled=bool(
            r_raw.get("funding_hard_block_short_enabled", False)),
        funding_hard_block_short_conf=float(
            r_raw.get("funding_hard_block_short_conf", 0.75)),
        funding_counter_trend_damp=float(
            (cfg._raw.get("aggregator", {}) or {})
            .get("funding_counter_trend_damp", 0.40)))
    log.info("funding counter-trend dampening: x%.2f weight on counter-1h-trend "
             "FUNDING votes", aggregator.funding_counter_trend_damp)
    if aggregator.funding_hard_block_enabled:
        log.info("funding HARD-block ENABLED — FundingRate SHORT conf >= %.2f "
                 "blocks all LONG entries", aggregator.funding_hard_block_conf)
    log.info("funding mapping: %s (risk.funding_smooth_mapping_enabled=%s)",
             "SMOOTH (continuous)" if funding_model.smooth_mapping
             else "BINARY (original fallback)", funding_model.smooth_mapping)
    if not (cfg.longs_enabled and cfg.shorts_enabled):
        log.warning("DIRECTION MODE — longs_enabled=%s shorts_enabled=%s "
                    "(toggle live from the Controls page)",
                    cfg.longs_enabled, cfg.shorts_enabled)
    if long_struct["enabled"]:
        log.info("LONG STRUCTURAL gate ENABLED — need spot leading (>%.4f) + "
                 "OI rising (>%.3f) + book bid-heavy (>=%.2f)",
                 long_struct["spot_lead_threshold"],
                 long_struct["oi_rise_threshold"],
                 long_struct["ob_bid_threshold"])
    if short_struct["enabled"]:
        log.info("SHORT STRUCTURAL gate ENABLED — need spot lagging (<-%.4f) + "
                 "OI rising w/ falling price (>%.3f) + book ask-heavy (<=-%.2f)",
                 short_struct["spot_lag_threshold"],
                 short_struct["oi_rise_threshold"],
                 short_struct["ob_ask_threshold"])
    maker_streaks = MakerTimeoutTracker(fallback_n, fallback_window_s)
    armed_signals = ArmedSignalTracker()
    log.info("armed-signal TTL ENABLED — drop stale setups: scalp %.0fs, "
             "trend %.0fs", scalp_armed_ttl, trend_armed_ttl)
    if entry_style == "maker" and fallback_enabled:
        log.info("maker timeout->taker fallback ENABLED — fire after %d "
                 "consecutive timeouts within %.0fs, skip if move > %.1fxATR",
                 fallback_n, fallback_window_s, fallback_exhaustion_mult)
    risk = RiskManager(cfg, buf, db, xc)
    signals = SignalWriter(cfg.db_path)
    # the legacy "paper_aggressive / conservative" mode is gone — every gate is
    # now an individual live_config override on top of the config.yaml floor.
    # Publish the effective gates for the dashboard instead of a mode label.
    db.set_state("trading_mode", "live_config")

    def close_position(coin: str, reason: str, side: str = "?") -> bool:
        """Submit a close order and arm the close-pending guard so the in-trade
        guard does not re-issue the identical close every cycle while the
        on-chain position state catches up (critical pre-mainnet duplicate-close
        fix). Returns True if the close order was accepted.

        We arm close-pending on *acceptance*, not on a short re-poll: the state
        lag that caused the duplicate-close bug runs 1-2 cycles, longer than any
        quick verify window, so a 2s re-check would still race. The re-check
        here only early-clears the flag (and logs) when the close is already
        visibly gone. A close that is never accepted leaves the flag unset, so
        a genuinely failed close retries next cycle."""
        res = with_retry(lambda: xc.market_close(coin), f"market_close({coin})")
        if res:
            risk.mark_close_pending(coin)
            # best-effort fast confirmation (informational / early-clear only)
            time.sleep(2)
            remaining = with_retry(xc.positions, "positions") or []
            gone = not any(
                (p.get("position") or {}).get("coin") == coin
                and float((p.get("position") or {}).get("szi") or 0) != 0
                for p in remaining)
            if gone:
                risk.clear_close_pending(coin)
                log.info("position confirmed closed for %s", coin)
            else:
                log.info("close submitted for %s — state still catching up, "
                         "suppressing re-close for %ds", coin,
                         int(CLOSE_PENDING_TIMEOUT_S))
        else:
            log.warning("close order FAILED for %s (%s) — will retry next cycle",
                        coin, reason)
        db.log_trade(coin, side, "CLOSE", status="ok" if res else "error",
                     band=risk._pos_track.get(coin, {}).get("band"), note=reason)
        return bool(res)

    def open_entry(coin: str, sig, band: str, g_allowed: bool, g_detail: dict,
                   s_allowed: bool, s_detail: dict) -> bool:
        """Attempt ONE band entry for coin given its aggregated signal. Returns
        True iff a position was opened (so the caller skips the other band on
        this coin — one-way exchange owns one position per coin). Mirrors the
        legacy single-band entry flow, scoped to the band's gate/geometry/size.
        Structural gates apply to the SCALP band only."""
        bp = risk.band_params(band)
        is_long = sig.direction == LONG

        # per-band direction toggles: effective = global master AND band flag
        if is_long:
            if not (cfg.longs_enabled
                    and bool(t_raw.get(f"{band}_longs_enabled", True))):
                if _should_log_skip(coin, f"{band}_dir_disabled_LONG"):
                    db.log_trade(coin, LONG, "OPEN", band=band,
                                 status="direction_disabled",
                                 note=f"{band} longs disabled")
                return False
        else:
            if not (cfg.shorts_enabled
                    and bool(t_raw.get(f"{band}_shorts_enabled", True))):
                if _should_log_skip(coin, f"{band}_dir_disabled_SHORT"):
                    db.log_trade(coin, SHORT, "OPEN", band=band,
                                 status="direction_disabled",
                                 note=f"{band} shorts disabled")
                return False

        # structural gates — SCALP band only (trend's own 1h signal is its gate)
        if band == "scalp" and bp["structural_gates_enabled"]:
            if is_long and long_struct["enabled"] and not g_allowed:
                reason = g_detail["block_reason"]
                why = (g_detail["pump_detail"] if reason == "recent_pump"
                       else f"imb={g_detail['imbalance']}")
                log.info("scalp LONG skipped on %s: long_blocked (%s)",
                         coin, reason)
                if _should_log_skip(coin, "long_blocked"):
                    db.log_trade(coin, LONG, "OPEN", band=band,
                                 status=f"long_blocked_{reason}",
                                 note=f"skip: structural gate ({reason}) {why}")
                return False
            if not is_long and short_struct["enabled"] and not s_allowed:
                reason = s_detail["block_reason"]
                why = (s_detail["dump_detail"] if reason == "recent_dump"
                       else f"imb={s_detail['imbalance']}")
                log.info("scalp SHORT skipped on %s: short_blocked (%s)",
                         coin, reason)
                if _should_log_skip(coin, "short_blocked"):
                    db.log_trade(coin, SHORT, "OPEN", band=band,
                                 status=f"short_blocked_{reason}",
                                 note=f"skip: structural gate ({reason}) {why}")
                return False

        # sizing — per-band size; per-coin override (Controls) wins when set
        pc = (cfg._raw.get("per_coin", {}) or {}).get(coin, {}) or {}
        coin_usd = float(pc.get("usd_size") or bp["position_size_usd"])
        coin_lev = float(pc.get("leverage") or default_lev)
        agreement = sig.long_votes if is_long else sig.short_votes
        ok, reason = risk.can_open(coin, sig.direction, sig.confidence,
                                   agreement, coin_lev, band=band)
        if not ok:
            # Fix 3 (2026-06-22): re-validate before each retry. The loop re-
            # aggregates every cycle, so a coin that was already armed and is
            # now failing can_open on conf/agreement is a setup that DEGRADED
            # while its maker sat unfilled — abort it loudly (and clear the
            # streak/arm) instead of silently letting the next cycle re-arm.
            if (armed_signals.is_armed(coin, band)
                    and ("confidence" in reason or "agreement" in reason)):
                log.info("[RETRY_ABORT] %s %s signal degraded on retry — %s, "
                         "dropping", coin, band, reason)
                db.log_trade(coin, sig.direction, "OPEN", band=band,
                             status="retry_abort",
                             note=f"maker:retry_abort ({reason})")
                armed_signals.reset(coin)
                maker_streaks.reset(coin)
            else:
                log.debug("skip %s %s [%s]: %s", coin, sig.direction, band,
                          reason)
            return False

        # Fix 2 (2026-06-22): armed-signal TTL. can_open just passed, so the
        # signal is armed. If it armed more than the band's TTL ago it has been
        # retrying too long to still trust the original setup — drop it (do NOT
        # submit another order) before this attempt goes out. age() is None on
        # the first armed attempt, so the very first submission is never
        # blocked; arm() then stamps the time used by later cycles.
        armed_ttl = scalp_armed_ttl if band == "scalp" else trend_armed_ttl
        armed_age = armed_signals.age(coin, band, sig.direction)
        if armed_age is not None and armed_age > armed_ttl:
            log.info("[ARMED_TTL] %s %s %s dropped — armed %.1fs ago, stale "
                     "signal", coin, band, sig.direction, armed_age)
            db.log_trade(coin, sig.direction, "OPEN", band=band,
                         status="armed_ttl_drop",
                         note=f"maker:armed_ttl_drop (armed {armed_age:.1f}s "
                              f"ago > {armed_ttl:.0f}s)")
            armed_signals.reset(coin)
            maker_streaks.reset(coin)
            return False
        armed_signals.arm(coin, band, sig.direction)

        lev = risk.clamp_leverage(coin_lev)
        atr = atr_from_candles(buf.latest_candles(coin, "1m", 60))
        entry_ref = buf.mid(coin)
        if not atr or not entry_ref:
            log.warning("skip %s [%s]: no ATR/mid for stops", coin, band)
            return False
        sl, tp = risk.calc_sl_tp(coin, entry_ref, is_long, atr, band=band)
        weights = SCALP_WEIGHTS if band == "scalp" else TREND_WEIGHTS
        interval = scalp_interval if band == "scalp" else trend_interval
        fmap = "smooth" if funding_smooth else "binary"
        # active = total non-FLAT directional voters (long+short); confidence is
        # normalized over these, so it exposes how thin the consensus was.
        active = sig.long_votes + sig.short_votes

        log.warning("ENTRY %s %s [%s] conf=%.2f votes=%d active=%d regime=%s "
                    "sl=%.4f tp=%.4f", sig.direction, coin, band,
                    sig.confidence, agreement, active, sig.regime, sl, tp)

        if entry_style == "maker":
            mres = xc.try_limit_entry(coin, is_long, coin_usd,
                                      timeout_s=entry_timeout)
            fill_px = mres.get("avg_px")
            note = f"maker:{mres['status']}"
            if mres["status"] not in ("filled", "partial"):
                triggered = False
                streak_count = 0
                if fallback_enabled:
                    streak = maker_streaks.record_timeout(
                        coin, sig.direction, entry_ref)
                    streak_count = streak["count"]
                    if streak_count >= fallback_n:
                        triggered = True
                        fb = run_taker_fallback(
                            coin, is_long, coin_usd, models=models,
                            aggregator=aggregator, buf=buf, xc=xc, db=db,
                            min_confidence=bp["min_confidence"],
                            min_model_agreement=bp["min_model_agreement"],
                            exhaustion_atr_mult=fallback_exhaustion_mult,
                            start_mid=streak["start_mid"], interval=interval,
                            weights=weights, regime_routing=False, band=band)
                        maker_streaks.reset(coin)
                        armed_signals.reset(coin)
                        if fb["status"] == "taker_fallback" and fb["fill_px"]:
                            fill_px = fb["fill_px"]
                            sl, tp = risk.calc_sl_tp(
                                coin, fill_px, is_long, atr, band=band)
                            risk.register_entry(coin, fill_px, sl, tp, is_long,
                                                band=band)
                            alerts.send(
                                f"TAKER FALLBACK {sig.direction} {coin} "
                                f"[{band}] @ {fill_px}\n"
                                f"conf={fb['sig'].confidence:.2f} "
                                f"votes={fb['agreement']} "
                                f"(maker timed out {streak['count']}x)\n"
                                f"sl={sl:.4f} tp={tp:.4f}")
                            return True
                if not triggered:
                    log.info("maker entry %s on %s [%s] — skipped "
                             "(timeout streak=%d/%d)", mres["status"], coin,
                             band, streak_count, fallback_n)
                    db.log_trade(coin, sig.direction, "OPEN", band=band,
                                 status=mres["status"], note=note)
                return False
            maker_streaks.reset(coin)
            armed_signals.reset(coin)
        else:
            res = with_retry(lambda: xc.market_open(coin, is_long, coin_usd),
                             f"market_open({coin})")
            if not res:
                db.log_trade(coin, sig.direction, "OPEN", band=band,
                             status="error", note="order failed")
                return False
            fill_px, note = parse_fill(res)

        if fill_px:
            armed_signals.reset(coin)  # position opened — clear the arm clock
            sl, tp = risk.calc_sl_tp(coin, fill_px, is_long, atr, band=band)
            risk.register_entry(coin, fill_px, sl, tp, is_long, band=band)
            alerts.send(
                f"OPEN {sig.direction} {coin} [{band}] @ {fill_px}\n"
                f"conf={sig.confidence:.2f} votes={agreement} active={active} "
                f"regime={sig.regime}\nsl={sl:.4f} tp={tp:.4f}")
        db.log_trade(coin, sig.direction, "OPEN",
                     size=coin_usd / (fill_px or entry_ref),
                     price=fill_px, leverage=lev, band=band,
                     status="filled" if fill_px else "failed",
                     note=f"conf={sig.confidence:.2f} votes={agreement} "
                          f"active={active} fmap={fmap} {note}")
        return bool(fill_px)

    def handle_command(command: str) -> str:
        """Execute one queued dashboard control command; return a status note."""
        cmd = (command or "").strip()
        if cmd == "pause":
            risk.manual_pause()
            return "paused (MANAGING — no new entries)"
        if cmd == "resume":
            risk.manual_resume()
            return "resumed"
        if cmd == "close_all":
            risk.manual_close_all()
            return "closed all positions"
        if cmd.startswith("close_coin/"):
            coin = cmd.split("/", 1)[1]
            if coin not in cfg.coins:
                raise ValueError(f"unknown coin {coin}")
            close_position(coin, "dashboard close_coin command")
            return f"closed {coin}"
        if cmd.startswith("set_state/"):
            target = cmd.split("/", 1)[1].upper()
            if target in ("ACTIVE", "RESUME"):
                risk.manual_resume()
            elif target in ("MANAGING", "PAUSE"):
                risk.manual_pause()
            elif target in ("HALTED", "HALT"):
                risk.manual_halt()
            else:
                risk._set_state(BotState(target), "dashboard set_state")
            return f"state -> {target}"
        if cmd.startswith("set_sltp/"):
            # manual SL/TP override from the dashboard chart sliders:
            # set_sltp/<coin>/<sl>/<tp>. Applies to the in-trade tracker so the
            # bot manages the position to the new levels (trailing may still
            # tighten the SL further in the favorable direction).
            parts = cmd.split("/")
            if len(parts) != 4:
                raise ValueError(f"bad set_sltp command {cmd!r}")
            coin = parts[1]
            if coin not in cfg.coins:
                raise ValueError(f"unknown coin {coin}")
            try:
                new_sl, new_tp = float(parts[2]), float(parts[3])
            except ValueError:
                raise ValueError(f"bad sl/tp in {cmd!r}")
            ok_set, msg = risk.set_manual_sltp(coin, new_sl, new_tp)
            if not ok_set:
                raise ValueError(msg)
            # not logged to the trades table on purpose: the marker/history
            # reconstruction treats any non-OPEN/BE_LOCK row as a CLOSE, which
            # would break OPEN/CLOSE pairing. The bot log + COMMAND log cover it.
            return f"set {coin} sl={new_sl:g} tp={new_tp:g}"
        raise ValueError(f"unknown command {cmd!r}")

    # Phase 8.6: cascade bounce — event-driven track, separate from ensemble
    cb_raw = (cfg._raw.get("cascade_bounce", {}) or {})
    cb_enabled = bool(cb_raw.get("enabled", False))
    cb_model = CascadeBounceModel(cb_raw)
    cb_maker_timeout = float(cb_raw.get("maker_timeout_seconds", 5))
    cb_taker_fallback = bool(cb_raw.get("taker_fallback", True))
    cb_tp_pct = float(cb_raw.get("profit_target_pct", 0.010))
    cb_sl_pct = float(cb_raw.get("stop_pct", 0.0075))
    # survives restarts: if a bounce trade was open, pick it back up so the
    # persisted CASCADE_BOUNCE_ACTIVE state can resolve back to ACTIVE
    cb_coin: str | None = db.get_state("cascade_bounce_coin") or None
    liq_conn = liquidation_store.connect() if cb_enabled else None
    if cb_enabled:
        log.info("cascade bounce track ENABLED — alloc %.0f%% equity, "
                 "max hold %.0fmin, maker-then-taker",
                 risk.cb_allocation_pct * 100, risk.cb_max_hold_minutes)

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    db.set_state("status", "running")

    hb_path = Path(cfg.heartbeat_path)
    last_status = 0.0
    prev_state = None
    alerts.send(f"paper trading loop started — network={cfg.network} "
                f"coins={coins_active} size=${usd_size:.0f} "
                f"entry={entry_style}")
    try:
        while _running:
            cycle_start = time.time()

            # --- hot-reload live config (controls page) ---------------------
            # merge live_config overrides onto the config.yaml floor, then push
            # the tunable values into RiskManager + the loop locals so changes
            # take effect THIS cycle without a restart. Nothing on the controls
            # page is cached across cycles.
            try:
                live_overrides = db.get_live_config()
            except Exception as e:
                log.warning("live_config read failed: %s", e)
                live_overrides = {}
            cfg.apply_overrides(live_overrides)
            for k, (old, new) in risk.refresh_params().items():
                log.warning("LIVE CONFIG: risk.%s changed %s -> %s", k, old, new)
            t_raw = cfg._raw.get("trading", {}) or {}
            r_raw = cfg._raw.get("risk", {}) or {}
            usd_size = float(t_raw.get("default_usd_size", 50))
            default_lev = float(t_raw.get("default_leverage", 3.0))
            coins_active = t_raw.get("coins_active", cfg.coins)
            entry_style = t_raw.get("entry_style", "maker")
            entry_timeout = float(t_raw.get("entry_timeout_seconds", 30))
            fallback_enabled = bool(
                t_raw.get("maker_timeout_fallback_enabled", True))
            fallback_n = int(t_raw.get("maker_timeout_fallback_n", 3))
            fallback_window_s = float(
                t_raw.get("maker_timeout_fallback_window_s", 180))
            fallback_exhaustion_mult = float(
                t_raw.get("maker_timeout_exhaustion_atr_mult", 1.5))
            maker_streaks.n = fallback_n
            maker_streaks.window_s = fallback_window_s
            scalp_armed_ttl = float(t_raw.get("scalp_armed_ttl_seconds", 20))
            trend_armed_ttl = float(t_raw.get("trend_armed_ttl_seconds", 45))
            long_struct = long_structural_params(
                t_raw, cfg._raw.get("models", {}) or {})
            short_struct = short_structural_params(
                t_raw, cfg._raw.get("models", {}) or {})
            scalp_band_enabled = bool(t_raw.get("scalp_band_enabled", True))
            trend_band_enabled = bool(t_raw.get("trend_band_enabled", True))
            scalp_interval = str(t_raw.get("scalp_interval", "5m"))
            trend_interval = str(t_raw.get("trend_interval", "1h"))
            short_conf_enabled = bool(
                t_raw.get("short_confirmation_enabled", False))
            short_conf_models = set(t_raw.get(
                "short_confirmation_models",
                ["OrderbookImbalanceModel", "VWAPModel"]))
            short_conf_min = int(t_raw.get("short_confirmation_min", 1))
            per_coin_cfg = cfg._raw.get("per_coin", {}) or {}
            loop_s = float(t_raw.get("loop_interval_seconds", 10))
            aggregator.funding_hard_block_enabled = bool(
                r_raw.get("funding_hard_block_enabled", True))
            aggregator.funding_hard_block_conf = float(
                r_raw.get("funding_hard_block_conf", 0.75))
            aggregator.funding_hard_block_short_enabled = bool(
                r_raw.get("funding_hard_block_short_enabled", False))
            aggregator.funding_hard_block_short_conf = float(
                r_raw.get("funding_hard_block_short_conf", 0.75))
            aggregator.funding_counter_trend_damp = float(
                (cfg._raw.get("aggregator", {}) or {})
                .get("funding_counter_trend_damp", 0.40))
            # funding mapping A/B switch — hot-reloaded onto the model and
            # stamped on each OPEN trade note (fmap=) for clean attribution.
            funding_smooth = bool(
                r_raw.get("funding_smooth_mapping_enabled", False))
            if funding_model.smooth_mapping != funding_smooth:
                log.warning("funding mapping -> %s",
                            "SMOOTH" if funding_smooth else "BINARY")
            funding_model.smooth_mapping = funding_smooth

            # --- drain one-shot control commands (controls page) -----------
            for cmd_id, command in db.get_pending_commands():
                try:
                    note = handle_command(command)
                    log.warning("COMMAND %s: %s", command, note)
                    db.mark_command_done(cmd_id, "done")
                except Exception as e:
                    log.error("COMMAND %s failed: %s", command, e)
                    db.mark_command_done(cmd_id, f"error: {e}")

            # honor dashboard control requests (bot_state table)
            ctrl = db.get_state("control_request")
            if ctrl == "halt":
                log.warning("dashboard HALT request — closing all")
                risk.manual_halt()
                db.set_state("control_request", "")
            elif ctrl == "resume":
                log.warning("dashboard RESUME request")
                risk.manual_resume()
                db.set_state("control_request", "")
            try:
                coins_disabled = set(json.loads(
                    db.get_state("control_coins_disabled") or "[]"))
            except Exception:
                coins_disabled = set()

            # a. run all guards
            state = risk.check()
            if prev_state is not None and state != prev_state:
                alerts.send(f"state {prev_state.value} → {state.value}\n"
                            f"{db.get_state('risk_state_reason') or ''}")
            prev_state = state

            # b. manage open positions (also while MANAGING / bounce open)
            if state in (BotState.ACTIVE, BotState.MANAGING,
                         BotState.CASCADE_BOUNCE_ACTIVE):
                positions = with_retry(xc.positions, "positions") or []
                if positions:
                    for act in risk.check_open_positions(positions):
                        if act["action"] == "CLOSE":
                            log.warning("CLOSE %s [%s]: %s", act["coin"],
                                        act.get("band"), act["reason"])
                            close_position(act["coin"], act["reason"])
                            alerts.send(f"CLOSE {act['coin']} "
                                        f"[{act.get('band')}] — "
                                        f"{act['reason']}")
                        elif act["action"] == "UPDATE_SL":
                            log.info("SL -> %.4f on %s (%s)", act["new_sl"],
                                     act["coin"], act["reason"])
                        elif act["action"] == "BREAKEVEN":
                            log.info("BREAKEVEN lock on %s: %s", act["coin"],
                                     act["reason"])
                            # persist a BE_LOCK row so the Live chart can place a
                            # "BE" marker at the point protection kicked in.
                            side = next(
                                ("LONG" if float((p.get("position") or {}).get(
                                    "szi") or 0) > 0 else "SHORT")
                                for p in positions
                                if (p.get("position") or {}).get("coin")
                                == act["coin"])  # coin is guaranteed present
                            # (the action was produced from this positions list)
                            db.log_trade(act["coin"], side, "BE_LOCK",
                                         price=act["new_sl"],
                                         band=act.get("band"),
                                         note=act["reason"])
                            alerts.send(f"BREAKEVEN {act['coin']} — "
                                        f"{act['reason']}")

            # c0. cascade bounce track (Phase 8.6) — event-driven, runs
            # outside the ensemble gate; one bounce position at a time.
            # While it's open the state is CASCADE_BOUNCE_ACTIVE, which
            # pauses ensemble entries (can_open requires ACTIVE) but keeps
            # position management (step b) running.
            if cb_enabled:
                if cb_coin:
                    positions = with_retry(xc.positions, "positions") or []
                    still_open = any(
                        (p.get("position") or {}).get("coin") == cb_coin
                        and float((p.get("position") or {}).get("szi") or 0)
                        != 0 for p in positions)
                    if not still_open:
                        log.warning("cascade bounce on %s closed — resuming "
                                    "ensemble", cb_coin)
                        db.set_state("cascade_bounce_coin", "")
                        risk.exit_cascade_bounce()
                        cb_coin = None
                        state = risk.state
                    elif state == BotState.ACTIVE:
                        # state was stomped by a reconnect/restart — re-assert
                        risk.enter_cascade_bounce(cb_coin)
                        state = risk.state
                elif state == BotState.ACTIVE:
                    for coin in coins_active:
                        if coin in coins_disabled:
                            continue
                        cbsig = cb_model.compute(coin, buf, liq_conn)
                        if not cbsig:
                            continue
                        ok, reason, max_usd = \
                            risk.check_cascade_bounce_allocation(coin)
                        if not ok:
                            log.warning("cascade bounce %s vetoed: %s",
                                        coin, reason)
                            continue
                        is_long = cbsig["side"] == LONG
                        signals.log(coin, "CASCADE_BOUNCE", cbsig["side"],
                                    cbsig["confidence"], cbsig)
                        # maker-then-taker: try post-only briefly, then take
                        # the market — in a dislocation speed beats fees
                        mres = xc.try_limit_entry(coin, is_long, max_usd,
                                                  timeout_s=cb_maker_timeout)
                        fill_px = mres.get("avg_px")
                        note = f"cb_maker:{mres['status']}"
                        if mres["status"] not in ("filled", "partial") \
                                and cb_taker_fallback:
                            res = with_retry(
                                lambda c=coin, lg=is_long:
                                xc.market_open(c, lg, max_usd),
                                f"cb_market_open({coin})")
                            if res:
                                fill_px, st_note = parse_fill(res)
                                note = f"cb_taker:{st_note}"
                            else:
                                note = "cb_taker:order failed"
                        if not fill_px:
                            db.log_trade(coin, cbsig["side"], "OPEN",
                                         status="failed", note=note)
                            continue
                        sl = fill_px * (1 - cb_sl_pct if is_long
                                        else 1 + cb_sl_pct)
                        tp = fill_px * (1 + cb_tp_pct if is_long
                                        else 1 - cb_tp_pct)
                        risk.register_entry(
                            coin, fill_px, sl, tp, is_long,
                            hold_hours=risk.cb_max_hold_minutes / 60)
                        risk.enter_cascade_bounce(coin)
                        cb_coin = coin
                        state = risk.state
                        db.set_state("cascade_bounce_coin", coin)
                        db.log_trade(coin, cbsig["side"], "OPEN",
                                     size=max_usd / fill_px, price=fill_px,
                                     leverage=1.0, status="filled",
                                     note=(f"cascade_bounce conf="
                                           f"{cbsig['confidence']:.2f} "
                                           f"move={cbsig['cascade_move_pct']:.4f}"
                                           f" {note}"))
                        alerts.send(
                            f"CASCADE BOUNCE {cbsig['side']} {coin} @ "
                            f"{fill_px}\nmove={cbsig['cascade_move_pct']:.2%}"
                            f" conf={cbsig['confidence']:.2f}\n"
                            f"sl={sl:.4f} tp={tp:.4f} "
                            f"max_hold={risk.cb_max_hold_minutes:.0f}min")
                        break

            # c. evaluate entries — DUAL BAND (2026-06-20)
            # Two aggregations per coin: SCALP on the fast resolution + TREND on
            # the slow one, each with its own fixed weight set (no regime weight
            # shuffle — trend-awareness is the explicit bias dampener below).
            # TREND is evaluated first (priority): a rare trend signal claims the
            # coin and scalp is then blocked on it by coin ownership (one-way
            # exchange nets per coin -> one band per coin at a time).
            if state == BotState.ACTIVE:
                live_tickets: dict = {}
                long_gates: dict = {}
                short_gates: dict = {}
                penalty = risk.regime_counter_trend_penalty
                for coin in coins_active:
                    if coin in coins_disabled:
                        continue
                    # dual aggregation — TREND first so its (1h) regime can
                    # dampen counter-trend OrderbookImbalance votes in BOTH
                    # bands (bid-heavy book in a downtrend = absorption, not a
                    # reversal — see BOOK_REGIME_DAMPEN).
                    trend_tickets = [m.compute(coin, buf, interval=trend_interval)
                                     for m in models]
                    trend_rt = next((t for t in trend_tickets
                                     if t.model == "RegimeDetectorModel"), None)
                    trend_regime = (trend_rt.direction
                                    if trend_rt and trend_rt.direction in REGIME_NAMES
                                    else "UNKNOWN")
                    trend_sig = aggregator.aggregate(
                        coin, trend_tickets, weights=TREND_WEIGHTS,
                        regime_routing=False, book_regime=trend_regime)
                    scalp_tickets = [m.compute(coin, buf, interval=scalp_interval)
                                     for m in models]
                    scalp_sig = aggregator.aggregate(
                        coin, scalp_tickets, weights=SCALP_WEIGHTS,
                        regime_routing=False, book_regime=trend_regime)
                    # regime bias: 1h (trend) regime DAMPENS counter-trend scalp
                    # confidence (never blocks). Trend signal is never modified.
                    apply_regime_bias(scalp_sig, trend_sig.regime, penalty)

                    # structural gates — SCALP band only; computed every loop for
                    # the dashboard regardless of verdict, reused for scalp entry.
                    g_allowed, g_detail = long_structural_gate(
                        coin, buf, long_struct)
                    long_gates[coin] = {**g_detail,
                                        "enabled": long_struct["enabled"]}
                    s_allowed, s_detail = short_structural_gate(
                        coin, buf, short_struct)
                    short_gates[coin] = {**s_detail,
                                         "enabled": short_struct["enabled"]}

                    # publish both bands' opinions for the dashboard
                    live_tickets[coin] = {
                        "scalp": _pack_band(scalp_tickets, scalp_sig),
                        "trend": _pack_band(trend_tickets, trend_sig)}
                    if scalp_sig.direction != FLAT:
                        signals.log(coin, "AGGREGATOR_SCALP", scalp_sig.direction,
                                    scalp_sig.confidence,
                                    {"regime": scalp_sig.regime,
                                     "long": scalp_sig.long_votes,
                                     "short": scalp_sig.short_votes,
                                     "flat": scalp_sig.flat_votes,
                                     **scalp_sig.meta})
                    if trend_sig.direction != FLAT:
                        signals.log(coin, "AGGREGATOR_TREND", trend_sig.direction,
                                    trend_sig.confidence,
                                    {"regime": trend_sig.regime,
                                     "long": trend_sig.long_votes,
                                     "short": trend_sig.short_votes,
                                     "flat": trend_sig.flat_votes,
                                     **trend_sig.meta})

                    # TREND first (priority), then SCALP if the coin is still free.
                    # Fix 1 (2026-06-22): order submission is SAME-ITERATION — the
                    # signal is aggregated immediately above and open_entry()
                    # submits the maker order synchronously here, in this cycle.
                    # There is no armed queue deferred to the next loop, so the
                    # arm->submit latency floor is just the per-coin model compute
                    # time (sub-second), not a 10s cycle. (Coins later in the list
                    # do wait on earlier coins' maker timeouts, but each coin's
                    # signal is re-aggregated right before its own submit, so it is
                    # never stale at submission — and the armed-signal TTL above
                    # bounds the across-cycle case.)
                    opened = False
                    if trend_band_enabled and trend_sig.direction != FLAT:
                        opened = open_entry(coin, trend_sig, "trend", g_allowed,
                                            g_detail, s_allowed, s_detail)
                    if (not opened and scalp_band_enabled
                            and scalp_sig.direction != FLAT):
                        open_entry(coin, scalp_sig, "scalp", g_allowed, g_detail,
                                   s_allowed, s_detail)

                if live_tickets:
                    db.set_state("live_tickets", json.dumps(
                        {"ts": int(time.time() * 1000),
                         "coins": live_tickets,
                         "long_gates": long_gates,
                         "short_gates": short_gates,
                         "bands": {"scalp_enabled": scalp_band_enabled,
                                   "trend_enabled": trend_band_enabled}},
                        default=str))

            # publish the in-trade tracker (entry/sl/tp/band per open coin) to
            # bot_state so the dashboard chart can draw the live TP/SL/entry
            # overlay. The tracker is the source of truth for sl/tp (it moves on
            # trailing-stop / breakeven-lock), and it's already kept in sync with
            # the real positions (closed coins are dropped from it each cycle).
            db.set_state("pos_track", json.dumps({
                c: {"entry": t["entry_px"], "sl": t["sl"], "tp": t["tp"],
                    "is_long": t["is_long"], "band": t.get("band")}
                for c, t in risk._pos_track.items()}, default=str))

            # d. heartbeat + status
            hb_path.write_text(str(int(time.time())))
            if time.time() - last_status >= 60:
                last_status = time.time()
                log.info("STATUS | state=%s | %s | feed_age=%.1fs",
                         state.value, buf.status_line(),
                         buf.seconds_since_msg())
                db.set_state("risk_state", state.value)

            # e. sleep out the cycle
            time.sleep(max(0.5, loop_s - (time.time() - cycle_start)))
    finally:
        log.info("shutting down...")
        db.set_state("status", "stopped")
        pollers.stop()
        feed.stop()
        spot_poller.stop()


if __name__ == "__main__":
    main()
