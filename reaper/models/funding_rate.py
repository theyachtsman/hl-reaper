"""Funding rate model: contrarian signal from current rate + 24h average
and 3h trend out of the funding_history table."""
import time

from reaper.logger import get_logger
from reaper.models import BaseModel, LONG, SHORT, Ticket

log = get_logger("model.funding")

H24_MS = 24 * 3600 * 1000
H3_MS = 3 * 3600 * 1000


class FundingRateModel(BaseModel):
    name = "FundingRateModel"

    def __init__(self, db):
        self.db = db

    def compute(self, coin: str, buf) -> Ticket:
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

            direction, confidence, zone = None, 0.0, None
            if rate_8h >= 0.001:
                # extreme positive funding: crowded longs -> contrarian short
                direction = SHORT
                confidence = 0.80 + min(0.12, (rate_8h - 0.001) * 100)
                zone = "extreme_positive"
            elif rate_8h >= 0.0001:
                # mildly positive: healthy uptrend confirmation
                direction = LONG
                confidence = 0.55
                zone = "positive_confirm"
            elif rate_8h <= -0.0005:
                # strongly negative: crowded shorts -> contrarian long
                direction = LONG
                confidence = 0.75 + min(0.12, (abs(rate_8h) - 0.0005) * 100)
                zone = "extreme_negative"
            elif rate_8h < -0.00005:
                direction = LONG
                confidence = 0.45
                zone = "negative_lean"
            else:
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
