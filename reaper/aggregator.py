"""SignalAggregator: weighted ticket voting with regime-based weight routing
and a funding-rate veto."""
import time
from dataclasses import dataclass, field

from reaper.logger import get_logger
from reaper.models import LONG, SHORT, FLAT, Ticket

log = get_logger("aggregator")

# Six active DIRECTIONAL voters (RegimeDetector is a meta-router at 0.0, ML and
# LiqHeatmap are zeroed — both permanently FLAT). The legacy five summed to 1.0;
# MomentumModel (2026-06-24) was added at 0.15 and the other five scaled by 0.85
# so their relative proportions are unchanged. Sum = 1.0 (renormalized anyway).
BASE_WEIGHTS = {
    "TAModel":                  0.19,   # 0.225 * 0.85
    # zeroed 2026-06-15: direction classification confirmed not viable after a
    # full 225d retrain (0/7 cleared the majority-class gate — see
    # docs/ml_retrain_report.md). Permanently FLAT; kept as a slot for a future
    # different-target model (volatility/regime). No vote weight.
    "MLForecastModel":          0.00,
    "RegimeDetectorModel":      0.00,  # meta-model, used for routing only
    "MeanReversionModel":       0.13,   # 0.15 * 0.85
    "FundingRateModel":         0.13,   # 0.15 * 0.85
    # OB is the only model with a measured positive directional tilt
    # (docs/microstructure_backtest_report.md — 1m hit 0.54-0.57, positive on
    # 7/7 coins). 0.325 * 0.85.
    "OrderbookImbalanceModel":  0.27,
    "VWAPModel":                0.13,   # 0.15 * 0.85
    # price-velocity / trend-following voter (2026-06-24). Answers "is price
    # moving hard one way right now?" — the question the other five miss in a
    # fast move (see reaper/models/momentum_model.py).
    "MomentumModel":            0.15,
    # zeroed 2026-06-14: 100% FLAT across all 7 coins on ~4 days recorded live
    # L2 — structurally inert on normal tape. Model still computes/logs (its
    # OI-distribution logic may feed Phase 8.6 cascade-v2); just no vote weight.
    "LiquidationHeatmapModel":  0.00,
}

# --- Dual-band weight sets (2026-06-20) ---------------------------------------
# The bot runs TWO aggregations per coin per cycle: a SCALP band on a fast
# resolution (5m) and a TREND band on a slow resolution (1h). Each has its own
# fixed weight set (no regime-routing weight shuffle — see aggregate's
# regime_routing flag). Both sum to 1.0 across the five active directional
# voters; the meta/dead slots stay 0.0.
#
#   SCALP — mean reversion is the dominant signal (fade the local top/bottom);
#   OB confirms live pressure; TA/VWAP/funding round it out. MomentumModel
#   (2026-06-24) gets a modest 0.15 here so a fast move can pull the scalp net
#   toward the move instead of letting MEANREV fade a freefall.
#   TREND — no mean reversion (it fails in sustained trends); TA + OB lead,
#   funding (smooth-mapped) and VWAP confirm the structural move. MomentumModel
#   gets a higher 0.20 — momentum matters most on the trend band.
# Existing voters were scaled (SCALP x0.85, TREND x0.80) to make room; each set
# still sums to 1.0 (the aggregator renormalizes regardless).
SCALP_WEIGHTS = {
    "TAModel":                  0.13,   # 0.15 * 0.85
    "MeanReversionModel":       0.38,   # 0.45 * 0.85
    "MLForecastModel":          0.00,
    "RegimeDetectorModel":      0.00,
    "FundingRateModel":         0.04,   # 0.05 * 0.85
    "OrderbookImbalanceModel":  0.17,   # 0.20 * 0.85
    "VWAPModel":                0.13,   # 0.15 * 0.85
    "MomentumModel":            0.15,
    "LiquidationHeatmapModel":  0.00,
}
TREND_WEIGHTS = {
    "TAModel":                  0.24,   # 0.30 * 0.80
    "MeanReversionModel":       0.00,
    "MLForecastModel":          0.00,
    "RegimeDetectorModel":      0.00,
    "FundingRateModel":         0.16,   # 0.20 * 0.80
    "OrderbookImbalanceModel":  0.24,   # 0.30 * 0.80
    "VWAPModel":                0.16,   # 0.20 * 0.80
    "MomentumModel":            0.20,
    "LiquidationHeatmapModel":  0.00,
}

