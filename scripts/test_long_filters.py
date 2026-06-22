#!/usr/bin/env python3
"""LONG-bleed filter tests:
  Change A — funding hard-block in SignalAggregator (extends the 0.6x veto)
  Change B — LONG-only microstructure confirmation gate (long_confirmation_count)
  LONG STRUCTURAL GATE (2026-06-17) — supersedes Change B: a LONG must clear
    ALL of {spot leading, OI rising, book bid-heavy}, else blocked.

All filters touch LONG entries only; SHORTs must be provably unaffected.
No network, no live services — pure Ticket objects + a MarketBuffer.
Mirrors test_taker_fallback.py style."""
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from reaper.aggregator import SignalAggregator  # noqa: E402
from reaper.data.buffer import MarketBuffer  # noqa: E402
from reaper.models import LONG, SHORT, FLAT, Ticket  # noqa: E402
from run_bot import (long_confirmation_count,  # noqa: E402
                     long_structural_gate, long_structural_params,
                     _momentum_cooldown_ok,
                     short_structural_gate, short_structural_params,
                     _dump_cooldown_ok)

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


# ---------------------------------------------------------------------------
print("\n--- LONG structural gate (spot + OI + book, ALL required) ---")
PARAMS = long_structural_params({}, {})  # defaults: 0.0002 / 0.001 / 0.20 / 5 / 5


def gate_buf(spot_now=100.5, spot_5m=100.0, perp_5m=100.2,
             best_bid=100.39, best_ask=100.41,
             bid_sz=70.0, ask_sz=30.0, oi_now=1010.0, oi_5m=1000.0,
             candle_closes=None):
    """A MarketBuffer wired so long_structural_gate can read every signal.
    Defaults form a clean ALL-PASS LONG setup; override one to fail one.

    candle_closes: optional list of 5m candle closes (oldest→newest) used by the
    momentum cooldown (Signal 4). Defaults to a flat, no-pump series whose last
    two closes still satisfy the Signal-1 perp reference ([-2]=perp_5m)."""
    bf = make_buf()
    n = time.time()
    bf.spot["BTC"] = {"px": spot_now, "ts": int(n * 1000)}
    bf.spot_history["BTC"] = deque([(n - 300, spot_5m), (n - 1, spot_now)],
                                   maxlen=60)
    bf.oi_history["BTC"] = deque([(n - 300, oi_5m), (n - 1, oi_now)], maxlen=60)
    bf.ctx["BTC"] = {"open_interest": oi_now}
    bf.on_book("BTC",
               [[{"px": best_bid, "sz": bid_sz, "n": 1}],
                [{"px": best_ask, "sz": ask_sz, "n": 1}]], int(n * 1000))
    # 5m candles: Signal 1 needs >=2 ([-2] close is the "perp 5m ago" ref);
    # Signal 4 reads up to the last 4 closes. Default: a flat series ending in
    # [perp_5m, spot_now] so no pump fires and Signal 1 keeps its reference.
    closes = (candle_closes if candle_closes is not None
              else [spot_now, spot_now, perp_5m, spot_now])
    for i, c in enumerate(closes):
        bf.on_candle("BTC", "5m", {"t": i, "c": c})
    return bf


allowed, d = long_structural_gate("BTC", gate_buf(), PARAMS)
check("ALL three pass -> LONG allowed", allowed and d["allowed"], str(d))
check("  spot_leading flagged", d["spot_leading"])
check("  oi_rising flagged", d["oi_rising"])
check("  ob_bid_heavy flagged", d["ob_bid_heavy"])

# fail signal 1: spot not leading (spot flat, perp up)
allowed, d = long_structural_gate(
    "BTC", gate_buf(spot_now=100.0, spot_5m=100.0), PARAMS)
check("spot not leading -> blocked", not allowed
      and d["block_reason"] == "spot_not_leading")

# fail signal 2: OI falling (short covering, not fresh longs)
allowed, d = long_structural_gate(
    "BTC", gate_buf(oi_now=990.0, oi_5m=1000.0), PARAMS)
check("OI not rising -> blocked", not allowed
      and d["block_reason"] == "oi_not_rising")

# fail signal 3: book ask-heavy
allowed, d = long_structural_gate(
    "BTC", gate_buf(bid_sz=30.0, ask_sz=70.0), PARAMS)
check("book not bid-heavy -> blocked", not allowed
      and d["block_reason"] == "book_not_bid_heavy")

