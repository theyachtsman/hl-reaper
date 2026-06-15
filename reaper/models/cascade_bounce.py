"""Cascade bounce strategy (Phase 8.6) — event-driven, SEPARATE track from
the 8-model ensemble. Not a BaseModel/Ticket voter: it emits at most one
entry signal per cascade episode and stays silent otherwise.

Concept: a liquidation cascade overshoots (forced market orders eat the book
past fair value), then snaps back once the forced flow exhausts. Detect the
cascade, wait for the first sign of stabilization, enter AGAINST the cascade
direction, exit fast (risk side: 20min max hold + tight pct stops, enforced
by RiskManager via register_entry).

Per-coin state machine:
  IDLE -> CASCADING (move+volume signature seen; track the extreme)
       -> signal fired after `stabilization_bars` 1m bars without a new
          extreme (returns the entry dict once) -> COOLDOWN (no re-trigger
          for `retrigger_cooldown_minutes`) -> IDLE
  CASCADING expires back to IDLE if no stabilization within
  `cascade_stale_minutes` (still falling — never catch the knife).

Confirmation (optional, confidence boost only): real backstop liquidation
events from data/liquidations.db (LiquidationVelocityModel feed) and an OI
contraction in buf.ctx history.
"""
import time

from reaper.logger import get_logger

log = get_logger("model.cbounce")

IDLE, CASCADING, COOLDOWN = "IDLE", "CASCADING", "COOLDOWN"


class CascadeBounceModel:

    def __init__(self, cfg: dict | None = None):
        c = cfg or {}
        self.min_move_pct = float(c.get("min_cascade_move_pct", 0.015))
        self.window_bars = int(c.get("cascade_window_minutes", 5))
        self.min_volume_mult = float(c.get("min_volume_mult", 3.0))
        self.stabilization_bars = int(c.get("stabilization_bars", 2))
        self.cascade_stale_minutes = float(c.get("cascade_stale_minutes", 15))
        self.cooldown_minutes = float(c.get("retrigger_cooldown_minutes", 30))
        self.base_confidence = float(c.get("base_confidence", 0.60))
        # per-coin episode state
        self._st: dict[str, dict] = {}

    def _state(self, coin: str) -> dict:
        return self._st.setdefault(coin, {"phase": IDLE})

    # ------------------------------------------------------------------
    def compute(self, coin: str, buf, liq_conn=None) -> dict | None:
        """Returns an entry signal dict exactly once per cascade episode:
        {side, confidence, cascade_move_pct, extreme_px, entry_ref,
         liq_confirmed, oi_confirmed} — or None. Never raises."""
        try:
            return self._compute(coin, buf, liq_conn)
        except Exception as e:
            log.warning("compute failed for %s: %s", coin, e)
            return None

    def _compute(self, coin: str, buf, liq_conn) -> dict | None:
        st = self._state(coin)
        now = time.time()

        if st["phase"] == COOLDOWN:
            if now >= st["until"]:
                self._st[coin] = {"phase": IDLE}
            return None

        candles = buf.latest_candles(coin, "1m", 75)
        if len(candles) < self.window_bars + 30:
            return None
        closes = [float(x["c"]) for x in candles]
        lows = [float(x["l"]) for x in candles]
        highs = [float(x["h"]) for x in candles]
        vols = [float(x["v"]) for x in candles]
        last_t = int(candles[-1]["t"])

        w = self.window_bars
        move = closes[-1] / closes[-w - 1] - 1
        vol_recent = sum(vols[-w:]) / w
        baseline = vols[:-w]
        vol_base = sum(baseline) / len(baseline)

        if st["phase"] == IDLE:
            if abs(move) < self.min_move_pct or vol_base <= 0 \
                    or vol_recent < self.min_volume_mult * vol_base:
                return None
            down = move < 0
            ctx = buf.ctx.get(coin) or {}
            # stamp the extreme with the bar that actually made it — if the
            # tape already turned, stabilization is partially elapsed
            n = len(candles)
            if down:
                ext_i = min(range(n - w, n), key=lambda i: lows[i])
            else:
                ext_i = max(range(n - w, n), key=lambda i: highs[i])
            st.update({
                "phase": CASCADING,
                "down": down,
                "started": now,
                "extreme": lows[ext_i] if down else highs[ext_i],
                "extreme_bar_t": int(candles[ext_i]["t"]),
                "move_pct": move,
                "oi_at_detect": float(ctx.get("open_interest") or 0),
            })
            log.warning("CASCADE DETECTED %s: %.2f%% in %dm, vol %.1fx — "
                        "watching for stabilization", coin, move * 100, w,
                        vol_recent / vol_base)
            return None

        # phase == CASCADING
        st["move_pct"] = (move if abs(move) > abs(st["move_pct"])
                          else st["move_pct"])
        if now - st["started"] > self.cascade_stale_minutes * 60:
            log.info("cascade on %s went stale without stabilizing — reset",
                     coin)
            self._st[coin] = {"phase": IDLE}
            return None

        # still making new extremes? push the marker forward
        bar_low, bar_high = lows[-1], highs[-1]
        if st["down"] and bar_low < st["extreme"]:
            st["extreme"], st["extreme_bar_t"] = bar_low, last_t
            return None
        if not st["down"] and bar_high > st["extreme"]:
            st["extreme"], st["extreme_bar_t"] = bar_high, last_t
            return None

        bars_since_extreme = (last_t - st["extreme_bar_t"]) // 60_000
        if bars_since_extreme < self.stabilization_bars:
            return None

        # stabilized — fire the bounce signal (once) and enter cooldown
        side = "LONG" if st["down"] else "SHORT"
        liq_confirmed = self._liq_confirms(coin, liq_conn, st["started"])
        oi_confirmed = self._oi_confirms(coin, buf, st.get("oi_at_detect", 0))
        confidence = min(0.92, self.base_confidence
                         + (0.15 if liq_confirmed else 0)
                         + (0.10 if oi_confirmed else 0))
        sig = {
            "side": side,
            "confidence": round(confidence, 2),
            "cascade_move_pct": round(st["move_pct"], 5),
            "extreme_px": st["extreme"],
            "entry_ref": closes[-1],
            "liq_confirmed": liq_confirmed,
            "oi_confirmed": oi_confirmed,
        }
        self._st[coin] = {"phase": COOLDOWN,
                          "until": now + self.cooldown_minutes * 60}
        log.warning("CASCADE BOUNCE SIGNAL %s %s conf=%.2f move=%.2f%% "
                    "extreme=%.6g liq=%s oi=%s", side, coin, confidence,
                    st["move_pct"] * 100, st["extreme"], liq_confirmed,
                    oi_confirmed)
        return sig

    # ------------------------------------------------------------------
    def _liq_confirms(self, coin: str, liq_conn, since_s: float) -> bool:
        """Real backstop liquidation events recorded during the episode."""
        if liq_conn is None:
            return False
        try:
            from reaper.data.liquidation_store import events_window
            return len(events_window(liq_conn, coin,
                                     int(since_s * 1000))) > 0
        except Exception:
            return False

    @staticmethod
    def _oi_confirms(coin: str, buf, oi_at_detect: float) -> bool:
        """Leverage actually flushed: OI now >= 1% below where it stood when
        the cascade was first detected (ctx polls every ~60s, so over a
        multi-minute episode this sees real contraction)."""
        if oi_at_detect <= 0:
            return False
        oi_now = float((buf.ctx.get(coin) or {}).get("open_interest") or 0)
        return oi_now > 0 and oi_now <= oi_at_detect * 0.99
