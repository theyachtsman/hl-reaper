#!/usr/bin/env python3
"""Aggregator test suite — regime-aware FundingRate weight dampening (2026-06-23).

Verifies that a counter-1h-trend FundingRateModel vote has its aggregator WEIGHT
cut to `funding_counter_trend_damp` (numerator AND denominator), while:
  - trend-aligned funding keeps full weight,
  - RANGING / HIGH_VOL / UNKNOWN / None regimes keep full weight,
  - every other model is untouched,
  - the net counter-trend confidence rises vs the undamped case.

Fake-ticket driven, no network — mirrors test_dual_band.py style.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.aggregator import (SCALP_WEIGHTS, TREND_WEIGHTS, SignalAggregator,
                               FUNDING_COUNTER_TREND_DAMP)
from reaper.models import LONG, SHORT, FLAT, Ticket

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def tk(model, direction, conf):
    return Ticket(model, direction, conf, {})


FUND = "FundingRateModel"
BOOK = "OrderbookImbalanceModel"

agg = SignalAggregator()
D = FUNDING_COUNTER_TREND_DAMP  # 0.40

# ===========================================================================
print("\n--- funding_weight_factor: counter-trend dampens to the factor ---")
# 1. TRENDING_DOWN + FUNDING LONG -> dampened
check("TRENDING_DOWN + FUNDING LONG -> x0.40",
      abs(agg.funding_weight_factor(FUND, LONG, "TRENDING_DOWN") - D) < 1e-9,
      str(agg.funding_weight_factor(FUND, LONG, "TRENDING_DOWN")))
# 2. TRENDING_UP + FUNDING SHORT -> dampened
check("TRENDING_UP + FUNDING SHORT -> x0.40",
      abs(agg.funding_weight_factor(FUND, SHORT, "TRENDING_UP") - D) < 1e-9,
      str(agg.funding_weight_factor(FUND, SHORT, "TRENDING_UP")))

print("\n--- funding_weight_factor: RANGING/HIGH_VOL keep full weight ---")
# 3/4. RANGING keeps funding at full weight either direction (squeeze setups)
check("RANGING + FUNDING LONG -> x1.0",
      agg.funding_weight_factor(FUND, LONG, "RANGING") == 1.0)
check("RANGING + FUNDING SHORT -> x1.0",
      agg.funding_weight_factor(FUND, SHORT, "RANGING") == 1.0)
check("HIGH_VOL + FUNDING LONG -> x1.0",
      agg.funding_weight_factor(FUND, LONG, "HIGH_VOL") == 1.0)
check("UNKNOWN/None regime -> x1.0",
      agg.funding_weight_factor(FUND, LONG, "UNKNOWN") == 1.0
      and agg.funding_weight_factor(FUND, LONG, None) == 1.0)

print("\n--- funding_weight_factor: trend-aligned funding keeps full weight ---")
# 5. TRENDING_DOWN + FUNDING SHORT -> agrees with trend, no dampening
check("TRENDING_DOWN + FUNDING SHORT -> x1.0 (agrees)",
      agg.funding_weight_factor(FUND, SHORT, "TRENDING_DOWN") == 1.0)
# 6. TRENDING_UP + FUNDING LONG -> agrees with trend, no dampening
check("TRENDING_UP + FUNDING LONG -> x1.0 (agrees)",
      agg.funding_weight_factor(FUND, LONG, "TRENDING_UP") == 1.0)

print("\n--- only FundingRateModel is dampened ---")
check("BOOK counter-trend -> x1.0 (funding factor leaves other models alone)",
      agg.funding_weight_factor(BOOK, LONG, "TRENDING_DOWN") == 1.0)
check("TA counter-trend -> x1.0",
      agg.funding_weight_factor("TAModel", SHORT, "TRENDING_UP") == 1.0)

print("\n--- damp factor is configurable ---")
agg2 = SignalAggregator(funding_counter_trend_damp=0.0)
check("damp=0.0 -> counter-trend funding fully ignored",
      agg2.funding_weight_factor(FUND, LONG, "TRENDING_DOWN") == 0.0)
agg3 = SignalAggregator(funding_counter_trend_damp=1.0)
check("damp=1.0 -> no dampening even counter-trend",
      agg3.funding_weight_factor(FUND, LONG, "TRENDING_DOWN") == 1.0)

# ===========================================================================
# 7. Net SHORT confidence in a TRENDING_DOWN scenario: dampened > undamped.
#    FUNDING LONG 0.95 fights two SHORT voters; dampening its weight shrinks the
#    cancellation so the net SHORT conviction rises.
print("\n--- TRENDING_DOWN scenario: dampening lifts SHORT confidence ---")
tickets = [
    tk("RegimeDetectorModel", "TRENDING_DOWN", 1.0),
    tk(FUND, LONG, 0.95),     # crowd-fade vote, persistent in the downtrend
    tk(BOOK, SHORT, 0.78),    # trend-aligned (no BOOK dampening)
    tk("VWAPModel", SHORT, 0.55),
    tk("TAModel", FLAT, 0.0),
]
undamped = agg.aggregate("BTC", tickets, weights=TREND_WEIGHTS,
                         regime_routing=False, book_regime=None)
damped = agg.aggregate("BTC", tickets, weights=TREND_WEIGHTS,
                       regime_routing=False, book_regime="TRENDING_DOWN")
check("undamped verdict is SHORT", undamped.direction == SHORT,
      undamped.direction)
check("damped verdict is SHORT", damped.direction == SHORT, damped.direction)
check("dampened SHORT confidence > undamped",
      damped.confidence > undamped.confidence,
      f"damped={damped.confidence:.4f} undamped={undamped.confidence:.4f}")
check("dampened run records funding_dampen meta",
      "funding_dampen" in damped.meta, str(damped.meta))
check("undamped run records NO funding_dampen meta",
      "funding_dampen" not in undamped.meta, str(undamped.meta))

print("\n--- RANGING scenario: funding keeps full weight (no meta, lower SHORT) ---")
ranging = agg.aggregate("BTC", [
    tk("RegimeDetectorModel", "RANGING", 1.0),
    tk(FUND, LONG, 0.95),
    tk(BOOK, SHORT, 0.78),
    tk("VWAPModel", SHORT, 0.55),
], weights=TREND_WEIGHTS, regime_routing=False, book_regime="RANGING")
check("RANGING: no funding dampening applied",
      "funding_dampen" not in ranging.meta, str(ranging.meta))

print("\n--- trend-aligned funding is never dampened in aggregate() ---")
aligned = agg.aggregate("BTC", [
    tk("RegimeDetectorModel", "TRENDING_DOWN", 1.0),
    tk(FUND, SHORT, 0.95),    # agrees with the downtrend
    tk(BOOK, SHORT, 0.78),
], weights=TREND_WEIGHTS, regime_routing=False, book_regime="TRENDING_DOWN")
check("TRENDING_DOWN + FUNDING SHORT: no dampening meta",
      "funding_dampen" not in aligned.meta, str(aligned.meta))

# ===========================================================================
print("\n" + "=" * 40)
print(f"RESULT: {PASS}/{PASS + FAIL} aggregator checks passed")
print("AGGREGATOR TEST: " + ("PASS" if FAIL == 0 else "FAIL"))
sys.exit(1 if FAIL else 0)
