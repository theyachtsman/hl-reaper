#!/usr/bin/env python3
"""Dual-band (5m scalp + 1h trend) test suite (2026-06-20).

Verifies the band-aware aggregator + RiskManager in isolation (no network,
no live services), mirroring the test_breakeven_lock.py / test_cascade_bounce.py
fake-driven style:

  - weight sets sum to 1.0; band aggregation uses the fixed set (no regime shuffle)
  - regime bias dampens counter-trend scalp confidence, never blocks, never
    touches the trend signal
  - per-band risk geometry (SL/TP/hold/breakeven) and entry gate (conf/agreement)
  - per-band concurrency counted independently
  - coin ownership: one band per coin at a time (one-way exchange)
  - independent in-trade tracking across bands/coins
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.aggregator import (SCALP_WEIGHTS, TREND_WEIGHTS, AggregatedSignal,
                               SignalAggregator, apply_regime_bias)
from reaper.models import LONG, SHORT, FLAT, Ticket
from reaper.risk.manager import RiskManager
from reaper.risk.state import BotState

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" +
          (f" — {detail}" if detail and not ok else ""))
    PASS += bool(ok)
    FAIL += (not ok)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class FakeDB:
    def __init__(self): self.kv = {}
    def get_state(self, k): return self.kv.get(k)
    def set_state(self, k, v): self.kv[k] = v
    def log_trade(self, *a, **k): pass


class FakeXC:
    account_address = "0xTEST"

    def __init__(self, equity=1000.0):
        self._positions = []
        class _Info:
            def __init__(s, eq): s.eq = eq
            def user_state(s, a):
                return {"marginSummary": {"accountValue": str(s.eq)}}
        self.info = _Info(equity)

    def set_positions(self, lst): self._positions = lst
    def positions(self): return self._positions
    def cancel_all(self): return 0
    def market_close(self, c): return {"ok": True}


class FakeBuf:
    def __init__(self):
        self.coins = ["BTC", "ETH", "SOL", "ARB"]
        self._mid = {"BTC": 100.0, "ETH": 50.0, "SOL": 20.0, "ARB": 1.0}
        self.ctx = {c: {"mark_px": self._mid[c]} for c in self.coins}
        # tight book (spread well under max_spread_pct) for every coin used
        self.books = {c: {"bids": [[self._mid[c] * 0.9999, 5]],
                          "asks": [[self._mid[c] * 1.0001, 5]]}
                      for c in self.coins}

    def set_mid(self, coin, px): self._mid[coin] = px
    def mid(self, coin): return self._mid.get(coin)
    def latest_candles(self, coin, interval, n=100):
        # flat 1m candles so atr_from_candles returns a usable value
        return [{"t": i, "o": "100", "h": "101", "l": "99", "c": "100",
                 "v": "1"} for i in range(n)]
    def seconds_since_msg(self): return 1.0


class FakeCfg:
    def __init__(self, risk=None, trading=None):
        self._raw = {"risk": risk or {}, "trading": trading or {},
                     "cascade_bounce": {}, "per_coin": {}}
        self.stale_feed_seconds = 30
        self.heartbeat_path = "/tmp/dualband_test_hb"
        self.heartbeat_interval = 30
        self.coins = ["BTC", "ETH"]


def make_risk(risk_cfg=None, trading_cfg=None, equity=1000.0):
    Path("/tmp/dualband_test_hb").write_text(str(int(time.time())))
    r = RiskManager(FakeCfg(risk_cfg, trading_cfg), FakeBuf(), FakeDB(),
                    FakeXC(equity))
    r.state = BotState.ACTIVE
    return r


def position(coin, entry, szi, upnl=0.0):
    return {"position": {"coin": coin, "szi": str(szi),
                         "entryPx": str(entry),
                         "unrealizedPnl": str(upnl)}}


def tk(model, direction, conf):
    return Ticket(model, direction, conf, {})


# ===========================================================================
print("\n--- weight sets ---")
check("SCALP_WEIGHTS sum == 1.0", abs(sum(SCALP_WEIGHTS.values()) - 1.0) < 1e-9,
      str(sum(SCALP_WEIGHTS.values())))
check("TREND_WEIGHTS sum == 1.0", abs(sum(TREND_WEIGHTS.values()) - 1.0) < 1e-9,
      str(sum(TREND_WEIGHTS.values())))
check("scalp: mean reversion dominant",
      SCALP_WEIGHTS["MeanReversionModel"] == max(SCALP_WEIGHTS.values()))
check("trend: mean reversion zeroed",
      TREND_WEIGHTS["MeanReversionModel"] == 0.0)

# ===========================================================================
print("\n--- band aggregation uses fixed weights (no regime shuffle) ---")
agg = SignalAggregator()
# A TRENDING regime would, under legacy routing, slam MeanReversion to ~0.02.
# In band mode (regime_routing=False) it must stay at its fixed scalp weight.
tickets = [
    tk("RegimeDetectorModel", "TRENDING_UP", 1.0),
    tk("MeanReversionModel", SHORT, 0.9),     # fade the local pop
    tk("OrderbookImbalanceModel", SHORT, 0.5),
    tk("TAModel", LONG, 0.3),
]
scalp = agg.aggregate("BTC", tickets, weights=SCALP_WEIGHTS,
                      regime_routing=False)
check("scalp aggregates SHORT (mean reversion carries it)",
      scalp.direction == SHORT, scalp.direction)
check("scalp weights used are the fixed scalp set",
      abs(scalp.weights["MeanReversionModel"] - 0.38) < 1e-9,
      str(scalp.weights.get("MeanReversionModel")))
check("regime string still surfaced", scalp.regime == "TRENDING_UP",
      scalp.regime)
# trend band on the same tape, fixed trend weights
trend = agg.aggregate("BTC", tickets, weights=TREND_WEIGHTS,
                      regime_routing=False)
check("trend weights used are the fixed trend set",
      abs(trend.weights["MeanReversionModel"] - 0.0) < 1e-9,
      str(trend.weights.get("MeanReversionModel")))

# ===========================================================================
print("\n--- regime bias (1h regime dampens counter-trend scalp) ---")
def mk_sig(direction, conf):
    return AggregatedSignal(coin="BTC", direction=direction, confidence=conf,
                            model_votes={}, long_votes=2, short_votes=2,
                            flat_votes=0, regime="RANGING", ts=0)

s = mk_sig(SHORT, 0.80)
apply_regime_bias(s, "TRENDING_UP", 0.7)
check("counter-trend short in uptrend dampened 0.80->0.56",
      abs(s.confidence - 0.56) < 1e-9, str(s.confidence))
s = mk_sig(LONG, 0.80)
apply_regime_bias(s, "TRENDING_UP", 0.7)
check("aligned long in uptrend untouched", abs(s.confidence - 0.80) < 1e-9,
      str(s.confidence))
s = mk_sig(LONG, 0.80)
apply_regime_bias(s, "TRENDING_DOWN", 0.7)
check("counter-trend long in downtrend dampened", abs(s.confidence - 0.56) < 1e-9,
      str(s.confidence))
s = mk_sig(SHORT, 0.80)
apply_regime_bias(s, "RANGING", 0.7)
check("ranging regime never dampens", abs(s.confidence - 0.80) < 1e-9,
      str(s.confidence))
# the bias is one-directional: a trend signal would never be passed through it
# (run_bot only calls apply_regime_bias on the scalp signal) — assert the helper
# is a pure confidence multiplier and leaves direction intact
s = mk_sig(SHORT, 0.80)
d0 = s.direction
apply_regime_bias(s, "TRENDING_UP", 0.7)
check("bias never changes direction", s.direction == d0)

# ===========================================================================
print("\n--- per-band risk params resolve distinctly ---")
risk = make_risk()
sp = risk.band_params("scalp")
tp = risk.band_params("trend")
check("scalp tight SL mult (1.0)", sp["atr_sl_multiplier"] == 1.0)
check("trend wide SL mult (2.5)", tp["atr_sl_multiplier"] == 2.5)
check("scalp short hold (0.5h)", sp["max_hold_hours"] == 0.5)
check("trend long hold (48h)", tp["max_hold_hours"] == 48.0)
check("scalp lower conf gate (0.40)", sp["min_confidence"] == 0.40)
check("trend higher conf gate (0.55)", tp["min_confidence"] == 0.55)
check("scalp structural gates on", sp["structural_gates_enabled"] is True)
check("trend structural gates off", tp["structural_gates_enabled"] is False)
legacy = risk.band_params(None)
check("band=None falls back to legacy globals",
      legacy["min_confidence"] == risk.min_confidence)

# ===========================================================================
print("\n--- calc_sl_tp uses band geometry ---")
# entry 100, atr 1.0. scalp: SL 100-1*1=99, TP 100+1.5*1=101.5
sl_s, tp_s = risk.calc_sl_tp("BTC", 100.0, True, 1.0, band="scalp")
check("scalp LONG SL=99.0", abs(sl_s - 99.0) < 1e-9, str(sl_s))
check("scalp LONG TP=101.5", abs(tp_s - 101.5) < 1e-9, str(tp_s))
# trend: SL 100-2.5=97.5, TP 100+4.0*2.5=110
sl_t, tp_t = risk.calc_sl_tp("BTC", 100.0, True, 1.0, band="trend")
check("trend LONG SL=97.5", abs(sl_t - 97.5) < 1e-9, str(sl_t))
check("trend LONG TP=110.0", abs(tp_t - 110.0) < 1e-9, str(tp_t))

# ===========================================================================
print("\n--- register_entry records band + band max-hold ---")
risk.register_entry("BTC", 100.0, 99.0, 101.5, True, band="scalp")
check("scalp tracker band stamped", risk._pos_track["BTC"]["band"] == "scalp")
check("scalp max_hold_s = 0.5h", risk._pos_track["BTC"]["max_hold_s"] == 1800.0,
      str(risk._pos_track["BTC"]["max_hold_s"]))
risk.register_entry("ETH", 50.0, 48.75, 55.0, True, band="trend")
check("trend max_hold_s = 48h", risk._pos_track["ETH"]["max_hold_s"] == 172800.0,
      str(risk._pos_track["ETH"]["max_hold_s"]))

# ===========================================================================
print("\n--- can_open per-band entry gate ---")
risk = make_risk()
risk.xc.set_positions([])
# scalp gate: conf 0.45 >= 0.40, agreement 2 >= 2 -> OK
ok, why = risk.can_open("BTC", LONG, 0.45, 2, 3.0, band="scalp")
check("scalp accepts conf 0.45 / agree 2", ok, why)
# same numbers fail the trend gate (needs 0.55 / 3)
ok, why = risk.can_open("BTC", LONG, 0.45, 2, 3.0, band="trend")
check("trend rejects conf 0.45 (needs 0.55)", not ok and "confidence" in why, why)
ok, why = risk.can_open("BTC", LONG, 0.60, 2, 3.0, band="trend")
check("trend rejects agreement 2 (needs 3)",
      not ok and "agreement" in why, why)
ok, why = risk.can_open("BTC", LONG, 0.60, 3, 3.0, band="trend")
check("trend accepts conf 0.60 / agree 3", ok, why)

# ===========================================================================
print("\n--- per-band concurrency counted independently ---")
risk = make_risk()
# fill scalp to its limit (3) on three coins, tracked as scalp
for c in ("BTC", "ETH", "SOL"):
    risk._pos_track[c] = {"band": "scalp"}
risk.xc.set_positions([position("BTC", 100, 1), position("ETH", 50, 1),
                       position("SOL", 20, 1)])
ok, why = risk.can_open("ARB", LONG, 0.60, 3, 3.0, band="scalp")
check("scalp blocked at scalp_max_concurrent (3)",
      not ok and "scalp max concurrent" in why, why)
# trend has its own budget (2) — 3 open scalps do not count against it
ok, why = risk.can_open("ARB", LONG, 0.60, 3, 3.0, band="trend")
check("trend NOT crowded out by 3 open scalps", ok, why)

# ===========================================================================
print("\n--- coin ownership: one band per coin ---")
risk = make_risk()
risk._pos_track["BTC"] = {"band": "trend"}
risk.xc.set_positions([position("BTC", 100, 1)])
ok, why = risk.can_open("BTC", SHORT, 0.60, 3, 3.0, band="scalp")
check("scalp blocked on a coin already held by trend",
      not ok and "already holding" in why and "trend" in why, why)
# a different free coin is fine for scalp
ok, why = risk.can_open("ETH", SHORT, 0.45, 2, 3.0, band="scalp")
check("scalp free to take a different coin", ok, why)

# ===========================================================================
print("\n--- in-trade guards use the position's band geometry ---")
risk = make_risk()
# scalp BTC: entry 100, sl 99 (r=1), breakeven_lock_r=0.4 -> trips at mid>=100.4
risk.register_entry("BTC", 100.0, 99.0, 101.5, True, band="scalp")
# trend ETH: entry 50, sl 48.75 (r=1.25), breakeven_lock_r=0.8 -> trips at
# mid>=50+0.8*1.25=51.0
risk.register_entry("ETH", 50.0, 48.75, 55.0, True, band="trend")
risk.buf.set_mid("BTC", 100.5)   # +0.5R for scalp -> breakeven should fire
risk.buf.set_mid("ETH", 50.6)    # +0.48R for trend -> NOT yet (needs 0.8R)
positions = [position("BTC", 100, 1, upnl=0.5), position("ETH", 50, 1, upnl=0.6)]
actions = risk.check_open_positions(positions)
be = {a["coin"]: a for a in actions if a["action"] == "BREAKEVEN"}
check("scalp BTC breakeven fired at 0.5R (scalp r=0.4)", "BTC" in be,
      str(actions))
check("scalp breakeven action carries band",
      be.get("BTC", {}).get("band") == "scalp")
check("trend ETH breakeven NOT fired yet (trend r=0.8)", "ETH" not in be,
      str(actions))
check("BTC scalp tracker latched breakeven",
      risk._pos_track["BTC"]["breakeven_locked"] is True)
check("ETH trend tracker independent (not latched)",
      risk._pos_track["ETH"]["breakeven_locked"] is False)

# now push trend ETH past 0.8R (mid>=51.0) and confirm it fires independently
risk.buf.set_mid("ETH", 51.1)
actions = risk.check_open_positions(
    [position("BTC", 100, 1, upnl=0.5), position("ETH", 50, 1, upnl=1.1)])
be = {a["coin"]: a for a in actions if a["action"] == "BREAKEVEN"}
check("trend ETH breakeven fires at 0.8R", "ETH" in be, str(actions))
check("trend breakeven action carries band",
      be.get("ETH", {}).get("band") == "trend")

# ===========================================================================
print("\n" + "=" * 40)
print(f"RESULT: {PASS}/{PASS + FAIL} dual-band checks passed")
print("DUAL BAND TEST: " + ("PASS" if FAIL == 0 else "FAIL"))
sys.exit(1 if FAIL else 0)
