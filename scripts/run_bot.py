#!/usr/bin/env python3
"""Full trading loop: data -> models -> aggregator -> risk -> execution.

Replaces run_phase1.py as the live entrypoint once Phases 2-4 are verified.
Every entry passes through RiskManager.can_open(); every open position is
managed by RiskManager.check_open_positions() each cycle."""
import fcntl
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from collections import Counter, deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Internal liveness watchdog. The main loop bumps _loop_alive at the end of
# every cycle; a daemon thread force-exits the process if that timestamp goes
# stale. systemd (Restart=always) then relaunches a fresh, warm bot. On
# 2026-06-25 the loop wedged on a timeout-less socket and sat dead for ~4.4h
# with an open, underwater position — a hung loop must self-heal, not linger.
_loop_alive = [0.0]  # mutable so the main loop can bump it without `global`


def _liveness_watchdog(stale_after_s: float):
    log = get_logger("liveness")
    log.info("internal liveness watchdog armed: force-exit if loop stalls "
             ">%.0fs", stale_after_s)
    while True:
        time.sleep(5.0)
        last = _loop_alive[0]
        if last and (time.time() - last) > stale_after_s:
            log.error("MAIN LOOP STALLED %.0fs — force-exiting for systemd "
                      "restart", time.time() - last)
            try:
                alerts.send(f"🔁 bot loop stalled "
                            f"{time.time() - last:.0f}s — self-restarting")
            except Exception:
                pass
            time.sleep(1.0)  # give the alert thread a moment
            os._exit(1)

