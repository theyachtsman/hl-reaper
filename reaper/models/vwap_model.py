"""VWAP model: session VWAP as dynamic support/resistance with ±1σ bands."""
import math
from datetime import datetime, timezone

from reaper.logger import get_logger
from reaper.models import BaseModel, LONG, SHORT, Ticket, candles_to_df

log = get_logger("model.vwap")


class VWAPModel(BaseModel):
    name = "VWAPModel"

    def compute(self, coin: str, buf, interval: str | None = None) -> Ticket:
        try:
            df = candles_to_df(buf.latest_candles(coin, interval or "1m", 1440))
            if df.empty:
                return self.flat(reason="no_candles")
            midnight_ms = int(datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
            day = df[df["t"] >= midnight_ms]
            if len(day) < 10:
                # young session: fall back to the last 120 candles
                day = df.tail(120)
            if len(day) < 10:
                return self.flat(reason="insufficient_candles", n=len(day))

            tp = (day["h"] + day["l"] + day["c"]) / 3.0
            vol = day["v"]
            vol_sum = float(vol.sum())
            if vol_sum <= 0:
                return self.flat(reason="zero_volume")
            vwap = float((tp * vol).sum() / vol_sum)
            var = float((vol * (tp - vwap) ** 2).sum() / vol_sum)
            std = math.sqrt(max(var, 0.0))
            px = float(day["c"].iloc[-1])
            prev = float(day["c"].iloc[-6]) if len(day) >= 6 else px

            meta = {"vwap": round(vwap, 4), "std": round(std, 6),
                    "px": px, "session_candles": len(day)}

            # band touches are the stronger mean-reversion signal
            if std > 0 and px <= vwap - std:
                return Ticket(self.name, LONG, 0.65, {**meta, "band": "-1std"})
            if std > 0 and px >= vwap + std:
                return Ticket(self.name, SHORT, 0.65, {**meta, "band": "+1std"})

            # at equilibrium -> no edge
            if abs(px - vwap) / vwap <= 0.001:
                return self.flat(**meta, band="equilibrium")

            rising = px > prev
            falling = px < prev
            if px > vwap * 1.001 and rising:
                return Ticket(self.name, LONG, 0.55,
                              {**meta, "band": "above_rising"})
            if px < vwap * 0.999 and falling:
                return Ticket(self.name, SHORT, 0.55,
                              {**meta, "band": "below_falling"})
            return self.flat(**meta, band="no_alignment")
        except Exception as e:
            log.warning("compute failed for %s: %s", coin, e)
            return self.flat(error=str(e))
