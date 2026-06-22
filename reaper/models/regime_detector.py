"""Regime detector: ADX + ATR classification into TRENDING_UP/DOWN,
RANGING, HIGH_VOL. Meta-model — its 'direction' is the regime string."""
from ta.trend import ADXIndicator
from ta.volatility import AverageTrueRange

from reaper.logger import get_logger
from reaper.models import BaseModel, Ticket, candles_to_df

log = get_logger("model.regime")

REGIMES = ("TRENDING_UP", "TRENDING_DOWN", "RANGING", "HIGH_VOL")


class RegimeDetectorModel(BaseModel):
    name = "RegimeDetectorModel"

    ADX_TREND = 25.0
    ATR_HIGH_VOL = 0.03

    def compute(self, coin: str, buf, interval: str | None = None) -> Ticket:
        try:
            # prefer the requested resolution (5m scalp / 1h trend) for a stable
            # regime read; fall back to 1m if too few candles. Default 5m keeps
            # legacy single-band behavior.
            primary = interval or "5m"
            candles = buf.latest_candles(coin, primary, 60)
            interval = primary
            if len(candles) < 30:
                candles = buf.latest_candles(coin, "1m", 100)
                interval = "1m"
            df = candles_to_df(candles)
            if len(df) < 30:
                return Ticket(self.name, "UNKNOWN", 1.0,
                              {"reason": "insufficient_candles", "n": len(df)})

            adx_ind = ADXIndicator(df["h"], df["l"], df["c"], window=14)
            adx = float(adx_ind.adx().iloc[-1])
            di_pos = float(adx_ind.adx_pos().iloc[-1])
            di_neg = float(adx_ind.adx_neg().iloc[-1])
            atr = float(AverageTrueRange(df["h"], df["l"], df["c"],
                                         window=14).average_true_range().iloc[-1])
            px = float(df["c"].iloc[-1])
            atr_ratio = atr / px if px > 0 else 0.0

            if atr_ratio > self.ATR_HIGH_VOL:
                regime = "HIGH_VOL"
            elif adx > self.ADX_TREND:
                regime = "TRENDING_UP" if di_pos >= di_neg else "TRENDING_DOWN"
            else:
                regime = "RANGING"

            # publish for other models (e.g. mean reversion gate)
            ctx = buf.ctx.get(coin)
            if isinstance(ctx, dict):
                ctx["regime"] = regime

            return Ticket(self.name, regime, 1.0,
                          {"adx": round(adx, 2), "di_pos": round(di_pos, 2),
                           "di_neg": round(di_neg, 2),
                           "atr_ratio": round(atr_ratio, 5),
                           "interval": interval})
        except Exception as e:
            log.warning("compute failed for %s: %s", coin, e)
            return Ticket(self.name, "UNKNOWN", 1.0, {"error": str(e)})
