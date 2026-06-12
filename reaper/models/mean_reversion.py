"""Mean reversion model: z-score of close vs rolling 20-period mean."""
from reaper.logger import get_logger
from reaper.models import (BaseModel, LONG, SHORT, Ticket, candles_to_df)

log = get_logger("model.meanrev")


class MeanReversionModel(BaseModel):
    name = "MeanReversionModel"

    WINDOW = 20
    Z_ENTRY = 2.0
    Z_FLAT = 1.0

    def compute(self, coin: str, buf) -> Ticket:
        try:
            # only fires when regime detector says RANGING (if regime set)
            regime = (buf.ctx.get(coin) or {}).get("regime")
            if regime and regime != "RANGING":
                return self.flat(reason="non_ranging_regime", regime=regime)

            df = candles_to_df(buf.latest_candles(coin, "1m", self.WINDOW + 10))
            if len(df) < self.WINDOW + 1:
                return self.flat(reason="insufficient_candles", n=len(df))
            close = df["c"]
            mean = float(close.rolling(self.WINDOW).mean().iloc[-1])
            std = float(close.rolling(self.WINDOW).std().iloc[-1])
            px = float(close.iloc[-1])
            if std <= 0:
                return self.flat(reason="zero_std")
            z = (px - mean) / std
            meta = {"z": round(z, 3), "mean": round(mean, 4),
                    "std": round(std, 6), "regime": regime}

            if abs(z) < self.Z_ENTRY:
                return self.flat(**meta)
            confidence = min(0.90, 0.55 + (abs(z) - self.Z_ENTRY) * 0.15)
            direction = SHORT if z > 0 else LONG
            return Ticket(self.name, direction, confidence, meta)
        except Exception as e:
            log.warning("compute failed for %s: %s", coin, e)
            return self.flat(error=str(e))
