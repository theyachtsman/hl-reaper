#!/usr/bin/env python3
"""MomentumModel tests (2026-06-24, rewritten 2026-06-26).

Covers the volatility-normalized price-velocity model in isolation (direction,
confidence that SCALES with move steepness instead of railing, threshold edges,
candle ordering, insufficient history) plus one ensemble-integration check: a
strong SHORT momentum vote pulls the net verdict away from a confident LONG
during a simulated drop — the 6/24 failure mode the model exists to fix.

It also pins the 6/26 regression: a steady decline (the ETH 07:00-09:00 slide)
must NOT produce a confident LONG, and must read SHORT once the slide is real.

No network, no live services — a synthetic MarketBuffer + pure Ticket objects."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from reaper.aggregator import TREND_WEIGHTS, SCALP_WEIGHTS  # noqa: E402
from reaper.aggregator import SignalAggregator  # noqa: E402
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
    """Build a MarketBuffer holding `closes` as sequential candles, appended in
    list order. The buffer appends newest to the right, so closes[-1] is the
    NEWEST candle — the chronological (oldest-first) ordering the model reads."""
    buf = MarketBuffer([coin], [interval], maxlen=200)
    t0 = 1_700_000_000_000
    for i, c in enumerate(closes):
        buf.on_candle(coin, interval, {
            "t": t0 + i * 60_000, "o": c, "h": c, "l": c, "c": c, "v": 100.0,
        })
    return buf


def trend_series(drift: float, n: int = 30, noise: float = 0.0015,
                 base: float = 100.0):
    """n candles whose per-candle return is `drift` plus a small alternating
    ±noise wobble. The wobble gives a NONZERO, roughly constant volatility
    (pstdev ~ noise) so the z-score denominator is well defined, while the mean
    return `drift` sets the direction/steepness. Bigger |drift| -> bigger |z|."""
    closes = [base]
    for i in range(1, n):
        r = drift + (noise if i % 2 == 0 else -noise)
        closes.append(closes[-1] * (1 + r))
    return closes


# default config = the live trend-band tuning
m = MomentumModel(enter_z=0.6, full_conf_z=2.6, vol_window=14,
                  lookbacks=(1, 2, 3), min_candles=20)

print("MomentumModel — unit (volatility-normalized)")

# 1. upward momentum -> LONG, confidence SCALES with steepness (not railed)
up_gentle = m.compute("BTC", buf_from_closes(trend_series(+0.0012)))
up_steep = m.compute("BTC", buf_from_closes(trend_series(+0.0030)))
check("1a gentle rise votes LONG", up_gentle.direction == LONG, str(up_gentle))
check("1b steeper rise -> higher confidence",
      up_steep.confidence > up_gentle.confidence,
      f"{up_steep.confidence:.3f} !> {up_gentle.confidence:.3f}")
check("1c gentle-rise confidence is NOT railed at 0.95",
      up_gentle.confidence < 0.95, f"conf={up_gentle.confidence:.3f}")
check("1d meta exposes composite + z + vol",
      "composite" in up_gentle.meta and "z" in up_gentle.meta
      and "vol" in up_gentle.meta, str(up_gentle.meta))

# 2. downward momentum -> SHORT, same scaling
dn_gentle = m.compute("BTC", buf_from_closes(trend_series(-0.0012)))
dn_steep = m.compute("BTC", buf_from_closes(trend_series(-0.0030)))
check("2a gentle drop votes SHORT", dn_gentle.direction == SHORT, str(dn_gentle))
check("2b steeper drop -> higher confidence",
      dn_steep.confidence > dn_gentle.confidence,
      f"{dn_steep.confidence:.3f} !> {dn_gentle.confidence:.3f}")
check("2c gentle-drop confidence is NOT railed at 0.95",
      dn_gentle.confidence < 0.95, f"conf={dn_gentle.confidence:.3f}")

# 2e. a genuinely violent move DOES reach the 0.95 ceiling (cap still works)
violent = m.compute("BTC", buf_from_closes(trend_series(-0.010)))
check("2e violent drop rails to the 0.95 cap",
      abs(violent.confidence - 0.95) < 1e-9, f"conf={violent.confidence:.3f}")

# 3. flat / choppy series (zero drift, only noise) -> FLAT or low confidence
chop = m.compute("BTC", buf_from_closes(trend_series(0.0, noise=0.0015)))
check("3 flat chop is FLAT or low-conf",
      chop.direction == FLAT or chop.confidence < 0.40,
      f"{chop.direction}:{chop.confidence:.3f}")

# 4. REGRESSION — the real ETH 1h slide (06-25 12:00 .. 06-26 08:00/09:00 UTC).
# At 08:00 (the exact failure candle) the OLD 3/6/12 model voted LONG:0.95 into
# the drop because a 6h-stale bounce dominated. The fix must NOT vote LONG there,
# and must read SHORT once the slide is established (09:00).
eth_slide = [1634.80, 1536.70, 1567.60, 1569.30, 1561.00, 1573.80, 1566.10,
             1557.80, 1559.50, 1580.10, 1569.10, 1566.60, 1563.90, 1559.10,
             1523.30, 1556.90, 1548.50, 1554.80, 1570.10, 1580.00,  # ..07:00
             1565.60,                                               # 08:00 fail
             1552.20]                                               # 09:00
at_fail = m.compute("ETH", buf_from_closes(eth_slide[:-1], coin="ETH"))  # 08:00
at_slide = m.compute("ETH", buf_from_closes(eth_slide, coin="ETH"))      # 09:00
check("4a slide TOP (old LONG:0.95 failure) no longer votes LONG",
      at_fail.direction != LONG, f"{at_fail.direction}:{at_fail.confidence:.3f}")
check("4b established slide votes SHORT",
      at_slide.direction == SHORT, f"{at_slide.direction}:{at_slide.confidence:.3f}")

# 5. CANDLE ORDER — the codebase feeds candles oldest-first (newest last); the
# model reads close[-1] as 'now'. A rising chronological series must vote LONG;
# feeding the SAME values reversed (so the latest appended is the old high) must
# flip the sign. Documents/locks the expected ordering.
rising = trend_series(+0.0025)
order_ok = m.compute("BTC", buf_from_closes(rising))
order_rev = m.compute("BTC", buf_from_closes(list(reversed(rising))))
check("5a oldest-first rising series votes LONG (documented ordering)",
      order_ok.direction == LONG, str(order_ok))
check("5b reversed ordering flips the sign to SHORT",
      order_rev.direction == SHORT, str(order_rev))

# 6. insufficient candles (< min_candles) -> FLAT, no error
short_hist = m.compute("BTC", buf_from_closes([100.0] * 10))
check("6 <min_candles -> FLAT (no raise)",
      short_hist.direction == FLAT
      and short_hist.meta.get("reason") == "insufficient_candles",
      str(short_hist.meta))

# 6b. zero-volatility (perfectly flat) history -> FLAT, no divide-by-zero
flat_hist = m.compute("BTC", buf_from_closes([100.0] * 30))
check("6b zero-volatility history -> FLAT (no div0)",
      flat_hist.direction == FLAT, str(flat_hist.meta))


print("MomentumModel — ensemble integration")

# 7. simulate the 6/24 freefall: MEANREV / VWAP / BOOK read the oversold drop as
# a LONG bounce while MomentumModel reads the velocity as a high-conf SHORT.
# Adding momentum must pull the net verdict AWAY from a confident LONG.
agg = SignalAggregator()
drop_buf = buf_from_closes(trend_series(-0.006))   # hard drop
mom_ticket = m.compute("BTC", drop_buf)
check("7a model reads a hard drop as a high-conf SHORT",
      mom_ticket.direction == SHORT and mom_ticket.confidence > 0.50,
      str(mom_ticket))


def signed(sig):
    return sig.confidence if sig.direction == LONG else (
        -sig.confidence if sig.direction == SHORT else 0.0)


long_voters = [
    Ticket("RegimeDetectorModel", "TRENDING_DOWN", 0.5),
    Ticket("VWAPModel", LONG, 0.65),
    Ticket("OrderbookImbalanceModel", LONG, 0.55),
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

# 8. RANGING regime damp still fires in RANGING only (aggregator wiring intact).
ranging_voters = [Ticket("RegimeDetectorModel", "RANGING", 0.5), mom_ticket]
trending_voters = [Ticket("RegimeDetectorModel", "TRENDING_DOWN", 0.5), mom_ticket]
rang = agg.aggregate("BTC", ranging_voters, weights=SCALP_WEIGHTS,
                     regime_routing=False)
trend = agg.aggregate("BTC", trending_voters, weights=SCALP_WEIGHTS,
                      regime_routing=False)
check("8a RANGING regime records momentum weight dampening",
      "momentum_dampen" in rang.meta, str(rang.meta))
check("8b TRENDING regime does NOT dampen momentum",
      "momentum_dampen" not in trend.meta, str(trend.meta))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
