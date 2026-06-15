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
from reaper.models import FLAT, LONG, atr_from_candles
from reaper.models.cascade_bounce import CascadeBounceModel
from reaper.models.funding_rate import FundingRateModel
from reaper.models.liquidation_heatmap import LiquidationHeatmapModel
from reaper.models.mean_reversion import MeanReversionModel
from reaper.models.ml_forecast import MLForecastModel
from reaper.models.orderbook_imbalance import OrderbookImbalanceModel
from reaper.models.regime_detector import RegimeDetectorModel
from reaper.models.ta_model import TAModel
from reaper.models.vwap_model import VWAPModel
from reaper.risk.manager import RiskManager, with_retry
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
    loop_s = float(t_raw.get("loop_interval_seconds", 10))
    usd_size = float(t_raw.get("default_usd_size", 50))
    default_lev = float(t_raw.get("default_leverage", 3.0))
    coins_active = t_raw.get("coins_active", cfg.coins)
    entry_style = t_raw.get("entry_style", "maker")
    entry_timeout = float(t_raw.get("entry_timeout_seconds", 30))
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
    aggregator = SignalAggregator()
    risk = RiskManager(cfg, buf, db, xc)
    signals = SignalWriter(cfg.db_path)
    # publish the ACTIVE mode for the dashboard — source of truth is the
    # running process, not the config file (which can change between restarts)
    db.set_state("trading_mode", risk.mode)

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
                            res = with_retry(
                                lambda c=act["coin"]: xc.market_close(c),
                                f"market_close({act['coin']})")
                            db.log_trade(act["coin"], "?", "CLOSE",
                                         status="ok" if res else "error",
                                         note=act["reason"])
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
                                 "flat": sig.flat_votes})
                    agreement = (sig.long_votes if sig.direction == LONG
                                 else sig.short_votes)
                    ok, reason = risk.can_open(coin, sig.direction,
                                               sig.confidence, agreement,
                                               default_lev)
                    if not ok:
                        log.debug("skip %s %s: %s", coin, sig.direction,
                                  reason)
                        continue

                    lev = risk.clamp_leverage(default_lev)
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
                        # post-only entry; not filled in time -> skip the
                        # signal entirely (never chase with a taker order)
                        mres = xc.try_limit_entry(coin, is_long, usd_size,
                                                  timeout_s=entry_timeout)
                        fill_px = mres.get("avg_px")
                        note = f"maker:{mres['status']}"
                        if mres["status"] not in ("filled", "partial"):
                            log.info("maker entry %s on %s — skipped",
                                     mres["status"], coin)
                            db.log_trade(coin, sig.direction, "OPEN",
                                         status=mres["status"], note=note)
                            continue
                    else:
                        res = with_retry(
                            lambda: xc.market_open(coin, is_long, usd_size),
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
                                 size=usd_size / (fill_px or entry_ref),
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
