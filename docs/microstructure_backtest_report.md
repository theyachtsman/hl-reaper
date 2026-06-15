# Microstructure Signal-Quality Backtest — TASK 1 Report
**Generated:** 2026-06-14 · `scripts/backtest_microstructure.py`
**Data:** ~4 days recorded L2 (top-20 book ~every 2s) + ctx + trades, 7 coins
**Method:** replay each L2 snapshot (30s stride) through the two models in
isolation; measure forward mid-price move at 1 / 5 / 15 min, bucketed by
model confidence. Signal quality only — no fees, no sizing.
Raw output: `data/microstructure_backtest_20260614.txt`

This is the **first-ever test** of the OrderbookImbalance + LiquidationHeatmap
models (26% of ensemble weight) — they cannot vote in candle replay, which is
why the recorder was built (Phase 4.6 Action 1).

---

## Finding 1 — LiquidationHeatmapModel is inert. Confirmed dead weight.

Across **all 7 coins**, ~5,400 evaluations each, the model emitted **zero
directional signals** — 100% FLAT. This matches the live attribution (TASK 2),
where LiqHeatmap had agree_rate 0.00 on every coin. Its trigger (funding
extremity AND elevated OI AND price within 3% of a 7.5%-offset zone) essentially
never fires on normal tape.

**Implication:** as currently designed it contributes nothing to the live
ensemble. It is the model the Phase 8.6 cascade-score work is already rebuilding;
this backtest is hard evidence the v1 heatmap should be removed from the live
weight set (or its 13% weight redistributed) until the v2 cascade model replaces
it. It is not "voting FLAT by good judgment" like ML — it is structurally never
able to fire.

## Finding 2 — OrderbookImbalance has a weak but real directional tilt at 1 min

High-confidence bucket (conf ≥ 0.70), hit rate / directional return:

| coin | n (0.70+) | hit_1m | dirret_1m | hit_5m | dirret_5m | hit_15m | dirret_15m |
|------|----------:|-------:|----------:|-------:|----------:|--------:|-----------:|
| BTC  | 3143 | **0.56** | +0.0067% | 0.54 | +0.0092% | 0.54 | +0.0126% |
| AVAX |  765 | **0.57** | +0.0102% | 0.54 | +0.0099% | 0.52 | +0.0199% |
| DOGE | 1440 | **0.57** | +0.0061% | 0.54 | +0.0114% | 0.51 | +0.0221% |
| WIF  | 1785 | 0.52 | +0.0055% | 0.52 | +0.0061% | 0.51 | +0.0128% |
| ETH  |  253 | 0.54 | +0.0083% | 0.54 | +0.0037% | 0.46 | −0.0137% |
| SOL  | 1845 | 0.55 | +0.0055% | 0.49 | −0.0014% | 0.47 | −0.0214% |
| ARB  |  448 | 0.48 | +0.0064% | 0.49 | +0.0012% | 0.48 | −0.0155% |

- **At the 1-minute horizon, dirret is positive for 7 of 7 coins** and hit rate
  ≥0.54 on 5 of 7. The signal is real and consistent: when OB imbalance is
  strongly lopsided, price tends to continue *into* the heavier side over the
  next ~60s. This is the first positive signal evidence the project has found.
- **BTC, AVAX, DOGE** hold the tilt out to 15 min (all horizons positive).
  SOL/ARB/ETH decay and **invert by 15 min** — the short-horizon push mean-reverts.
  So the edge is a **1-minute momentum effect**, not a multi-minute one.

## Finding 3 — but the edge magnitude is at or below the fee floor

Per-signal directional return at 1 min is **0.005%–0.010%**. Round-trip cost is
~**0.045% maker** / 0.07% taker. **The signal does not clear fees on its own as
a standalone taker entry.** This is the same wall every Phase 4.6 result hit:
a genuine but sub-fee tilt.

Where it *could* still pay:
- As a **maker-entry timing / tilt** — improving fill side and direction on
  entries the ensemble already wants, not as its own round-trip strategy.
- As a **confirming vote** that raises confidence (and thus size) on agreement,
  rather than a primary signal. TASK 2 already showed OB had the best (if thin)
  `win|agree` vs `win|disagree` split of the live models — consistent with this.
- At **1-min holding only** — the 15-min inversion on half the coins means any
  use must exit fast, which fights maker-entry latency.

## Recommendations
1. **Drop LiquidationHeatmapModel v1 from the live ensemble weight** (or zero its
   weight) until the Phase 8.6 cascade-score v2 is ready — it can never fire.
2. **Keep OrderbookImbalance, but reframe it** as a short-horizon confirmation /
   entry-timing tilt, not a primary directional model. Do not expect it to clear
   fees alone.
3. Consider an experiment: gate maker entries on OB-imbalance *agreement* and
   measure fill-side improvement — that's where a 1-min tilt is monetizable.

## Caveats
- 30s-stride samples are autocorrelated (book imbalance persists), so n
  overstates independent observations — treat hit rates as directional, not
  precise. Even so the 1-min positivity is consistent across coins.
- Forward returns use recorded mid; real fills cross the spread.
- ~4 days, mostly trending/quiet tape — no large cascade in the window (also why
  LiqHeatmap never fired). A volatile window could change the heatmap picture.
- Confidence buckets below 0.50 are unreachable (OB confidence starts at 0.50
  the moment it leaves FLAT), so only the 0.50–0.70 and 0.70+ rows populate.
