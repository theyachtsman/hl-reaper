"""Liquidation heatmap model: estimates leveraged-cluster liquidation zones
from open interest + funding skew, and fades price approaching a hunt zone."""
from collections import deque

from reaper.logger import get_logger
from reaper.models import BaseModel, LONG, SHORT, Ticket

log = get_logger("model.liqmap")


class LiquidationHeatmapModel(BaseModel):
    name = "LiquidationHeatmapModel"

    FUNDING_SKEW_8H = 0.0002   # |rate_8h| below this = neutral market
    ZONE_OFFSET = 0.075        # liq clusters estimated 7.5% from the anchor
    ZONE_PROXIMITY = 0.03      # signal when price within 3% of the zone

    def __init__(self):
        # rolling OI per coin to judge whether OI is elevated
        self._oi_hist: dict[str, deque] = {}

    def compute(self, coin: str, buf, interval: str | None = None) -> Ticket:
        try:
            ctx = buf.ctx.get(coin) or {}
            funding = ctx.get("funding")
            oi = float(ctx.get("open_interest") or 0)
            mark = float(ctx.get("mark_px") or 0) or (buf.mid(coin) or 0)
            if funding is None or oi <= 0 or mark <= 0:
                return self.flat(reason="missing_ctx")
            rate_8h = float(funding) * 8

            hist = self._oi_hist.setdefault(coin, deque(maxlen=240))
            hist.append(oi)
            oi_avg = sum(hist) / len(hist)
            oi_elevated = len(hist) < 5 or oi >= oi_avg * 0.95

            meta = {"rate_8h": round(rate_8h, 6), "oi": oi,
                    "oi_avg": round(oi_avg, 2), "mark": mark}

            if abs(rate_8h) < self.FUNDING_SKEW_8H or not oi_elevated:
                return self.flat(reason="neutral_market", **meta)

            candles = buf.latest_candles(coin, "1m", 240)
            if len(candles) < 30:
                return self.flat(reason="insufficient_candles", **meta)
            highs = max(float(x["h"]) for x in candles)
            lows = min(float(x["l"]) for x in candles)
            conf = min(0.65, 0.50 + (abs(rate_8h) - self.FUNDING_SKEW_8H) * 150)

            if rate_8h > 0:
                # long-leveraged market: long liqs cluster below recent highs
                zone = highs * (1 - self.ZONE_OFFSET)
                dist = (mark - zone) / mark
                meta.update({"bias": "long_levered", "liq_zone": round(zone, 4),
                             "dist": round(dist, 4)})
                if 0 <= dist <= self.ZONE_PROXIMITY:
                    # price drifting into the long-liq pool -> expect a hunt down
                    return Ticket(self.name, SHORT, conf, meta)
            else:
                # short-leveraged market: short liqs cluster above recent lows
                zone = lows * (1 + self.ZONE_OFFSET)
                dist = (zone - mark) / mark
                meta.update({"bias": "short_levered", "liq_zone": round(zone, 4),
                             "dist": round(dist, 4)})
                if 0 <= dist <= self.ZONE_PROXIMITY:
                    return Ticket(self.name, LONG, conf, meta)
            return self.flat(reason="no_zone_proximity", **meta)
        except Exception as e:
            log.warning("compute failed for %s: %s", coin, e)
            return self.flat(error=str(e))
