"""Momentum / price-velocity model: votes in the direction of strong, fast
price movement.

Added 2026-06-24 after a BTC -4.86% / ETH -5.91% intraday drop during which the
ensemble called LONG at 0.65-0.83 the whole way down — MEANREV and VWAP read the
oversold freefall as a bounce setup while no model answered the one question that
matters in a fast move: "is price moving hard in one direction RIGHT NOW?"

Unlike the other directional models this one ignores support levels, funding, and
book depth. It measures the rate of change of close over three lookback windows,
blends them (recent matters more), and votes SHORT on strong downward momentum /
LONG on strong upward momentum. It is a TREND-FOLLOWING signal by construction —
it never fades the move.
"""
from reaper.logger import get_logger
from reaper.models import BaseModel, LONG, SHORT, Ticket, candles_to_df

log = get_logger("model.momentum")


class MomentumModel(BaseModel):
    name = "MomentumModel"

    # Weighted composite of three rate-of-change windows. Recent ROC dominates
    # so a sharp fresh move registers before the slower windows catch up.
    ROC_WEIGHTS = (0.50, 0.30, 0.20)  # (roc_3, roc_6, roc_12)

    def __init__(self, short_threshold: float = -0.003,
                 long_threshold: float = 0.003,
                 full_conf_move: float = 0.010,
                 min_candles: int = 15):
        # composite move that starts registering a vote (signed)
        self.short_threshold = short_threshold   # e.g. -0.003 = -0.3%
        self.long_threshold = long_threshold     # e.g. +0.003 = +0.3%
        # composite move that pins confidence to the 0.95 ceiling
        self.full_conf_move = full_conf_move     # e.g. 0.010 = 1.0%
        self.min_candles = min_candles

    def compute(self, coin: str, buf, interval: str | None = None) -> Ticket:
        try:
            # roc_12 needs close[-13]; require min_candles (>=15) for headroom.
            df = candles_to_df(
                buf.latest_candles(coin, interval or "1m", self.min_candles + 5))
            if len(df) < self.min_candles:
                return self.flat(reason="insufficient_candles", n=len(df))
            close = df["c"]
            c_now = float(close.iloc[-1])
            c_3 = float(close.iloc[-4])
            c_6 = float(close.iloc[-7])
            c_12 = float(close.iloc[-13])
            if c_3 <= 0 or c_6 <= 0 or c_12 <= 0:
                return self.flat(reason="bad_price")

            roc_3 = (c_now - c_3) / c_3
            roc_6 = (c_now - c_6) / c_6
            roc_12 = (c_now - c_12) / c_12
            w3, w6, w12 = self.ROC_WEIGHTS
            composite = roc_3 * w3 + roc_6 * w6 + roc_12 * w12

            base_meta = {
                "roc_3": round(roc_3 * 100, 3),      # percent
                "roc_6": round(roc_6 * 100, 3),
                "roc_12": round(roc_12 * 100, 3),
                "composite": round(composite * 100, 3),
            }

            if composite <= self.short_threshold:
                conf = self._confidence(composite, self.short_threshold)
                if conf < 1e-9:
                    return self.flat(**base_meta, threshold=self.short_threshold)
                return Ticket(self.name, SHORT, conf,
                              {**base_meta, "threshold": self.short_threshold})
            if composite >= self.long_threshold:
                conf = self._confidence(composite, self.long_threshold)
                if conf < 1e-9:
                    return self.flat(**base_meta, threshold=self.long_threshold)
                return Ticket(self.name, LONG, conf,
                              {**base_meta, "threshold": self.long_threshold})
            return self.flat(**base_meta, reason="below_threshold")
        except Exception as e:
            log.warning("compute failed for %s: %s", coin, e)
            return self.flat(error=str(e))

    def _confidence(self, composite: float, threshold: float) -> float:
        """Linear ramp from |threshold| (conf 0) to full_conf_move (conf 0.95).

        Uses magnitudes so the same math serves both directions. Clamped to
        [0.0, 0.95] — the 0.95 ceiling matches the other models' caps."""
        span = self.full_conf_move - abs(threshold)
        if span <= 0:
            return 0.95
        raw = (abs(composite) - abs(threshold)) / span
        return min(0.95, max(0.0, raw))