# fail-safe: missing spot history -> spot signal fails -> blocked
bf = gate_buf()
bf.spot_history["BTC"] = deque([(time.time() - 1, 100.5)], maxlen=60)
allowed, d = long_structural_gate("BTC", bf, PARAMS)
check("missing 5m spot history -> blocked (fail safe)", not allowed
      and not d["spot_leading"])

# fail-safe: no book at all -> ob signal can't compute -> blocked
bf = gate_buf()
bf.books["BTC"] = None
allowed, d = long_structural_gate("BTC", bf, PARAMS)
check("no book -> blocked (fail safe)", not allowed
      and not d["ob_bid_heavy"])

# imbalance exactly at threshold (0.20) passes (>=)
# bid/ask 60/40 -> (60-40)/100 = 0.20
allowed, d = long_structural_gate(
    "BTC", gate_buf(bid_sz=60.0, ask_sz=40.0), PARAMS)
check("imbalance exactly 0.20 -> bid-heavy passes (>=)",
      d["ob_bid_heavy"] and abs(d["imbalance"] - 0.20) < 1e-9)

# the clean ALL-PASS setup also clears Signal 4 (no recent pump)
allowed, d = long_structural_gate("BTC", gate_buf(), PARAMS)
check("ALL four pass -> momentum_ok flagged", allowed and d["momentum_ok"])


# ---------------------------------------------------------------------------
print("\n--- Signal 4: momentum cooldown (anti-pump-top) ---")


def cd_buf(closes):
    """A MarketBuffer holding only the given 5m candle closes (oldest→newest),
    for isolating the momentum cooldown helper."""
    bf = make_buf()
    for i, c in enumerate(closes):
        bf.on_candle("BTC", "5m", {"t": i, "c": c})
    return bf


# 5m pump: close jumps 100 -> 100.6 in the last bar (+0.6% > 0.5%)
ok, reason, mv = _momentum_cooldown_ok("BTC", cd_buf([100, 100, 100, 100.6]),
                                       PARAMS)
check("5m move +0.6% > 0.5% -> blocked", not ok and reason.startswith("pump_5m"),
      reason)
check("  move_5m reported ~+0.6%", abs(mv["move_5m"] - 0.006) < 1e-6)

# 10m pump while 5m is clear: +0.45% last 5m (<0.5%) but +0.85% over 10m (>0.8%)
ok, reason, mv = _momentum_cooldown_ok(
    "BTC", cd_buf([100, 100, 100.40, 100.85]), PARAMS)
check("5m clear but 10m +0.85% > 0.8% -> blocked",
      not ok and reason.startswith("pump_10m"), reason)

# 15m pump while 5m and 10m are clear: the run happened 3 bars back
ok, reason, mv = _momentum_cooldown_ok(
    "BTC", cd_buf([98.5, 99.5, 99.7, 100.0]), PARAMS)
check("5m & 10m clear but 15m +1.52% > 1.2% -> blocked",
      not ok and reason.startswith("pump_15m"), reason)

# all three windows below threshold -> allowed
ok, reason, mv = _momentum_cooldown_ok(
    "BTC", cd_buf([100.0, 100.1, 100.2, 100.3]), PARAMS)
check("all windows below threshold -> allowed", ok
      and reason.startswith("momentum_ok"), reason)

# fail-OPEN during warmup: <3 candles -> allowed even if the move looks like a pump
ok, reason, mv = _momentum_cooldown_ok("BTC", cd_buf([100, 100.6]), PARAMS)
check("insufficient candles -> allowed (fail-open)",
      ok and reason == "insufficient_candles", reason)

# disabled toggle -> never blocks, even on an obvious pump
DISABLED = {**PARAMS, "pump_cooldown_enabled": False}
ok, reason, mv = _momentum_cooldown_ok(
    "BTC", cd_buf([100, 100, 100, 102]), DISABLED)
check("cooldown disabled -> allowed regardless", ok
      and reason == "cooldown_disabled", reason)

# integration: spot/OI/book all pass but a fresh 5m pump blocks the LONG, and
# the structural gate surfaces block_reason=recent_pump (logged as
# long_blocked_recent_pump). candle_closes[-2]=100.2 keeps the Signal-1 perp ref.
allowed, d = long_structural_gate(
    "BTC", gate_buf(candle_closes=[100.0, 100.0, 100.2, 100.8]), PARAMS)
check("full gate: 3 structural pass + 5m pump -> blocked (recent_pump)",
      not allowed and d["block_reason"] == "recent_pump"
      and d["spot_leading"] and d["oi_rising"] and d["ob_bid_heavy"]
      and not d["momentum_ok"], str(d))
check("  pump_detail names the 5m window",
      d["pump_detail"].startswith("pump_5m"))

