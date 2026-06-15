# OI Decomposition Backtest — Results (Task 3)

_2026-06-14. Script:
[scripts/backtest_oi_decomposition.py](../scripts/backtest_oi_decomposition.py).
Raw: `data/backtest_oi_decomp_20260614.json`. ~192 days, 5m bars, BTC/ETH/SOL
(the coins with OI history)._

## Verdict

**The specific hypothesis tested (long-liquidation moves bounce) is NOT robust —
but a different, consistent signal falls out that corroborates the lead/lag
result.** Both findings are sub-fee.

- "long_liq" (px↓ + OI↓, exhaustible flush) was hypothesized to **bounce**. It
  does on **BTC** (+0.02–0.04% forward, up-rate 53–56%) but **fails on ETH and
  SOL** (price keeps falling, −0.02 to −0.05%). Pooled it's negative. **Not a
  reliable bounce signal.**
- The consistent pattern across **all three** coins is **new_shorts (px↓ + OI↑,
  fresh leveraged selling) → price recovers** (positive forward return, up-rate
  53–56%). This is a *fade-the-fresh-shorts* signal — the same "leverage-driven
  moves fade" conclusion as the spot-perp lead/lag test, reached independently.

## Method (locked before results)

For each 5m bar: `dpx` = perp 5m return, `doi` = OI 5m change. Require
|dpx|≥0.15% and |doi|≥0.10% (real moves only). Classify by joint sign; measure
**raw** forward perp return (+ = price rose) at 5/15/30/60m, plus P(fwd>0).
Thresholds fixed before running.

## Pooled results (BTC+ETH+SOL), raw forward perp return (avg / up-rate)

| class | meaning | +5m | +15m | +30m | +60m |
|-------|---------|-----|------|------|------|
| new_longs | px↑ OI↑ fresh buying | −0.003% / 45% | −0.005% / 46% | −0.006% / 48% | +0.003% / 47% |
| short_covering | px↑ OI↓ | −0.001% / 46% | −0.001% / 47% | −0.005% / 48% | −0.015% / 47% |
| **new_shorts** | px↓ OI↑ fresh selling | **+0.014% / 53%** | **+0.025% / 54%** | +0.015% / 53% | +0.017% / 52% |
| long_liq | px↓ OI↓ exhaustible | −0.005% / 51% | −0.010% / 53% | −0.003% / 53% | −0.021% / 52% |

Reading: after **fresh shorts** pile in on a dip, price tends to **rise** (fade
the shorts) — consistent and positive on all three coins. After a **long
liquidation** flush, direction is mixed (BTC up, ETH/SOL down) → no usable edge.

## Per-coin (long_liq, the hypothesized-bounce class), +30m

| Coin | long_liq +30m | supports bounce? |
|------|---------------|------------------|
| BTC  | +0.031% / 56% | yes |
| ETH  | −0.031% / 53% | no (keeps falling) |
| SOL  | −0.000% / 52% | no (flat) |

One of three is not a signal. The "exhaustible bounce" intuition does not
generalize.

## Cross-corroboration with lead/lag

| Signal source | "leverage-driven" class | forward behavior |
|---------------|-------------------------|------------------|
| spot-perp lead/lag | perp_leads, divergent | fade (price reverses) |
| OI decomposition | new_shorts (fresh leveraged selling) | fade (price recovers) |

Two independent decompositions of the same 192 days agree that **fresh
leverage-driven moves fade**. That mutual confirmation is more interesting than
either result alone.

## Caveats

- **Sub-fee.** new_shorts edge peaks at +0.025% (15m) vs 0.045% maker RT. Real
  but not tradeable alone — same class as OB imbalance and lead/lag.
- BTC/ETH/SOL only (OI history limited to these). The ~4 days of recorded ctx
  OI for the other 4 coins is too short for this analysis.
- Binance futures OI used (the only deep OI history available); HL OI may differ
  but should be broadly representative for majors.
- Overlapping forward windows → effective n < raw n; consistency across coins is
  the evidence, not raw sample size.

## Bottom line

The OI decomposition does not deliver a standalone trade, and it falsifies the
textbook "long liquidations bounce" claim (works on 1 of 3 coins). Its value is
the **independent corroboration** that leverage-driven moves fade — feeding the
same combined-filter idea proposed in the lead/lag report. `new_shorts` is the
cleanest of the four OI classes and the natural third factor (with perp_leads
and OB-imbalance-against) for a future stacked-fade backtest.
