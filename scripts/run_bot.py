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
from reaper.aggregator import SignalAggregator
from reaper.config import PROJECT_ROOT, Config
from reaper.data.buffer import MarketBuffer
from reaper.data.rest_pollers import RestPollers
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
from reaper.risk.manager import CLOSE_PENDING_TIMEOUT_S, RiskManager, with_retry
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
    """How many of `models` are actively voting LONG (Change B gate). Used by
    the entry loop and the dashboard verdict so they never drift apart."""
    return sum(1 for t in model_votes.values()
               if t.model in models and t.direction == LONG)


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


def run_taker_fallback(coin, is_long, usd_size, *, models, aggregator, buf, xc,
                       db, min_confidence, min_model_agreement,
                       exhaustion_atr_mult, start_mid) -> dict:
    """Maker-timeout streak hit N: re-validate the signal on the CURRENT
    buffer (never the cached one from streak start) and confirm the move is
    still live, then take the market only if both hold. Every decision —
    fired or skipped — is logged to the trades table for later audit.

    Returns {"status": taker_fallback|taker_skipped_degraded|
    taker_skipped_exhausted|taker_failed, "fill_px", "sig", "agreement"}."""
    direction = LONG if is_long else SHORT

    # Step 2 — re-validate the signal on current buffer state
    tickets = [m.compute(coin, buf) for m in models]
    sig = aggregator.aggregate(coin, tickets)
    agreement = (sig.long_votes if sig.direction == LONG else sig.short_votes)
    degraded = None
    if sig.direction != direction:
        degraded = f"direction {sig.direction} != {direction}"
    elif sig.confidence < min_confidence:
        degraded = f"conf {sig.confidence:.2f} < {min_confidence:.2f}"
    elif agreement < min_model_agreement:
        degraded = f"agreement {agreement} < {min_model_agreement}"
    if degraded:
        log.info("taker fallback %s %s SKIP — signal degraded: %s",
                 direction, coin, degraded)
        db.log_trade(coin, direction, "OPEN", status="taker_skipped_degraded",
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
    db.log_trade(coin, direction, "OPEN",
                 size=usd_size / (fill_px or cur_mid or 1),
                 price=fill_px,
                 status="taker_fallback" if fill_px else "taker_failed",
                 note=(f"maker:taker_fallback conf={sig.confidence:.2f} "
                       f"votes={agreement} {st_note}"))
    return {"status": "taker_fallback" if fill_px else "taker_failed",
            "fill_px": fill_px, "sig": sig, "agreement": agreement}


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
    # LONG-only microstructure confirmation gate (Change B, 2026-06-16): a LONG
    # verdict must be confirmed by at least N of the listed live models actively
    # voting LONG, else skip. SHORTs (the working side) are never gated.
    long_conf_enabled = bool(t_raw.get("long_confirmation_enabled", True))
    long_conf_models = set(t_raw.get(
        "long_confirmation_models",
        ["OrderbookImbalanceModel", "VWAPModel"]))
    long_conf_min = int(t_raw.get("long_confirmation_min", 1))
    # SHORT mirror — OFF by default (working side stays untouched); exposable
    # for testing via the controls page.
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
    feed.start()
    pollers.start()

    # 2-4. models, aggregator, risk
    xc = ExchangeClient(cfg)
    models = [
        RegimeDetectorModel(),   # first: publishes regime for the others
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
    aggregator = SignalAggregator(
        funding_hard_block_enabled=bool(
            r_raw.get("funding_hard_block_enabled", True)),
        funding_hard_block_conf=float(
            r_raw.get("funding_hard_block_conf", 0.75)),
        funding_hard_block_short_enabled=bool(
            r_raw.get("funding_hard_block_short_enabled", False)),
        funding_hard_block_short_conf=float(
            r_raw.get("funding_hard_block_short_conf", 0.75)))
    if aggregator.funding_hard_block_enabled:
        log.info("funding HARD-block ENABLED — FundingRate SHORT conf >= %.2f "
                 "blocks all LONG entries", aggregator.funding_hard_block_conf)
    if long_conf_enabled:
        log.info("LONG confirmation gate ENABLED — need >=%d of %s voting LONG",
                 long_conf_min, sorted(long_conf_models))
    maker_streaks = MakerTimeoutTracker(fallback_n, fallback_window_s)
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
                     note=reason)
        return bool(res)

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
            long_conf_enabled = bool(
                t_raw.get("long_confirmation_enabled", True))
            long_conf_models = set(t_raw.get(
                "long_confirmation_models",
                ["OrderbookImbalanceModel", "VWAPModel"]))
            long_conf_min = int(t_raw.get("long_confirmation_min", 1))
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
                            log.warning("CLOSE %s: %s", act["coin"],
                                        act["reason"])
                            close_position(act["coin"], act["reason"])
                            alerts.send(f"CLOSE {act['coin']} — "
                                        f"{act['reason']}")
                        elif act["action"] == "UPDATE_SL":
                            log.info("SL -> %.4f on %s (%s)", act["new_sl"],
                                     act["coin"], act["reason"])

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

            # c. evaluate entries
            if state == BotState.ACTIVE:
                live_tickets: dict = {}
                for coin in coins_active:
                    if coin in coins_disabled:
                        continue
                    tickets = [m.compute(coin, buf) for m in models]
                    # publish current model opinions for the dashboard —
                    # single overwritten key, zero table growth
                    live_tickets[coin] = [
                        {"model": t.model, "direction": t.direction,
                         "confidence": round(t.confidence, 3),
                         "meta": t.meta} for t in tickets]
                    sig = aggregator.aggregate(coin, tickets)
                    if sig.direction == FLAT:
                        continue
                    signals.log(coin, "AGGREGATOR", sig.direction,
                                sig.confidence,
                                {"regime": sig.regime,
                                 "long": sig.long_votes,
                                 "short": sig.short_votes,
                                 "flat": sig.flat_votes,
                                 **sig.meta})
                    # Change B: LONG entries require live microstructure
                    # confirmation. SHORTs (the working side) are never gated.
                    if long_conf_enabled and sig.direction == LONG:
                        confirming = long_confirmation_count(
                            sig.model_votes, long_conf_models)
                        if confirming < long_conf_min:
                            log.info("LONG entry skipped: no microstructure "
                                     "confirmation (%d/%d of %s voting LONG) "
                                     "on %s", confirming, long_conf_min,
                                     sorted(long_conf_models), coin)
                            db.log_trade(coin, sig.direction, "OPEN",
                                         status="long_unconfirmed",
                                         note=("skip: no microstructure "
                                               f"confirmation ({confirming}/"
                                               f"{long_conf_min})"))
                            continue
                    # SHORT mirror (OFF by default) — same OB/VWAP gate
                    if short_conf_enabled and sig.direction == SHORT:
                        confirming = sum(
                            1 for t in sig.model_votes.values()
                            if t.model in short_conf_models
                            and t.direction == SHORT)
                        if confirming < short_conf_min:
                            log.info("SHORT entry skipped: no microstructure "
                                     "confirmation (%d/%d of %s voting SHORT) "
                                     "on %s", confirming, short_conf_min,
                                     sorted(short_conf_models), coin)
                            db.log_trade(coin, sig.direction, "OPEN",
                                         status="short_unconfirmed",
                                         note=("skip: no microstructure "
                                               f"confirmation ({confirming}/"
                                               f"{short_conf_min})"))
                            continue
                    # per-coin size/leverage overrides (controls page) fall back
                    # to the global defaults when unset.
                    pc = per_coin_cfg.get(coin, {}) or {}
                    coin_usd = float(pc.get("usd_size") or usd_size)
                    coin_lev = float(pc.get("leverage") or default_lev)
                    agreement = (sig.long_votes if sig.direction == LONG
                                 else sig.short_votes)
                    ok, reason = risk.can_open(coin, sig.direction,
                                               sig.confidence, agreement,
                                               coin_lev)
                    if not ok:
                        log.debug("skip %s %s: %s", coin, sig.direction,
                                  reason)
                        continue

                    lev = risk.clamp_leverage(coin_lev)
                    atr = atr_from_candles(buf.latest_candles(coin, "1m", 60))
                    entry_ref = buf.mid(coin)
                    if not atr or not entry_ref:
                        log.warning("skip %s: no ATR/mid for stops", coin)
                        continue
                    is_long = sig.direction == LONG
                    sl, tp = risk.calc_sl_tp(coin, entry_ref, is_long, atr)

                    log.warning("ENTRY %s %s conf=%.2f votes=%d regime=%s "
                                "sl=%.4f tp=%.4f", sig.direction, coin,
                                sig.confidence, agreement, sig.regime, sl, tp)
                    for tk in tickets:   # full ticket breakdown for the journal
                        signals.log(coin, tk.model, tk.direction,
                                    tk.confidence, tk.meta)
                    if entry_style == "maker":
                        # post-only entry; maker stays the default. On repeated
                        # timeouts (price outrunning the limit) the intelligent
                        # taker fallback re-validates and may take the market —
                        # otherwise the signal is skipped (never blind-chase).
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
                                        aggregator=aggregator, buf=buf, xc=xc,
                                        db=db,
                                        min_confidence=risk.min_confidence,
                                        min_model_agreement=(
                                            risk.min_model_agreement),
                                        exhaustion_atr_mult=(
                                            fallback_exhaustion_mult),
                                        start_mid=streak["start_mid"])
                                    # streak resolved either way — reset it
                                    maker_streaks.reset(coin)
                                    if fb["status"] == "taker_fallback" \
                                            and fb["fill_px"]:
                                        fill_px = fb["fill_px"]
                                        sl, tp = risk.calc_sl_tp(
                                            coin, fill_px, is_long, atr)
                                        risk.register_entry(
                                            coin, fill_px, sl, tp, is_long)
                                        alerts.send(
                                            f"TAKER FALLBACK {sig.direction} "
                                            f"{coin} @ {fill_px}\n"
                                            f"conf={fb['sig'].confidence:.2f} "
                                            f"votes={fb['agreement']} "
                                            f"(maker timed out {streak['count']}"
                                            f"x)\nsl={sl:.4f} tp={tp:.4f}")
                            if not triggered:
                                log.info("maker entry %s on %s — skipped "
                                         "(timeout streak=%d/%d)",
                                         mres["status"], coin, streak_count,
                                         fallback_n)
                                db.log_trade(coin, sig.direction, "OPEN",
                                             status=mres["status"], note=note)
                            continue
                        # maker filled — clear any prior timeout streak
                        maker_streaks.reset(coin)
                    else:
                        res = with_retry(
                            lambda: xc.market_open(coin, is_long, coin_usd),
                            f"market_open({coin})")
                        if not res:
                            db.log_trade(coin, sig.direction, "OPEN",
                                         status="error", note="order failed")
                            continue
                        fill_px, note = parse_fill(res)
                    if fill_px:
                        sl, tp = risk.calc_sl_tp(coin, fill_px, is_long, atr)
                        risk.register_entry(coin, fill_px, sl, tp, is_long)
                        alerts.send(
                            f"OPEN {sig.direction} {coin} @ {fill_px}\n"
                            f"conf={sig.confidence:.2f} votes={agreement} "
                            f"regime={sig.regime}\nsl={sl:.4f} tp={tp:.4f}")
                    db.log_trade(coin, sig.direction, "OPEN",
                                 size=coin_usd / (fill_px or entry_ref),
                                 price=fill_px, leverage=lev,
                                 status="filled" if fill_px else "failed",
                                 note=f"conf={sig.confidence:.2f} "
                                      f"votes={agreement} {note}")

                if live_tickets:
                    db.set_state("live_tickets", json.dumps(
                        {"ts": int(time.time() * 1000),
                         "coins": live_tickets}, default=str))

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


if __name__ == "__main__":
    main()