# LONGs never call long_structural_gate's SHORT counterpart; the two gates are
# disjoint by direction (asserted structurally — each is wired only into its own
# entry branch in run_bot.py).


# ---------------------------------------------------------------------------
print("\n--- SHORT structural gate (spot-lag + OI + book + dump, ALL req) ---")
SPARAMS = short_structural_params({}, {})  # defaults: 0.0002/0.001/0.20/5/5


def short_gate_buf(spot_now=99.5, spot_5m=100.0, perp_5m=99.8,
                   best_bid=99.59, best_ask=99.61,
                   bid_sz=30.0, ask_sz=70.0, oi_now=1010.0, oi_5m=1000.0,
                   candle_closes=None):
    """A MarketBuffer wired so short_structural_gate can read every signal.
    Defaults form a clean ALL-PASS SHORT setup (spot falling faster than perp,
    OI rising with falling price, book ask-heavy, no recent dump); override one
    to fail one. Mirror of gate_buf for the SHORT side."""
    bf = make_buf()
    n = time.time()
    bf.spot["BTC"] = {"px": spot_now, "ts": int(n * 1000)}
    bf.spot_history["BTC"] = deque([(n - 300, spot_5m), (n - 1, spot_now)],
                                   maxlen=60)
    bf.oi_history["BTC"] = deque([(n - 300, oi_5m), (n - 1, oi_now)], maxlen=60)
    bf.ctx["BTC"] = {"open_interest": oi_now}
    bf.on_book("BTC",
               [[{"px": best_bid, "sz": bid_sz, "n": 1}],
                [{"px": best_ask, "sz": ask_sz, "n": 1}]], int(n * 1000))
    # 5m candles: Signal 1 reads [-2] as "perp 5m ago"; Signal 4 reads up to the
    # last 4 closes. Default: a series ending in [perp_5m, spot_now] with no
    # sharp drop so the dump cooldown stays clear and Signal 1 keeps its ref.
    closes = (candle_closes if candle_closes is not None
              else [spot_now, spot_now, perp_5m, spot_now])
    for i, c in enumerate(closes):
        bf.on_candle("BTC", "5m", {"t": i, "c": c})
    return bf


allowed, d = short_structural_gate("BTC", short_gate_buf(), SPARAMS)
check("ALL four pass -> SHORT allowed", allowed and d["allowed"], str(d))
check("  spot_lagging flagged", d["spot_lagging"])
check("  oi_rising flagged", d["oi_rising"])
check("  ob_ask_heavy flagged", d["ob_ask_heavy"])
check("  momentum_ok flagged (no recent dump)", d["momentum_ok"])

# fail signal 1: spot NOT lagging (spot flat while perp falls = perp-led bounce)
allowed, d = short_structural_gate(
    "BTC", short_gate_buf(spot_now=100.0, spot_5m=100.0), SPARAMS)
check("spot not lagging -> blocked", not allowed
      and d["block_reason"] == "spot_not_lagging")

# fail signal 2: OI falling with price (long liquidation, not fresh shorts)
allowed, d = short_structural_gate(
    "BTC", short_gate_buf(oi_now=990.0, oi_5m=1000.0), SPARAMS)
check("OI not rising -> blocked", not allowed
      and d["block_reason"] == "oi_not_rising")

# fail signal 3: book bid-heavy (buyers in control)
allowed, d = short_structural_gate(
    "BTC", short_gate_buf(bid_sz=70.0, ask_sz=30.0), SPARAMS)
check("book not ask-heavy -> blocked", not allowed
      and d["block_reason"] == "book_not_ask_heavy")

# fail-safe: missing spot history -> spot signal fails -> blocked
bf = short_gate_buf()
bf.spot_history["BTC"] = deque([(time.time() - 1, 99.5)], maxlen=60)
allowed, d = short_structural_gate("BTC", bf, SPARAMS)
check("missing 5m spot history -> blocked (fail safe)", not allowed
      and not d["spot_lagging"])

# fail-safe: no book at all -> ob signal can't compute -> blocked
bf = short_gate_buf()
bf.books["BTC"] = None
allowed, d = short_structural_gate("BTC", bf, SPARAMS)
check("no book -> blocked (fail safe)", not allowed
      and not d["ob_ask_heavy"])

# imbalance exactly at -0.20 passes (<=): bid/ask 40/60 -> (40-60)/100 = -0.20
allowed, d = short_structural_gate(
    "BTC", short_gate_buf(bid_sz=40.0, ask_sz=60.0), SPARAMS)
check("imbalance exactly -0.20 -> ask-heavy passes (<=)",
      d["ob_ask_heavy"] and abs(d["imbalance"] + 0.20) < 1e-9)

