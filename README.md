# HL Reaper — Phase 1: Foundation & Data Layer

No strategy logic yet. This phase proves: stable data streaming, working
storage, and verified order placement on **testnet**.

## 1. Install on the server

```bash
sudo mkdir -p /opt/hl-reaper && sudo chown $USER /opt/hl-reaper
# copy this project's files into /opt/hl-reaper, then:
cd /opt/hl-reaper
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 2. Configure

```bash
cp .env.example .env
nano .env        # paste your API wallet private key
chmod 600 .env
```

Generate the API wallet at https://app.hyperliquid-testnet.xyz/API while
connected with your main wallet (0xBC6F...08b0). Name it, authorize it,
copy its private key into `.env`. The main wallet's key is never used.

`config.yaml` is already set to testnet with your account address.

## 3. Test sequence (in order)

**Step 1 — connection smoke test (no orders):**
```bash
venv/bin/python scripts/test_connection.py
```
Expect 5 green [OK] lines incl. your USDC balance. Fix anything red
before continuing.

**Step 2 — order test (limit rest + cancel):**
```bash
venv/bin/python scripts/test_order.py
```
Rests a BUY 20% below mid, verifies it, cancels it. Nothing fills.

**Step 3 — full round trip (opens & closes a real testnet position):**
```bash
venv/bin/python scripts/test_order.py --roundtrip
```
Market-opens ~$12 of BTC, holds 5s, closes. Check the testnet UI —
you should see the fill appear and disappear.

**Step 4 — run the data layer:**
```bash
venv/bin/python scripts/run_phase1.py
```
Watch for a STATUS line every 30s with live mids, candle counts and
funding. Ctrl+C to stop. `feed_age` should stay under ~5s.

## 4. Install as a service (24h stability test)

```bash
sudo cp systemd/hl-reaper.service /etc/systemd/system/
sudo nano /etc/systemd/system/hl-reaper.service   # fix User= if needed
sudo systemctl daemon-reload
sudo systemctl enable --now hl-reaper
journalctl -u hl-reaper -f
```

## 5. Phase 1 exit criteria checklist

- [ ] test_connection.py: all 5 checks pass
- [ ] test_order.py: limit rest + cancel verified
- [ ] test_order.py --roundtrip: position opened and closed cleanly
- [ ] run_phase1.py via systemd: 24h uptime, no crash
      (`systemctl status hl-reaper` shows no restarts:
       `journalctl -u hl-reaper | grep -c "starting"` should be 1)
- [ ] WS resilience: `feed_age` recovers after forced disconnect
      (briefly pull network or restart wifi — feed should reconnect)
- [ ] SQLite populated: `sqlite3 data/hl_reaper.db "select count(*) from
      funding_history; select * from equity_snapshots limit 3;"`

All green → report back and we start **Phase 2: Risk Engine** (RiskManager
state machine, all 4 guard layers, watchdog dead-man's switch).

## Project layout

```
hl-reaper/
├── config.yaml                 # network, coins, intervals, pollers
├── .env                        # HL_REAPER_SECRET (API wallet key)
├── reaper/
│   ├── config.py               # yaml + env loader
│   ├── logger.py
│   ├── db.py                   # SQLite: trades/signals/funding/equity/state
│   ├── data/
│   │   ├── buffer.py           # thread-safe rolling market data
│   │   ├── websocket_feed.py   # WS: candles + l2Book + trades, auto-reconnect
│   │   └── rest_pollers.py     # funding, OI, mark px, equity snapshots
│   └── execution/
│       └── exchange_client.py  # SDK wrapper, precision-safe orders
├── scripts/
│   ├── test_connection.py      # step 1
│   ├── test_order.py           # steps 2-3
│   └── run_phase1.py           # step 4 / systemd entrypoint
└── systemd/hl-reaper.service
```
