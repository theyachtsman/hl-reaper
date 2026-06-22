# ML Retrain on Full History — Report (2026-06-15)

_Scripts: scripts/train_ml_model.py (deployable run), scripts/train_ml_experiment.py
(offline feature test). 5m bars, horizon 12 (=1h ahead), 80/20 time-split,
majority-class+1% gate (unchanged)._

## Verdict: still NO edge. 0/7 models saved. Gate NOT lowered.

225 days of Binance history (up from the original ~3.5d HL testnet) changed
nothing: next-direction prediction has no out-of-sample edge on any of the 7
coins. Every model memorizes the training window (in-sample ~0.74) and collapses
to ~majority-class on OOS. This is the third independent confirmation of the
Phase 4.6 finding (candle backtest, live attribution, now full-history ML).

## TASK 1 — existing 15 live-computable features, 230 days

| Coin | IS acc | OOS acc | majority | required | result |
|------|-------:|--------:|---------:|---------:|--------|
| BTC  | 0.7380 | 0.5131 | 0.5227 | 0.5327 | fail |
| ETH  | 0.7454 | 0.5183 | 0.5129 | 0.5229 | fail (closest, −0.0046) |
| SOL  | 0.7454 | 0.5137 | 0.5163 | 0.5263 | fail |
| ARB  | 0.7364 | 0.5063 | 0.5439 | 0.5539 | fail |
| AVAX | 0.7401 | 0.5094 | 0.5108 | 0.5208 | fail |
| DOGE | 0.7499 | 0.5110 | 0.5193 | 0.5293 | fail |
| WIF  | 0.7358 | 0.5007 | 0.5174 | 0.5274 | fail |

IS/OOS gap ~0.22–0.24 on every coin = classic memorization, no generalization.
`funding` and the cyclical time features (hour/dow) dominate importance — i.e.
the model leans on weak seasonality, not price structure. ETH is the only
near-miss (0.5183 vs 0.5229) and still fails honestly.

## New features — spot/OI/divergence (offline experiment, deployed nothing)

Added `spot_ret_1/5`, `oi_change_1/5`, `perp_spot_divergence_1` — direct
encodings of the two real-but-sub-fee signals from the lead/lag and OI work.

| Coin | base-15 OOS | base+new OOS | required | effect |
|------|------------:|-------------:|---------:|--------|
| BTC  | 0.5131 | 0.5147 | 0.5327 | +0.002, still fail |
| ETH  | 0.5183 | 0.5098 | 0.5229 | worse |
| SOL  | 0.5137 | 0.5052 | 0.5263 | worse |
| ARB  | 0.5063 | 0.5132 | 0.5539 | +0.007, miles off |
| AVAX | 0.5094 | 0.5117 | 0.5208 | +0.002, fail |
| DOGE | 0.5110 | 0.5102 | 0.5293 | worse |
| WIF  | 0.5007 | 0.5000 | 0.5274 | worse |

**0/7 pass, 4/7 got worse.** All five new features rank in the **bottom third**
of importance on every coin (0.029–0.047). Conclusion: the spot/OI/divergence
signals are real as tiny forward-return edges at 1–15m, but as features for
1h-ahead direction *classification* they are swamped by noise and add nothing.

### Why these weren't deployed regardless (deployability constraint)
The new features are **not computable by the live MLForecastModel**: the bot's
buffer holds only the latest spot tick (`buf.spot`) and latest OI (`buf.ctx`),
not the bar-aligned spot/OI *series* these features require. A model trained on
them would silently break the train/inference parity the module guarantees.
Deploying them would have required first wiring spot/OI history into the live
feature pipeline — a real live-path change, out of scope for a model swap — and
since they don't clear the gate anyway, that wiring is not worth doing.

## Outcome
- 0 models saved to `models/` → MLForecastModel correctly returns
  `FLAT / "model not trained"` for all 7 coins. The Signals page "not trained"
  status is accurate (no saved-but-FLAT coins exist, so nothing to relabel).
- Gate left untouched. New features left out of the live FEATURES set.
- 29/29 risk guards pass. No live trading logic changed.

## What this rules out (now definitive)
Direction prediction via gradient-boosted trees on candle features — and on the
best microstructure features we have — does not work on this market at the 1h
horizon, across 7 coins and 225 days. ML stays FLAT in the ensemble, by evidence,
not omission. Any future attempt should change the *target* (not direction —
e.g. volatility/regime), not add more features to a direction classifier.