# testnet override: oi_rise_threshold=0.0 lets a tiny OI tick count as rising
TESTNET = {**SPARAMS, "oi_rise_threshold": 0.0}
allowed, d = short_structural_gate(
    "BTC", short_gate_buf(oi_now=1000.4, oi_5m=1000.0), TESTNET)
check("testnet override (oi thr=0.0) -> tiny OI tick counts as rising",
      d["oi_rising"], str(d))


# ---------------------------------------------------------------------------
print("\n--- SHORT Signal 4: dump cooldown (anti-dump-bottom) ---")

# 5m dump: close drops 100 -> 99.4 in the last bar (-0.6% < -0.5%)
ok, reason, mv = _dump_cooldown_ok("BTC", cd_buf([100, 100, 100, 99.4]),
                                   SPARAMS)
check("5m move -0.6% < -0.5% -> blocked", not ok
      and reason.startswith("dump_5m"), reason)
check("  move_5m reported ~-0.6%", abs(mv["move_5m"] + 0.006) < 1e-6)

# 10m dump while 5m is clear: -0.45% last 5m (>-0.5%) but -0.85% over 10m
ok, reason, mv = _dump_cooldown_ok(
    "BTC", cd_buf([100, 100, 99.60, 99.15]), SPARAMS)
check("5m clear but 10m -0.85% < -0.8% -> blocked",
      not ok and reason.startswith("dump_10m"), reason)

# 15m dump while 5m and 10m are clear: the drop happened 3 bars back
ok, reason, mv = _dump_cooldown_ok(
    "BTC", cd_buf([101.5, 100.5, 100.3, 100.0]), SPARAMS)
check("5m & 10m clear but 15m -1.48% < -1.2% -> blocked",
      not ok and reason.startswith("dump_15m"), reason)

# all three windows above (negative) threshold -> allowed
ok, reason, mv = _dump_cooldown_ok(
    "BTC", cd_buf([100.0, 99.9, 99.8, 99.7]), SPARAMS)
check("all windows shallower than threshold -> allowed", ok
      and reason.startswith("momentum_ok"), reason)

# fail-OPEN during warmup: <3 candles -> allowed even if the move looks like dump
ok, reason, mv = _dump_cooldown_ok("BTC", cd_buf([100, 99.4]), SPARAMS)
check("insufficient candles -> allowed (fail-open)",
      ok and reason == "insufficient_candles", reason)

# disabled toggle -> never blocks, even on an obvious dump
SDISABLED = {**SPARAMS, "dump_cooldown_enabled": False}
ok, reason, mv = _dump_cooldown_ok(
    "BTC", cd_buf([100, 100, 100, 98]), SDISABLED)
check("dump cooldown disabled -> allowed regardless", ok
      and reason == "cooldown_disabled", reason)

# integration: spot/OI/book all pass but a fresh 5m dump blocks the SHORT, and
# the gate surfaces block_reason=recent_dump (logged short_blocked_recent_dump).
# candle_closes[-2]=99.8 keeps the Signal-1 perp ref.
allowed, d = short_structural_gate(
    "BTC", short_gate_buf(candle_closes=[100.0, 100.0, 99.8, 99.2]), SPARAMS)
check("full gate: 3 structural pass + 5m dump -> blocked (recent_dump)",
      not allowed and d["block_reason"] == "recent_dump"
      and d["spot_lagging"] and d["oi_rising"] and d["ob_ask_heavy"]
      and not d["momentum_ok"], str(d))
check("  dump_detail names the 5m window",
      d["dump_detail"].startswith("dump_5m"))

# LONG gate completely unaffected: the clean LONG ALL-PASS setup still fires
allowed, d = long_structural_gate("BTC", gate_buf(), PARAMS)
check("LONG gate still ALLOWS its clean setup (SHORT gate didn't touch it)",
      allowed and d["allowed"])
# and a clean SHORT setup is NOT a valid LONG (spot falling -> LONG blocked)
allowed, d = long_structural_gate(
    "BTC", gate_buf(spot_now=99.5, spot_5m=100.0,
                    bid_sz=30.0, ask_sz=70.0), PARAMS)
check("clean SHORT-shaped setup -> LONG blocked (gates disjoint)", not allowed)


# ---------------------------------------------------------------------------
print("\n" + "=" * 40)
print(f"RESULT: {PASS}/{PASS + FAIL} LONG-filter checks passed")
print("LONG FILTER TEST:", "PASS" if FAIL == 0 else "FAIL")
sys.exit(0 if FAIL == 0 else 1)