REGIME_NAMES = ("TRENDING_UP", "TRENDING_DOWN", "RANGING", "HIGH_VOL")
FUNDING_VETO_FACTOR = 0.6

# A ticket is treated as an abstention (FLAT) — and therefore excluded from BOTH
# the directional numerator AND the weight denominator — if it has no direction
# OR its confidence sits at/near zero. Genuine opposing votes (a LONG when the
# net is SHORT) are NOT abstentions; they still count and reduce net conf.
FLAT_CONF_EPS = 0.05

# BOOK regime dampening (2026-06-22): a bid/ask-imbalance vote that OPPOSES the
# confirmed 1h trend is usually absorption (large size distributing into the
# trend), not a reversal. Down-weight such a counter-trend BOOK vote to 40% so
# it can't cancel several trend-aligned voters, without zeroing it (a very
# strong opposing imbalance can still register as a weak contrarian signal).
# Agreeing BOOK votes and RANGING/UNKNOWN regimes are untouched.
BOOK_REGIME_DAMPEN = 0.40

# Funding regime dampening (2026-06-23): FundingRateModel fades the crowd — in a
# RANGING/HIGH_VOL squeeze that's its edge, but in a SUSTAINED 1h trend a
# persistently negative (or positive) funding rate makes it vote counter-trend
# for hours (e.g. LONG all the way down a $2k BTC drop), systematically
# cancelling the SHORT conviction of BOOK/VWAP/TA. So a counter-1h-trend FUNDING
# vote has its WEIGHT cut to this factor (numerator AND denominator), shrinking
# its drag without removing it. Trend-aligned FUNDING and RANGING/HIGH_VOL/
# UNKNOWN regimes keep full weight. Mirrors BOOK_REGIME_DAMPEN's intent but acts
# on the weight (per spec) rather than the confidence.
FUNDING_COUNTER_TREND_DAMP = 0.40

# Momentum regime dampening (2026-06-24): momentum exists in every regime, but in
# a RANGING market fast moves are noisier (false breakouts that snap back), so a
# MomentumModel vote has its WEIGHT cut to this factor in RANGING only. In
# TRENDING_UP/DOWN and HIGH_VOL it keeps FULL weight — momentum is especially
# meaningful there. This is a directionless weight cut (it does NOT depend on
# whether momentum agrees or fights the trend): MomentumModel is trend-following
# by construction so it can't be a counter-trend voter, and the existing
# counter-trend penalty (apply_regime_bias) only dampens a scalp whose NET
# direction fades the 1h trend — a strong momentum vote pushes the net WITH the
# move, so it is never the thing being penalized there.
MOMENTUM_RANGING_DAMP = 0.70

# Permanently non-voting models (ML: direction classification not viable;
# LiqHeatmap: inert on normal tape). Both carry 0 weight, so excluding them from
# the vote tally is DISPLAY-ONLY — no effect on score, confidence, or
# model_agreement (which counts long_votes/short_votes only). They stay defined
# in the ensemble and still produce/log INACTIVE tickets; they're just not
# counted as voters, so the reported L/S/F tally reflects the real active count.
INACTIVE_MODELS = ("MLForecastModel", "LiquidationHeatmapModel")


