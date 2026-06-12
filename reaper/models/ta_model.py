"""TA model: RSI + MACD + EMA cross + Bollinger combined into one score.

Two scoring modes (Phase 4.6 experiment, 2026-06-11):
- "blend" (default): the original 4-component graduated blend. Despite a
  state-conditioned audit (scripts/audit_ta.py) suggesting the trend
  components were dead weight, the blend empirically outperformed in the
  full ensemble: BTC 15m training split PF 1.61 vs PF 0.84 for fade-only.
- "fade": extremes-only (RSI <=25/>=75, Bollinger touches). Kept for
  regime-routing experiments; do not default to it without new evidence.
"""
import numpy as np
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import BollingerBands

from reaper.logger import get_logger
from reaper.models import (BaseModel, LONG, SHORT, Ticket, candles_to_df)

log = get_logger("model.ta")


class TAModel(BaseModel):
    name = "TAModel"

    def __init__(self, mode: str = "blend", rsi_low: float = 25.0,
                 rsi_high: float = 75.0):
        if mode not in ("blend", "fade"):
            raise ValueError(f"unknown TA mode: {mode}")
        self.mode = mode
        self.rsi_low = rsi_low
        self.rsi_high = rsi_high

    def compute(self, coin: str, buf) -> Ticket:
        try:
            df = candles_to_df(buf.latest_candles(coin, "1m", 100))
            if len(df) < 30:
                return self.flat(reason="insufficient_candles", n=len(df))
            close = df["c"]
            px = float(close.iloc[-1])

            rsi = float(RSIIndicator(close, window=14).rsi().iloc[-1])
            bb = BollingerBands(close, window=20, window_dev=2)
            bb_hi = float(bb.bollinger_hband().iloc[-1])
            bb_lo = float(bb.bollinger_lband().iloc[-1])
            bb_score = 1.0 if px <= bb_lo else (-1.0 if px >= bb_hi else 0.0)
            meta = {"rsi": round(rsi, 2), "mode": self.mode,
                    "bb_pos": "lower" if px <= bb_lo
                              else ("upper" if px >= bb_hi else "mid")}

            if self.mode == "fade":
                rsi_score = (1.0 if rsi <= self.rsi_low
                             else (-1.0 if rsi >= self.rsi_high else 0.0))
                active = [s for s in (rsi_score, bb_score) if abs(s) > 0.05]
                if not active:
                    return self.flat(**meta)
                score = sum(active) / len(active)
                if abs(score) < 0.5:
                    return self.flat(**meta, score=round(score, 3))
                confidence = min(0.78, 0.60 + 0.09 * (len(active) - 1)
                                 + 0.09 * (abs(score) - 0.5))
                direction = LONG if score > 0 else SHORT
                meta["score"] = round(score, 3)
                return Ticket(self.name, direction, confidence, meta)

            # ---- blend mode: original graduated 4-component score ----
            macd_hist = float(MACD(close).macd_diff().iloc[-1])
            ema9 = float(EMAIndicator(close, window=9).ema_indicator().iloc[-1])
            ema21 = float(EMAIndicator(close, window=21).ema_indicator().iloc[-1])

            rsi_score = float(np.clip((50.0 - rsi) / 20.0, -1.0, 1.0))
            macd_score = float(np.tanh(macd_hist / (px * 2e-4)))
            ema_score = 1.0 if ema9 > ema21 else -1.0

            score = (rsi_score + macd_score + ema_score + bb_score) / 4.0
            meta.update({"macd_hist": round(macd_hist, 6),
                         "ema9": round(ema9, 4), "ema21": round(ema21, 4),
                         "score": round(score, 4)})
            if abs(score) < 0.15:
                return self.flat(**meta)
            direction = LONG if score > 0 else SHORT
            confidence = min(0.95, 0.50 + abs(score) * 0.45)
            return Ticket(self.name, direction, confidence, meta)
        except Exception as e:
            log.warning("compute failed for %s: %s", coin, e)
            return self.flat(error=str(e))
