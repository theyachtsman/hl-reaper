"""SignalAggregator: weighted ticket voting with regime-based weight routing
and a funding-rate veto."""
import time
from dataclasses import dataclass, field

from reaper.logger import get_logger
from reaper.models import LONG, SHORT, FLAT, Ticket

log = get_logger("aggregator")

BASE_WEIGHTS = {
    "TAModel":                  0.18,
    "MLForecastModel":          0.20,
    "RegimeDetectorModel":      0.00,  # meta-model, used for routing only
    "MeanReversionModel":       0.12,
    "FundingRateModel":         0.12,
    "OrderbookImbalanceModel":  0.15,
    "VWAPModel":                0.12,
    "LiquidationHeatmapModel":  0.11,
}

REGIME_NAMES = ("TRENDING_UP", "TRENDING_DOWN", "RANGING", "HIGH_VOL")
FUNDING_VETO_FACTOR = 0.6


@dataclass
class AggregatedSignal:
    coin: str
    direction: str      # LONG | SHORT | FLAT
    confidence: float   # 0.0 – 1.0 weighted score
    model_votes: dict   # {model_name: Ticket}
    long_votes: int
    short_votes: int
    flat_votes: int
    regime: str
    ts: int
    weights: dict = field(default_factory=dict)  # weights actually used


class SignalAggregator:
    def __init__(self, base_weights: dict | None = None):
        self.base_weights = dict(base_weights or BASE_WEIGHTS)

    def adjusted_weights(self, regime: str) -> dict:
        """Regime-routed model weights, re-normalized to sum to 1.0
        (RegimeDetectorModel stays at 0 — routing only)."""
        w = dict(self.base_weights)
        if regime in ("TRENDING_UP", "TRENDING_DOWN"):
            w["TAModel"] += 0.05
            w["MLForecastModel"] += 0.05
            w["MeanReversionModel"] = 0.02   # mean reversion fails in trends
        elif regime == "RANGING":
            w["MeanReversionModel"] += 0.08
            w["VWAPModel"] += 0.05
            w["TAModel"] = max(0.0, w["TAModel"] - 0.05)
        elif regime == "HIGH_VOL":
            w = {k: v * 0.8 for k, v in w.items()}
            w["OrderbookImbalanceModel"] += 0.08  # microstructure matters more
        w["RegimeDetectorModel"] = 0.0
        total = sum(w.values())
        if total > 0:
            w = {k: v / total for k, v in w.items()}
        return w

    def aggregate(self, coin: str, tickets: list[Ticket]) -> AggregatedSignal:
        votes = {t.model: t for t in tickets}

        # 1. regime from the regime detector ticket
        regime_ticket = votes.get("RegimeDetectorModel")
        regime = (regime_ticket.direction
                  if regime_ticket and regime_ticket.direction in REGIME_NAMES
                  else "UNKNOWN")

        # 2. regime-adjusted weights
        weights = self.adjusted_weights(regime)

        # 3/4. weighted directional score in [-1, +1]
        score = 0.0
        long_votes = short_votes = flat_votes = 0
        for t in tickets:
            if t.model == "RegimeDetectorModel":
                continue
            if t.direction == LONG:
                long_votes += 1
                score += weights.get(t.model, 0.0) * t.confidence
            elif t.direction == SHORT:
                short_votes += 1
                score -= weights.get(t.model, 0.0) * t.confidence
            else:
                flat_votes += 1

        direction = FLAT
        confidence = abs(score)
        if score > 1e-9:
            direction = LONG
        elif score < -1e-9:
            direction = SHORT

        # funding veto: fading the crowd overrides trend-following entries
        funding = votes.get("FundingRateModel")
        if funding is not None and direction != FLAT:
            if funding.direction == SHORT and direction == LONG:
                confidence *= FUNDING_VETO_FACTOR
            elif funding.direction == LONG and direction == SHORT:
                confidence *= FUNDING_VETO_FACTOR

        return AggregatedSignal(
            coin=coin,
            direction=direction,
            confidence=min(1.0, confidence),
            model_votes=votes,
            long_votes=long_votes,
            short_votes=short_votes,
            flat_votes=flat_votes,
            regime=regime,
            ts=int(time.time() * 1000),
            weights=weights,
        )
