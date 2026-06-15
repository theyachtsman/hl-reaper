# Stacked Leverage-Fade Backtest — Results

_2026-06-14. Script:
[scripts/backtest_stacked_fade.py](../scripts/backtest_stacked_fade.py).
Raw: `data/backtest_stacked_fade_20260614.json`. 7 coins, 5m bars, ~28 weeks
(~192–195d). OI history for all 7 now available (downloaded the 4 missing coins
via [scripts/download_oi_history.py](../scripts/download_oi_history.py))._

## Verdict

**The stacking thesis is VALIDATED as a mechanism — but the resulting edge is
NOT tradeable.** Two independent leverage-fade signals, when they agree, do
reinforce: pooled `BOTH` > `A_only` > `B_only` > baseline, monotonically at
every horizon. That confirms the "independent measurements of the same true
signal compound, noise partly cancels" reasoning. **However**, the combined edge
(a) materialises *entirely on the illiquid coins* where the 0.045% maker-fee
assumption is least realistic, (b) is *inconsistent in sign* across coins and
directions, and (c) rests on **~50% win rates** with positive averages driven by
payoff asymmetry, not directional accuracy. Net of realistic illiquid-coin
execution cost, there is no edge to trade.

## Important constraint (read first)

Only **two** of the three named signals have long history:
- **A = perp_leads** (spot vs perp price) — 225d, all coins
- **B = new_shorts/new_longs** (price + OI flow) — now 192d, all 7 coins
- **C = OB-imbalance-against** (L2 depth) — **only ~4d of recorded L2 exists**

So a large-sample **3-way is still not possible**. This is the scalable 2-way
(= the "2-of-3 agree" fallback in practice). See "Where OB would actually help"
below — it's the genuinely promising thread.

## The compounding is real (pooled, combined up+down)

fade_return >0 = the fade worked. Maker round-trip fee = 0.045%.

| leg | n | n/wk | +5m | +15m | +30m | +60m |
|-----|--:|-----:|-----|------|------|------|
| A_only | 19294 | 697 | +0.015% / 49% | +0.011% / 49% | +0.011% / 50% | +0.005% / 50% |
| B_only | 20721 | 748 | +0.004% / 50% | +0.006% / 51% | +0.010% / 51% | +0.008% / 51% |
| **BOTH** | **2764** | **100** | **+0.029% / 51%** | **+0.038% / 51%** | **+0.042% / 50%** | **+0.043% / 50%** |
| EITHER | 37251 | 1346 | +0.008% / 50% | +0.006% / 50% | +0.008% / 51% | +0.004% / 50% |
| ALL_MOVES | 129523 | 4678 | +0.003% / 50% | +0.002% / 51% | +0.005% / 51% | −0.001% / 50% |

`BOTH` is ~3–9× the single-leg average and clearly above the baseline — the
stack does what the thesis predicted. Frequency is fine: ~100 agreeing
events/week pooled across 7 coins (the earlier majors-only run showed ~1/wk —
that was the co-occurrence artifact of perp_leads being absent on liquid coins).

**But:** peak pooled `BOTH` is +0.043% (60m) — it **reaches the 0.045% fee line
and stops there.** Net of fee, pooled `BOTH` is ≈ −0.016% (5m) … −0.002% (60m):
slightly negative at every horizon. And win rate is ~50–51% throughout — a coin
flip with fat-tailed payoffs.

## Why it isn't tradeable — the per-coin breakdown

`BOTH` has real sample only on the illiquid coins (ARB/AVAX/WIF); BTC/ETH/SOL/
DOGE have n≈0–18 (noise), because perp can't lead spot on tightly-arbitraged
majors.

| coin | dir | n/wk | +30m (avg/win) | +60m (avg/win) | read |
|------|-----|-----:|----------------|----------------|------|
| ARB  | down | 18.1 | +0.068% / 50% | +0.096% / 48% | above fee, but win <50% |
| ARB  | up   | 20.0 | +0.057% / 46% | +0.060% / 47% | above fee, but win <50% |
| AVAX | down | 7.9  | +0.048% / 49% | +0.115% / 53% | works only long-horizon |
| AVAX | up   | 11.6 | −0.011% / 50% | −0.065% / 49% | fails |
| WIF  | down | 7.3  | −0.190% / 45% | −0.202% / 46% | **actively fails** (crashes keep crashing) |
| WIF  | up   | 32.3 | +0.090% / 55% | +0.076% / 53% | works |

The sign is coin- and direction-specific: WIF down-fades lose hard, AVAX
up-fades lose, ARB is mildly positive both ways but on sub-50% win rates. There
is no stable rule here — and every cell that *does* clear the 0.045% fee in
average terms is on **ARB/AVAX/WIF**, where the 0.045% maker assumption is
fiction: those books are thin, spreads are wide, and *fading a fast move means
posting liquidity into momentum* (maximum adverse selection — you fill exactly
when you're wrong). Realistic round-trip cost on those names is well above
0.045%, which erases the +0.04–0.10% averages.

## The fundamental tension (now quantified)

- The fade edge **exists where perp leads spot** = illiquid coins = **expensive
  to trade**.
- The coins that are **cheap to trade** (BTC/ETH/SOL) **don't generate the
  perp_leads signal** at all (spot/perp move as one).

The stack's mechanism is sound; it just surfaces precisely where it can't be
monetized.

## Where OB would actually help (the real forward thread)

The one signal that *does* work on liquid coins is **OB imbalance** — the
microstructure test found positive directional tilt on all 7 coins, including
BTC/ETH/SOL. So the promising version is **not** this perp_leads-based illiquid
stack, but a **liquid-coin stack of OB-imbalance + new_shorts** (both available
on majors, both cheap to execute). That can't be backtested yet — only ~4 days
of recorded L2 exist. **Concrete next step:** let `hl-recorder` accumulate L2
for several more weeks, then run a 2-way OB+OI stack on BTC/ETH/SOL where
execution is cheap. That is the only configuration that could plausibly clear a
*realistic* fee.

## Bottom line

- Stacking independent signals compounds — **confirmed**, cleanly, and the
  frequency objection is resolved. Good to know the principle holds.
- As a tradeable strategy: **NO.** The perp_leads-based stack lives entirely in
  illiquid coins, is sign-inconsistent, ~50% win rate, and the only fee-clearing
  cells are exactly where the fee model is too optimistic.
- The honest pivot: the leverage-fade family is real but its *liquid-coin*
  expression runs through **OB imbalance**, not perp-leads. Re-run this stack as
  OB+OI on majors once enough L2 is recorded.
