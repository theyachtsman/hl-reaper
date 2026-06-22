#!/usr/bin/env python3
"""FundingRateModel mapping test (2026-06-20 continuous rework).

Verifies the smoothed funding -> direction/confidence mapping: positive funding
leans SHORT, negative leans LONG, the neutral band is FLAT, confidence is
monotonic in magnitude, and the funding hard-block still fires at the extreme
but NOT at mild-positive funding. No network — pure function + a tiny aggregator
integration. Mirrors run_phase2_test.py style."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.models import FLAT, LONG, SHORT, Ticket
from reaper.models.funding_rate import (
    funding_direction, funding_direction_binary, FundingRateModel,
    FUNDING_NEUTRAL_BAND, FUNDING_EXTREME)
from reaper.aggregator import SignalAggregator

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" +
          (f" — {detail}" if detail and not ok else ""))
    PASS += ok
    FAIL += not ok


# ---------------------------------------------------------------------------
print("\n--- direction + neutral band ---")
d, c, z = funding_direction(0.0)
check("zero funding -> FLAT", d == FLAT and c == 0.0)
d, c, z = funding_direction(FUNDING_NEUTRAL_BAND)          # +0.0001 edge
check("at +neutral edge -> still FLAT", d == FLAT, f"{d} {z}")
d, c, z = funding_direction(-FUNDING_NEUTRAL_BAND)
check("at -neutral edge -> still FLAT", d == FLAT)

d, c, z = funding_direction(0.0005)                       # mild positive
check("mild positive -> SHORT", d == SHORT, f"{d}")
d, c, z = funding_direction(-0.0005)                      # mild negative
check("mild negative -> LONG", d == LONG, f"{d}")

print("\n--- confidence ramp ---")
# just above the band -> floor ~0.30
d, c, _ = funding_direction(FUNDING_NEUTRAL_BAND + 1e-9)
check("just above band -> conf ~0.30 (well below 0.75)",
      0.29 <= c <= 0.31 and c < 0.75, f"conf={c:.3f}")
# midpoint of the ramp -> ~0.60
mid = FUNDING_NEUTRAL_BAND + 0.5 * (FUNDING_EXTREME - FUNDING_NEUTRAL_BAND)
d, c, _ = funding_direction(mid)
check("ramp midpoint -> conf ~0.60", abs(c - 0.60) < 0.01, f"conf={c:.3f}")
# spec's example: 0.0005 -> SHORT ~0.6
d, c, _ = funding_direction(0.0005)
check("0.0005 -> SHORT conf roughly 0.6", d == SHORT and 0.50 <= c <= 0.65,
      f"conf={c:.3f}")
# extreme -> >= 0.90, capped
d, c, z = funding_direction(FUNDING_EXTREME)
check("at extreme +0.001 -> SHORT conf >= 0.90", d == SHORT and c >= 0.90 - 1e-9,
      f"conf={c:.6f}")
check("at extreme -> zone extreme_positive", z == "extreme_positive", z)
d, c, _ = funding_direction(0.005)                        # well past extreme
check("past extreme -> conf capped at 0.90 (pre-boost)", abs(c - 0.90) < 1e-9,
      f"conf={c:.3f}")

print("\n--- monotonicity in magnitude ---")
pos = [0.00011, 0.0002, 0.0004, 0.0006, 0.0008, 0.001, 0.002]
confs = [funding_direction(r)[1] for r in pos]
check("SHORT side: confidence non-decreasing in magnitude",
      all(b >= a - 1e-12 for a, b in zip(confs, confs[1:])), str(confs))
check("SHORT side: all SHORT", all(funding_direction(r)[0] == SHORT for r in pos))
neg = [-r for r in pos]
nconfs = [funding_direction(r)[1] for r in neg]
check("LONG side: confidence non-decreasing in magnitude",
      all(b >= a - 1e-12 for a, b in zip(nconfs, nconfs[1:])), str(nconfs))
check("LONG side: all LONG", all(funding_direction(r)[0] == LONG for r in neg))
check("symmetry: +x and -x give equal confidence",
      all(abs(funding_direction(r)[1] - funding_direction(-r)[1]) < 1e-12
          for r in pos))

print("\n--- hard-block threshold preserved (conf >= 0.75 only near extreme) ---")
# confidence crosses the 0.75 hard-block threshold only in the upper part of the
# ramp — near the extreme, never at mild-positive band edge.
edge_conf = funding_direction(FUNDING_NEUTRAL_BAND + 1e-9)[1]
check("hard-block does NOT trigger at band edge (conf < 0.75)", edge_conf < 0.75)
check("hard-block triggers at extreme (conf >= 0.75)",
      funding_direction(FUNDING_EXTREME)[1] >= 0.75)
# the crossover point should sit in the upper ~75% of the ramp (near extreme)
cross = next(r for r in [FUNDING_NEUTRAL_BAND + i * 1e-5 for i in range(1, 100)]
             if funding_direction(r)[1] >= 0.75)
frac = (cross - FUNDING_NEUTRAL_BAND) / (FUNDING_EXTREME - FUNDING_NEUTRAL_BAND)
check("0.75 crossover is in the upper half of the ramp (near extreme)",
      frac >= 0.5, f"crossover at {cross:.6f} ({frac:.0%} of ramp)")

print("\n--- aggregator integration: hard-block fires/doesn't ---")
agg = SignalAggregator(funding_hard_block_conf=0.75)


def tk(model, direction, conf=0.6):
    return Ticket(model, direction, conf, {})


# build a LONG-leaning ensemble + a funding SHORT vote, vary funding strength
def ensemble(funding_rate):
    fd, fc, _ = funding_direction(funding_rate)
    return [tk("TAModel", LONG, 0.7), tk("OrderbookImbalanceModel", LONG, 0.7),
            tk("VWAPModel", LONG, 0.6), tk("MeanReversionModel", FLAT, 0.0),
            Ticket("FundingRateModel", fd, fc, {})]


# extreme positive funding -> funding SHORT conf >= 0.75 -> LONG hard-blocked
sig = agg.aggregate("BTC", ensemble(FUNDING_EXTREME))
check("extreme +funding hard-blocks the LONG ensemble (-> FLAT)",
      sig.direction == FLAT and "funding_hard_block" in str(sig.meta),
      f"dir={sig.direction} meta={sig.meta}")
# mild positive funding -> funding SHORT conf < 0.75 -> NOT hard-blocked
sig2 = agg.aggregate("BTC", ensemble(0.0003))
check("mild +funding does NOT hard-block the LONG ensemble",
      "funding_hard_block" not in str(sig2.meta),
      f"meta={sig2.meta}")

print("\n--- diagnostic-window sanity (what the new model would vote) ---")
window = {"BTC": -0.000239, "SOL": 0.000100, "ETH": 0.000692}
for coin, rate in window.items():
    d, c, z = funding_direction(rate)
    print(f"  {coin}: rate_8h={rate:+.6f} -> {d} conf={c:.3f} ({z})")
check("BTC slightly-negative still leans LONG",
      funding_direction(window["BTC"])[0] == LONG)
check("SOL +0.0001 -> FLAT (at neutral edge, no longer a forced LONG)",
      funding_direction(window["SOL"])[0] == FLAT)
check("ETH +0.0007 now leans SHORT (was LONG before the fix)",
      funding_direction(window["ETH"])[0] == SHORT)
check("ETH +0.0007 SHORT conf below hard-block (mild, not extreme)",
      funding_direction(window["ETH"])[1] < 0.75,
      f"conf={funding_direction(window['ETH'])[1]:.3f}")

print("\n--- binary fallback mapping (unchanged old behavior) ---")
# the pre-2026-06-20 rule: mild-positive -> LONG, SHORT only past 0.001 extreme
d, c, z = funding_direction_binary(0.0005)
check("binary: mild positive (0.0005) -> LONG 0.55 (old behavior)",
      d == LONG and abs(c - 0.55) < 1e-9, f"{d} {c}")
d, c, z = funding_direction_binary(0.0015)
check("binary: extreme positive -> SHORT", d == SHORT and z == "extreme_positive")
d, c, z = funding_direction_binary(-0.0008)
check("binary: strong negative -> LONG", d == LONG and z == "extreme_negative")
d, c, z = funding_direction_binary(0.0)
check("binary: zero -> FLAT", d == FLAT)
# the key divergence: same mild-positive input, opposite directions
check("smooth vs binary diverge on mild-positive (SHORT vs LONG)",
      funding_direction(0.0005)[0] == SHORT
      and funding_direction_binary(0.0005)[0] == LONG)


print("\n--- model flag selects mapping + stamps meta ---")
class FakeDB:
    def funding_window(self, coin, since): return []


class FakeBuf:
    def __init__(self, hourly): self.ctx = {"ETH": {"funding": hourly}}


eth_mild = 0.0007 / 8  # hourly; *8 -> +0.0007/8h
m_default = FundingRateModel(FakeDB())
check("model default is binary (smooth_mapping False)",
      m_default.smooth_mapping is False)
t = m_default.compute("ETH", FakeBuf(eth_mild))
check("default model -> LONG on mild+ (binary fallback)", t.direction == LONG,
      f"{t.direction}")
check("default model stamps meta mapping=binary", t.meta.get("mapping") == "binary")

m_smooth = FundingRateModel(FakeDB(), smooth_mapping=True)
t2 = m_smooth.compute("ETH", FakeBuf(eth_mild))
check("smooth model -> SHORT on mild+", t2.direction == SHORT, f"{t2.direction}")
check("smooth model stamps meta mapping=smooth", t2.meta.get("mapping") == "smooth")

# flipping the flag on a live instance switches behavior (hot-reload path)
m_default.smooth_mapping = True
t3 = m_default.compute("ETH", FakeBuf(eth_mild))
check("flipping flag live -> SHORT (hot-reload behavior)", t3.direction == SHORT)

# ---------------------------------------------------------------------------
print("\n" + "=" * 40)
print(f"RESULT: {PASS}/{PASS + FAIL} funding mapping checks passed")
print("FUNDING MAPPING TEST:", "PASS" if FAIL == 0 else "FAIL")
sys.exit(0 if FAIL == 0 else 1)
