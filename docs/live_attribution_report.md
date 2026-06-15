# Live Signal Attribution — TASK 2 Report
**Generated:** 2026-06-14 · `scripts/analyze_live_signals.py`
**Window:** 2026-06-11 17:09 → 2026-06-14 14:30 UTC (testnet, aggressive mode)
**Source of truth:** exchange `user_fills` (closedPnl/fee) + `signals` ticket batches

---

## REFRESH — post-rebalance full sample (2026-06-14 22:21 UTC)
Re-run after the weight rebalance (LiqHeatmap 0→0, OB 0.15→0.26, applied
~19:30 UTC) and after ETH's 6/14 evening reversal. Note: the rebalance only
affects *which* entries fire after ~19:30, so this sample is still dominated by
pre-rebalance trades — read it as a refreshed baseline, not a clean A/B.

**55 round-trip trades, 40% win, net −$0.74** (gross +$0.56, fees $1.30). The
total went **negative** vs the +$0.12 snapshot above — ETH's 5 post-reversal
shorts (see `docs/eth_regime_change_attribution.md`) plus BTC losses pulled it
down. Fees still exceed gross.

| coin | trades | win | net | gross |
|------|-------:|----:|----:|------:|
| ETH  | 32 | 0.44 | +1.02 | +1.73 |
| AVAX |  1 | 1.00 | +0.11 | +0.14 |
| SOL  |  4 | 0.25 | −0.28 | −0.16 |
| WIF  |  1 | 0.00 | −0.09 | −0.08 |
| BTC  | 14 | 0.43 | −0.54 | −0.20 |
| ARB  |  3 | 0.00 | −0.96 | −0.88 |

ETH model attribution (n=32): unchanged in character from the 27-trade
snapshot — Funding agrees 100% but = base rate (0.44, no discrimination);
OB agrees 94% with `win|agree` 0.47 vs `win|disagree` 0.00 (n=2, thin);
ML/LiqHeatmap/Regime still cast zero directional votes. **Adding OB weight did
not change the attribution picture — OB still shows only a marginal, thin edge,
and the regime test (below) shows it has no reversal-detection ability.**

---

## HEADLINE: the win-rate numbers in the gameplan were inflated by counting partial fills

The gameplan's 3-day checkpoint cited **ETH +$6.15, ~89 closes, ~74% win rate**.
That came from counting every *close fill* as a trade (the `/api/fills` dashboard
method). A single position is often closed in 2–3 partial maker fills, and each
partial fill of a winning close was counted as a separate win.

Reconstructing **round-trip trades** (open → flat) from the same fills:

| metric                | dashboard fill-count | round-trip (correct) |
|-----------------------|---------------------:|---------------------:|
| ETH "trades"          | 49                   | **27**               |
| ETH win rate          | 0.71                 | **0.48**             |
| ETH net PnL           | +$1.71               | +$1.71 (same)        |
| Total trades (7 coins)| ~79                  | **49**               |
| Total win rate        | —                    | **0.43**             |
| Total **net PnL**     | **+$0.11**           | **+$0.12**           |

Net PnL reconciles exactly (anchor); only the *unit count* differs. **The real
3-day result is ≈ breakeven (+$0.12 net), not +$5.** Gross +$1.26, fees $1.14 —
**fees ate ~90% of gross**, the same fee-dominated picture as the Phase 4.6
candle backtests.

## Per-coin round-trip summary

| coin | trades | win_rate | net_pnl | fees | gross |
|------|-------:|---------:|--------:|-----:|------:|
| ETH  | 27 | 0.48 | +1.71 | 0.59 | +2.30 |
| AVAX |  1 | 1.00 | +0.11 | 0.03 | +0.14 |
| BTC  | 13 | 0.46 | −0.37 | 0.31 | −0.06 |
| SOL  |  4 | 0.25 | −0.28 | 0.12 | −0.16 |
| WIF  |  1 | 0.00 | −0.09 | 0.01 | −0.08 |
| ARB  |  3 | 0.00 | −0.96 | 0.08 | −0.88 |

ETH carries the book. Its gross +$2.30 over 27 trades is real but win rate is a
coin-flip 48% — the positive result is a handful of larger wins, consistent with
ETH's multi-day downtrend, **not** a high-accuracy signal. Regime, not edge.

## Model attribution (ETH, n=27 — only coin with enough sample)

| model                   | agree_rate | win\|agree | win\|disagree/flat |
|-------------------------|-----------:|-----------:|-------------------:|
| FundingRateModel        | 1.00 | 0.48 (n=27) | — |
| OrderbookImbalanceModel | 0.93 | 0.52 (n=25) | 0.00 (n=2) |
| VWAPModel               | 0.85 | 0.52 (n=23) | 0.25 (n=4) |
| TAModel                 | 0.93 | 0.44 (n=25) | 1.00 (n=2) |
| MeanReversionModel      | 0.07 | 1.00 (n=2)  | 0.44 (n=25) |
| RegimeDetectorModel     | 0.00 | — | 0.48 (n=27) |
| LiquidationHeatmapModel | 0.00 | — | 0.48 (n=27) |
| MLForecastModel         | 0.00 | — | 0.48 (n=27) |

### Reading it
- **FundingRateModel** votes on every entry (agree 1.00) but `win|agree` = the
  base rate (0.48) → it's the always-on driver with **zero discriminating power**.
  It's effectively choosing direction, but not profitably.
- **OrderbookImbalance** and **VWAP** are the only models whose `win|agree`
  (0.52) sits above their `win|disagree` (0.00 / 0.25) — a faint positive
  tilt, but the disagree samples are n=2 and n=4, far too thin to trust.
- **TAModel** agreeing is *worse* than the base (0.44 vs 1.00 on n=2 disagree) —
  no evidence it helps.
- **LiquidationHeatmap, MLForecast, RegimeDetector never vote directionally**
  (agree_rate 0.00 across every coin). Confirmed: the liquidation heatmap is
  effectively dead weight in the live directional ensemble, exactly as ML is.
  That's 3 of 8 models contributing nothing to entry direction.

## Conclusions
1. The "ETH edge" is **not** statistically visible at the round-trip level — 48%
   win rate, fee-dominated, single-regime. Do not treat it as proven edge.
2. The earlier +$5 / 74% framing was a **counting artifact** (partial fills).
   `/api/fills` and the gameplan checkpoint should switch to round-trip counting.
3. Of the 8 models, **3 (ML, LiqHeatmap, Regime) cast no directional vote**;
   Funding dominates but doesn't discriminate; OB/VWAP show only a noise-level
   positive tilt. This *motivates* TASK 1 — testing OB-imbalance + liq-heatmap
   signal quality directly on recorded L2 data, since their live contribution
   here is either zero (liq) or unconfirmed (OB).

## Caveats
- 49 round trips is a small sample; per-coin model rows below n≈10 are noise.
- `user_fills` returned 143 fills back to 2026-06-11 17:09 only. If the API
  truncates older fills, longer-window numbers would need archived fills.
- One BTC position was still open at snapshot time (dropped from round trips;
  accounts for the ~$0.007 gap vs the all-fills total).