def apply_regime_bias(signal: "AggregatedSignal", regime_1h: str,
                      counter_trend_penalty: float = 0.7) -> "AggregatedSignal":
    """Bias a SCALP signal's confidence by the 1h (trend-band) regime.

    DAMPENS — never blocks. A scalp that fades INTO the prevailing 1h trend
    (short within an uptrend, long within a downtrend) is a counter-trend scalp;
    its confidence is multiplied by counter_trend_penalty so it needs higher
    conviction to clear the entry gate, but it can still fire. Trend-aligned
    scalps and RANGING regimes are untouched. The trend band's own signal is
    never modified by this (the bias flows 1h -> 5m only).

    Mutates and returns `signal` (in place, like the other aggregator helpers).
    """
    if regime_1h not in ("TRENDING_UP", "TRENDING_DOWN"):
        return signal  # RANGING / UNKNOWN / HIGH_VOL — scalp both ways freely
    counter = ((regime_1h == "TRENDING_UP" and signal.direction == SHORT)
               or (regime_1h == "TRENDING_DOWN" and signal.direction == LONG))
    if counter:
        before = signal.confidence
        signal.confidence = min(1.0, signal.confidence * counter_trend_penalty)
        signal.meta["regime_bias"] = (
            f"counter_trend x{counter_trend_penalty:.2f} "
            f"({before:.2f}->{signal.confidence:.2f}) vs 1h {regime_1h}")
    else:
        signal.meta["regime_bias"] = f"aligned 1h {regime_1h}"
    return signal

# Funding hard-block (2026-06-16): the 0.6x veto above only dampens — TA/OB can
# still net the signal LONG against extreme positive funding. The LONG side bled
# (-$3.60 over 111 trades, 37% win) while SHORTs carried; Phase 4.6 confirmed
# leverage-crowded longs get squeezed. So when FundingRateModel is in extreme
# SHORT zone (confidence >= FUNDING_HARD_BLOCK_CONF) we hard-override any LONG
# verdict to FLAT. SHORTs are untouched. See docs / master gameplan.
FUNDING_HARD_BLOCK_CONF = 0.75


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
    meta: dict = field(default_factory=dict)     # block reasons, etc.


