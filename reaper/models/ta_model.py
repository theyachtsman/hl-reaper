"""TA model: RSI + MACD + EMA cross + Bollinger combined into one score.

Two scoring modes (Phase 4.6 experiment, 2026-06-11):
- "blend" (default): the original 4-component graduated blend. Despite a
  state-conditioned audit (scripts/audit_ta.py) suggesting the trend
  components were dead weight, the blend empirically outperformed in the
  full ensemble: BTC 15m training split PF 1.61 vs PF 0.84 for fade-only.
- "fade": extremes-only (RSI <=25/>=75, Bollinger touches). Kept for
  regime-routing experiments; do not default to it without new evidence.

Regime-aware trending relaxation (2026-06-24)
---------------------------------------------
The blend's RSI and Bollinger components are mean-reversion oriented, so in a
sustained trend they fight the EMA/MACD trend components and the net score
collapses inside the |score| < 0.15 dead-band — TAModel abstains exactly when
the regime is clearly TRENDING. With only 1-2 active voters left, the
aggregator can't reach the confidence / agreement gate.

Fix: when the RegimeDetector (published to buf.ctx[coin]["regime"]) reports
TRENDING_UP / TRENDING_DOWN, blend mode switches to a relaxed, regime-aware RSI
rule (trending_rsi_vote): TA agrees with the trend at MODERATE RSI rather than
waiting for a ranging-market extreme, and only fades it at a genuine oversold/
overbought extreme. RANGING / HIGH_VOL / UNKNOWN are untouched (the original
blend), so the calibrated ranging behavior is preserved. HIGH_VOL deliberately
keeps the conservative blend — TA reads are noisy there.
"""
import numpy as np
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import BollingerBands

from reaper.logger import get_logger
from reaper.models import (BaseModel, FLAT, LONG, SHORT, Ticket, candles_to_df)

log = get_logger("model.ta")

TRENDING_UP = "TRENDING_UP"
TRENDING_DOWN = "TRENDING_DOWN"

# Trending-regime RSI thresholds (config.yaml models.ta.trending; hot-reloaded
# by run_bot each loop). Defined in TRENDING_DOWN space; TRENDING_UP is the
# exact mirror (reflect RSI around 50). See trending_rsi_vote().
TRENDING_DEFAULTS = {
    "rsi_short": 48.0,        # downtrend: SHORT (trend-aligned) once RSI >= this
    "rsi_long": 38.0,         # downtrend: LONG only at this extreme oversold
    "rsi_neutral_low": 48.0,  # confidence anchor: firing edge -> conf 0.40
    "rsi_neutral_high": 55.0, # confidence anchor: +0.20 conf per neutral-band width
}


def trending_rsi_vote(rsi: float, regime: str | None, *,
                      rsi_short: float, rsi_long: float,
                      rsi_neutral_low: float, rsi_neutral_high: float):
    """Pure regime-aware RSI vote for trending regimes.

    Returns (direction, confidence) — direction may be FLAT in the narrow
    neutral zone — or None when `regime` is not trending (the caller then falls
    back to the standard blend, so RANGING / HIGH_VOL / UNKNOWN keep their
    calibrated behavior unchanged).

    Everything is evaluated in TRENDING_DOWN space; TRENDING_UP is the exact
    mirror, obtained by reflecting RSI around 50 and swapping the resulting
    direction. In TRENDING_DOWN: SHORT (trend-aligned) once RSI clears
    rsi_short, LONG only at an extreme-oversold RSI <= rsi_long, FLAT in the
    narrow band between them. Confidence starts at 0.40 the moment the firing
    threshold is cleared and rises +0.20 for every (rsi_neutral_high -
    rsi_neutral_low) of extra RSI travel, capped at 0.95 — so a marginal
    reading votes weakly and only a stretched RSI votes with conviction.
    """
    if regime not in (TRENDING_UP, TRENDING_DOWN):
        return None
    band = max(1e-6, rsi_neutral_high - rsi_neutral_low)
    r = rsi if regime == TRENDING_DOWN else 100.0 - rsi
    if r >= rsi_short:                       # trend-aligned (SHORT in a downtrend)
        direction, conf = SHORT, 0.40 + 0.20 * (r - rsi_short) / band
    elif r <= rsi_long:                      # extreme-oversold counter-trend bounce
        direction, conf = LONG, 0.40 + 0.20 * (rsi_long - r) / band
    else:                                    # narrow neutral zone -> abstain
        return FLAT, 0.0
    conf = max(0.40, min(0.95, conf))
    if regime == TRENDING_UP:                # reflect direction back to real space
        direction = LONG if direction == SHORT else SHORT
    return direction, conf


