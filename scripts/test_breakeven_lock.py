#!/usr/bin/env python3
"""Breakeven profit lock test: drives RiskManager.check_open_positions() with
synthetic positions to verify the breakeven SL snap fires at breakeven_lock_r,
only ever tightens the stop, latches once, mirrors for SHORT, and coexists with
the existing trailing stop. No network, no live services — mirrors the
run_phase2_test.py / test_cascade_bounce.py style."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.risk.manager import RiskManager
from reaper.risk.state import BotState

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" +
          (f" — {detail}" if detail and not ok else ""))
    PASS += ok
    FAIL += not ok


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
        class _Info:
            def __init__(s, eq): s.eq = eq
            def user_state(s, a):
                return {"marginSummary": {"accountValue": str(s.eq)}}
        self.info = _Info(equity)

    def positions(self): return []
    def cancel_all(self): return 0
    def market_close(self, c): return {"ok": True}


class FakeBuf:
    def __init__(self):
        self.coins = ["BTC"]
        self._mid = {"BTC": 100.0}
        self.ctx = {"BTC": {"mark_px": 100.0}}

    def set_mid(self, coin, px): self._mid[coin] = px
    def mid(self, coin): return self._mid.get(coin)
    def latest_candles(self, coin, interval, n=100): return []
    def seconds_since_msg(self): return 1.0


class FakeCfg:
    def __init__(self, risk=None):
        self._raw = {"risk": risk or {}, "trading": {},
                     "cascade_bounce": {}}
        self.stale_feed_seconds = 30
        self.heartbeat_path = "/tmp/be_test_hb"
        self.heartbeat_interval = 30
        self.coins = ["BTC"]


def make_risk(risk_cfg=None, equity=1000.0):
    Path("/tmp/be_test_hb").write_text(str(int(time.time())))
    r = RiskManager(FakeCfg(risk_cfg), FakeBuf(), FakeDB(), FakeXC(equity))
    r.state = BotState.ACTIVE
    return r


def position(coin, entry, szi, upnl):
    """Synthetic on-chain position row shaped like xc.positions() output."""
    return {"position": {"coin": coin, "szi": str(szi),
                         "entryPx": str(entry),
                         "unrealizedPnl": str(upnl)}}


# ---------------------------------------------------------------------------
print("\n--- breakeven lock: LONG ---")
# entry=100, sl=99 (r_px=1), tp=102. breakeven_lock_r=0.5 -> trips at mid>=100.5
# buffer 0.05% -> be_sl = 100 * 1.0005 = 100.05
r = make_risk()
check("defaults plumbed: enabled", r.breakeven_lock_enabled is True)
check("defaults plumbed: r=0.5", abs(r.breakeven_lock_r - 0.5) < 1e-9)
check("defaults plumbed: buffer=0.05",
      abs(r.breakeven_lock_buffer_pct - 0.05) < 1e-9)

r.register_entry("BTC", 100.0, 99.0, 102.0, True)
check("tracker seeds breakeven_locked=False",
      r._pos_track["BTC"]["breakeven_locked"] is False)

# below 0.5R -> nothing happens
r.buf.set_mid("BTC", 100.3)
acts = r.check_open_positions([position("BTC", 100.0, 1.0, 0.3)])
check("below breakeven_lock_r -> no BREAKEVEN action",
      not any(a["action"] == "BREAKEVEN" for a in acts))
check("below threshold -> SL unchanged", r._pos_track["BTC"]["sl"] == 99.0)
check("below threshold -> not latched",
      r._pos_track["BTC"]["breakeven_locked"] is False)

# reach 0.5R -> breakeven snap
r.buf.set_mid("BTC", 100.5)
acts = r.check_open_positions([position("BTC", 100.0, 1.0, 0.5)])
be = [a for a in acts if a["action"] == "BREAKEVEN"]
check("0.5R reached -> BREAKEVEN action emitted", len(be) == 1)
check("SL snapped to entry+buffer (100.05)",
      abs(r._pos_track["BTC"]["sl"] - 100.05) < 1e-6,
      f"sl={r._pos_track['BTC']['sl']}")
check("action carries new_sl", be and abs(be[0]["new_sl"] - 100.05) < 1e-6)
check("latched after firing", r._pos_track["BTC"]["breakeven_locked"] is True)

# latch prevents re-trigger even at higher R
r.buf.set_mid("BTC", 100.6)
acts = r.check_open_positions([position("BTC", 100.0, 1.0, 0.6)])
check("latch: no second BREAKEVEN action",
      not any(a["action"] == "BREAKEVEN" for a in acts))

# never moves backward: a pullback toward entry must NOT lower the SL
r.buf.set_mid("BTC", 100.2)
r.check_open_positions([position("BTC", 100.0, 1.0, 0.2)])
check("SL never retreats below breakeven (still 100.05)",
      abs(r._pos_track["BTC"]["sl"] - 100.05) < 1e-6)

# a LONG that never reaches 0.5R keeps its original SL untouched
r2 = make_risk()
r2.register_entry("BTC", 100.0, 99.0, 102.0, True)
for mid in (100.1, 100.2, 100.4):
    r2.buf.set_mid("BTC", mid)
    r2.check_open_positions([position("BTC", 100.0, 1.0, mid - 100.0)])
check("never-0.5R LONG keeps original SL (99.0)",
      r2._pos_track["BTC"]["sl"] == 99.0
      and r2._pos_track["BTC"]["breakeven_locked"] is False)

print("\n--- breakeven lock: SHORT mirror ---")
# entry=100, sl=101 (r_px=1), tp=98. trips at mid<=99.5 -> be_sl=100*0.9995=99.95
r3 = make_risk()
r3.register_entry("BTC", 100.0, 101.0, 98.0, False)
r3.buf.set_mid("BTC", 99.7)            # 0.3R, below threshold
r3.check_open_positions([position("BTC", 100.0, -1.0, 0.3)])
check("SHORT below threshold -> SL unchanged (101.0)",
      r3._pos_track["BTC"]["sl"] == 101.0)
r3.buf.set_mid("BTC", 99.5)            # 0.5R
acts = r3.check_open_positions([position("BTC", 100.0, -1.0, 0.5)])
check("SHORT 0.5R -> BREAKEVEN emitted",
      any(a["action"] == "BREAKEVEN" for a in acts))
check("SHORT SL snapped DOWN to entry-buffer (99.95)",
      abs(r3._pos_track["BTC"]["sl"] - 99.95) < 1e-6,
      f"sl={r3._pos_track['BTC']['sl']}")
check("SHORT latched", r3._pos_track["BTC"]["breakeven_locked"] is True)
# SHORT SL must only move DOWN — a bounce back up must not raise it
r3.buf.set_mid("BTC", 99.8)
r3.check_open_positions([position("BTC", 100.0, -1.0, 0.2)])
check("SHORT SL never rises back toward entry (still 99.95)",
      abs(r3._pos_track["BTC"]["sl"] - 99.95) < 1e-6)

print("\n--- interaction with trailing stop ---")
# breakeven at 0.5R, then continue to 1.5R (default trail_activation_r) and
# confirm the trailing stop activates ON TOP of the breakeven-locked SL,
# moving it further into profit.
r4 = make_risk()
r4.register_entry("BTC", 100.0, 99.0, 102.0, True)
r4.buf.set_mid("BTC", 100.5)
r4.check_open_positions([position("BTC", 100.0, 1.0, 0.5)])
sl_after_be = r4._pos_track["BTC"]["sl"]
check("breakeven set first (100.05)", abs(sl_after_be - 100.05) < 1e-6)
r4.buf.set_mid("BTC", 101.5)           # 1.5R -> trailing activates
acts = r4.check_open_positions([position("BTC", 100.0, 1.0, 1.5)])
check("trailing UPDATE_SL fires at trail_activation_r",
      any(a["action"] == "UPDATE_SL" for a in acts))
check("trailing tightened SL further into profit (> breakeven SL)",
      r4._pos_track["BTC"]["sl"] > sl_after_be,
      f"sl={r4._pos_track['BTC']['sl']}")
check("trailing flag set", r4._pos_track["BTC"]["trailing"] is True)

print("\n--- disabled switch ---")
r5 = make_risk({"breakeven_lock_enabled": False})
r5.register_entry("BTC", 100.0, 99.0, 102.0, True)
r5.buf.set_mid("BTC", 100.8)           # well past 0.5R
acts = r5.check_open_positions([position("BTC", 100.0, 1.0, 0.8)])
check("disabled -> no BREAKEVEN action",
      not any(a["action"] == "BREAKEVEN" for a in acts))
check("disabled -> SL unchanged (99.0)", r5._pos_track["BTC"]["sl"] == 99.0)

# ---------------------------------------------------------------------------
print("\n" + "=" * 40)
print(f"RESULT: {PASS}/{PASS + FAIL} breakeven lock checks passed")
print("BREAKEVEN LOCK TEST:", "PASS" if FAIL == 0 else "FAIL")
sys.exit(0 if FAIL == 0 else 1)
