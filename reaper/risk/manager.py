"""RiskManager: 4-layer guard system + bot state machine (Phase 2)."""
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from reaper.logger import get_logger
from reaper.models import atr_from_candles
from reaper.risk.state import BotState

log = get_logger("risk")

# After a close order is accepted we suppress further SL/TP re-evaluation on
# that coin for this long. The on-chain position state can lag the close by
# 1-2 cycles; without this, the in-trade guard re-fires the identical close
# every loop until the state refreshes (the duplicate-close bug). If the
# position is STILL open once this expires it's treated as a genuinely stuck
# position and re-evaluated (so a real failed close eventually retries).
CLOSE_PENDING_TIMEOUT_S = 30.0

# Confidence-gate float tolerance (2026-06-22). The aggregator computes
# confidence as a weighted-score division (abs(score)/active_weight), which can
# land one ULP below an intended at-threshold value (e.g. a true 0.30 stored as
# 0.2999999998) and be wrongly rejected by a strict `< threshold`. Subtracting
# this epsilon lets at-threshold values pass. It does NOT lower the effective
# gate for genuinely-below values — 0.29 still fails a 0.30 gate. (It is NOT a
# fix for marginal signals reading ~0.29 in 2-decimal logs; those are truly
# below the gate and only a lower scalp_min_confidence will admit them.)
CONF_GATE_EPS = 1e-9


def with_retry(fn: Callable, what: str, tries: int = 3):
    """Layer 4 API wrapper: exponential backoff, returns None on final failure."""
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            wait = 2 ** i
            log.warning("%s failed (%s) — retry in %ds", what, e, wait)
            if i < tries - 1:
                time.sleep(wait)
    log.error("%s failed after %d tries", what, tries)
    return None


