#!/usr/bin/env python3
"""TAModel regime-aware trending relaxation test suite (2026-06-24).

Verifies the pure trending_rsi_vote() rule that powers the blend-mode relaxation:
  - RANGING / HIGH_VOL / UNKNOWN return None (no relaxation -> original blend),
  - TRENDING_DOWN fires SHORT at moderate RSI and LONG only at extreme oversold,
  - TRENDING_UP is the exact mirror,
  - confidence scales with RSI distance from the firing threshold,
  - the narrow neutral zone abstains (FLAT),
and a couple of end-to-end TAModel.compute() checks against synthetic candles
(trending regime -> a real directional vote where the old blend went FLAT).

Plain-script style (no pytest) to match scripts/test_aggregator.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.models import LONG, SHORT, FLAT
from reaper.models.ta_model import (TAModel, trending_rsi_vote,
                                    TRENDING_DEFAULTS)

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def vote(rsi, regime):
    return trending_rsi_vote(rsi, regime, **TRENDING_DEFAULTS)


# ===========================================================================
print("\n--- non-trending regimes: no relaxation (return None) ---")
check("RANGING + RSI 55 -> None (defers to blend, RSI 55 alone won't fire)",
      vote(55, "RANGING") is None, repr(vote(55, "RANGING")))
check("HIGH_VOL + RSI 55 -> None (conservative, no relaxation)",
      vote(55, "HIGH_VOL") is None, repr(vote(55, "HIGH_VOL")))
check("UNKNOWN + RSI 55 -> None", vote(55, "UNKNOWN") is None)
check("None regime + RSI 55 -> None", vote(55, None) is None)

# ===========================================================================
print("\n--- TRENDING_DOWN: trend-aligned SHORT at moderate RSI ---")
d55 = vote(55, "TRENDING_DOWN")
check("TRENDING_DOWN + RSI 55 -> SHORT", d55 is not None and d55[0] == SHORT,
      repr(d55))
check("TRENDING_DOWN + RSI 55 -> conf ~0.60",
      d55 is not None and 0.55 <= d55[1] <= 0.65, repr(d55))
d48 = vote(48, "TRENDING_DOWN")  # exactly at the firing threshold
check("TRENDING_DOWN + RSI 48 -> SHORT just cleared, conf ~0.40",
      d48 is not None and d48[0] == SHORT and 0.39 <= d48[1] <= 0.45, repr(d48))
d65 = vote(65, "TRENDING_DOWN")
check("TRENDING_DOWN + RSI 65 -> SHORT, higher conf than RSI 55",
      d65 is not None and d65[0] == SHORT and d65[1] > d55[1], repr(d65))
d75 = vote(75, "TRENDING_DOWN")
check("TRENDING_DOWN + RSI 75 -> SHORT conf saturates near 0.95",
      d75 is not None and d75[0] == SHORT and d75[1] >= 0.90, repr(d75))

print("\n--- TRENDING_DOWN: LONG only at extreme oversold ---")
d32 = vote(32, "TRENDING_DOWN")
check("TRENDING_DOWN + RSI 32 -> LONG (extreme oversold fires)",
      d32 is not None and d32[0] == LONG, repr(d32))
d42 = vote(42, "TRENDING_DOWN")  # between rsi_long(38) and rsi_short(48)
check("TRENDING_DOWN + RSI 42 -> FLAT (narrow neutral zone)",
      d42 is not None and d42[0] == FLAT, repr(d42))

# ===========================================================================
print("\n--- TRENDING_UP: exact mirror of TRENDING_DOWN ---")
u45 = vote(45, "TRENDING_UP")
check("TRENDING_UP + RSI 45 -> LONG (trend-aligned)",
      u45 is not None and u45[0] == LONG, repr(u45))
check("TRENDING_UP + RSI 45 -> conf ~0.55",
      u45 is not None and 0.50 <= u45[1] <= 0.65, repr(u45))
u68 = vote(68, "TRENDING_UP")
check("TRENDING_UP + RSI 68 -> SHORT (extreme overbought fires)",
      u68 is not None and u68[0] == SHORT, repr(u68))
u58 = vote(58, "TRENDING_UP")  # mirror of 42 -> neutral zone
check("TRENDING_UP + RSI 58 -> FLAT (narrow neutral zone)",
      u58 is not None and u58[0] == FLAT, repr(u58))

# mirror symmetry: TRENDING_UP at RSI r should mirror TRENDING_DOWN at 100-r
print("\n--- mirror symmetry ---")
mu = vote(45, "TRENDING_UP")
md = vote(55, "TRENDING_DOWN")
check("UP(45) confidence == DOWN(55) confidence (mirror)",
      mu is not None and md is not None and abs(mu[1] - md[1]) < 1e-9,
      f"up={mu} down={md}")

# ===========================================================================
print("\n--- damp/extreme bounds ---")
check("confidence never below 0.40",
      all(v is None or v[0] == FLAT or v[1] >= 0.40
          for v in (vote(r, "TRENDING_DOWN") for r in range(0, 101))))
check("confidence never above 0.95",
      all(v is None or v[1] <= 0.95
          for v in (vote(r, "TRENDING_DOWN") for r in range(0, 101))))

# ===========================================================================
# End-to-end: a synthetic strong downtrend. The classic blend cancels to FLAT
# (mean-reversion RSI/BB vs trend EMA/MACD); the regime-aware path must vote.
print("\n--- end-to-end compute(): downtrend that blend would FLAT ---")


class FakeBuf:
    """Minimal MarketBuffer stand-in: latest_candles + ctx (regime)."""
    def __init__(self, candles, regime):
        self._candles = candles
        self.ctx = {"BTC": {"regime": regime}}

    def latest_candles(self, coin, interval, n):
        return self._candles[-n:]


def make_candles(prices):
    out = []
    t = 1_700_000_000_000
    for i, p in enumerate(prices):
        o = prices[i - 1] if i else p
        hi = max(o, p) * 1.001
        lo = min(o, p) * 0.999
        out.append({"t": t + i * 60_000, "o": o, "h": hi, "l": lo,
                    "c": p, "v": 100.0})
    return out


# steady decline -> RegimeDetector would call TRENDING_DOWN; RSI sits moderate
# (~mid-40s/50s), exactly where the old blend abstained.
down_prices = [100.0 * (0.999 ** i) for i in range(80)]
ta = TAModel()

# force the regime context the bot would have published
buf_down = FakeBuf(make_candles(down_prices), "TRENDING_DOWN")
t_down = ta.compute("BTC", buf_down)
check("downtrend compute() does NOT abstain (regime relaxation active)",
      t_down.direction in (LONG, SHORT), f"{t_down.direction} {t_down.meta}")
check("downtrend compute() stamps regime_mode meta",
      t_down.meta.get("regime_mode") is True, str(t_down.meta))

# same candles but RANGING context -> original blend path (regime_mode absent)
buf_rng = FakeBuf(make_candles(down_prices), "RANGING")
t_rng = ta.compute("BTC", buf_rng)
check("RANGING compute() uses the original blend (no regime_mode meta)",
      t_rng.meta.get("regime_mode") is None, str(t_rng.meta))

# ===========================================================================
print("\n" + "=" * 44)
print(f"RESULT: {PASS}/{PASS + FAIL} TA model checks passed")
print("TA MODEL TEST: " + ("PASS" if FAIL == 0 else "FAIL"))
sys.exit(1 if FAIL else 0)