class SignalAggregator:
    def __init__(self, base_weights: dict | None = None, *,
                 funding_hard_block_enabled: bool = True,
                 funding_hard_block_conf: float = FUNDING_HARD_BLOCK_CONF,
                 funding_hard_block_short_enabled: bool = False,
                 funding_hard_block_short_conf: float = FUNDING_HARD_BLOCK_CONF,
                 funding_counter_trend_damp: float = FUNDING_COUNTER_TREND_DAMP,
                 momentum_ranging_damp: float = MOMENTUM_RANGING_DAMP):
        self.base_weights = dict(base_weights or BASE_WEIGHTS)
        self.funding_hard_block_enabled = funding_hard_block_enabled
        self.funding_hard_block_conf = funding_hard_block_conf
        # SHORT mirror — OFF by default (the SHORT side is the working side).
        # When on, FundingRateModel voting LONG at >= conf (crowded shorts /
        # deeply negative funding) blocks SHORT entries outright.
        self.funding_hard_block_short_enabled = funding_hard_block_short_enabled
        self.funding_hard_block_short_conf = funding_hard_block_short_conf
        # weight multiplier for a counter-1h-trend FUNDING vote (see
        # FUNDING_COUNTER_TREND_DAMP). 1.0 = no dampening, 0.0 = ignore funding
        # entirely when counter-trend. Hot-reloadable.
        self.funding_counter_trend_damp = funding_counter_trend_damp
        # weight multiplier for a MomentumModel vote in a RANGING regime (see
        # MOMENTUM_RANGING_DAMP). 1.0 = no dampening. Hot-reloadable.
        self.momentum_ranging_damp = momentum_ranging_damp

    def momentum_weight_factor(self, model: str, regime: str | None) -> float:
        """Weight multiplier for `model` given the band's `regime`.

        Only MomentumModel in a RANGING regime is dampened
        (-> momentum_ranging_damp). Every other model and every other regime
        (TRENDING_UP/DOWN, HIGH_VOL, UNKNOWN, None) returns 1.0 (full weight).
        Directionless by design — momentum is trend-following so there is no
        counter-trend case to distinguish."""
        if model == "MomentumModel" and regime == "RANGING":
            return self.momentum_ranging_damp
        return 1.0

    def funding_weight_factor(self, model: str, direction: str,
                              regime: str | None) -> float:
        """Weight multiplier for `model` given the confirmed 1h `regime`.

        Only FundingRateModel voting AGAINST a TRENDING regime is dampened
        (LONG in TRENDING_DOWN, SHORT in TRENDING_UP) -> funding_counter_trend_damp.
        Every other model, trend-aligned funding, and RANGING/HIGH_VOL/UNKNOWN/
        None regimes return 1.0 (full weight)."""
        if model != "FundingRateModel" or regime not in (
                "TRENDING_UP", "TRENDING_DOWN"):
            return 1.0
        counter = ((regime == "TRENDING_DOWN" and direction == LONG)
                   or (regime == "TRENDING_UP" and direction == SHORT))
        return self.funding_counter_trend_damp if counter else 1.0

    @staticmethod
    def _normalize_fixed(weights: dict) -> dict:
        """Renormalize a fixed band weight set to sum to 1.0 with
        RegimeDetectorModel forced to 0 (it is meta/routing only). No
        regime-based redistribution — band weight sets are deliberately fixed."""
        w = dict(weights)
        w["RegimeDetectorModel"] = 0.0
        total = sum(w.values())
        if total > 0:
            w = {k: v / total for k, v in w.items()}
        return w

    def adjusted_weights(self, regime: str, base: dict | None = None) -> dict:
        """Regime-routed model weights, re-normalized to sum to 1.0
        (RegimeDetectorModel stays at 0 — routing only)."""
        w = dict(base if base is not None else self.base_weights)
        if regime in ("TRENDING_UP", "TRENDING_DOWN"):
            # ML was the other trend-following model; with it zeroed the trend
            # boost goes entirely to TA (never to the dead ML slot).
            w["TAModel"] += 0.10
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

    def aggregate(self, coin: str, tickets: list[Ticket],
                  weights: dict | None = None,
                  regime_routing: bool = True,
                  book_regime: str | None = None) -> AggregatedSignal:
        """Aggregate model tickets into one directional signal.

        weights: optional fixed weight set (e.g. SCALP_WEIGHTS / TREND_WEIGHTS).
            When None, the legacy self.base_weights are used.
        regime_routing: when True, the regime-detector ticket reshapes the
            weights (legacy ensemble behavior). When False (dual-band mode),
            the supplied weight set is used as-is, just renormalized — band
            weight sets are fixed and trend-awareness is applied separately via
            apply_regime_bias(). The regime string is still computed/returned.
        book_regime: optional confirmed (1h) regime used to dampen a
            counter-trend OrderbookImbalanceModel vote (see BOOK_REGIME_DAMPEN).
            When None, no BOOK dampening is applied (legacy/backtest callers).
        """
        votes = {t.model: t for t in tickets}

        # 1. regime from the regime detector ticket
        regime_ticket = votes.get("RegimeDetectorModel")
        regime = (regime_ticket.direction
                  if regime_ticket and regime_ticket.direction in REGIME_NAMES
                  else "UNKNOWN")

        # 2. resolve weights
        if not regime_routing:
            weights = self._normalize_fixed(weights or self.base_weights)
        else:
            weights = self.adjusted_weights(regime, base=weights)

        # 3/4. weighted directional score, normalized over ACTIVE voters only.
        # Abstaining models (FLAT / near-zero conf) are dropped from both the
        # numerator and the denominator so a model sitting out (e.g.
        # MeanReversion in a trending regime) can't dilute the conviction of the
        # models that actually voted. Opposing directional votes still count.
        meta: dict = {}
        score = 0.0
        active_weight = 0.0
        long_votes = short_votes = flat_votes = 0
        for t in tickets:
            # RegimeDetector is a meta-router; ML/LiqHeatmap are parked
            # non-voters — neither counts toward the L/S/F tally (the inactive
            # ones carry 0 weight, so the score is unaffected either way).
            if t.model == "RegimeDetectorModel" or t.model in INACTIVE_MODELS:
                continue
            # genuine abstention — excluded from the weight denominator too.
            if t.direction not in (LONG, SHORT) or t.confidence < FLAT_CONF_EPS:
                flat_votes += 1
                continue

            conf = t.confidence
            # BOOK regime dampening: a counter-1h-trend imbalance vote is
            # absorption, not reversal — shrink it so it can't cancel the
            # trend-aligned voters. Agreeing votes / RANGING are untouched.
            if (t.model == "OrderbookImbalanceModel" and book_regime is not None
                    and ((book_regime == "TRENDING_DOWN" and t.direction == LONG)
                         or (book_regime == "TRENDING_UP"
                             and t.direction == SHORT))):
                conf *= BOOK_REGIME_DAMPEN
                meta["book_dampen"] = (
                    f"OB {t.direction} {t.confidence:.2f}->{conf:.2f} "
                    f"x{BOOK_REGIME_DAMPEN:.2f} vs 1h {book_regime}")

            w = weights.get(t.model, 0.0)
            # funding regime dampening: a counter-1h-trend FUNDING vote has its
            # WEIGHT cut (numerator AND denominator) so a persistent crowd-fade
            # vote can't cancel trend-aligned conviction in a sustained trend.
            ff = self.funding_weight_factor(t.model, t.direction, book_regime)
            if ff != 1.0:
                w_eff = w * ff
                meta["funding_dampen"] = (
                    f"FUNDING {t.direction} weight {w:.3f}->{w_eff:.3f} "
                    f"x{ff:.2f} vs 1h {book_regime}")
                log.info("AGGREGATOR: %s FUNDING dampened (%s + %s vote) "
                         "weight %.3f -> %.3f", coin, book_regime, t.direction,
                         w, w_eff)
                w = w_eff
            # momentum regime dampening: a MomentumModel vote in a RANGING
            # regime has its WEIGHT cut (noisier false breakouts). Trending /
            # high-vol regimes keep full weight. Uses the band's own regime, not
            # the 1h trend regime — momentum is a same-band velocity read.
            mf = self.momentum_weight_factor(t.model, regime)
            if mf != 1.0:
                w_eff = w * mf
                meta["momentum_dampen"] = (
                    f"MOMENTUM {t.direction} weight {w:.3f}->{w_eff:.3f} "
                    f"x{mf:.2f} in {regime}")
                w = w_eff
            active_weight += w
            if t.direction == LONG:
                long_votes += 1
                score += w * conf
            else:
                short_votes += 1
                score -= w * conf

        direction = FLAT
        confidence = abs(score) / active_weight if active_weight > 0 else 0.0
        if active_weight <= 0:
            score = 0.0
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

        # funding HARD-block: extreme positive funding (FundingRateModel SHORT at
        # >= hard-block confidence) blocks LONG entries outright — crowded longs
        # get squeezed. A hard override AFTER aggregation, not a weight change.
        # SHORTs are never touched.
        if (self.funding_hard_block_enabled and direction == LONG
                and funding is not None and funding.direction == SHORT
                and funding.confidence >= self.funding_hard_block_conf):
            meta["block_reason"] = (
                f"funding_hard_block (funding_conf={funding.confidence:.2f})")
            log.info("%s LONG blocked — funding extreme SHORT conf=%.2f >= %.2f",
                     coin, funding.confidence, self.funding_hard_block_conf)
            direction = FLAT
            confidence = 0.0
        elif (self.funding_hard_block_short_enabled and direction == SHORT
                and funding is not None and funding.direction == LONG
                and funding.confidence >= self.funding_hard_block_short_conf):
            meta["block_reason"] = (
                f"funding_hard_block_short "
                f"(funding_conf={funding.confidence:.2f})")
            log.info("%s SHORT blocked — funding extreme LONG conf=%.2f >= %.2f",
                     coin, funding.confidence,
                     self.funding_hard_block_short_conf)
            direction = FLAT
            confidence = 0.0

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
            meta=meta,
        )
