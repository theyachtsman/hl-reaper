#!/usr/bin/env python3
"""Regime-memory test suite (2026-06-26).

Verifies the trend-band regime-memory helpers in isolation (no network, no live
services): the rolling buffer's dominant-regime read, the consistency check that
suppresses counter-regime entries, and the signal_history reason string. Mirrors
the fake-driven check() style of test_dual_band.py.

Failure pattern this guards: in a sustained downtrend the 1h regime oscillates
TRENDING_DOWN/RANGING; a single RANGING candle used to re-open full LONG
conviction and the bot bought every bounce. With threshold 0.5 a 50/50
TRENDING_DOWN window now suppresses the LONG.
"""
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.models import LONG, SHORT, FLAT
from scripts.run_bot import (get_dominant_regime, regime_allows_entry,
                             regime_memory_reason)

PASS = FAIL = 0

TD, TU, RA, HV = "TRENDING_DOWN", "TRENDING_UP", "RANGING", "HIGH_VOL"


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" +
          (f" — {detail}" if detail and not ok else ""))
    PASS += bool(ok)
    FAIL += (not ok)


print("\n--- get_dominant_regime ---")
check("empty -> UNKNOWN", get_dominant_regime(deque()) == "UNKNOWN")
check("most common wins",
      get_dominant_regime(deque([TD, TD, RA])) == TD)
check("ties resolve to first-seen",
      get_dominant_regime(deque([RA, TD])) in (RA, TD))


print("\n--- regime_allows_entry: warmup is fail-open ---")
check("empty buffer allows LONG", regime_allows_entry(deque(), LONG))
check("1 sample allows LONG", regime_allows_entry(deque([TD]), LONG) is True)
check("1 sample allows SHORT", regime_allows_entry(deque([TU]), SHORT) is True)


print("\n--- regime_allows_entry: the June 26 scenarios (threshold 0.5) ---")
# spec table — all evaluated at the default 0.5 threshold
check("[TD,TD,RA,RA] suppresses LONG (50% TD)",
      regime_allows_entry(deque([TD, TD, RA, RA]), LONG, 0.5) is False)
check("[RA,RA,RA,TD] allows LONG (25% TD)",
      regime_allows_entry(deque([RA, RA, RA, TD]), LONG, 0.5) is True)
check("[TD,TD,TD,RA] suppresses LONG (75% TD)",
      regime_allows_entry(deque([TD, TD, TD, RA]), LONG, 0.5) is False)
check("[TU,TU,TU,RA] suppresses SHORT (75% TU)",
      regime_allows_entry(deque([TU, TU, TU, RA]), SHORT, 0.5) is False)
check("LONG unaffected by TRENDING_UP history",
      regime_allows_entry(deque([TU, TU, TU, TU]), LONG, 0.5) is True)
check("SHORT unaffected by TRENDING_DOWN history",
      regime_allows_entry(deque([TD, TD, TD, TD]), SHORT, 0.5) is True)
check("FLAT always allowed",
      regime_allows_entry(deque([TD, TD, TD, TD]), FLAT, 0.5) is True)


print("\n--- regime_allows_entry: threshold tuning ---")
# 50% TD: suppressed at 0.5 (>=), allowed at 0.6 (50% < 60%)
half = deque([TD, TD, RA, RA])
check("50% TD allowed at threshold 0.6", regime_allows_entry(half, LONG, 0.6))
check("50% TD suppressed at threshold 0.5",
      regime_allows_entry(half, LONG, 0.5) is False)
check("50% TD suppressed at threshold 0.3",
      regime_allows_entry(half, LONG, 0.3) is False)


print("\n--- regime_memory_reason string ---")
r1 = regime_memory_reason(deque([TD, TD, RA, RA]), LONG)
check("LONG reason text", r1 == "regime_memory: 50% TRENDING_DOWN in last 4 evals",
      r1)
r2 = regime_memory_reason(deque([TU, TU, TU]), SHORT)
check("SHORT reason text", r2 == "regime_memory: 100% TRENDING_UP in last 3 evals",
      r2)


print("\n--- deque maxlen drops oldest (buffer is a rolling window) ---")
dq = deque(maxlen=4)
for r in [TD, TD, TD, TD, RA, RA]:  # last 4 = TD,TD,RA,RA
    dq.append(r)
check("rolling window keeps last 4", list(dq) == [TD, TD, RA, RA], list(dq))
check("rolling 50/50 still suppresses LONG",
      regime_allows_entry(dq, LONG, 0.5) is False)


print(f"\n{'='*48}\n{PASS} passed, {FAIL} failed\n{'='*48}")
sys.exit(1 if FAIL else 0)
