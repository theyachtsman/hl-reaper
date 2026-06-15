# Cascade Score Backtest Report (Phase 8.6, Task 4)

**Date:** 2026-06-12
**Script:** `scripts/backtest_cascade_score.py` (results: `data/backtest_cascade_20260612.json`)
**Data:** 192 days (2025-12-01 → 2026-06-10), 5m bars, BTC/ETH/SOL.
Price/volume/funding from `data/history/` (Binance archive), OI from the
Binance `metrics` archive (5m, downloaded + cached as `{COIN}_oi_5m.csv`).

## Setup

Real per-event HL liquidation history isn't freely available
(docs/liquidation_data_sources.md), so **cascade events are derived**: a bar
where, over the trailing 30m, OI contracted ≥1.5%, |price| moved ≥1.25%, and
30m volume ≥2.5× the trailing 24h average — the forced-deleveraging
signature. Events within 2h merge into one. Counts: BTC 20, ETH 51, SOL 35.

**No lookahead anywhere:** the score at bar *t* uses only windows ending at
*t* (4h OI/range, 30m/24h volume, funding at *t*); clusters come from the
OI-math fallback (no real events were used). Event detection is likewise
trailing, so onset = first bar where the signature is fully visible.

Evaluation: an *alert episode* is a contiguous run of bars with score ≥ T.
Precision = episodes followed by an event onset within 6h. Recall = events
with score ≥ T at some bar in the 6h before onset. Base rate = probability a
random bar has an onset within the next 6h — the hindsight-bias control.

## Results

| Coin | Events | Base rate | Thr | Alerts | Precision | **Lift** | Recall | Median lead |
|------|-------:|----------:|----:|-------:|----------:|---------:|-------:|------------:|
| BTC  | 20 | 2.62% | 30 | 305 | 6.9%  | **2.63×** | 50.0% | 165 min |
| ETH  | 51 | 6.44% | 30 | 423 | 12.3% | **1.91×** | 56.9% | 70 min  |
| SOL  | 35 | 4.57% | 30 | 544 | 8.1%  | **1.77×** | 54.3% | 150 min |

Observed hits at threshold 30 vs chance expectation: BTC 21 vs ~8 expected,
ETH 52 vs ~27, SOL 44 vs ~25 — all well above chance, consistent in
direction across all three coins.

Score distribution note: the geometric combiner compresses the scale —
p99 ≈ 33–35, max < 55. Thresholds ≥ 40 produce too few alerts to use;
the operating range is 25–35. If integrated, rescale or use percentile
thresholds rather than absolute ones.

## Interpretation (honest)

**The score has real predictive value — it is not hindsight description.**
It rises 1–3 hours before derived cascade onsets (positive median lead at
every threshold/coin with recall > 0), and elevated readings are 1.8–2.6×
more likely than a random bar to precede an event. This held on three coins
without per-coin tuning.

**But it is weak as a standalone signal.** Absolute precision at the useful
threshold is 7–12%: roughly 9 in 10 alert episodes are not followed by a
cascade within 6h. Trading the score directly would churn fees exactly the
way the Phase 4.6 candle ensemble did.

Caveats:
- Events are *derived* from Binance OI/price/volume, not real HL
  liquidations. The signature is reasonable but uncalibrated; real backstop
  events from the new poller are the eventual ground truth.
- Some lift may come from volatility clustering (an alert in a stormy
  regime "predicts" the next storm). The 6h-window base rate controls for
  this only partially.
- The cluster components currently run on the OI-math fallback. The
  events-driven cluster path exists but has no data yet.

## Recommendation: CONDITIONAL GO — continue research, do NOT integrate yet

1. **Keep collecting** — run `scripts/run_liquidation_poller.py` continuously
   (systemd unit recommended) so real HL backstop events accumulate. They
   are the calibration set this backtest couldn't have.
2. **Architecture confirmed** — use the cascade score as a *context/gating
   input* (where leverage is trapped, conditions ripening), with
   `LiquidationVelocityModel` as the *trigger* (cascade actually firing).
   The score alone is too imprecise to be a direction model; as a
   precondition filter for an event-driven entry it is exactly the shape
   of signal that helps.
3. **Re-evaluate for ensemble integration** once (a) the poller has lived
   through ≥1 real cascade so velocity thresholds can be calibrated, and
   (b) the score's cluster components can run on real events. That decision
   point is a follow-up task per the original scope.
4. Integration as a 9th model / LIQMAP replacement is **not justified by
   these numbers alone** — 2× lift with 10% precision would add noise to
   the live quorum, not edge.
