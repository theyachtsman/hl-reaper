# Cascade-Bounce Strategy — Infrastructure Assessment & Design (Phase 8.6)

_Task 1 + Task 2 deliverable. Written 2026-06-14._

## TL;DR

Most of this strategy **already exists and is wired** from the 2026-06-12 Phase
8.6 work. This assessment documents the real current state (correcting the task
brief, which treated several pieces as un-built), and the design as implemented.
The genuine gap is **Task 3** — nobody has ever backtested whether *fading the
overshoot is actually profitable*. The existing `cascade_backtest_report.md`
tested a different thing: the cascade *score predictor* (will a cascade happen),
not the *bounce* (does fading it make money).

---

## Task 1 — Infrastructure assessment

### `hl-liq-poller` — EXISTS, running, but capturing nothing

- Service `hl-liq-poller.service`, active since 2026-06-12 (~2 days), PID 389674.
- Source: [reaper/data/liquidation_poller.py](../reaper/data/liquidation_poller.py),
  entrypoint [scripts/run_liquidation_poller.py](../scripts/run_liquidation_poller.py).
- What it does: watches **mainnet** public trades and flags fills where the HLP
  Liquidator vault is the counterparty (backstop liquidations), writing them to
  `data/liquidations.db` (table `liquidation_events`).
- **Status: 0 events captured from ~924,000 trades over 2 days.**
  `liquidations.db` has zero rows; file unmodified since 2026-06-12 15:08.

  This is the honest headline finding. Either (a) the last 2 days were calm
  enough that no liquidation routed through the backstop vault, or (b) the
  counterparty-detection heuristic doesn't match how HL labels these fills.
  Backstop routing only happens when normal book depth can't absorb a
  liquidation — i.e. specifically during cascades — so zero catches in calm
  tape is consistent with (a), but we cannot distinguish (a) from (b) until a
  real volatile window passes with the poller running.

  **Implication for the bounce strategy:** the real-liquidation *confirmation*
  signal (`_liq_confirms`) is currently always False. The bounce model already
  treats this as confidence-boost-only and works without it, so this does not
  block the strategy — but it means we cannot yet calibrate against real
  liquidation events, only against price+volume cascade signatures.

### OI / liquidation data available

| Source | Coverage | Cadence | Use |
|--------|----------|---------|-----|
| `buf.ctx[coin]` (live) | all 7 pairs | 60s poll | live OI/funding/mark for confirmation |
| `data/recorded/ctx_*.jsonl.gz` | 7 pairs, ~4 days | 60s | recorded OI snapshots |
| `data/recorded/l2_*.jsonl.gz` | 7 pairs, ~4 days, ~99 MB | 2s | L2 book depth |
| `data/history/{COIN}_1m.csv` | **7 pairs, 192 days** | 1m OHLCV | **primary cascade backtest input** |
| `data/history/{COIN}_oi_5m.csv` | BTC/ETH/SOL only | 5m | OI confirmation (historical) |
| `data/liquidations.db` | empty | — | real backstop events (none yet) |

### Is 60s OI polling fast enough to catch a cascade?

For **detection**: it doesn't need to be. The implemented model triggers on
**price move + volume spike from 1m candles**, not OI. A cascade that moves
price >1.5% on >3x volume inside a 5-minute window is fully visible at 1m
resolution. OI is used only as an after-the-fact *confirmation* (did leverage
actually flush) and a confidence boost — there, 60s polling over a multi-minute
episode is adequate (it will see the contraction).

So: **price velocity is the primary trigger, OI is confirmation.** This is the
right architecture and it's already what's implemented. We do not need faster OI
polling for the strategy to function.

---

## Task 2 — Cascade detection & bounce design (as implemented)

Implemented in [reaper/models/cascade_bounce.py](../reaper/models/cascade_bounce.py).
It is **not** a `BaseModel`/`Ticket` voter — it runs as a separate event-driven
track in `run_bot.py` (lines ~223-310) and can fire while the main ensemble is
FLAT, but still passes through every RiskManager guard.

### Per-coin state machine

```
IDLE ──(move ≥1.5% in 5m AND vol ≥3× 1h baseline)──▶ CASCADING
CASCADING ──(2 bars with no new extreme)──▶ fire bounce signal ──▶ COOLDOWN(30m) ──▶ IDLE
CASCADING ──(15m, still making new extremes)──▶ abandon ──▶ IDLE   (never catch the knife)
```

### Detection (trigger)
- **Move:** `closes[-1]/closes[-6] - 1` ≥ `min_cascade_move_pct` (default 1.5%)
  over a 5-bar (5-min) window.
- **Volume confirmation:** mean volume of the 5 cascade bars ≥ `min_volume_mult`
  (default 3.0) × mean volume of the prior baseline bars. This is what
  distinguishes a real liquidation flush from a slow drift — a price move with no
  volume spike is ignored (verified by the unit test).

### Direction
- Down-cascade (price crashed) → **LONG** the bounce.
- Up-cascade (short squeeze) → **SHORT** the bounce.

### Entry timing (the hard part) — "wait for stabilization"
The model does **not** predict the reversal — it waits for evidence the knife has
landed. It tracks the cascade extreme (lowest low / highest high), and only fires
once **`stabilization_bars` (default 2) consecutive 1m bars pass without a new
extreme**. If price keeps making new extremes past `cascade_stale_minutes`
(15m), the episode is abandoned — this is the "don't catch the falling knife"
guard. Entry is taken against the cascade direction at market.

### Exit (fast scalp)
- **Take profit:** `profit_target_pct` (default +1.0%).
- **Stop:** `stop_pct` (default 0.75%) — a move beyond the extreme means the
  cascade wasn't done, thesis is dead, get out.
- **Max hold:** `max_hold_minutes` (default 20m), enforced by RiskManager via
  `register_entry(hold_hours=...)`.

### Risk integration (already built & tested)
- New `BotState.CASCADE_BOUNCE_ACTIVE` coexists with the main state machine.
- `RiskManager.check_cascade_bounce_allocation()` caps the bounce position at
  `allocation_pct` (12%) of equity, one bounce at a time, denies when already
  holding the coin / equity too small / not in ACTIVE state.
- While a bounce is open, the ensemble's `can_open` is blocked, but position
  management keeps running.
- Maker-then-taker entry (`try_limit_entry` 5s, then market) — speed beats fees
  in a dislocation.
- All 29 risk guard tests + the dedicated `scripts/test_cascade_bounce.py`
  synthetic-cascade suite pass.

### Current config ([config.yaml](../config.yaml) `cascade_bounce:`)
`enabled: false` — built and validated against synthetic tape, but **not turned
on live**, pending the Task 3 historical backtest below.

---

## What's actually missing → Task 3

The only un-done piece: **does fading the overshoot make money after fees?**
The model's detection has been verified against *synthetic* cascades; its
*profitability* has never been measured against real price history. That is the
backtest in [cascade_bounce_backtest.md](cascade_bounce_backtest.md).
