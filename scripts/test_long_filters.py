#!/usr/bin/env python3
"""LONG-bleed filter tests:
  Change A — funding hard-block in SignalAggregator (extends the 0.6x veto)
  Change B — LONG-only microstructure confirmation gate (long_confirmation_count)

SCALP BAND RETIRED 2026-06-26 — the LONG/SHORT structural gates and pump/dump
cooldowns were removed with the scalp band, so their tests are gone. The funding
hard-block (Change A), the Change-B confirmation count, and the MarketBuffer
rolling-history helpers remain (still used by the trend-only loop / dashboard).
No network, no live services — pure Ticket objects + a MarketBuffer."""
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from reaper.aggregator import SignalAggregator  # noqa: E402
from reaper.data.buffer import MarketBuffer  # noqa: E402
from reaper.models import LONG, SHORT, FLAT, Ticket  # noqa: E402
from run_bot import long_confirmation_count  # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" +
          (f" — {detail}" if detail and not ok else ""))
    PASS += ok
    FAIL += not ok


def tk(model, direction, conf=0.7):
    return Ticket(model, direction, conf)


# A neutral regime ticket so adjusted_weights uses the base set.
REGIME = tk("RegimeDetectorModel", "TRENDING_UP", 0.5)


# ---------------------------------------------------------------------------
print("--- Change A: funding hard-block ---")
agg = SignalAggregator(funding_hard_block_enabled=True,
                       funding_hard_block_conf=0.75)

# net-LONG ensemble (TA + OB LONG) but funding SHORT at 0.83 >= 0.75 -> blocked
blocked = agg.aggregate("BTC", [
    REGIME,
    tk("TAModel", LONG, 0.80),
    tk("OrderbookImbalanceModel", LONG, 0.80),
    tk("FundingRateModel", SHORT, 0.83),
])
check("LONG blocked when funding SHORT conf 0.83 >= 0.75",
      blocked.direction == FLAT and blocked.confidence == 0.0)
check("block reason logged in meta",
      "funding_hard_block" in blocked.meta.get("block_reason", ""))

# funding SHORT but only 0.70 < gate -> LONG survives (dampened by 0.6x veto)
survives = agg.aggregate("BTC", [
    REGIME,
    tk("TAModel", LONG, 0.80),
    tk("OrderbookImbalanceModel", LONG, 0.80),
    tk("FundingRateModel", SHORT, 0.70),
])
check("LONG NOT blocked when funding conf 0.70 < 0.75",
      survives.direction == LONG and "block_reason" not in survives.meta)

# SHORT verdict with extreme funding SHORT -> never blocked (funding agrees)
short_ok = agg.aggregate("BTC", [
    REGIME,
    tk("TAModel", SHORT, 0.80),
    tk("OrderbookImbalanceModel", SHORT, 0.80),
    tk("FundingRateModel", SHORT, 0.90),
])
check("SHORT unaffected by extreme funding (0.90 SHORT)",
      short_ok.direction == SHORT and "block_reason" not in short_ok.meta)

# disabled -> no block even at extreme funding
agg_off = SignalAggregator(funding_hard_block_enabled=False)
not_blocked = agg_off.aggregate("BTC", [
    REGIME,
    tk("TAModel", LONG, 0.80),
    tk("OrderbookImbalanceModel", LONG, 0.80),
    tk("FundingRateModel", SHORT, 0.90),
])
check("disabled flag -> LONG not hard-blocked",
      not_blocked.direction == LONG)

# boundary: exactly at gate (0.75) blocks
at_gate = agg.aggregate("BTC", [
    REGIME,
    tk("TAModel", LONG, 0.80),
    tk("OrderbookImbalanceModel", LONG, 0.80),
    tk("FundingRateModel", SHORT, 0.75),
])
check("funding conf exactly 0.75 -> blocked (>=)",
      at_gate.direction == FLAT)


