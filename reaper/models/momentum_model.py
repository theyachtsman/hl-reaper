"""Momentum / price-velocity model: votes in the direction of strong, fast
price movement.

Added 2026-06-24 after a BTC -4.86% / ETH -5.91% intraday drop during which the
ensemble called LONG at 0.65-0.83 the whole way down — MEANREV and VWAP read the
oversold freefall as a bounce setup while no model answered the one question that
matters in a fast move: "is price moving hard in one direction RIGHT NOW?"

Unlike the other directional models this one ignores support levels, funding, and
book depth. It measures the rate of change of close over a few short lookback
windows, blends them (recent matters more), and votes SHORT on strong downward
momentum / LONG on strong upward momentum. It is a TREND-FOLLOWING signal by
construction — it never fades the move.

2026-06-26 rewrite (sign/saturation fix). A full day of signal logging exposed two
defects on the 1h trend band:
  1. Confidence railed to the 0.95 ceiling on >75% of votes — `full_conf_move`
     (1.0%) was tiny next to the composite moves a 1h band actually produces
     (p90 ~ 1.5-1.9%), so almost everything clamped to the cap.
  2. "Confident LONG into a drop." The 3/6/12-candle lookback on a 1h band spans
     3-12 HOURS; a fresh 2h down-leg was swamped by a 6h-old bounce off a low, so
     the composite stayed POSITIVE and the model voted max-confidence LONG into a
     steady slide (forward-return validation: anti-predictive at every horizon).

Both are fixed by (a) shortening the lookback to (1,2,3) candles so it reads the
RECENT move, and (b) normalizing the composite by the band's own recent
volatility (a z-score). The z-score is scale-free — the same thresholds work
regardless of the candle interval or volatility regime, and confidence spans its
range instead of railing. See scripts/diagnose_momentum.py for the validation.
"""
import statistics

from reaper.logger import get_logger
from reaper.models import BaseModel, LONG, SHORT, Ticket, candles_to_df

log = get_logger("model.momentum")


class MomentumModel(BaseModel):
    name = "MomentumModel"

    # Weighted composite of the rate-of-change windows. Recent ROC dominates so a
    # sharp fresh move registers before the slower windows catch up.
    ROC_WEIGHTS = (0.50, 0.30, 0.20)

    def __init__(self, enter_z: float = 0.6,
                 full_conf_z: float = 2.6,
                 vol_window: int = 14,
                 lookbacks: tuple[int, ...] = (1, 2, 3),
                 min_candles: int = 20):
        # composite z-score (move in units of recent per-candle volatility) at
        # which a vote starts registering; symmetric for LONG and SHORT.
        self.enter_z = enter_z
        # composite z-score that pins confidence to the 0.95 ceiling.
        self.full_conf_z = full_conf_z
        # trailing per-candle returns used to estimate volatility (the z denominator)
        self.vol_window = vol_window
        # ROC lookbacks in candles; len must match ROC_WEIGHTS
        self.lookbacks = tuple(lookbacks)
        self.min_candles = min_candles

    def compute(self, coin: str, buf, interval: str | None = None) -> Ticket:
        try:
            need = max(self.vol_window + 1, max(self.lookbacks) + 1,
                       self.min_candles)
            df = candles_to_df(
                buf.latest_candles(coin, interval or "1m", need + 5))
            if len(df) < self.min_candles:
                return self.flat(reason="insufficient_candles", n=len(df))
            close = df["c"].to_numpy(dtype=float)
            c_now = float(close[-1])

            # weighted composite ROC over the (recent) lookback windows
            rocs = []
            composite = 0.0
            for n, w in zip(self.lookbacks, self.ROC_WEIGHTS):
                past = float(close[-1 - n])
                if past <= 0:
                    return self.flat(reason="bad_price")
                roc = (c_now - past) / past
                rocs.append(roc)
                composite += roc * w

            # recent per-candle return volatility -> scale-free denominator
            window = close[-(self.vol_window + 1):]
            rets = [(window[i] - window[i - 1]) / window[i - 1]
                    for i in range(1, len(window)) if window[i - 1] > 0]
            vol = statistics.pstdev(rets) if len(rets) >= 2 else 0.0

            base_meta = {
                **{f"roc_{n}": round(r * 100, 3)
                   for n, r in zip(self.lookbacks, rocs)},
                "composite": round(composite * 100, 3),   # percent
                "vol": round(vol * 100, 3),               # percent
            }
            if vol <= 0:
                return self.flat(reason="no_volatility", **base_meta)

            z = composite / vol
            base_meta["z"] = round(z, 3)

            if z <= -self.enter_z:
                conf = self._confidence(z)
                if conf < 1e-9:
                    return self.flat(**base_meta, enter_z=self.enter_z)
                return Ticket(self.name, SHORT, conf,
                              {**base_meta, "enter_z": self.enter_z})
            if z >= self.enter_z:
                conf = self._confidence(z)
                if conf < 1e-9:
                    return self.flat(**base_meta, enter_z=self.enter_z)
                return Ticket(self.name, LONG, conf,
                              {**base_meta, "enter_z": self.enter_z})
            return self.flat(**base_meta, reason="below_threshold")
        except Exception as e:
            log.warning("compute failed for %s: %s", coin, e)
            return self.flat(error=str(e))

    def _confidence(self, z: float) -> float:
        """Linear ramp from enter_z (conf 0) to full_conf_z (conf 0.95).

        Operates on the z-score magnitude so the same math serves both
        directions and is independent of timeframe / volatility regime. Clamped
        to [0.0, 0.95] — the 0.95 ceiling matches the other models' caps."""
        span = self.full_conf_z - self.enter_z
        if span <= 0:
            return 0.95
        raw = (abs(z) - self.enter_z) / span
        return min(0.95, max(0.0, raw))
