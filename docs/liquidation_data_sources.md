# Liquidation Data Sources — Findings (Phase 8.6, Task 1)

**Date:** 2026-06-12
**Question:** Where can we get historical + real-time Hyperliquid liquidation
events per coin, ideally back to May 2025, without a paid dependency?

---

## TL;DR

| Source | Real liq events? | History | Cost | Verdict |
|---|---|---|---|---|
| HL `/info` REST | No market-wide type exists | — | free | Not available |
| HL `userFills` / `userEvents` | Yes, but **per-user only** | per-user | free | Unusable market-wide |
| HL public trades (WS + `recentTrades`) | **Backstop liqs only**, via liquidator-vault address match | none (collect-forward) | free | **USE — real-time collector** |
| HL S3 node archive (`node_fills`) | Yes, fully tagged | genesis→now | requester-pays $ | Rejected (same reason as candle history) |
| 0xArchive (oxarchive on PyPI) | Yes (liquidator, px, sz, PnL) | May 2025→now claimed | paid (14d trial) | Documented, not adopted |
| Coinalyze free API | Aggregated per-interval liq volume | months | free w/ API key | Optional backfill, needs key + symbol verification |
| Binance archive `liquidationSnapshot` | — | **discontinued** (404, folder gone) | — | Dead end |
| Binance archive `metrics` (5m OI) + our 192d candles | **Derived** cascade events (OI contraction + price + volume) | ~2021→now | free, no auth | **USE — backtest calibration** |

---

## 1. Official Hyperliquid `/info` endpoint

Checked the [info endpoint docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)
and the [websocket subscriptions docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions).

- There is **no** `liquidations` / `liquidationHistory` info type. Nothing
  market-wide.
- Liquidation tagging exists only on **per-user** queries: `WsFill` has an
  optional `liquidation: {liquidatedUser?, markPx, method: "market"|"backstop"}`
  field, and `userEvents` can carry a `WsLiquidation`. Both require knowing the
  user address in advance — you cannot subscribe market-wide.
- The `liquidatable` type seen in some SDK wrappers returns *currently*
  liquidatable positions (point-in-time), not history. Not useful for backfill.

## 2. Public trade feeds DO expose counterparty addresses (verified live)

`recentTrades` REST and the `trades` WS channel both return a
`users: [buyer, seller]` field — **verified 2026-06-12 against mainnet**
(`POST https://api.hyperliquid.xyz/info {"type":"recentTrades","coin":"BTC"}`).

Per the [liquidations docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations),
HL has a two-tier flow:

1. **Market-order liquidations** (primary): position closed via a market order
   into the book. The liquidated user appears in `users` like any other trader
   — these are **indistinguishable from normal trades** in the public feed.
2. **Backstop liquidations** (secondary, equity < 2/3 maintenance margin):
   position transfers to the **HLP Liquidator vault**
   `0x2e3d94f0562703b25c83308a05046ddaf9a8dd14`. Any public trade where this
   address is buyer or seller is a tagged, real backstop-liquidation fill.

Implication: a free WS collector catches the *severe* tail of liquidations —
backstop events, which only occur when the book couldn't absorb the market
liquidation. **Those are precisely the cascade events this research targets.**
Routine single-position market liquidations are missed, but those aren't
cascades.

Detection quality note: if HL ever rotates/adds liquidator vault addresses,
the watch list must be updated (configurable in the poller).

## 3. HL S3 node archive (requester-pays)

The official [historical data docs](https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data)
describe S3 node data including all fills with liquidation/ADL tags
(`node_fills`). This is the ground-truth source 0xArchive and similar vendors
derive their `liquidations.history` from. It is **requester-pays** — the same
cost problem that disqualified it for candle history in Phase 4.6. Rejected
for now; revisit only if a one-off calibration download is ever justified.

## 4. 0xArchive (`oxarchive` on PyPI)

[0xArchive](https://www.0xarchive.io/) ([pricing](https://www.0xarchive.io/pricing/))
offers historical liquidation events (liquidator, price, size, realized PnL)
via REST/WS-replay/Parquet. It is a **paid** product (14-day trial, then
credit-billed). Its data is derived from HL node fills (source #3), so it is
reproducible without them — by paying AWS instead of them, or by collecting
forward ourselves. **Not adopted** per the no-paid-dependency preference.

## 5. Coinalyze free API

[Coinalyze](https://api.coinalyze.net/v1/doc/) has a free (API-key) REST API
with a `liquidation-history` endpoint (aggregated long/short liquidation
volume per interval, per exchange-symbol) and lists Hyperliquid among
supported exchanges. **Caveats:** per-interval aggregates only (no per-event
price), and HL symbol coverage for liquidations is unverified — their public
pages suggest HL liquidation coverage may be limited. The poller supports
this as an **optional backfill** (`COINALYZE_API_KEY` env var); verify symbol
coverage when a key is available.

## 6. Binance proxies (free, no auth — consistent with our Phase 4.6 data layer)

- `liquidationSnapshot` in the public futures archive is **discontinued**
  (verified 2026-06-12: folder absent from bucket listing, sample URLs 404).
- `metrics/` **is available** (verified HTTP 200): daily CSVs with 5-minute
  `sum_open_interest`, long/short ratios, taker buy/sell ratio, back to ~2021.
- Combined with the 192 days of 1m candles + funding already in
  `data/history/`, this supports **derived cascade-event detection**: a sharp
  OI contraction + outsized price displacement + volume spike inside a short
  window is a forced-deleveraging signature. Not per-fill ground truth, but
  free, deep, and good enough to calibrate/backtest the cascade score
  (Task 4) until real HL backstop events accumulate from the collector.

---

## Recommendation (implemented)

1. **Collect forward, free, starting now** — `reaper/data/liquidation_poller.py`
   (run via `scripts/run_liquidation_poller.py`, standalone process) subscribes
   to the public `trades` WS for the 7 target pairs and records every fill where
   a liquidator-vault address is a counterparty into the new
   `liquidation_events` table (`source='hl_ws_backstop'`). Every day running =
   real calibration data accumulating.
2. **Optional Coinalyze backfill** — same script, `--backfill-coinalyze`
   (requires `COINALYZE_API_KEY`); writes aggregated events with
   `source='coinalyze'`.
3. **Backtest on derived events now** — `scripts/backtest_cascade_score.py`
   downloads Binance 5m OI metrics (cached in `data/history/{COIN}_oi_5m.csv`),
   derives historical cascade events from OI+price+volume, and validates the
   cascade score against them with no lookahead.
4. **Revisit paid sources only if** the go decision needs per-event ground
   truth that the forward collector hasn't accumulated yet.

Earliest data available per source: forward collector = today (2026-06-12);
Binance metrics ≈ 2021+; HL S3 / 0xArchive ≈ exchange genesis / May 2025
(paid). The task's "May 2025 onwards" target is only reachable through paid
sources or aggregated Coinalyze data — documented trade-off accepted.

Sources:
- [Hyperliquid info endpoint docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)
- [Hyperliquid websocket subscriptions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions)
- [Hyperliquid liquidations docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations)
- [Hyperliquid historical data docs](https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data)
- [Dwellir: HL liquidation tracker](https://www.dwellir.com/blog/building-real-time-hyperliquid-liquidation-tracker)
- [0xArchive](https://www.0xarchive.io/blog/introducing-0xarchive/) / [pricing](https://www.0xarchive.io/pricing/)
- [Coinalyze API docs](https://api.coinalyze.net/v1/doc/)
