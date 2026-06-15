# OB-Confirmation Experiment — ABANDONED (the trade-log design cannot answer it)
**Status (2026-06-15): CANCELLED.** Two pre-mortem checks on the first ~20
shadow trades showed the trade-log approach is structurally incapable of
producing the comparison — under *any* bucketing, no matter how long it runs.
The ≥30/bucket wait would have been wasted. Details below; original design kept
for the record beneath the strikethrough.

The question itself ("is OB a useful confirmation filter?") is still live — but
it must be answered at the **bar-evaluation level on raw data**, not from the
entered-trade log. See "What actually answers this" at the bottom.

---

## Why the trade-log design is degenerate (2026-06-15, script:
## scripts/ob_confirmation_experiment.py)

**Layer 1 — tautology.** Original design buckets by `ob_direction == direction`.
But OB is 26% of the vote that *chooses* `direction`, so OB agreed on 20/20
trades. Empty disagreed bucket by construction.

**Layer 2 — selection bias (deeper, survives the redesign).** Redesigned to use
the **non-OB ensemble** as the base (re-run the real aggregator with OB weight
→ 0; reproduced the logged entry on 20/20, so the reconstruction is faithful).
*Still* 0 trades in OB_contradicts / OB_decides. Reason: the trade log only
contains **gate-clearing entries**, and the aggressive gate (quorum ≥3, conf
≥0.35) only admits **consensus bars** — per-entry, the non-OB models vote the
entry direction or FLAT and essentially *never* opposite (18/20 entries had zero
opposing non-OB votes). A bar where OB pushes against the rest doesn't reach the
gate, so it never becomes a trade and is **unobservable in `shadow_trades`**.
Accumulating to ≥30 yields the same all-confirms population.

So: the disagreed bucket isn't empty for lack of data — it's empty because the
trade log is *defined* as consensus events. No bucketing or wait fixes this.

## What actually answers this
The OB-as-filter question is a **forward-return analysis over ALL candidate
bars** (entered or not), not a split of entered trades. That is exactly what the
microstructure and stacked-fade backtests already do on raw recorded/historical
data (OB direction vs forward return on every bar):
- OB has real but **sub-fee** 1m tilt (`docs/microstructure_backtest_report.md`).
- Stacking OB with another leverage-fade signal is the open thread, pending more
  recorded L2 — the **OB+OI majors stack** at the ~2026-07-05 checkpoint
  (`docs/stacked_fade_backtest.md`). That, not the shadow trade log, is where an
  OB-confirmation verdict will come from.

No shadow-trade-log OB experiment will be built. The shadow run remains useful
for the **15m-horizon vs aggressive-mode** comparison — a different question that
*is* answerable from `shadow_trades`.

---

## ~~ORIGINAL DESIGN (superseded — kept for record)~~
**~~Status:~~** design parked until the horizon shadow run has accumulated a few
days of `shadow_trades`. Data dependency is now satisfied (see below).

## Hypothesis
OrderbookImbalance's measured 1-min directional tilt is real but **sub-fee** as
a standalone signal (dirret ~0.005–0.010% vs ~0.045% maker round-trip — see
`docs/microstructure_backtest_report.md`). It may become **fee-clearing** when
used as a *confirmation filter* on top of the 15m horizon signal rather than as
its own vote.

## Test (build later)
1. Take the shadow run's would-be 15m entries (`shadow_trades`, the
   `dry_run_horizon.py` output — 15m-ATR×1.5 / 2R-TP / trail-1.5R / 8h-hold).
2. For each entry, check whether OrderbookImbalance agreed with the trade
   direction **at the moment of entry**.
3. Split into two buckets: **OB agreed** vs **OB disagreed/flat**.
4. Compare **win rate and profit factor** between buckets (use `net_pnl_pct`,
   which already subtracts an assumed maker round-trip fee).
5. If the "OB agreed" bucket shows a meaningfully higher profit factor → that's
   an evidence-based entry filter worth implementing live.

## Data dependency — SATISFIED (2026-06-14)
`dry_run_horizon.py` now logs the per-model ticket breakdown with every shadow
trade, so the split is a trivial query — no re-run or backfill needed when the
experiment is built:
- `shadow_trades.ob_direction` — OrderbookImbalance's vote (LONG/SHORT/FLAT) at entry
- `shadow_trades.ob_conf` — its confidence at entry
- `shadow_trades.tickets_json` — full {model: {dir, conf}} snapshot at entry

### Sketch of the eventual query
```sql
SELECT
  CASE WHEN ob_direction = direction THEN 'OB_agreed'
       ELSE 'OB_disagreed_or_flat' END AS bucket,
  COUNT(*)                              AS n,
  AVG(CASE WHEN net_pnl_pct > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
  SUM(CASE WHEN net_pnl_pct > 0 THEN net_pnl_pct ELSE 0 END) /
    NULLIF(-SUM(CASE WHEN net_pnl_pct < 0 THEN net_pnl_pct ELSE 0 END), 0) AS profit_factor
FROM shadow_trades
WHERE status = 'CLOSED'
GROUP BY bucket;
```

## Readiness gate before building
- Shadow run live as a durable service (`hl-shadow.service`).
- ≥ ~30 closed shadow trades per bucket for a non-noise comparison (the live
  attribution showed how thin sub-30 samples are — don't conclude early).
