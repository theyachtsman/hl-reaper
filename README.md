# HL Reaper

An automated perpetual-futures trading bot for [Hyperliquid](https://hyperliquid.xyz),
built around a regime-aware, multi-model signal ensemble with a hard risk engine,
an independent dead-man's switch, and a live web dashboard.

> ⚠️ **Status: research / testnet.** This is a personal research system that trades
> Hyperliquid **testnet** by default (`network: testnet` in `config.yaml`). Every
> directional edge here has been measured, and several models are deliberately
> zeroed because they didn't earn their weight (see [`docs/`](docs/)). Do not point
> it at mainnet without working through [`docs/MAINNET_CHECKLIST.md`](docs/MAINNET_CHECKLIST.md).
> Trading derivatives risks total loss of capital. Nothing here is financial advice.

---

## How it works

Each cycle (`trading.loop_interval_seconds`, default 10s) the bot, **per coin**:

1. **Ingests market data** — a websocket feed (candles, L2 book, trades, auto-reconnect)
   plus REST pollers (funding, open interest, mark price, spot price for lead/lag,
   equity snapshots). Candle deques are REST-backfilled on startup so a restart comes
   back fully warm.
2. **Runs a model ensemble** — each model emits a `LONG / SHORT / FLAT` ticket with a
   confidence. A `RegimeDetector` classifies the 1h regime (`TRENDING_UP`,
   `TRENDING_DOWN`, `RANGING`, `HIGH_VOL`) and acts as a meta-router rather than a voter.
3. **Aggregates** the tickets into a single weighted, regime-biased signal,
   normalizing confidence over the *active* voters only (FLAT votes don't dilute).
4. **Gates the signal** through the risk engine and structural entry gates.
5. **Executes** maker-first (post-only) with a taker fallback, precision-rounded to
   each asset's size/price decimals.
6. **Manages open positions** every cycle — stop loss, breakeven lock, trailing stop,
   take-profit, and max-hold timeouts.

### Dual-band aggregation

The bot runs **two aggregations per coin per cycle** with separate weight sets,
risk, and gates:

- **Scalp band** (`5m`) — mean reversion dominant: fade the local top/bottom, with
  order-book pressure and momentum confirming.
- **Trend band** (`1h`) — no mean reversion (it fails in sustained trends); TA and
  order-book imbalance lead, funding (smooth-mapped) and VWAP confirm.

Hyperliquid is one-way per coin, so a coin holds **one position in one band at a
time** (Option B).

### The voters

Six active directional models, each weighted per band (`reaper/aggregator.py`):

| Model | Role |
|-------|------|
| `TAModel` | RSI/TA blend; regime-aware thresholds so it agrees with a clear trend instead of abstaining |
| `MeanReversionModel` | Fade local extremes (scalp band only) |
| `OrderbookImbalanceModel` | L2 bid/ask pressure — the only model with a measured positive directional tilt |
| `VWAPModel` | Distance from VWAP |
| `FundingRateModel` | Fade the funding crowd; dampened when it votes counter-trend |
| `MomentumModel` | Price-velocity / trend-following — added so a hard fast move can't be faded into a freefall |

Two models are kept wired but **zeroed** after their edge was disproven on live data:
`MLForecastModel` (direction classification didn't survive a 225-day retrain — see
[`docs/ml_retrain_report.md`](docs/ml_retrain_report.md)) and `LiquidationHeatmapModel`
(structurally inert on normal tape). They still compute and log; they just don't vote.

### Risk engine & gates

`reaper/risk/manager.py` enforces a mainnet-safe **floor** (`config.yaml → risk:`):

- Minimum confidence (`0.62`) and a quorum of agreeing active voters (`min_model_agreement`).
- Per-trade and per-day drawdown limits, max concurrent positions, max per symbol,
  max leverage, max spread.
- ATR-based stop loss, **breakeven profit lock**, trailing stop, R-multiple take-profit,
  and max-hold timeouts.
- Structural entry gates (spot lead/lag + OI + book) and a funding hard-block.
- Cascade-halt on liquidation cascades.

Everything above the floor is a **hot-reload override** set live from the Controls page —
no restart needed; clearing all overrides restores the `config.yaml` floor.

### Safety: dead-man's switch + liveness self-heal

Trading derivatives with a bot means a hung process can leave an open, unmanaged
position. Two independent safeguards (hardened 2026-06-25 after a real incident):

- **External watchdog** (`scripts/run_watchdog.py`, own process + systemd unit) —
  watches the heartbeat file; if it goes stale it **cancels all orders and
  market-closes every position** through its own exchange client. A hard socket
  timeout means it can never wedge mid-flatten.
- **Internal liveness watchdog** (in `run_bot.py`) — a thread that force-exits the
  process if the main loop stalls past `max(90, 3·heartbeat_interval)` seconds;
  systemd (`Restart=always`) then relaunches a warm bot. Self-heal with no operator
  action.
- All SDK REST calls are bounded by a 20s timeout (`exchange_client.py`), so no
  single stalled HTTP request can freeze the loop in the first place.

---

## Architecture

The system runs as a set of independent systemd services (`systemd/`):

| Service | Process | What it does |
|---------|---------|--------------|
| `hl-reaper` | `scripts/run_bot.py` | The trading loop |
| `hl-watchdog` | `scripts/run_watchdog.py` | Dead-man's switch — flattens on stale heartbeat |
| `hl-dashboard` | `dashboard/api.py` | FastAPI bridge on `127.0.0.1:8801` |
| `hl-frontend` | Next.js (`dashboard/web`) | Web UI on `:8888` |
| `hl-recorder` | `scripts/run_recorder.py` | L2/OI microstructure capture for book models |
| `hl-liq-poller` | `scripts/run_liquidation_poller.py` | Liquidation event collector |
| `hl-spot-poller` | `scripts/run_spot_poller.py` | Spot price feed for lead/lag |

The **dashboard** (`dashboard/`) is a Python FastAPI bridge that reads the bot's
SQLite DB and exposes status/positions/signals/history; the **Next.js frontend**
(`dashboard/web`) renders the Live, Signals, Risk, History, and Controls pages and
writes hot-reload overrides + commands back through the bridge.

```
hl-reaper/
├── config.yaml                 # network, coins, risk floor, model weights, gates
├── .env                        # HL_REAPER_SECRET (API/agent wallet key) — never committed
├── reaper/
│   ├── config.py               # yaml + env loader, live-override merge
│   ├── db.py                   # SQLite: trades, signals, funding, equity, state, live_config
│   ├── aggregator.py           # weighted regime-aware ensemble, dual-band weight sets
│   ├── data/                   # websocket_feed, rest_pollers, spot_poller, recorder, buffer
│   ├── models/                 # ta, mean_reversion, orderbook_imbalance, vwap, funding_rate,
│   │                           #   momentum, regime_detector, ml_forecast, cascade_*, ...
│   ├── risk/                   # manager (gates + position management), state machine
│   └── execution/exchange_client.py   # SDK wrapper, precision-safe orders, REST timeout
├── scripts/                    # run_bot, run_watchdog, run_recorder, pollers, backtests, tests
├── dashboard/
│   ├── api.py                  # FastAPI bridge (:8801)
│   └── web/                    # Next.js frontend (:8888)
├── systemd/                    # one unit file per service
└── docs/                       # backtests, attribution reports, mainnet checklist
```

---

## Setup

Requires Python 3.10+, Node.js (for the dashboard), and a Hyperliquid account.

```bash
# 1. clone into /opt/hl-reaper and create the venv
cd /opt/hl-reaper
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. configure the API wallet
cp .env.example .env
nano .env          # paste your API/agent wallet private key (NOT your main wallet key)
chmod 600 .env
```

Generate an **API/agent wallet** at https://app.hyperliquid-testnet.xyz/API while
connected with your main wallet, authorize it, and put its private key in `.env`.
Set your main account address and network in `config.yaml` (`account_address`,
`network: testnet | mainnet`). The main wallet's private key is never used.

### Smoke tests (run in order)

```bash
venv/bin/python scripts/test_connection.py          # 1. connectivity + balance, no orders
venv/bin/python scripts/test_order.py               # 2. rest a limit far from mid, then cancel
venv/bin/python scripts/test_order.py --roundtrip   # 3. open & close ~$12 of BTC (testnet)
```

### Run

```bash
# foreground (for development)
venv/bin/python scripts/run_bot.py

# or as managed services
sudo cp systemd/*.service /etc/systemd/system/
sudo nano /etc/systemd/system/hl-reaper.service     # set User= and paths
sudo systemctl daemon-reload
sudo systemctl enable --now hl-reaper hl-watchdog hl-dashboard hl-frontend
journalctl -u hl-reaper -f
```

The dashboard is then at `http://<host>:8888` (frontend) backed by the bridge on
`127.0.0.1:8801`.

---

## Operations

- **Restart the bot:** `systemctl restart hl-reaper.service` (and `hl-watchdog.service`).
  A fresh bot REST-backfills its candles and comes back warm in ~45s.
- **Dashboard shows OFFLINE / "no bot heartbeat":** the bridge can't see a fresh
  heartbeat. Check `curl -s 127.0.0.1:8801/api/status` — `heartbeat_age_s` tells you
  whether the bot loop is alive. A stale heartbeat with the process still running
  means a hung loop (the liveness watchdog now self-heals this).
- **Tune live:** the Controls page writes hot-reload overrides into the
  `live_config` table; the loop re-reads them each cycle. Clearing an override falls
  back to the `config.yaml` floor.
- **Going to mainnet:** work through [`docs/MAINNET_CHECKLIST.md`](docs/MAINNET_CHECKLIST.md) —
  several gates are zeroed on testnet (where OI/spot barely move) and **must** be
  restored, or the structural gates are meaningless.

---

## Research & backtests

The strategy is the product of measured experiments, not assumption. The
[`docs/`](docs/) folder documents what was tested and why models live or die,
including:

- `microstructure_backtest_report.md` — order-book imbalance edge (the strongest signal)
- `spot_perp_leadlag_backtest.md` — spot→perp lead/lag
- `oi_decomposition_backtest.md` — open-interest decomposition
- `ml_retrain_report.md` — why direction-classification ML was zeroed
- `cascade_*` — liquidation-cascade fade/bounce assessments
- `live_attribution_report.md`, `eth_regime_change_attribution.md` — live P&L attribution

`scripts/` includes the corresponding `backtest_*.py`, `train_ml_model.py`,
`tune_params.py`, and per-model `test_*.py` harnesses.

---

## Disclaimer

For research and educational purposes. Automated trading of leveraged perpetual
futures can lose your entire balance. Use testnet. You are responsible for any use
of this software and any funds you put at risk.
