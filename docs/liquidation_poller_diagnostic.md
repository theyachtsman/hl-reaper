# hl-liq-poller Diagnostic — why zero events in 2 days (Task 0)

_2026-06-14. Verdict: **NOT a bug.** Detection logic is correct; backstop
liquidations on major coins are structurally near-nonexistent. The
"momentum-follow on confirmed liquidations" door is blocked on **data
availability**, not a quick fix — and likely can't be unblocked with this
free source at all._

## What the poller looks for

[reaper/data/liquidation_poller.py](../reaper/data/liquidation_poller.py)
subscribes to the public `trades` WS channel and flags any trade where a
counterparty in `users:[buyer,seller]` matches the HLP Liquidator vault
`0x2e3d94f0562703b25c83308a05046ddaf9a8dd14`.

## Checks run

1. **Is the `users` field present in the public trades feed?** YES. Live probe
   of BTC/ETH/SOL/DOGE/WIF trades: every trade carries
   `users:[buyer, seller]` (2 entries). The poller's parse path is structurally
   correct — `len(users)==2` passes, the address comparison runs.

2. **Is the configured address the right one?** YES. `vaultDetails` for
   `0x2e3d…dd14` returns **name = "HLP Liquidator"**, accountValue $1M. This is
   exactly HL's backstop liquidation vault.

3. **How often does that vault actually fill?** (`userFillsByTime`, the
   decisive check)

   | Window | Total fills | Fills in our 7 coins | Notes |
   |--------|------------:|---------------------:|-------|
   | last 3d  | 0    | 0 | — |
   | last 14d | 0    | 0 | — |
   | last 30d | 2000* | **0** | all TST, single event 05-17/05-18 |
   | last 90d | 2000* | **0** | 1997 TST + 3 MELANIA |

   \*2000 = API page cap; the burst is one TST event, not steady flow.

## Interpretation

The vault **works** — the 05-17/18 burst shows it eats backstop flow during a
real dislocation (a TST depeg/delisting-type event). But over **90 days its only
fills were in illiquid test/meme tokens (TST, MELANIA)** — **zero** in
BTC/ETH/SOL/ARB/AVAX/DOGE/WIF.

This is consistent with how HL's two-tier liquidation works (and with the
poller's own docstring): a liquidation only routes to the backstop vault when
normal book depth **can't** absorb the forced market order. Major coins have
deep books, so their liquidations — even during the ~24 price-action
"cascade events"/week we measured — are absorbed as **ordinary market orders**,
which are indistinguishable from normal trades in public data and are *not*
captured. The backstop only catches the tail-of-the-tail, which on majors is
vanishingly rare.

## Which case are we in?

Neither "bug" nor "wait a few weeks for volatility." It's a **third case**: the
confirmation signal essentially **never fires for the coins we trade**, by
construction. The poller could run for months and catch ~nothing on majors.

**Consequence for momentum-follow-on-cascades** (the open door from
[cascade_bounce_backtest.md](cascade_bounce_backtest.md)): blocked. Real
backstop-liquidation confirmation on majors is not obtainable from this free
public-trades source. Getting it would require an **external aggregated source**
(paid 0xArchive, or Coinalyze's aggregated liquidation-history — HL coverage
unverified, free tier). Not worth pursuing unless/until a strategy actually
depends on it.

## Recommendation

- Leave `hl-liq-poller` running (it's harmless, read-only, and *will* catch a
  genuine major-coin backstop cascade if an extreme one ever happens — useful to
  have the capture in place). But **do not treat its empty DB as a problem to
  fix**, and **do not gate any near-term strategy on it.**
- One caveat I did not exhaustively rule out: HL could route some liquidations
  through *additional* liquidator addresses not in `LIQUIDATOR_VAULTS`. The
  configured one is the canonical "HLP Liquidator," and the 90-day fill history
  is so sparse that adding addresses is unlikely to change the major-coin
  picture — but if this signal ever becomes load-bearing, enumerate all HL
  liquidator sub-vaults before relying on completeness.
