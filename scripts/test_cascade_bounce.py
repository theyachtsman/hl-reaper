#!/usr/bin/env python3
"""Cascade bounce test: synthetic cascades against CascadeBounceModel and
the RiskManager allocation guard / CASCADE_BOUNCE_ACTIVE state transitions.
Mirrors run_phase2_test.py style — no network, no live services touched."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.models.cascade_bounce import CascadeBounceModel
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
# synthetic market
# ---------------------------------------------------------------------------
def candles(prices: list[float], vols: list[float],
            end_ms: int | None = None) -> list[dict]:
    """1m candles from close prices; each bar's low/high hug the closes."""
    n = len(prices)
    end_ms = end_ms or int(time.time() * 1000)
    out = []
    for i, (px, v) in enumerate(zip(prices, vols)):
        t = end_ms - (n - i) * 60_000
        prev = prices[i - 1] if i else px
        out.append({"t": t, "T": t + 60_000, "o": str(prev),
                    "h": str(max(prev, px) * 1.0002),
                    "l": str(min(prev, px) * 0.9998),
                    "c": str(px), "v": str(v), "n": 5})
    return out


class FakeBuf:
    def __init__(self):
        self.coins = ["BTC"]
        self.candle_list: list[dict] = []
        self.ctx = {"BTC": {"funding": 0.0, "open_interest": 1000.0,
                            "mark_px": 100.0}}

    def latest_candles(self, coin, interval, n=100):
        return self.candle_list[-n:]

    def mid(self, coin):
        return float(self.candle_list[-1]["c"]) if self.candle_list else None


def crash_tape(stable_bars_after_low: int, end_ms: int) -> FakeBuf:
    """60 flat bars @100, then 5-bar cascade to 97 on 6x volume, then
    `stable_bars_after_low` bars that hold above the low."""
    px = [100.0] * 60 + [99.4, 98.8, 98.2, 97.6, 97.0]
    vol = [10.0] * 60 + [60.0] * 5
    px += [97.3] * stable_bars_after_low
    vol += [30.0] * stable_bars_after_low
    buf = FakeBuf()
    buf.candle_list = candles(px, vol, end_ms)
    return buf


# ---------------------------------------------------------------------------
print("\n--- CascadeBounceModel: detection & signal ---")
now_ms = int(time.time() * 1000)

m = CascadeBounceModel({"min_cascade_move_pct": 0.015,
                        "cascade_window_minutes": 5,
                        "min_volume_mult": 3.0,
                        "stabilization_bars": 2})

# calm tape -> nothing
calm = FakeBuf()
calm.candle_list = candles([100.0] * 75, [10.0] * 75, now_ms)
check("calm tape -> no signal", m.compute("BTC", calm) is None)
check("calm tape leaves model IDLE", m._st["BTC"]["phase"] == "IDLE"
      if "BTC" in m._st else True)

# cascade bar arrives -> detected, but no entry while still falling
m2 = CascadeBounceModel({"stabilization_bars": 2})
buf = crash_tape(0, now_ms)
sig = m2.compute("BTC", buf)
check("cascade detected, no signal while falling", sig is None
      and m2._st["BTC"]["phase"] == "CASCADING")
check("extreme tracked at the low",
      abs(m2._st["BTC"]["extreme"] - 97.0 * 0.9998) < 0.02)

# one stable bar -> still waiting (needs 2)
buf1 = crash_tape(1, now_ms + 60_000)
check("1 stable bar < stabilization_bars -> still no signal",
      m2.compute("BTC", buf1) is None)

# two stable bars -> LONG bounce signal fired once, then cooldown
buf2 = crash_tape(2, now_ms + 120_000)
sig = m2.compute("BTC", buf2)
check("bounce signal after stabilization", sig is not None)
check("bounce side is LONG (fading a down-cascade)",
      bool(sig) and sig["side"] == "LONG")
check("signal carries cascade magnitude",
      bool(sig) and sig["cascade_move_pct"] <= -0.015)
check("model enters cooldown after firing",
      m2._st["BTC"]["phase"] == "COOLDOWN")
check("no re-trigger during cooldown",
      m2.compute("BTC", buf2) is None)

# volume-less drop -> not a cascade
m3 = CascadeBounceModel({})
quiet = FakeBuf()
quiet.candle_list = candles([100.0] * 60 + [99.4, 98.8, 98.2, 97.6, 97.0],
                            [10.0] * 65, now_ms)
check("price move without volume spike -> ignored",
      m3.compute("BTC", quiet) is None and
      m3._st["BTC"]["phase"] == "IDLE")

# up-cascade (short squeeze) -> SHORT bounce
m4 = CascadeBounceModel({"stabilization_bars": 1})
px = [100.0] * 60 + [100.6, 101.2, 101.8, 102.4, 103.0] + [102.6]
vol = [10.0] * 60 + [60.0] * 5 + [30.0]
up = FakeBuf()
up.candle_list = candles(px, vol, now_ms)
m4.compute("BTC", up)              # detect
sig = m4.compute("BTC", up)        # stabilized (1 bar off the high)
check("up-cascade fades SHORT", bool(sig) and sig["side"] == "SHORT")