# ---------------------------------------------------------------------------
print("\n--- Change B: LONG microstructure confirmation ---")
MODELS = {"OrderbookImbalanceModel", "VWAPModel"}


def votes(*tickets):
    return {t.model: t for t in tickets}


# OB + VWAP both FLAT, TA LONG -> 0 confirmers -> would skip (min 1)
v0 = votes(tk("TAModel", LONG), tk("OrderbookImbalanceModel", FLAT),
           tk("VWAPModel", FLAT))
check("OB=FLAT VWAP=FLAT -> 0 confirmers (skip)",
      long_confirmation_count(v0, MODELS) == 0)

# OB LONG, VWAP FLAT -> 1 confirmer -> passes min 1
v1 = votes(tk("TAModel", LONG), tk("OrderbookImbalanceModel", LONG),
           tk("VWAPModel", FLAT))
check("OB=LONG VWAP=FLAT -> 1 confirmer (passes)",
      long_confirmation_count(v1, MODELS) == 1)

# OB FLAT, VWAP LONG -> 1 confirmer -> passes
v2 = votes(tk("TAModel", LONG), tk("OrderbookImbalanceModel", FLAT),
           tk("VWAPModel", LONG))
check("OB=FLAT VWAP=LONG -> 1 confirmer (passes)",
      long_confirmation_count(v2, MODELS) == 1)

# OB SHORT counts as NOT confirming LONG
v3 = votes(tk("TAModel", LONG), tk("OrderbookImbalanceModel", SHORT),
           tk("VWAPModel", FLAT))
check("OB=SHORT -> 0 confirmers (SHORT != LONG confirm)",
      long_confirmation_count(v3, MODELS) == 0)

# both confirm -> 2
v4 = votes(tk("OrderbookImbalanceModel", LONG), tk("VWAPModel", LONG))
check("OB=LONG VWAP=LONG -> 2 confirmers", long_confirmation_count(v4, MODELS) == 2)

# the gate is irrelevant to SHORTs — it's only consulted for LONG verdicts in
# run_bot; confirm the count helper simply doesn't credit SHORT votes as LONG
v5 = votes(tk("OrderbookImbalanceModel", SHORT), tk("VWAPModel", SHORT))
check("all-SHORT confirmers -> 0 (gate can't block a SHORT path)",
      long_confirmation_count(v5, MODELS) == 0)


# ---------------------------------------------------------------------------
print("\n--- MarketBuffer rolling history helpers ---")


def make_buf():
    return MarketBuffer(coins=["BTC"], intervals=["5m"], maxlen=500)


# spot/oi history: craft timestamps so we can look back exactly 5 minutes.
b = make_buf()
now = time.time()
b.spot_history["BTC"] = deque(
    [(now - 300, 100.0), (now - 150, 100.3), (now - 1, 100.5)], maxlen=60)
b.oi_history["BTC"] = deque(
    [(now - 300, 1000.0), (now - 1, 1010.0)], maxlen=60)
check("spot_price_n_minutes_ago(5) finds the ~5m sample",
      b.spot_price_n_minutes_ago("BTC", 5) == 100.0)
check("oi_n_minutes_ago(5) finds the ~5m sample",
      b.oi_n_minutes_ago("BTC", 5) == 1000.0)

# insufficient / stale history -> None (fail safe)
b2 = make_buf()
b2.spot_history["BTC"] = deque([(now - 1, 100.5)], maxlen=60)  # only "now"
check("no ~5m-old spot sample -> None (fail safe)",
      b2.spot_price_n_minutes_ago("BTC", 5) is None)
check("empty OI history -> None (fail safe)",
      b2.oi_n_minutes_ago("BTC", 5) is None)




print("\n" + "=" * 40)
print(f"RESULT: {PASS}/{PASS + FAIL} LONG-filter checks passed")
print("LONG FILTER TEST:", "PASS" if FAIL == 0 else "FAIL")
sys.exit(0 if FAIL == 0 else 1)
