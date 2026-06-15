# Spot-Perp Lead/Lag Backtest — Results (Task 2)

_2026-06-14. Script:
[scripts/backtest_spot_perp_leadlag.py](../scripts/backtest_spot_perp_leadlag.py).
Raw: `data/backtest_leadlag_20260614.json`. ~225 days, 1m, 7 coins._

## Verdict

**Hypothesis CONFIRMED directionally — but the edge is SUB-FEE.** This is the
first hypothesis tested in the whole project that comes out with the *predicted
sign, consistently, across coins and time windows*:

- **spot-led moves continue**, **perp-led moves fade**, **divergent moves fade
  hardest** — exactly as the "real demand vs leverage-driven" framing predicts.

But the magnitudes (~0.005–0.035% forward) sit **below the ~0.045% maker
round-trip fee**, so it is not tradeable standalone. Same verdict class as the
OB-imbalance finding: *real signal, too small to trade alone, usable as a
combination/filter input.* The directional **hit rate** is the more promising
artifact than the average return (see below).

## Method (locked before results)

perp = Binance USD-M futures close (HL-perp proxy; HL oracle tracks CEX), spot =
Binance spot close, aligned on the shared 1m grid. At each minute, over lookback
N: classify by sign agreement + the ratio |spot_ret|/|perp_ret| (band 0.83/1.2),
requiring a real move (≥0.05%/0.15%/0.30% for N=1/5/15). Forward return measured
on perp, **continuation-signed** (>0 = price kept going in the move's direction,
<0 = reversed). Thresholds fixed in the script and not tuned after seeing output.

## Pooled results (all 7 coins)

Continuation-signed forward perp return (avg / win-rate). spot_leads expects
`>0`; perp_leads expects `<0`.

| N | class | +5m | +15m | +30m | +60m |
|---|-------|-----|------|------|------|
| 1m | spot_leads | +0.005% / 46% | +0.005% / 47% | +0.004% / 48% | +0.005% / 48% |
| 1m | perp_leads | **−0.013% / 41%** | −0.009% / 45% | −0.011% / 46% | −0.009% / 47% |
| 1m | aligned | −0.003% / 46% | −0.001% / 47% | −0.001% / 48% | −0.001% / 48% |
| 1m | divergent | **−0.023% / 38%** | −0.018% / 43% | −0.021% / 45% | −0.018% / 46% |
| 5m | spot_leads | +0.006% / 45% | +0.005% / 46% | +0.005% / 47% | +0.007% / 48% |
| 5m | perp_leads | −0.013% / 39% | −0.011% / 43% | −0.012% / 45% | −0.012% / 46% |
| 15m | spot_leads | +0.005% / 44% | +0.001% / 45% | +0.001% / 46% | +0.008% / 47% |
| 15m | perp_leads | −0.012% / 40% | −0.019% / 42% | −0.017% / 44% | −0.001% / 45% |

The sign structure is remarkably stable: **spot_leads positive, perp_leads
negative, divergent most negative**, at every horizon and every lookback. The
spot_leads−perp_leads spread at +5m is ~0.018%.

## The hit-rate is the real signal

Average returns are tiny because the occasional large continuation drags the
mean. The directional consistency is stronger:

- **Fading a perp-led move wins ~59% of the time** (pooled win 41% = 59% reversal)
  at +5m, N=1. On ARB it's ~65–66%.
- **Fading a divergent move wins ~62%** pooled at +5m (38% continuation).
- spot_leads continuation is the weakest leg (~46% — barely better than the
  ~46% aligned baseline), i.e. the *fade* side carries most of the signal, not
  the *continuation* side.

## Per-coin notes

- **ARB has the cleanest, largest spread** by far: perp_leads −0.034%/35% win and
  divergent −0.051%/29% win at +5m (N=1). ARB divergent fade (+0.051% gross) is
  the *only* cell that clears the 0.045% maker fee — but divergent is rare/illiquid
  and real slippage on ARB would likely eat the margin.
- **BTC/ETH/SOL/DOGE have ~zero divergent samples** at 1m — liquid pairs are too
  tightly arbitraged for spot/futures to disagree in sign. Divergence (and the
  largest fade edges) lives in the lower-liquidity names (ARB/AVAX/WIF), exactly
  where execution cost is worst. This is the central tension.
- AVAX is the one coin where perp_leads is mildly *positive* (continuation) — the
  signal is weakest/noisiest there.

## Caveats (honest)

- **Sub-fee.** Every pooled cell is < 0.045% maker RT in magnitude. Not tradeable
  alone. Reported as a "real but needs combination" finding, not a go.
- **Overlapping samples.** 1m spacing + overlapping forward windows mean the
  effective independent n is far below the raw counts; don't read the big n as
  big statistical power. The real evidence is the *consistency across 7
  independent coins × 3 windows*, not any single t-stat.
- **Proxy.** Binance futures-vs-spot stands in for HL-perp-vs-spot. Lead/lag
  between a leveraged venue and spot is a general structural effect, but HL's own
  perp could differ; live recorded HL data would be the confirmation.

## Bottom line / path forward

This is the **best-corroborated directional signal found to date** — and it's
independently echoed by the OI-decomposition test
([oi_decomposition_backtest.md](oi_decomposition_backtest.md)), where
fresh-leveraged-selling (px↓ + OI↑) also fades. Two independent decompositions
agree: **leverage-driven moves fade, real-demand moves continue.**

It is sub-fee per 1-bar event, so the only viable use is as a **factor in a
combined filter** alongside OB imbalance (the other real-but-sub-fee signal),
not as a standalone strategy. Concretely: a "fade" entry that requires
*agreement* of perp-led + OB-imbalance-against + (optionally) fresh-shorts could
stack three weak edges — worth a follow-up combined backtest, but only on the
*liquid* coins where execution is cheap, which is unfortunately where each
individual edge is smallest. Net: promising direction, no standalone trade.
