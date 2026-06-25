#!/usr/bin/env python3
"""MomentumModel tests (2026-06-24).

Covers the price-velocity model in isolation (direction + confidence ramp,
threshold edges, insufficient history) and one ensemble-integration check: a
strong SHORT momentum vote pulls the net verdict away from a confident LONG
during a simulated -2% drop — the 6/24 failure mode the model exists to fix.

No network, no live services — a synthetic MarketBuffer + pure Ticket objects.
Mirrors test_long_filters.py / test_dual_band.py style."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from reaper.aggregator import (SCALP_WEIGHTS, TREND_WEIGHTS,  # noqa: E402
                               SignalAggregator)
from reaper.data.buffer import MarketBuffer  # noqa: E402
from reaper.models import LONG, SHORT, FLAT, Ticket  # noqa: E402
from reaper.models.momentum_model import MomentumModel  # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" +
          (f" — {detail}" if detail and not ok else ""))
    PASS += ok
    FAIL += not ok


def buf_from_closes(closes, coin="BTC", interval="1m") -> MarketBuffer:
    """Build a MarketBuffer holding `closes` as sequential candles. o/h/l track
    close, volume is constant — only close matters for the ROC math."""
    buf = MarketBuffer([coin], [interval], maxlen=200)
    t0 = 1_700_000_000_000
    for i, c in enumerate(closes):
        buf.on_candle(coin, interval, {
            "t": t0 + i * 60_000, "o": c, "h": c, "l": c, "c": c, "v": 100.0,
        })
    return buf


def closes_with_final_move(x: float, base: float = 100.0, n: int = 20):
    """n-1 flat candles at `base`, final candle = base*(1+x). With a flat
    history every ROC window (3/6/12) measures exactly `x`, so the weighted
    composite equals `x` — deterministic confidence control."""
    return [base] * (n - 1) + [base * (1 + x)]


THR = 0.003          # default short/long threshold magnitude
FULL = 0.010         # default full-confidence move
m = MomentumModel(short_threshold=-THR, long_threshold=THR, full_conf_move=FULL,
                  min_candles=15)


def conf_for(x):
    """Expected ramp confidence for composite == x (matches the model math)."""
    span = FULL - THR
    return min(0.95, max(0.0, (abs(x) - THR) / span))


print("MomentumModel — unit")

# 1. strong downward momentum -> SHORT, confidence proportional to move size
t_small = m.compute("BTC", buf_from_closes(closes_with_final_move(-0.005)))
t_big = m.compute("BTC", buf_from_closes(closes_with_final_move(-0.008)))
check("1a strong drop votes SHORT", t_small.direction == SHORT, str(t_small))
check("1b bigger drop -> higher confidence",
      t_big.confidence > t_small.confidence,
      f"{t_big.confidence:.3f} !> {t_small.confidence:.3f}")
check("1c confidence matches the ramp",
      abs(t_small.confidence - conf_for(-0.005)) < 1e-6,
      f"{t_small.confidence:.4f} vs {conf_for(-0.005):.4f}")
check("1d meta exposes ROC + composite",
      t_small.meta.get("composite") == round(-0.005 * 100, 3)
      and "roc_3" in t_small.meta and "roc_12" in t_small.meta, str(t_small.meta))

# 2. strong upward momentum -> LONG, confidence proportional to move size
u_small = m.compute("BTC", buf_from_closes(closes_with_final_move(0.005)))
u_big = m.compute("BTC", buf_from_closes(closes_with_final_move(0.008)))
check("2a strong pump votes LONG", u_small.direction == LONG, str(u_small))
check("2b bigger pump -> higher confidence",
      u_big.confidence > u_small.confidence,
      f"{u_big.confidence:.3f} !> {u_small.confidence:.3f}")

# 3. weak move inside the threshold band -> FLAT
weak = m.compute("BTC", buf_from_closes(closes_with_final_move(-0.001)))
check("3 weak move (-0.1%) is FLAT", weak.direction == FLAT, str(weak))

# 4. just past threshold -> a real but LOW-confidence vote
edge = m.compute("BTC", buf_from_closes(closes_with_final_move(-0.0044)))
check("4a just past threshold still votes SHORT", edge.direction == SHORT, str(edge))
check("4b just-past-threshold confidence is low (<0.30)",
      0.0 < edge.confidence < 0.30, f"conf={edge.confidence:.3f}")

# 5. at full_conf_move -> confidence pinned to the 0.95 ceiling
full = m.compute("BTC", buf_from_closes(closes_with_final_move(-FULL)))
check("5 composite == full_conf_move -> conf ~0.95",
      abs(full.confidence - 0.95) < 1e-6, f"conf={full.confidence:.4f}")

# 6. insufficient candles (< min_candles) -> FLAT, no error
short_hist = m.compute("BTC", buf_from_closes([100.0] * 10))
check("6 <15 candles -> FLAT (no raise)",
      short_hist.direction == FLAT
      and short_hist.meta.get("reason") == "insufficient_candles",
      str(short_hist.meta))

# extra: exactly at threshold -> no edge -> FLAT (ramp value is 0 there)
at_thr = m.compute("BTC", buf_from_closes(closes_with_final_move(-THR)))
check("6b exactly at threshold -> FLAT (zero edge)",
      at_thr.direction == FLAT, str(at_thr))


print("MomentumModel — ensemble integration")

# 7. simulate the 6/24 freefall: MEANREV / VWAP / BOOK read the oversold drop
# as a LONG bounce while MomentumModel reads the -2% velocity as SHORT 0.95.
# Adding momentum must pull the net verdict AWAY from a confident LONG.
agg = SignalAggregator()
drop_buf = buf_from_closes(closes_with_final_move(-0.02))   # -2% velocity
mom_ticket = m.compute("BTC", drop_buf)
check("7a model reads -2% drop as a high-conf SHORT",
      mom_ticket.direction == SHORT and mom_ticket.confidence > 0.90,
      str(mom_ticket))


def signed(sig):
    """+conf for a LONG verdict, -conf for SHORT, 0 for FLAT — a single axis
    so 'shift toward SHORT' is just 'smaller number'."""
    return sig.confidence if sig.direction == LONG else (
        -sig.confidence if sig.direction == SHORT else 0.0)


# trend band — momentum carries its heaviest weight (0.20) there
long_voters = [
    Ticket("RegimeDetectorModel", "TRENDING_DOWN", 0.5),
    Ticket("VWAPModel", LONG, 0.65),         # below VWAP -> bounce read
    Ticket("OrderbookImbalanceModel", LONG, 0.55),  # bids at support
    Ticket("TAModel", FLAT, 0.0),
    Ticket("FundingRateModel", FLAT, 0.0),
]
without = agg.aggregate("BTC", long_voters, weights=TREND_WEIGHTS,
                        regime_routing=False)
with_mom = agg.aggregate("BTC", long_voters + [mom_ticket],
                         weights=TREND_WEIGHTS, regime_routing=False)
check("7b without momentum the ensemble calls a confident LONG",
      without.direction == LONG and without.confidence >= 0.40,
      f"{without.direction} {without.confidence:.3f}")
check("7c adding momentum shifts the net toward SHORT",
      signed(with_mom) < signed(without),
      f"signed {signed(with_mom):.3f} !< {signed(without):.3f}")
check("7d momentum kills the confident-LONG entry (not a clean LONG fire)",
      not (with_mom.direction == LONG and with_mom.confidence >= 0.49),
      f"{with_mom.direction} {with_mom.confidence:.3f}")

# 8. RANGING regime damp: the same SHORT momentum vote carries less weight in a
# RANGING regime than in a TRENDING one (noisier — false breakouts).
ranging_voters = [Ticket("RegimeDetectorModel", "RANGING", 0.5), mom_ticket]
trending_voters = [Ticket("RegimeDetectorModel", "TRENDING_DOWN", 0.5), mom_ticket]
rang = agg.aggregate("BTC", ranging_voters, weights=SCALP_WEIGHTS,
                     regime_routing=False)
trend = agg.aggregate("BTC", trending_voters, weights=SCALP_WEIGHTS,
                      regime_routing=False)
# momentum is the only voter, so confidence == its own conf either way (it's the
# whole active weight); the damp shows up in the published meta + weight, not the
# lone-voter confidence. Assert the damp meta fires in RANGING only.
check("8a RANGING regime records momentum weight dampening",
      "momentum_dampen" in rang.meta, str(rang.meta))
check("8b TRENDING regime does NOT dampen momentum",
      "momentum_dampen" not in trend.meta, str(trend.meta))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