from reaper import alerts
from reaper.aggregator import (REGIME_NAMES, TREND_WEIGHTS,  # noqa: F401
                               SignalAggregator)
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
from reaper.models.momentum_model import MomentumModel
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
    Not used for live entries (the structural gates it was superseded by were
    retired with the scalp band, 2026-06-26); retained for the regression suite."""
    return sum(1 for t in model_votes.values()
               if t.model in models and t.direction == LONG)


# --- Regime memory (2026-06-26) --------------------------------------------
# The trend band has no regime memory: it treats every 1h evaluation as
# independent, so a single RANGING candle in a sustained downtrend immediately
# re-opens the door to full LONG conviction (VWAP sees price below session VWAP
# as "cheap", Momentum sees the bounce velocity). The June 25-26 signal history
# shows this firing confidently-wrong LONGs on every bounce (30.8% accuracy,
# avg 30m return -0.61%). Regime memory keeps a rolling buffer of the last N 1h
# regime reads and SUPPRESSES a trend entry whose direction is contradicted by
# the recent dominant regime. It is NOT a gate — confidence scores and model
# weights are untouched; it only blocks opening a position the regime history
# argues against. Trend band only (scalp has its own structural gates).
def get_dominant_regime(history) -> str:
    """Most common regime in recent history (UNKNOWN if empty)."""
    if not history:
        return "UNKNOWN"
    return Counter(history).most_common(1)[0][0]


def regime_allows_entry(history, direction: str,
                        threshold: float = 0.5) -> bool:
    """True if the recent regime history is consistent with `direction`.

    `threshold` is the fraction of recent regimes that must OPPOSE the entry to
    suppress it (default 0.5 -> suppress if >=50% of the window opposes). LONG is
    opposed by TRENDING_DOWN, SHORT by TRENDING_UP. With <2 samples there is not
    enough history to judge, so the entry is allowed (warmup is fail-open)."""
    if not history or len(history) < 2:
        return True  # insufficient history — allow entry
    counts = Counter(history)
    total = len(history)
    if direction == LONG:
        return counts.get("TRENDING_DOWN", 0) / total < threshold
    if direction == SHORT:
        return counts.get("TRENDING_UP", 0) / total < threshold
    return True  # FLAT or unknown — allow


def regime_memory_reason(history, direction: str) -> str:
    """signal_history gate_block_reason string for a suppressed entry, e.g.
    'regime_memory: 75% TRENDING_DOWN in last 4 evals'."""
    opp = "TRENDING_DOWN" if direction == LONG else "TRENDING_UP"
    n = len(history) if history else 0
    frac = (Counter(history).get(opp, 0) / n) if n else 0.0
    return f"regime_memory: {frac:.0%} {opp} in last {n} evals"


# --- Trend-band ensemble weights (2026-06-27) ------------------------------
# config models.trend_weights uses short keys; the aggregator weight set is
# keyed by model class name. This maps one to the other.
_TREND_WEIGHT_KEYS = {
    "ob": "OrderbookImbalanceModel",
    "vwap": "VWAPModel",
    "ta": "TAModel",
    "funding": "FundingRateModel",
    "momentum": "MomentumModel",
}


def load_trend_weights(m_raw: dict) -> dict:
    """Build the trend-band weight set by overlaying config models.trend_weights
    onto the TREND_WEIGHTS defaults. Returns a fresh class-name-keyed dict so it
    can be hot-reloaded each loop (the aggregator renormalizes, so the values
    need not sum to 1.0). Unknown/omitted keys keep their default."""
    w = dict(TREND_WEIGHTS)
    cfg_w = (m_raw.get("trend_weights", {}) or {})
    for short, model in _TREND_WEIGHT_KEYS.items():
        if short in cfg_w:
            try:
                w[model] = float(cfg_w[short])
            except (TypeError, ValueError):
                pass
    return w


# Throttle high-frequency skip logging. The direction switches (and other
# per-coin skips) reject the same coin every loop (~10s); without this the trades
# table
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
    tid = db.log_trade(coin, direction, "OPEN",
                       size=usd_size / (fill_px or cur_mid or 1),
                       price=fill_px, band=band,
                       status="taker_fallback" if fill_px else "taker_failed",
                       note=(f"maker:taker_fallback conf={sig.confidence:.2f} "
                             f"votes={agreement} active={sig.long_votes + sig.short_votes} "
                             f"fmap={fmap} {st_note}"))
    return {"status": "taker_fallback" if fill_px else "taker_failed",
            "fill_px": fill_px, "sig": sig, "agreement": agreement,
            "trade_id": tid if fill_px else None}


# model name -> signal_history vote_* column. MomentumModel got vote_momentum
# (it's the 6th active voter, post-dates the original spec); ML/LiqHeatmap are
# parked non-voters but still produce tickets, so their real FLAT vote is logged.
_VOTE_COLS = {
    "TAModel": "vote_ta",
    "MeanReversionModel": "vote_meanrev",
    "VWAPModel": "vote_vwap",
    "FundingRateModel": "vote_funding",
    "OrderbookImbalanceModel": "vote_ob",
    "MomentumModel": "vote_momentum",
    "RegimeDetectorModel": "vote_regime",
    "LiquidationHeatmapModel": "vote_liqmap",
    "MLForecastModel": "vote_ml",
}


def _signal_gate_eval(sig, threshold: float, required: int,
                      conf_pre_bias: float | None = None):
    """(cleared, reason) for the conf+agreement gate, in the spec's priority
    order. Pure read — never mutates sig. `conf_pre_bias` is the scalp band's
    confidence before apply_regime_bias dampened it (None for the trend band)."""
    if str(sig.meta.get("block_reason", "")).startswith("funding_hard_block"):
        return False, "funding_hard_block"
    if sig.direction == LONG:
        agreement = sig.long_votes
    elif sig.direction == SHORT:
        agreement = sig.short_votes
    else:
        agreement = max(sig.long_votes, sig.short_votes)
    # regime dampening is reported only when it is what pushed an otherwise
    # passing confidence below the gate — a more specific reason than plain conf.
    if (conf_pre_bias is not None and conf_pre_bias >= threshold
            and sig.confidence < threshold
            and "counter_trend" in str(sig.meta.get("regime_bias", ""))):
        return False, f"regime_bias_dampened to {sig.confidence:.3f}"
    if sig.confidence < threshold:
        return False, f"conf {sig.confidence:.3f} < {threshold:.3f}"
    if agreement < required:
        return False, f"agreement {agreement} < {required}"
    return True, None


def _signal_history_fields(coin: str, band: str, sig, tickets: list,
                           threshold: float, required: int,
                           conf_pre_bias: float | None, trade_id,
                           override_block_reason: str | None = None) -> dict:
    """Build a signal_history row dict from an aggregator evaluation.

    `override_block_reason` forces the row to NOT-cleared with that reason — used
    by the trend band's regime-memory suppression, which blocks an otherwise
    gate-clearing signal for a reason the conf/agreement gate eval can't see."""
    import datetime as _dt
    cleared, reason = _signal_gate_eval(sig, threshold, required, conf_pre_bias)
    if override_block_reason:
        cleared, reason = False, override_block_reason
    votes = {t.model: f"{t.direction}:{t.confidence:.2f}" for t in tickets}
    # bool is a subclass of int — guard so a truthy non-id return never lands in
    # the trade_id column as 0/1.
    tid = trade_id if isinstance(trade_id, int) and not isinstance(
        trade_id, bool) else None
    row = {
        "ts_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "coin": coin, "band": band, "regime": sig.regime,
        "final_direction": sig.direction,
        "final_conf": round(sig.confidence, 4),
        "active_voters": sig.long_votes + sig.short_votes,
        "cleared_gate": 1 if cleared else 0,
        "gate_block_reason": reason,
        "trade_id": tid,
    }
    for model, col in _VOTE_COLS.items():
        row[col] = votes.get(model, "INACTIVE:0.00")
    return row


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
    # armed-signal TTL ceiling (2026-06-22) — drop a setup that has stayed armed
    # (clearing the gate but not filling) longer than this.
    trend_armed_ttl = float(t_raw.get("trend_armed_ttl_seconds", 45))
    # Hard RANGING lockout (2026-06-27): trend-band's FIRST entry check — block
    # ALL new entries (both directions) when the current 1h regime is RANGING.
    ranging_lockout_enabled = bool(t_raw.get("ranging_lockout_enabled", True))
    # Regime memory (2026-06-26): trend-band pre-entry check — suppress a trend
    # entry whose direction is contradicted by the recent 1h regime history.
    regime_memory_enabled = bool(t_raw.get("regime_memory_enabled", True))
    regime_memory_window = int(t_raw.get("regime_memory_window", 4))
    regime_memory_threshold = float(t_raw.get("regime_memory_threshold", 0.5))
    # Trend-band ensemble weights (2026-06-27): config models.trend_weights
    # overlaid on the TREND_WEIGHTS defaults, re-read each loop (hot-reload).
    trend_weights = load_trend_weights(m_raw)
    # SCALP BAND RETIRED 2026-06-26 — data-driven decision, trend-only operation.
    # The scalp band (5m) and the structural gates that fed it are gone from the
    # live loop; only the 1h trend band evaluates and trades. Scalp/gate config
    # keys may remain in config.yaml but are inert. trend_band_enabled is kept so
    # the trend band can still be paused from Controls.
    trend_band_enabled = bool(t_raw.get("trend_band_enabled", True))
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
    # TAModel regime-aware RSI thresholds (config models.ta.*; hot-reloaded each
    # loop below). Kept as a named instance so the loop can update its trending
    # thresholds in place, like funding_model.smooth_mapping.
    ta_cfg = (m_raw.get("ta", {}) or {})
    ta_trending = (ta_cfg.get("trending", {}) or {})
    ta_ranging = (ta_cfg.get("ranging", {}) or {})
    ta_model = TAModel(
        trending_rsi_short=float(ta_trending.get("rsi_short", 48.0)),
        trending_rsi_long=float(ta_trending.get("rsi_long", 38.0)),
        trending_rsi_neutral_low=float(ta_trending.get("rsi_neutral_low", 48.0)),
        trending_rsi_neutral_high=float(ta_trending.get("rsi_neutral_high", 55.0)),
        ranging_rsi_short=float(ta_ranging.get("rsi_short", 68.0)),
        ranging_rsi_long=float(ta_ranging.get("rsi_long", 32.0)))
    # MomentumModel price-velocity thresholds (config models.momentum.*; hot-
    # reloaded each loop below). Named instance so the loop can update its
    # thresholds in place, like ta_model / funding_model.
    mom_cfg = (m_raw.get("momentum", {}) or {})
    momentum_model = MomentumModel(
        enter_z=float(mom_cfg.get("enter_z", 0.6)),
        full_conf_z=float(mom_cfg.get("full_conf_z", 2.6)),
        vol_window=int(mom_cfg.get("vol_window", 14)),
        lookbacks=tuple(mom_cfg.get("lookbacks", (1, 2, 3))),
        min_candles=int(mom_cfg.get("min_candles", 20)))
    models = [
        RegimeDetectorModel(),   # first: publishes regime for the others
        ta_model,
        MeanReversionModel(),
        funding_model,
        OrderbookImbalanceModel(
            top_levels=int(m_raw.get("ob_top_levels", 10)),
            min_imbalance=float(m_raw.get("ob_min_imbalance", 0.30))),
        VWAPModel(),
        momentum_model,
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
            .get("funding_counter_trend_damp", 0.40)),
        momentum_ranging_damp=float(
            (m_raw.get("momentum", {}) or {}).get("ranging_weight_damp", 0.70)))
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
    log.info("SCALP BAND RETIRED 2026-06-26 — trend-only operation; structural "
             "gates removed")
    maker_streaks = MakerTimeoutTracker(fallback_n, fallback_window_s)
    armed_signals = ArmedSignalTracker()
    log.info("armed-signal TTL ENABLED — drop stale trend setups after %.0fs",
             trend_armed_ttl)
    # Regime memory buffers (2026-06-26): per-coin rolling deque of the last
    # `window` 1h regime reads + the last trend candle ts that fed each buffer
    # (so we append once per CLOSED 1h candle, not once per ~10s loop). Start
    # EMPTY — entries are allowed until a buffer has >=2 samples (fail-open
    # warmup), which is correct: a fresh restart has no regime history to argue
    # against. The buffers are NOT persisted; they refill from live candles.
    regime_history: dict[str, deque] = {}
    last_regime_candle_t: dict[str, int] = {}
    if ranging_lockout_enabled:
        log.info("RANGING LOCKOUT ENABLED (trend band) — no new entries while "
                 "the current 1h regime is RANGING")
    log.info("trend weights: OB=%.2f VWAP=%.2f TA=%.2f FUND=%.2f MOM=%.2f",
             trend_weights["OrderbookImbalanceModel"], trend_weights["VWAPModel"],
             trend_weights["TAModel"], trend_weights["FundingRateModel"],
             trend_weights["MomentumModel"])
    if regime_memory_enabled:
        log.info("REGIME MEMORY ENABLED (trend band) — suppress entries when "
                 ">=%.0f%% of the last %d 1h regimes oppose the direction",
                 regime_memory_threshold * 100, regime_memory_window)
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

    def open_entry(coin: str, sig, band: str) -> bool:
        """Attempt ONE band entry for coin given its aggregated signal. Returns
        the opened trade's id (truthy) iff a position was opened, else False (the
        id links signal_history to the trade). Trend-only since 2026-06-26 —
        `band` is always "trend"; the trend band's own 1h signal IS its gate.
        Structural gates were retired with the scalp band."""
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
        armed_ttl = trend_armed_ttl
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
        weights = TREND_WEIGHTS
        interval = trend_interval
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
                            return fb.get("trade_id") or True
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
        tid = db.log_trade(coin, sig.direction, "OPEN",
                           size=coin_usd / (fill_px or entry_ref),
                           price=fill_px, leverage=lev, band=band,
                           status="filled" if fill_px else "failed",
                           note=f"conf={sig.confidence:.2f} votes={agreement} "
                                f"active={active} fmap={fmap} {note}")
        return tid if fill_px else False

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
    # signal_history retention: prune rows older than this window, hourly. The
    # table grows ~one row per active coin×band every loop, so it must self-trim.
    signal_retention_days = int(
        (cfg._raw.get("signal_history", {}) or {}).get("retention_days", 7))
    last_sig_prune = 0.0  # 0 -> prune on the first cycle (trims on every restart)
    prev_state = None
    # Arm the internal liveness watchdog: if a cycle wedges (hung socket,
    # deadlock) and stops bumping _loop_alive, self-restart via systemd well
    # before the external dead-man's switch has to flatten the account.
    _loop_alive[0] = time.time()
    liveness_stale_after = max(90.0, 3.0 * cfg.heartbeat_interval)
    threading.Thread(target=_liveness_watchdog, args=(liveness_stale_after,),
                     daemon=True).start()
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
            trend_armed_ttl = float(t_raw.get("trend_armed_ttl_seconds", 45))
            ranging_lockout_enabled = bool(
                t_raw.get("ranging_lockout_enabled", True))
            regime_memory_enabled = bool(
                t_raw.get("regime_memory_enabled", True))
            regime_memory_window = int(t_raw.get("regime_memory_window", 4))
            regime_memory_threshold = float(
                t_raw.get("regime_memory_threshold", 0.5))
            new_trend_weights = load_trend_weights(
                cfg._raw.get("models", {}) or {})
            if new_trend_weights != trend_weights:
                log.warning("LIVE CONFIG: trend weights %s -> %s",
                            trend_weights, new_trend_weights)
                trend_weights = new_trend_weights
            # SCALP BAND RETIRED 2026-06-26 — trend-only; structural gates gone.
            trend_band_enabled = bool(t_raw.get("trend_band_enabled", True))
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

            # TA regime-aware trending RSI thresholds — hot-reload in place from
            # the (override-merged) config each loop (like the funding mapping).
            ta_t = ((cfg._raw.get("models", {}) or {}).get("ta", {}) or {}) \
                .get("trending", {}) or {}
            new_trending = {
                "rsi_short": float(ta_t.get("rsi_short",
                                            ta_model.trending["rsi_short"])),
                "rsi_long": float(ta_t.get("rsi_long",
                                           ta_model.trending["rsi_long"])),
                "rsi_neutral_low": float(ta_t.get(
                    "rsi_neutral_low", ta_model.trending["rsi_neutral_low"])),
                "rsi_neutral_high": float(ta_t.get(
                    "rsi_neutral_high", ta_model.trending["rsi_neutral_high"])),
            }
            if new_trending != ta_model.trending:
                log.warning("LIVE CONFIG: TA trending thresholds %s -> %s",
                            ta_model.trending, new_trending)
                ta_model.trending = new_trending

            # Momentum thresholds + ranging weight damp — hot-reload in place
            # from the (override-merged) config each loop (like TA / funding).
            mom_c = (cfg._raw.get("models", {}) or {}).get("momentum", {}) or {}
            momentum_model.enter_z = float(
                mom_c.get("enter_z", momentum_model.enter_z))
            momentum_model.full_conf_z = float(
                mom_c.get("full_conf_z", momentum_model.full_conf_z))
            momentum_model.vol_window = int(
                mom_c.get("vol_window", momentum_model.vol_window))
            momentum_model.lookbacks = tuple(
                mom_c.get("lookbacks", momentum_model.lookbacks))
            momentum_model.min_candles = int(
                mom_c.get("min_candles", momentum_model.min_candles))
            new_mom_damp = float(
                mom_c.get("ranging_weight_damp", aggregator.momentum_ranging_damp))
            if new_mom_damp != aggregator.momentum_ranging_damp:
                log.warning("LIVE CONFIG: momentum ranging damp x%.2f -> x%.2f",
                            aggregator.momentum_ranging_damp, new_mom_damp)
                aggregator.momentum_ranging_damp = new_mom_damp

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
                for coin in coins_active:
                    if coin in coins_disabled:
                        continue
                    # trend-only aggregation (1h). Its regime also dampens
                    # counter-trend OrderbookImbalance votes (bid-heavy book in a
                    # downtrend = absorption, not a reversal — BOOK_REGIME_DAMPEN).
                    trend_tickets = [m.compute(coin, buf, interval=trend_interval)
                                     for m in models]
                    trend_rt = next((t for t in trend_tickets
                                     if t.model == "RegimeDetectorModel"), None)
                    trend_regime = (trend_rt.direction
                                    if trend_rt and trend_rt.direction in REGIME_NAMES
                                    else "UNKNOWN")
                    # regime-memory buffer: append the freshly-classified 1h
                    # regime ONCE per closed trend candle (keyed on the candle
                    # ts), so the deque tracks the last `window` HOURS of regime,
                    # not the last `window` ~10s loops. Rebuild the deque when the
                    # window is hot-reloaded (preserving the most recent samples).
                    rh = regime_history.get(coin)
                    if rh is None or rh.maxlen != regime_memory_window:
                        rh = deque(list(rh or [])[-regime_memory_window:],
                                   maxlen=regime_memory_window)
                        regime_history[coin] = rh
                    tc = buf.latest_candles(coin, trend_interval, 1)
                    cur_ct = tc[-1]["t"] if tc else None
                    if (cur_ct is not None
                            and cur_ct != last_regime_candle_t.get(coin)
                            and trend_regime in REGIME_NAMES):
                        rh.append(trend_regime)
                        last_regime_candle_t[coin] = cur_ct
                    trend_sig = aggregator.aggregate(
                        coin, trend_tickets, weights=trend_weights,
                        regime_routing=False, book_regime=trend_regime)

                    # SCALP BAND RETIRED 2026-06-26 — data-driven decision,
                    # trend-only operation. The 5m scalp aggregation, the regime
                    # bias that dampened it, and the structural gates that fed it
                    # are gone from the live loop. Only the 1h trend band below
                    # evaluates and trades.

                    # publish the trend band's opinion for the dashboard
                    live_tickets[coin] = {"trend": _pack_band(trend_tickets,
                                                              trend_sig)}
                    if trend_sig.direction != FLAT:
                        signals.log(coin, "AGGREGATOR_TREND", trend_sig.direction,
                                    trend_sig.confidence,
                                    {"regime": trend_sig.regime,
                                     "long": trend_sig.long_votes,
                                     "short": trend_sig.short_votes,
                                     "flat": trend_sig.flat_votes,
                                     **trend_sig.meta})

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
                    trend_tid = None
                    trend_rm_reason = None  # block reason (lockout / regime mem)
                    if trend_band_enabled and trend_sig.direction != FLAT:
                        # Hard RANGING lockout (2026-06-27): the FIRST entry check,
                        # ahead of regime memory. The bot has edge only in
                        # sustained 1h trends — in a RANGING regime high confidence
                        # is anti-predictive, so block ALL new entries (both
                        # directions) and simply wait. Binary on the CURRENT regime
                        # (not history); HIGH_VOL is not RANGING and is unaffected.
                        if (ranging_lockout_enabled
                                and trend_regime == "RANGING"):
                            trend_rm_reason = "ranging_lockout"
                            log.info("[RANGING_LOCKOUT] %s — regime is RANGING, "
                                     "no entry (%s conf=%.2f)", coin,
                                     trend_sig.direction, trend_sig.confidence)
                            if _should_log_skip(coin, "ranging_lockout"):
                                db.log_trade(
                                    coin, trend_sig.direction, "OPEN",
                                    band="trend", status="ranging_lockout",
                                    note="skip: ranging_lockout")
                        # Regime-memory pre-entry check (trend band only): if the
                        # recent dominant 1h regime opposes this direction, skip
                        # the entry (does NOT touch conf/weights — see
                        # regime_allows_entry). Buffer warmup is fail-open.
                        elif (regime_memory_enabled and not regime_allows_entry(
                                rh, trend_sig.direction,
                                regime_memory_threshold)):
                            trend_rm_reason = regime_memory_reason(
                                rh, trend_sig.direction)
                            log.info("[REGIME_MEMORY] %s %s suppressed — recent "
                                     "regime history: %s", coin,
                                     trend_sig.direction, list(rh))
                            if _should_log_skip(coin, "regime_memory"):
                                db.log_trade(
                                    coin, trend_sig.direction, "OPEN",
                                    band="trend",
                                    status="regime_memory_suppressed",
                                    note=f"skip: {trend_rm_reason}")
                        else:
                            trend_tid = open_entry(coin, trend_sig, "trend")

                    # signal_history: one row per cycle for this active coin,
                    # traded or not. Additive diagnostics only — a failure here
                    # must never break the loop.
                    try:
                        tpb = risk.band_params("trend")
                        db.log_signal_history(_signal_history_fields(
                            coin, "trend", trend_sig, trend_tickets,
                            tpb["min_confidence"], tpb["min_model_agreement"],
                            None, trend_tid,
                            override_block_reason=trend_rm_reason))
                    except Exception as e:
                        log.warning("signal_history logging failed for %s: %s",
                                    coin, e)

                if live_tickets:
                    db.set_state("live_tickets", json.dumps(
                        {"ts": int(time.time() * 1000),
                         "coins": live_tickets,
                         "bands": {"trend_enabled": trend_band_enabled}},
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

            # publish the regime-memory buffers so the Controls page can show
            # what the trend band is remembering per coin (oldest -> newest).
            db.set_state("regime_history", json.dumps({
                "ts": int(time.time() * 1000),
                "enabled": regime_memory_enabled,
                "window": regime_memory_window,
                "threshold": regime_memory_threshold,
                "dominant": {c: get_dominant_regime(h)
                             for c, h in regime_history.items()},
                "coins": {c: list(h) for c, h in regime_history.items()},
            }, default=str))

            # d. heartbeat + status
            hb_path.write_text(str(int(time.time())))
            _loop_alive[0] = time.time()  # feed the internal liveness watchdog
            if time.time() - last_status >= 60:
                last_status = time.time()
                log.info("STATUS | state=%s | %s | feed_age=%.1fs",
                         state.value, buf.status_line(),
                         buf.seconds_since_msg())
                db.set_state("risk_state", state.value)

            # d2. prune signal_history to the retention window (hourly, indexed
            # on ts_utc — cheap). Bounded growth without an external cron job.
            if time.time() - last_sig_prune >= 3600:
                last_sig_prune = time.time()
                try:
                    n = db.prune_signal_history(signal_retention_days)
                    if n:
                        log.info("pruned %d signal_history rows older than %dd",
                                 n, signal_retention_days)
                except Exception as e:
                    log.warning("signal_history prune failed: %s", e)

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