class RiskManager:
    """Owns the bot state machine and every pre-trade / in-trade / market /
    infra guard. Nothing opens a position without passing through here."""

    def __init__(self, cfg, buf, db, xc):
        self.cfg = cfg
        self.buf = buf
        self.db = db
        self.xc = xc

        # all tunable guard params live in _load_params() so the trading loop
        # can hot-reload them each cycle (refresh_params) after live_config
        # overrides are merged onto the config — no restart needed.
        self._load_params()

        # internal tracking
        self._oi_hist: dict[str, deque] = {c: deque(maxlen=120) for c in buf.coins}
        self._pos_track: dict[str, dict] = {}        # coin -> entry/sl/tp tracker
        # coin -> ts a close order was accepted; suppresses duplicate close
        # attempts while the exchange position state catches up (see
        # check_open_positions / mark_close_pending).
        self._close_pending: dict[str, float] = {}
        self._flash_pause_until: dict[str, float] = {}
        self._long_halt: set[str] = set()
        self._short_halt: set[str] = set()
        self._equity_cache: tuple[float, float] = (0.0, 0.0)  # (fetched_at, value)
        self._halted_until = float(db.get_state("risk_halted_until") or 0)
        self._cooldown_until = float(db.get_state("risk_cooldown_until") or 0)
        self.day_open_equity = float(db.get_state("risk_day_open_equity") or 0)
        self.week_open_equity = float(db.get_state("risk_week_open_equity") or 0)
        self._day_anchor = db.get_state("risk_day_anchor") or ""
        self._week_anchor = db.get_state("risk_week_anchor") or ""

        persisted = db.get_state("risk_state")
        try:
            self.state = BotState(persisted) if persisted else BotState.ACTIVE
        except ValueError:
            self.state = BotState.ACTIVE
        log.info("RiskManager up — state=%s day_open=%.2f week_open=%.2f",
                 self.state.value, self.day_open_equity, self.week_open_equity)

    # ------------------------------------------------------------------
    # tunable params — re-read from the (override-merged) config each cycle
    # ------------------------------------------------------------------
    def _load_params(self):
        raw = getattr(self.cfg, "_raw", {}) or {}
        r = raw.get("risk", {}) or {}
        # Layer 1 — pre-trade
        self.daily_drawdown_limit = float(r.get("daily_drawdown_limit", 0.05))
        self.severe_drawdown_limit = float(r.get("severe_drawdown_limit", 0.10))
        self.max_concurrent_positions = int(r.get("max_concurrent_positions", 3))
        self.max_per_symbol = int(r.get("max_per_symbol", 1))
        self.max_leverage = float(r.get("max_leverage", 5.0))
        self.min_confidence = float(r.get("min_confidence", 0.62))
        self.min_model_agreement = int(r.get("min_model_agreement", 4))
        # cascade bounce track (Phase 8.6) — separate allocation pool
        cb = raw.get("cascade_bounce", {}) or {}
        self.cb_allocation_pct = float(cb.get("allocation_pct", 0.12))
        self.cb_max_hold_minutes = float(cb.get("max_hold_minutes", 20))
        self.max_spread_pct = float(r.get("max_spread_pct", 0.0015))
        # Layer 2 — in-trade
        self.atr_sl_multiplier = float(r.get("atr_sl_multiplier", 1.5))
        self.atr_trail_multiplier = float(r.get("atr_trail_multiplier", 1.0))
        self.trail_activation_r = float(r.get("trail_activation_r", 1.5))
        self.take_profit_r = float(r.get("take_profit_r", 2.0))
        # breakeven profit lock — earlier/tighter than the trailing stop
        self.breakeven_lock_enabled = bool(r.get("breakeven_lock_enabled", True))
        self.breakeven_lock_r = float(r.get("breakeven_lock_r", 0.5))
        self.breakeven_lock_buffer_pct = float(
            r.get("breakeven_lock_buffer_pct", 0.05))
        self.max_loss_per_trade_pct = float(r.get("max_loss_per_trade_pct", 0.02))
        self.emergency_loss_pct = float(r.get("emergency_loss_pct", 0.03))
        self.max_hold_hours_scalp = float(r.get("max_hold_hours_scalp", 4))
        self.max_hold_hours_swing = float(r.get("max_hold_hours_swing", 48))
        # Layer 3 — market kill switches
        self.cascade_detection_enabled = bool(
            r.get("cascade_detection_enabled", True))
        self.cascade_oi_drop_pct = float(r.get("cascade_oi_drop_pct", 0.15))
        self.cascade_price_move_pct = float(r.get("cascade_price_move_pct", 0.03))
        self.cascade_window_minutes = float(r.get("cascade_window_minutes", 5))
        self.cascade_halt_hours = float(r.get("cascade_halt_hours", 2))
        self.extreme_funding_long_halt = float(r.get("extreme_funding_long_halt", 0.001))
        self.extreme_funding_short_halt = float(r.get("extreme_funding_short_halt", -0.001))
        self.flash_crash_candle_pct = float(r.get("flash_crash_candle_pct", 0.05))
        self.weekly_drawdown_limit = float(r.get("weekly_drawdown_limit", 0.10))
        self.cooldown_hours = float(r.get("cooldown_hours", 48))

        # --- Dual-band geometry (2026-06-20) -------------------------------
        # Each band (scalp 5m / trend 1h) has its own entry gate, risk
        # geometry, concurrency limit and position size. Defaults fall back to
        # the legacy global values so a config without scalp_*/trend_* keys
        # behaves like the single band it replaces. band_params() resolves a
        # band name to its dict; an unknown/None band -> legacy globals.
        self.scalp_band_enabled = bool(
            (raw.get("trading", {}) or {}).get("scalp_band_enabled", True))
        self.trend_band_enabled = bool(
            (raw.get("trading", {}) or {}).get("trend_band_enabled", True))
        self.regime_counter_trend_penalty = float(
            r.get("regime_counter_trend_penalty", 0.7))
        self.bands = {
            "scalp": {
                "min_confidence": float(r.get("scalp_min_confidence", 0.40)),
                "min_model_agreement": int(
                    r.get("scalp_min_model_agreement", 2)),
                "atr_sl_multiplier": float(r.get("scalp_atr_sl_multiplier", 1.0)),
                "take_profit_r": float(r.get("scalp_take_profit_r", 1.5)),
                "trail_activation_r": float(
                    r.get("scalp_trail_activation_r", 1.0)),
                "max_hold_hours": float(r.get("scalp_max_hold_hours", 0.5)),
                "breakeven_lock_r": float(r.get("scalp_breakeven_lock_r", 0.4)),
                "max_concurrent_positions": int(
                    r.get("scalp_max_concurrent_positions", 3)),
                "position_size_usd": float(
                    r.get("scalp_position_size_usd", 30)),
                "structural_gates_enabled": bool(
                    r.get("scalp_structural_gates_enabled", True)),
            },
            "trend": {
                "min_confidence": float(r.get("trend_min_confidence", 0.55)),
                "min_model_agreement": int(
                    r.get("trend_min_model_agreement", 3)),
                "atr_sl_multiplier": float(r.get("trend_atr_sl_multiplier", 2.5)),
                "take_profit_r": float(r.get("trend_take_profit_r", 4.0)),
                "trail_activation_r": float(
                    r.get("trend_trail_activation_r", 2.0)),
                "max_hold_hours": float(r.get("trend_max_hold_hours", 48.0)),
                "breakeven_lock_r": float(r.get("trend_breakeven_lock_r", 0.8)),
                "max_concurrent_positions": int(
                    r.get("trend_max_concurrent_positions", 2)),
                "position_size_usd": float(
                    r.get("trend_position_size_usd", 75)),
                "structural_gates_enabled": False,  # trend's own signal IS its gate
            },
        }

    def band_params(self, band: str | None) -> dict:
        """Resolve a band name to its param dict. None/unknown band -> legacy
        global values (so band=None callers keep pre-dual-band behavior)."""
        if band in self.bands:
            return self.bands[band]
        return {
            "min_confidence": self.min_confidence,
            "min_model_agreement": self.min_model_agreement,
            "atr_sl_multiplier": self.atr_sl_multiplier,
            "take_profit_r": self.take_profit_r,
            "trail_activation_r": self.trail_activation_r,
            "max_hold_hours": self.max_hold_hours_scalp,
            "breakeven_lock_r": self.breakeven_lock_r,
            "max_concurrent_positions": self.max_concurrent_positions,
            "position_size_usd": float(
                (getattr(self.cfg, "_raw", {}) or {}).get("trading", {})
                .get("default_usd_size", 50)),
            "structural_gates_enabled": True,
        }

    def refresh_params(self) -> dict:
        """Re-read all tunable guard params from the (already override-merged)
        config. Returns {param: (old, new)} for values that changed, so the
        loop can log an audit line. Called once per cycle by run_bot.py."""
        scalar_keys = (
            "daily_drawdown_limit", "severe_drawdown_limit",
            "max_concurrent_positions", "max_leverage", "min_confidence",
            "min_model_agreement", "max_spread_pct", "atr_sl_multiplier",
            "atr_trail_multiplier", "trail_activation_r", "take_profit_r",
            "breakeven_lock_enabled", "breakeven_lock_r",
            "breakeven_lock_buffer_pct",
            "max_loss_per_trade_pct", "emergency_loss_pct",
            "max_hold_hours_scalp", "cascade_detection_enabled",
            "cascade_oi_drop_pct", "cascade_price_move_pct",
            "cascade_window_minutes", "weekly_drawdown_limit",
            "scalp_band_enabled", "trend_band_enabled",
            "regime_counter_trend_penalty")
        before = {k: getattr(self, k) for k in scalar_keys}
        # flatten per-band params as "scalp.<key>" / "trend.<key>"
        before_bands = {f"{b}.{k}": v
                        for b, d in self.bands.items() for k, v in d.items()}
        self._load_params()
        changed = {k: (before[k], getattr(self, k))
                   for k in before if before[k] != getattr(self, k)}
        after_bands = {f"{b}.{k}": v
                       for b, d in self.bands.items() for k, v in d.items()}
        changed.update({k: (before_bands[k], after_bands[k])
                        for k in before_bands
                        if before_bands.get(k) != after_bands.get(k)})
        return changed

    # ------------------------------------------------------------------
    # state helpers
    # ------------------------------------------------------------------
    def _set_state(self, new: BotState, reason: str):
        if new == self.state:
            return
        log.warning("STATE %s -> %s (%s)", self.state.value, new.value, reason)
        self.state = new
        self.db.set_state("risk_state", new.value)
        self.db.set_state("risk_state_reason", reason)

    def _halt(self, until_ts: float, reason: str):
        self._halted_until = until_ts
        self.db.set_state("risk_halted_until", str(until_ts))
        self._set_state(BotState.HALTED, reason)

    def _cooldown(self, until_ts: float, reason: str):
        self._cooldown_until = until_ts
        self.db.set_state("risk_cooldown_until", str(until_ts))
        self._set_state(BotState.COOLDOWN, reason)

    @staticmethod
    def _next_utc_midnight() -> float:
        now = datetime.now(timezone.utc)
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                microsecond=0)
        return nxt.timestamp()

    # ------------------------------------------------------------------
    # equity / anchors
    # ------------------------------------------------------------------
    def _equity(self) -> float:
        """Current account value, cached 30s. 0.0 if unavailable."""
        now = time.time()
        ts, val = self._equity_cache
        if now - ts < 30 and val > 0:
            return val
        state = with_retry(
            lambda: self.xc.info.user_state(self.xc.account_address),
            "user_state", tries=2)
        if not state:
            return val  # stale value better than nothing
        eq = float(state.get("marginSummary", {}).get("accountValue", 0) or 0)
        self._equity_cache = (now, eq)
        return eq

    def record_day_open_equity(self, equity: float):
        """Set the daily drawdown baseline (called at UTC midnight rollover)."""
        self.day_open_equity = equity
        self._day_anchor = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.db.set_state("risk_day_open_equity", str(equity))
        self.db.set_state("risk_day_anchor", self._day_anchor)
        if self.state == BotState.MANAGING:
            self._set_state(BotState.ACTIVE, "new trading day")
        log.info("day-open equity baseline set: %.2f", equity)

    def _roll_anchors(self, equity: float):
        if equity <= 0:
            return
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        week = datetime.now(timezone.utc).strftime("%G-%V")
        if self._day_anchor != today or self.day_open_equity <= 0:
            self.record_day_open_equity(equity)
        if self._week_anchor != week or self.week_open_equity <= 0:
            self.week_open_equity = equity
            self._week_anchor = week
            self.db.set_state("risk_week_open_equity", str(equity))
            self.db.set_state("risk_week_anchor", week)
            log.info("week-open equity baseline set: %.2f", equity)

    # ------------------------------------------------------------------
    # Layer 3 detectors
    # ------------------------------------------------------------------
    def _cascade_triggered(self, coin: str) -> bool:
        ctx = self.buf.ctx.get(coin) or {}
        oi = float(ctx.get("open_interest") or 0)
        px = self.buf.mid(coin) or float(ctx.get("mark_px") or 0)
        if oi <= 0 or px <= 0:
            return False
        now = time.time()
        hist = self._oi_hist[coin]
        hist.append((now, oi, px))
        cutoff = now - self.cascade_window_minutes * 60
        in_win = [e for e in hist if e[0] >= cutoff]
        if len(in_win) < 2:
            return False
        _, oi0, px0 = in_win[0]
        oi_drop = (oi0 - oi) / oi0 if oi0 > 0 else 0.0
        px_move = abs(px - px0) / px0 if px0 > 0 else 0.0
        if oi_drop > self.cascade_oi_drop_pct and px_move > self.cascade_price_move_pct:
            log.error("CASCADE on %s: OI -%.1f%%, price %.1f%% in %dmin",
                      coin, oi_drop * 100, px_move * 100,
                      self.cascade_window_minutes)
            return True
        return False

    def _update_funding_halts(self, coin: str):
        ctx = self.buf.ctx.get(coin) or {}
        funding = ctx.get("funding")
        if funding is None:
            return
        rate_8h = float(funding) * 8
        if rate_8h > self.extreme_funding_long_halt:
            if coin not in self._long_halt:
                log.warning("extreme +funding on %s (%.5f/8h) — longs halted",
                            coin, rate_8h)
            self._long_halt.add(coin)
        else:
            self._long_halt.discard(coin)
        if rate_8h < self.extreme_funding_short_halt:
            if coin not in self._short_halt:
                log.warning("extreme -funding on %s (%.5f/8h) — shorts halted",
                            coin, rate_8h)
            self._short_halt.add(coin)
        else:
            self._short_halt.discard(coin)

    def _update_flash_pause(self, coin: str):
        candles = self.buf.latest_candles(coin, "1m", 2)
        if not candles:
            return
        cd = candles[-1]
        o, c = float(cd["o"]), float(cd["c"])
        if o <= 0:
            return
        move = abs(c - o) / o
        if move > self.flash_crash_candle_pct:
            until = float(cd["T"]) / 1000 + 2 * 60  # pause for 2 more 1m candles
            if until > self._flash_pause_until.get(coin, 0):
                self._flash_pause_until[coin] = until
                log.warning("flash move %.1f%% on %s — entries paused 2 candles",
                            move * 100, coin)

    # ------------------------------------------------------------------
    # Layer 4
    # ------------------------------------------------------------------
    def heartbeat_ok(self) -> bool:
        try:
            raw = Path(self.cfg.heartbeat_path).read_text().strip()
            age = time.time() - float(raw)
        except Exception:
            return False
        return age <= 3 * self.cfg.heartbeat_interval

    # ------------------------------------------------------------------
    # main guard pass
    # ------------------------------------------------------------------
    def check(self) -> BotState:
        """Run all Layer 1/3/4 guards. Update self.state. Return current state."""
        now = time.time()
        equity = self._equity()
        self._roll_anchors(equity)

        # timed lockouts hold until expiry
        if self.state == BotState.HALTED:
            if now < self._halted_until:
                return self.state
            self._set_state(BotState.ACTIVE, "halt window expired")
        if self.state == BotState.COOLDOWN:
            if now < self._cooldown_until:
                return self.state
            self._set_state(BotState.ACTIVE, "cooldown expired")

        # Layer 4: feed staleness
        if self.buf.seconds_since_msg() > self.cfg.stale_feed_seconds:
            self._set_state(BotState.RECONNECTING, "stale market data feed")
            return self.state
        if self.state == BotState.RECONNECTING:
            self._set_state(BotState.ACTIVE, "feed recovered")

        if not self.heartbeat_ok():
            log.warning("heartbeat file stale/missing at %s",
                        self.cfg.heartbeat_path)

        # Layer 1: account drawdowns
        if equity > 0 and self.day_open_equity > 0:
            dd = 1 - equity / self.day_open_equity
            if dd >= self.severe_drawdown_limit:
                self._close_all(f"severe daily drawdown {dd:.1%}")
                self._halt(self._next_utc_midnight(),
                           f"severe daily drawdown {dd:.1%}")
                return self.state
            if dd >= self.daily_drawdown_limit and self.state == BotState.ACTIVE:
                self._set_state(BotState.MANAGING,
                                f"daily drawdown {dd:.1%} — no new entries")

        # Layer 3: weekly drawdown -> cooldown
        if equity > 0 and self.week_open_equity > 0:
            wdd = 1 - equity / self.week_open_equity
            if wdd >= self.weekly_drawdown_limit:
                self._cooldown(now + self.cooldown_hours * 3600,
                               f"weekly drawdown {wdd:.1%}")
                return self.state

        # Layer 3: per-coin kill switches
        for coin in self.buf.coins:
            if self.cascade_detection_enabled and self._cascade_triggered(coin):
                self._close_all(f"liquidation cascade on {coin}")
                self._halt(now + self.cascade_halt_hours * 3600,
                           f"cascade on {coin}")
                return self.state
            self._update_funding_halts(coin)
            self._update_flash_pause(coin)

        return self.state

    # ------------------------------------------------------------------
    # Layer 1: pre-trade gate
    # ------------------------------------------------------------------
    def _band_open_count(self, positions: list, band: str) -> int:
        """How many currently-open positions belong to `band` (via the
        per-coin tracker). Untracked/adopted coins aren't counted toward a
        band's concurrency limit but still block re-entry via max_per_symbol."""
        n = 0
        for p in positions:
            c = (p.get("position") or {}).get("coin")
            if c and self._pos_track.get(c, {}).get("band") == band:
                n += 1
        return n

    def can_open(self, coin: str, direction: str, confidence: float,
                 model_agreement: int, leverage: float,
                 band: str | None = None) -> tuple[bool, str]:
        """Returns (allowed, reason). Checks all pre-trade guards. With `band`
        set, the confidence/agreement gate and the concurrency limit are the
        band's; coin ownership (max_per_symbol) is enforced across ALL bands so
        a coin is held by at most one band at a time (one-way exchange)."""
        bp = self.band_params(band)
        if self.state != BotState.ACTIVE:
            return False, f"state={self.state.value}"
        if direction not in ("LONG", "SHORT"):
            return False, f"no tradeable direction ({direction})"
        if confidence < bp["min_confidence"] - CONF_GATE_EPS:
            # 3-decimal reason so "0.30 < 0.30" no longer hides whether the value
            # was a genuinely-below 0.298 or float noise just under 0.30.
            return False, (f"confidence {confidence:.3f} < "
                           f"{bp['min_confidence']:.3f}")
        if model_agreement < bp["min_model_agreement"]:
            return False, (f"model agreement {model_agreement} < "
                           f"{bp['min_model_agreement']}")
        if direction == "LONG" and coin in self._long_halt:
            return False, "extreme positive funding — longs halted"
        if direction == "SHORT" and coin in self._short_halt:
            return False, "extreme negative funding — shorts halted"
        if time.time() < self._flash_pause_until.get(coin, 0):
            return False, "flash-crash pause active"

        positions = with_retry(self.xc.positions, "positions") or []
        if band in self.bands:
            band_count = self._band_open_count(positions, band)
            if band_count >= bp["max_concurrent_positions"]:
                return False, (f"{band} max concurrent positions "
                               f"({bp['max_concurrent_positions']}) reached")
        elif len(positions) >= self.max_concurrent_positions:
            return False, (f"max concurrent positions "
                           f"({self.max_concurrent_positions}) reached")
        # coin ownership: one-way exchange nets per coin, so a coin can hold at
        # most one position regardless of band. A second band wanting the same
        # coin is blocked here ("already holding" -> owned by the other band).
        per_sym = sum(1 for p in positions
                      if p.get("position", {}).get("coin") == coin)
        if per_sym >= self.max_per_symbol:
            held_band = self._pos_track.get(coin, {}).get("band")
            owner = f" (held by {held_band} band)" if held_band else ""
            return False, f"already holding {coin}{owner}"

        book = self.buf.books.get(coin)
        if not book or not book.get("bids") or not book.get("asks"):
            return False, "no orderbook"
        bid, ask = book["bids"][0][0], book["asks"][0][0]
        mid = (bid + ask) / 2
        if mid <= 0:
            return False, "bad mid price"
        spread = (ask - bid) / mid
        if spread > self.max_spread_pct:
            return False, f"spread {spread:.4%} > {self.max_spread_pct:.4%}"

        if leverage <= 0:
            return False, "non-positive leverage"
        return True, "OK"

    def clamp_leverage(self, requested: float) -> float:
        """Return min(requested, max_leverage)."""
        return min(requested, self.max_leverage)

    # ------------------------------------------------------------------
    # cascade bounce track (Phase 8.6) — event-driven, separate from the
    # ensemble gate. Deliberately does NOT check spread or flash-pause
    # (a cascade IS a flash move — that's the trade), but still requires
    # ACTIVE state, so Layer 3 halts and drawdown locks veto it.
    # ------------------------------------------------------------------
    def check_cascade_bounce_allocation(
            self, coin: str, min_order_usd: float = 12.0
    ) -> tuple[bool, str, float]:
        """Gate + size a cascade bounce entry. Returns
        (allowed, reason, max_usd) where max_usd = allocation_pct x equity."""
        if self.state != BotState.ACTIVE:
            return False, f"state={self.state.value}", 0.0
        equity = self._equity()
        if equity <= 0:
            return False, "equity unavailable", 0.0
        max_usd = self.cb_allocation_pct * equity
        if max_usd < min_order_usd:
            return False, (f"allocation {max_usd:.2f} below min order "
                           f"{min_order_usd:.2f}"), 0.0
        positions = with_retry(self.xc.positions, "positions") or []
        for p in positions:
            if (p.get("position") or {}).get("coin") == coin and \
                    float(p.get("position", {}).get("szi") or 0) != 0:
                return False, f"already holding {coin}", 0.0
        return True, "OK", max_usd

    def enter_cascade_bounce(self, coin: str):
        """Bounce position open: pause ensemble entries, keep managing."""
        self._set_state(BotState.CASCADE_BOUNCE_ACTIVE,
                        f"cascade bounce open on {coin}")

    def exit_cascade_bounce(self, reason: str = "bounce position closed"):
        if self.state == BotState.CASCADE_BOUNCE_ACTIVE:
            self._set_state(BotState.ACTIVE, reason)

    # ------------------------------------------------------------------
    # Layer 2: stops & targets
    # ------------------------------------------------------------------
    def calc_sl_tp(self, coin: str, entry_px: float, is_long: bool,
                   atr: float, band: str | None = None) -> tuple[float, float]:
        """Returns (stop_loss_px, take_profit_px). SL at atr_sl_multiplier*ATR,
        TP at take_profit_r * the initial risk (R). Uses the band's geometry
        (scalp tight / trend wide) when band is given; legacy globals when not."""
        bp = self.band_params(band)
        r = atr * bp["atr_sl_multiplier"]
        tp_r = bp["take_profit_r"]
        if is_long:
            return entry_px - r, entry_px + tp_r * r
        return entry_px + r, entry_px - tp_r * r

    def register_entry(self, coin: str, entry_px: float, sl: float, tp: float,
                       is_long: bool, hold_hours: float | None = None,
                       band: str | None = None):
        """Seed the in-trade tracker right after an order fills, so SL/TP/
        trailing/expiry are enforced from the first tick. `band` selects the
        max-hold (and, in check_open_positions, the trail/breakeven geometry)."""
        bp = self.band_params(band)
        self._pos_track[coin] = {
            "entry_px": entry_px,
            "sl": sl,
            "tp": tp,
            "is_long": is_long,
            "band": band,
            "opened_ts": time.time(),
            "r_px": abs(entry_px - sl),
            "trailing": False,
            "breakeven_locked": False,
            "max_hold_s": (hold_hours or bp["max_hold_hours"]) * 3600,
        }

    def set_manual_sltp(self, coin: str, sl: float, tp: float) -> tuple[bool, str]:
        """Override an open position's SL/TP from the dashboard. Validates the
        levels sit on the correct sides of entry, then updates the in-trade
        tracker (and r_px, so trailing/breakeven R-math stays consistent).
        Trailing may still tighten the SL further in the favorable direction;
        TP is otherwise left at the chosen level. Returns (ok, message)."""
        tr = self._pos_track.get(coin)
        if not tr:
            return False, f"no open tracked position for {coin}"
        entry = tr["entry_px"]
        if not (sl > 0 and tp > 0):
            return False, "sl/tp must be positive"
        if tr["is_long"]:
            if not (tp > entry > sl):
                return False, (f"LONG needs tp>entry>sl "
                               f"(entry={entry:g}, got sl={sl:g} tp={tp:g})")
        else:
            if not (tp < entry < sl):
                return False, (f"SHORT needs tp<entry<sl "
                               f"(entry={entry:g}, got sl={sl:g} tp={tp:g})")
        tr["sl"], tr["tp"], tr["r_px"] = sl, tp, abs(entry - sl)
        log.warning("MANUAL SL/TP %s: sl=%.6f tp=%.6f (entry=%.6f)",
                    coin, sl, tp, entry)
        return True, "ok"

    def check_open_positions(self, positions: list) -> list[dict]:
        """Check all open positions against in-trade guards.
        Returns list of actions: [{"coin":str,"action":"CLOSE"|"UPDATE_SL",
        "new_sl":float,"reason":str}]. UPDATE_SL is already applied to the
        internal tracker; callers act on CLOSE."""
        actions: list[dict] = []
        equity = self._equity()
        now = time.time()
        seen: set[str] = set()

        for p in positions:
            pos = p.get("position") or {}
            coin = pos.get("coin")
            if not coin:
                continue
            szi = float(pos.get("szi") or 0)
            if szi == 0:
                continue
            seen.add(coin)

            # close-pending guard: a close order was already accepted for this
            # coin. Skip re-evaluating SL/TP until the position state refreshes
            # (the position should disappear from the next positions() call). If
            # it's still here after the timeout, treat it as stuck and re-check
            # so a genuinely failed close eventually retries.
            pending_ts = self._close_pending.get(coin)
            if pending_ts is not None:
                elapsed = now - pending_ts
                if elapsed < CLOSE_PENDING_TIMEOUT_S:
                    log.debug("skipping %s — close pending (%.0fs ago)",
                              coin, elapsed)
                    continue
                log.warning("close pending timeout for %s (%.0fs) — position "
                            "still open, re-evaluating", coin, elapsed)
                del self._close_pending[coin]

            is_long = szi > 0
            entry = float(pos.get("entryPx") or 0)
            upnl = float(pos.get("unrealizedPnl") or 0)

            tr = self._pos_track.get(coin)
            if tr is None or tr["is_long"] != is_long:
                # position not opened through this process — adopt it
                atr = atr_from_candles(self.buf.latest_candles(coin, "1m", 60))
                if not atr or entry <= 0:
                    continue
                sl, tp = self.calc_sl_tp(coin, entry, is_long, atr)
                self.register_entry(coin, entry, sl, tp, is_long)
                tr = self._pos_track[coin]
                log.info("adopted untracked %s position: entry=%.4f sl=%.4f "
                         "tp=%.4f", coin, entry, sl, tp)

            ctx = self.buf.ctx.get(coin) or {}
            mid = self.buf.mid(coin) or float(ctx.get("mark_px") or 0) or entry
            band = tr.get("band")
            bp = self.band_params(band)

            # hard $ loss floors (account-relative)
            if equity > 0 and upnl <= -self.emergency_loss_pct * equity:
                actions.append({"coin": coin, "band": band, "action": "CLOSE",
                                "reason": f"EMERGENCY loss {upnl:.2f} "
                                          f"<= -{self.emergency_loss_pct:.0%} equity"})
                continue
            if equity > 0 and upnl <= -self.max_loss_per_trade_pct * equity:
                actions.append({"coin": coin, "band": band, "action": "CLOSE",
                                "reason": f"max per-trade loss {upnl:.2f}"})
                continue

            # stop loss / take profit
            hit_sl = mid <= tr["sl"] if is_long else mid >= tr["sl"]
            hit_tp = mid >= tr["tp"] if is_long else mid <= tr["tp"]
            if hit_sl:
                label = "trailing stop" if tr["trailing"] else "stop loss"
                actions.append({"coin": coin, "band": band, "action": "CLOSE",
                                "reason": f"{label} @ {tr['sl']:.4f}"})
                continue
            if hit_tp:
                actions.append({"coin": coin, "band": band, "action": "CLOSE",
                                "reason": f"take profit @ {tr['tp']:.4f}"})
                continue

            # time expiry
            if now - tr["opened_ts"] > tr["max_hold_s"]:
                actions.append({"coin": coin, "band": band, "action": "CLOSE",
                                "reason": "max hold time expired"})
                continue

            r_px = tr["r_px"]
            if r_px > 0:
                unreal_r = ((mid - tr["entry_px"]) if is_long
                            else (tr["entry_px"] - mid)) / r_px

                # breakeven profit lock: once the position reaches
                # breakeven_lock_r of unrealized profit, snap the SL to entry +
                # a small fee-covering buffer so a winning trade can't give back
                # into a loss. Fires once (breakeven_locked latch), sits BELOW
                # the trailing-stop activation, and only ever tightens the stop.
                if (self.breakeven_lock_enabled
                        and not tr["breakeven_locked"]
                        and unreal_r >= bp["breakeven_lock_r"]):
                    buf_px = tr["entry_px"] * (
                        self.breakeven_lock_buffer_pct / 100.0)
                    be_sl = (tr["entry_px"] + buf_px if is_long
                             else tr["entry_px"] - buf_px)
                    improved = ((be_sl > tr["sl"]) if is_long
                                else (be_sl < tr["sl"]))
                    if improved:
                        tr["sl"] = be_sl
                        tr["breakeven_locked"] = True
                        # recalc TP to hold the original R:R measured from the
                        # NEW (tight) SL. Otherwise SL snaps to entry+buffer
                        # while TP stays at the entry-time target, blowing R:R
                        # out (e.g. 1:13.7) and leaving an unreachable moonshot.
                        # Only ever tighten TP — never push it further away.
                        new_sl_dist = abs(tr["entry_px"] - be_sl)
                        recalc_tp = (
                            tr["entry_px"] + new_sl_dist * bp["take_profit_r"]
                            if is_long else
                            tr["entry_px"] - new_sl_dist * bp["take_profit_r"])
                        tighter = ((recalc_tp < tr["tp"]) if is_long
                                   else (recalc_tp > tr["tp"]))
                        if tighter:
                            tr["tp"] = recalc_tp
                        actions.append({"coin": coin, "band": band,
                                        "action": "BREAKEVEN",
                                        "new_sl": be_sl,
                                        "new_tp": tr["tp"],
                                        "reason": f"breakeven lock @ "
                                                  f"{unreal_r:.2f}R "
                                                  f"(SL -> {be_sl:.4f}, "
                                                  f"TP -> {tr['tp']:.4f})"})

                # trailing stop: activate at >= trail_activation_r unrealized,
                # trail at atr_trail_multiplier * ATR
                if unreal_r >= bp["trail_activation_r"]:
                    atr = (atr_from_candles(
                        self.buf.latest_candles(coin, "1m", 60))
                        or r_px / bp["atr_sl_multiplier"])
                    new_sl = (mid - atr * self.atr_trail_multiplier if is_long
                              else mid + atr * self.atr_trail_multiplier)
                    improved = (new_sl > tr["sl"]) if is_long else (new_sl < tr["sl"])
                    if improved:
                        tr["sl"] = new_sl
                        tr["trailing"] = True
                        actions.append({"coin": coin, "band": band,
                                        "action": "UPDATE_SL",
                                        "new_sl": new_sl,
                                        "reason": f"trailing @ {unreal_r:.2f}R"})

        # drop trackers for positions that no longer exist
        for coin in list(self._pos_track):
            if coin not in seen:
                del self._pos_track[coin]
        # a close-pending coin that's no longer in the positions list is
        # confirmed closed — clear the flag so a future re-entry can be managed.
        for coin in list(self._close_pending):
            if coin not in seen:
                log.info("position confirmed closed for %s — clearing "
                         "close-pending", coin)
                del self._close_pending[coin]
        return actions

    def mark_close_pending(self, coin: str):
        """Record that a close order was accepted for coin. The in-trade guard
        then suppresses further close attempts on it until the position state
        refreshes (or CLOSE_PENDING_TIMEOUT_S expires). Primary defense against
        duplicate close orders firing every cycle on a stale position."""
        self._close_pending[coin] = time.time()
        log.info("CLOSE_PENDING set for %s", coin)

    def clear_close_pending(self, coin: str):
        """Drop the close-pending flag (e.g. once the close is confirmed)."""
        self._close_pending.pop(coin, None)

    # ------------------------------------------------------------------
    # manual controls (dashboard)
    # ------------------------------------------------------------------
    def manual_halt(self):
        """Emergency stop: close everything, hold HALTED until manual resume."""
        self._close_all("manual halt from dashboard")
        self._halt(time.time() + 365 * 86_400, "manual halt from dashboard")

    def manual_pause(self, reason: str = "manual pause from dashboard"):
        """Soft pause: stop opening new entries (MANAGING), keep guards and
        open-position management running. Reversible with manual_resume."""
        if self.state == BotState.ACTIVE:
            self._set_state(BotState.MANAGING, reason)

    def manual_close_all(self, reason: str = "manual close-all from dashboard"):
        """Close every open position now and drop to MANAGING (no new entries
        until resumed). Lighter than manual_halt — no long timed lockout."""
        self._close_all(reason)
        if self.state in (BotState.ACTIVE, BotState.CASCADE_BOUNCE_ACTIVE):
            self._set_state(BotState.MANAGING, reason)

    def manual_resume(self):
        if self.state in (BotState.HALTED, BotState.COOLDOWN):
            self._halted_until = 0.0
            self._cooldown_until = 0.0
            self.db.set_state("risk_halted_until", "0")
            self.db.set_state("risk_cooldown_until", "0")
            self._set_state(BotState.ACTIVE, "manual resume from dashboard")
        elif self.state == BotState.MANAGING:
            self._set_state(BotState.ACTIVE, "manual resume from dashboard")

    # ------------------------------------------------------------------
    # emergency close
    # ------------------------------------------------------------------
    def _close_all(self, reason: str):
        log.error("CLOSING ALL POSITIONS: %s", reason)
        with_retry(self.xc.cancel_all, "cancel_all")
        positions = with_retry(self.xc.positions, "positions") or []
        for p in positions:
            pos = p.get("position") or {}
            coin = pos.get("coin")
            if not coin:
                continue
            side = "LONG" if float(pos.get("szi") or 0) > 0 else "SHORT"
            band = self._pos_track.get(coin, {}).get("band")
            res = with_retry(lambda c=coin: self.xc.market_close(c),
                             f"market_close({coin})")
            if res:
                self.mark_close_pending(coin)
            self.db.log_trade(coin, side, "CLOSE",
                              size=abs(float(pos.get("szi") or 0)),
                              status="ok" if res else "error",
                              band=band, note=f"risk: {reason}")
        self._pos_track.clear()