# knife: keeps making new lows past stale window -> episode abandoned
m5 = CascadeBounceModel({"cascade_stale_minutes": 0.0})
m5.compute("BTC", crash_tape(0, now_ms))
check("stale cascade abandoned (never catch the knife)",
      m5.compute("BTC", crash_tape(0, now_ms + 60_000)) is None
      and m5._st["BTC"]["phase"] == "IDLE")

# ---------------------------------------------------------------------------
print("\n--- RiskManager: allocation guard & state transitions ---")


class FakeDB:
    def __init__(self): self.kv = {}
    def get_state(self, k): return self.kv.get(k)
    def set_state(self, k, v): self.kv[k] = v
    def log_trade(self, *a, **k): pass


class FakeXC:
    account_address = "0xTEST"
    def __init__(self, equity=1000.0, held=None):
        self.held = held or []
        class _Info:
            def __init__(s, eq): s.eq = eq
            def user_state(s, a):
                return {"marginSummary": {"accountValue": str(s.eq)}}
        self.info = _Info(equity)
    def positions(self):
        return [{"position": {"coin": c, "szi": "1.0"}} for c in self.held]
    def cancel_all(self): return 0
    def market_close(self, c): return {"ok": True}


class FakeCfg:
    def __init__(self):
        self._raw = {"risk": {}, "trading": {"mode": "conservative"},
                     "cascade_bounce": {"allocation_pct": 0.12,
                                        "max_hold_minutes": 20}}
        self.stale_feed_seconds = 30
        self.heartbeat_path = "/tmp/cb_test_hb"
        self.heartbeat_interval = 30
        self.coins = ["BTC"]


class GuardBuf(FakeBuf):
    def seconds_since_msg(self): return 1.0


Path("/tmp/cb_test_hb").write_text(str(int(time.time())))
risk = RiskManager(FakeCfg(), GuardBuf(), FakeDB(), FakeXC(equity=1000.0))
risk.state = BotState.ACTIVE

ok, reason, max_usd = risk.check_cascade_bounce_allocation("BTC")
check("allocation allowed when ACTIVE", ok, reason)
check("sized at 12% of equity ($1000 -> $120)", abs(max_usd - 120.0) < 0.01,
      f"got {max_usd}")
check("config plumbed: max_hold 20min", risk.cb_max_hold_minutes == 20)

risk_held = RiskManager(FakeCfg(), GuardBuf(), FakeDB(),
                        FakeXC(equity=1000.0, held=["BTC"]))
risk_held.state = BotState.ACTIVE
ok, reason, _ = risk_held.check_cascade_bounce_allocation("BTC")
check("denied when already holding the coin", not ok and "holding" in reason)

risk_small = RiskManager(FakeCfg(), GuardBuf(), FakeDB(), FakeXC(equity=50.0))
risk_small.state = BotState.ACTIVE
ok, reason, _ = risk_small.check_cascade_bounce_allocation("BTC")
check("denied when 12% of equity < min order", not ok and "min order" in reason)

for bad in (BotState.HALTED, BotState.MANAGING, BotState.COOLDOWN,
            BotState.CASCADE_BOUNCE_ACTIVE):
    risk.state = bad
    ok, reason, _ = risk.check_cascade_bounce_allocation("BTC")
    check(f"denied in state {bad.value}", not ok)

risk.state = BotState.ACTIVE
risk.enter_cascade_bounce("BTC")
check("enter -> CASCADE_BOUNCE_ACTIVE",
      risk.state == BotState.CASCADE_BOUNCE_ACTIVE)
ok, _, _ = risk.check_cascade_bounce_allocation("ETH")
check("second bounce vetoed while one is open", not ok)
risk.exit_cascade_bounce()
check("exit -> ACTIVE", risk.state == BotState.ACTIVE)
risk.exit_cascade_bounce()
check("exit is a no-op when not in bounce state",
      risk.state == BotState.ACTIVE)

# ensemble gate blocked during bounce
risk.enter_cascade_bounce("BTC")
allowed, why = risk.can_open("BTC", "LONG", 0.99, 8, 3.0)
check("ensemble can_open blocked during bounce",
      not allowed and "CASCADE_BOUNCE_ACTIVE" in why)
risk.exit_cascade_bounce()

# 20-minute hold registered correctly
risk.register_entry("BTC", 100.0, 99.25, 101.0, True,
                    hold_hours=risk.cb_max_hold_minutes / 60)
check("bounce hold cap = 20min",
      abs(risk._pos_track["BTC"]["max_hold_s"] - 1200) < 1)

# ---------------------------------------------------------------------------
print("\n" + "=" * 40)
print(f"RESULT: {PASS}/{PASS + FAIL} cascade bounce checks passed")
print("CASCADE BOUNCE TEST:", "PASS" if FAIL == 0 else "FAIL")
sys.exit(0 if FAIL == 0 else 1)
