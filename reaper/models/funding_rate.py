"""Funding rate model: contrarian signal from current rate + 24h average
and 3h trend out of the funding_history table."""
import time

from reaper.logger import get_logger
from reaper.models import BaseModel, FLAT, LONG, SHORT, Ticket

log = get_logger("model.funding")

H24_MS = 24 * 3600 * 1000
H3_MS = 3 * 3600 * 1000

# Funding -> direction mapping (2026-06-20 rework). Funding crowding is a
# CONTINUOUS signal, so the direction/confidence scale smoothly with magnitude
# instead of the old binary rule (which mapped the entire mild-positive band to
# LONG and only ever voted SHORT past the extreme cliff — leaving funding a
# near-permanent LONG voter under normal conditions; see the SHORT-scarcity
# diagnostic). Positive funding = longs paying shorts = longs crowded -> SHORT
# lean. Negative funding = shorts crowded -> LONG lean. Confidence ramps from
# 0.30 at the neutral-band edge to 0.90 at (and beyond) the extreme threshold,
# so high-confidence SHORT (the funding hard-block trigger) still only happens
# near the extreme — a smooth ramp, not a cliff.
FUNDING_NEUTRAL_BAND = 0.0001    # |rate_8h| within this -> no signal (noise)
FUNDING_EXTREME = 0.001          # magnitude cap / top of the confidence ramp
FUNDING_CONF_FLOOR = 0.30        # confidence at the neutral-band edge
FUNDING_CONF_SPAN = 0.60         # added across the ramp -> 0.90 at the extreme


def funding_direction(rate_8h: float) -> tuple[str, float, str]:
    """Pure, continuous funding -> (direction, confidence, zone) mapping.

    Monotonic in |rate_8h| on both sides of zero. FLAT inside the neutral band.
    `zone` is "extreme_*" once magnitude saturates at FUNDING_EXTREME (the old
    threshold), so the avg/trend confirmation boosts and the funding hard-block
    keep keying off genuinely crowded conditions."""
    if abs(rate_8h) <= FUNDING_NEUTRAL_BAND:
        return FLAT, 0.0, "neutral"
    span = FUNDING_EXTREME - FUNDING_NEUTRAL_BAND
    magnitude = min(1.0, (abs(rate_8h) - FUNDING_NEUTRAL_BAND) / span)
    confidence = FUNDING_CONF_FLOOR + magnitude * FUNDING_CONF_SPAN
    if rate_8h > 0:
        # longs crowded -> contrarian SHORT
        return SHORT, confidence, ("extreme_positive" if magnitude >= 1.0
                                   else "positive_crowded")
    # shorts crowded -> contrarian LONG
    return LONG, confidence, ("extreme_negative" if magnitude >= 1.0
                              else "negative_crowded")


def funding_direction_binary(rate_8h: float) -> tuple[str, float, str]:
    """Original (pre-2026-06-20) binary zone rule, kept as the default fallback
    behind the funding_smooth_mapping_enabled flag. Mild-positive funding maps
    to LONG ("uptrend confirmation"); SHORT only past the 0.001 extreme. Returns
    the same (direction, confidence, zone) tuple as funding_direction() so the
    avg/trend boosts and meta downstream are identical regardless of mapping."""
    if rate_8h >= 0.001:
        # extreme positive funding: crowded longs -> contrarian short
        return SHORT, 0.80 + min(0.12, (rate_8h - 0.001) * 100), "extreme_positive"
    if rate_8h >= 0.0001:
        # mildly positive: healthy uptrend confirmation
        return LONG, 0.55, "positive_confirm"
    if rate_8h <= -0.0005:
        # strongly negative: crowded shorts -> contrarian long
        return LONG, 0.75 + min(0.12, (abs(rate_8h) - 0.0005) * 100), "extreme_negative"
    if rate_8h < -0.00005:
        return LONG, 0.45, "negative_lean"
    return FLAT, 0.0, "neutral"


class FundingRateModel(BaseModel):
    name = "FundingRateModel"

    def __init__(self, db, smooth_mapping: bool = False):
        self.db = db
        # False = original binary mapping (funding_direction_binary), the safe
        # default. True = smoothed continuous mapping. Hot-reloaded each loop by
        # run_bot from risk.funding_smooth_mapping_enabled.
        self.smooth_mapping = smooth_mapping

    def compute(self, coin: str, buf, interval: str | None = None) -> Ticket:
        try:
            ctx = buf.ctx.get(coin) or {}
            funding = ctx.get("funding")
            if funding is None:
                return self.flat(reason="no_funding_ctx")
            rate_8h = float(funding) * 8  # ctx rate is hourly

            now_ms = int(time.time() * 1000)
            rows = self.db.funding_window(coin, now_ms - H24_MS) or []
            avg_8h = (sum(r for _, r in rows) / len(rows) * 8) if rows else None
            recent = [r for ts, r in rows if ts >= now_ms - H3_MS]
            prior = [r for ts, r in rows if now_ms - 2 * H3_MS <= ts < now_ms - H3_MS]
            trend = ((sum(recent) / len(recent)) - (sum(prior) / len(prior))
                     if recent and prior else 0.0)

            meta = {"rate_8h": round(rate_8h, 6),
                    "avg_8h": round(avg_8h, 6) if avg_8h is not None else None,
                    "trend_3h": round(trend * 8, 7), "n_hist": len(rows)}

            # funding -> direction/confidence. Mapping selected by the hot-reload
            # flag: smoothed continuous (new) vs original binary zones (fallback).
            mapping = (funding_direction if self.smooth_mapping
                       else funding_direction_binary)
            meta["mapping"] = "smooth" if self.smooth_mapping else "binary"
            direction, confidence, zone = mapping(rate_8h)
            if direction == FLAT:
                return self.flat(zone="neutral", **meta)

            # 24h average confirms the extreme -> boost
            if avg_8h is not None and zone in ("extreme_positive",
                                               "extreme_negative"):
                if (zone == "extreme_positive" and avg_8h >= 0.0005) or \
                   (zone == "extreme_negative" and avg_8h <= -0.0003):
                    confidence += 0.05
                    meta["avg_boost"] = True
            # 3h trend accelerating toward the extreme -> boost
            if (zone == "extreme_positive" and trend > 0) or \
               (zone == "extreme_negative" and trend < 0):
                confidence += 0.03
                meta["trend_boost"] = True

            meta["zone"] = zone
            return Ticket(self.name, direction, min(0.95, confidence), meta)
        except Exception as e:
            log.warning("compute failed for %s: %s", coin, e)
            return self.flat(error=str(e))