class TAModel(BaseModel):
    name = "TAModel"

    def __init__(self, mode: str = "blend", rsi_low: float = 25.0,
                 rsi_high: float = 75.0,
                 trending_rsi_short: float = 48.0,
                 trending_rsi_long: float = 38.0,
                 trending_rsi_neutral_low: float = 48.0,
                 trending_rsi_neutral_high: float = 55.0,
                 ranging_rsi_short: float = 68.0,
                 ranging_rsi_long: float = 32.0):
        if mode not in ("blend", "fade"):
            raise ValueError(f"unknown TA mode: {mode}")
        self.mode = mode
        self.rsi_low = rsi_low
        self.rsi_high = rsi_high
        # Trending thresholds — a mutable dict so run_bot can hot-reload it in
        # place each loop (like funding_model.smooth_mapping).
        self.trending = {
            "rsi_short": float(trending_rsi_short),
            "rsi_long": float(trending_rsi_long),
            "rsi_neutral_low": float(trending_rsi_neutral_low),
            "rsi_neutral_high": float(trending_rsi_neutral_high),
        }
        # Ranging thresholds are accepted for config completeness, but the
        # RANGING path below is the original blend — deliberately NOT changed.
        self.ranging = {
            "rsi_short": float(ranging_rsi_short),
            "rsi_long": float(ranging_rsi_long),
        }

    def compute(self, coin: str, buf, interval: str | None = None) -> Ticket:
        try:
            df = candles_to_df(buf.latest_candles(coin, interval or "1m", 100))
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

            # ---- regime-aware trending relaxation (blend mode) --------------
            # In a clear trend the blend's mean-reversion RSI/BB components
            # cancel the trend EMA/MACD ones and the score dies in the dead-band
            # -> FLAT. When the regime is TRENDING_*, use the relaxed RSI rule so
            # TA agrees with the trend at moderate RSI instead of abstaining.
            # RANGING / HIGH_VOL / UNKNOWN return None here and fall through to
            # the original blend below (unchanged).
            regime = (buf.ctx.get(coin) or {}).get("regime")
            tv = trending_rsi_vote(rsi, regime, **self.trending)
            if tv is not None:
                direction, conf = tv
                meta.update({"regime": regime, "regime_mode": True,
                             "rsi_thresholds": dict(self.trending)})
                if direction == FLAT:
                    return self.flat(**meta, reason="trending_neutral_zone")
                # trend-aligned votes confirm with the Bollinger mid / price
                # direction; a failed confirmation dampens (never blocks) so the
                # vote still counts. The counter-trend extreme (oversold LONG in
                # a downtrend / overbought SHORT in an uptrend) is a mean-
                # reversion bounce and needs no trend confirmation.
                aligned = ((regime == TRENDING_DOWN and direction == SHORT)
                           or (regime == TRENDING_UP and direction == LONG))
                if aligned:
                    bb_mid = float(bb.bollinger_mavg().iloc[-1])
                    prev = float(close.iloc[-2]) if len(close) >= 2 else px
                    confirm = ((px < bb_mid or px < prev)
                               if regime == TRENDING_DOWN
                               else (px > bb_mid or px > prev))
                    if not confirm:
                        conf *= 0.85
                    meta["confirm"] = bool(confirm)
                conf = min(0.95, conf)
                log.info("TA [%s mode]: %s conf %.2f (RSI %.1f, relaxed "
                         "threshold)", regime, direction, conf, rsi)
                return Ticket(self.name, direction, conf, meta)

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
