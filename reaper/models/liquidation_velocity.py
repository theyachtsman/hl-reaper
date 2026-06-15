"""Liquidation velocity / acceleration tracker (Phase 8.6 research).

Detects a self-feeding cascade via acceleration of liquidation volume —
the "$5M -> $15M -> $50M -> $120M per minute" signature. This is the
trigger signal once price is near a cluster (CascadeScoreModel says WHERE,
this says it's HAPPENING NOW).

Research module — not yet integrated into live ensemble.

DATA REQUIREMENTS: reads the liquidation_events table fed by
scripts/run_liquidation_poller.py. The free WS source only captures
BACKSTOP liquidations, which are sparse outside genuine cascades — that is
acceptable here (this model only needs to fire DURING cascades), but it
means: (a) is_cascading will essentially never be true in calm markets,
(b) thresholds below are conservative first guesses that cannot be tuned
until the poller has lived through at least one real cascade. Re-calibrate
MIN_CASCADE_USD_PER_MIN per coin once events accumulate.
"""
import time

from reaper.logger import get_logger

log = get_logger("model.liqvel")


class LiquidationVelocityModel:
    """Detects self-feeding cascade via acceleration of liquidation volume.
    Requires liquidation_events table with real-time data.
    Research module — not yet integrated into live ensemble."""

    # conservative defaults pending real-event calibration (see module doc)
    MIN_CASCADE_USD_PER_MIN = 1_000_000   # latest-minute floor to even consider
    ACCEL_RATIO = 2.0                     # latest minute >= 2x previous minute
    MIN_EVENTS = 3                        # avoid triggering on one stray fill

    def compute_velocity(self, coin: str, db,
                         window_minutes: int = 5) -> dict:
        """db: sqlite3.Connection to the liquidation store
        (reaper.data.liquidation_store.connect()).

        Returns:
        {
          "liq_volume_per_minute": list[float],  # oldest..newest, USD
          "acceleration": float,   # 2nd derivative, USD/min^2
          "is_cascading": bool,
          "dominant_side": str,    # LONG | SHORT | NONE
        }
        """
        try:
            from reaper.data.liquidation_store import events_window
            now_ms = int(time.time() * 1000)
            since = now_ms - window_minutes * 60_000
            events = events_window(db, coin, since)
        except Exception as e:
            log.warning("compute_velocity failed for %s: %s", coin, e)
            events = []

        per_min = [0.0] * window_minutes
        long_usd = short_usd = 0.0
        for ts, side, size_usd, _price, _src in events:
            idx = min(window_minutes - 1,
                      int((ts - since) / 60_000))
            usd = float(size_usd or 0)
            per_min[idx] += usd
            if side == "LONG":
                long_usd += usd
            elif side == "SHORT":
                short_usd += usd

        # 2nd derivative over the last three minute-buckets
        accel = (per_min[-1] - 2 * per_min[-2] + per_min[-3]
                 if window_minutes >= 3 else 0.0)
        speeding_up = (per_min[-1] >= self.MIN_CASCADE_USD_PER_MIN
                       and per_min[-1] >= self.ACCEL_RATIO
                       * max(1.0, per_min[-2])
                       and accel > 0)
        dominant = ("LONG" if long_usd > short_usd else
                    "SHORT" if short_usd > long_usd else "NONE")
        return {
            "liq_volume_per_minute": [round(v, 2) for v in per_min],
            "acceleration": round(accel, 2),
            "is_cascading": bool(speeding_up and len(events)
                                 >= self.MIN_EVENTS),
            "dominant_side": dominant,
        }
