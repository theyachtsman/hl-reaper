#!/usr/bin/env python3
"""Phase 2 risk-engine test: simulate guard triggers with mock market data
and verify state transitions / pre-trade gates / in-trade actions."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.risk.manager import CLOSE_PENDING_TIMEOUT_S, RiskManager
from reaper.risk.state import BotState

RISK_CFG = {
    "daily_drawdown_limit": 0.05,
    "severe_drawdown_limit": 0.10,
    "max_concurrent_positions": 3,
    "max_per_symbol": 1,
    "max_leverage": 5.0,
    "min_confidence": 0.62,
    "min_model_agreement": 5,
    "max_spread_pct": 0.0015,
    "atr_sl_multiplier": 1.5,
    "atr_trail_multiplier": 1.0,
    "trail_activation_r": 1.5,
    "max_loss_per_trade_pct": 0.02,
    "emergency_loss_pct": 0.03,
    "max_hold_hours_scalp": 4,
    "max_hold_hours_swing": 48,
    "cascade_oi_drop_pct": 0.15,
    "cascade_price_move_pct": 0.03,
    "cascade_window_minutes": 5,
    "cascade_halt_hours": 2,
    "extreme_funding_long_halt": 0.001,
    "extreme_funding_short_halt": -0.001,
    "flash_crash_candle_pct": 0.05,
    "weekly_drawdown_limit": 0.10,
    "cooldown_hours": 48,
}


class FakeCfg:
    def __init__(self, hb_path: str):
        self._raw = {"risk": dict(RISK_CFG)}
        self.stale_feed_seconds = 30
        self.heartbeat_path = hb_path
        self.heartbeat_interval = 30
        self.coins = ["BTC"]


def make_candles(n=60, px=100.0, last_move=0.0):
    """n flat 1m candles around px; optional % move on the last candle."""
    now_ms = int(time.time() * 1000)
    out = []
    for i in range(n):
        t = now_ms - (n - i) * 60_000
        o = c = px
        if i == n - 1 and last_move:
            c = px * (1 + last_move)
        out.append({"t": t, "T": t + 60_000, "o": str(o),
                    "h": str(max(o, c) * 1.001), "l": str(min(o, c) * 0.999),
                    "c": str(c), "v": "10", "n": 5})
    return out


class FakeBuf:
    def __init__(self):
        self.coins = ["BTC"]
        self.feed_age = 1.0
        self.mid_px = 100.0
        self.spread = 0.0005
        self.candles = make_candles()
        self.ctx = {"BTC": {"funding": 0.00001, "open_interest": 1000.0,
                            "mark_px": 100.0}}

    @property
    def books(self):
        half = self.mid_px * self.spread / 2
        return {"BTC": {"bids": [(self.mid_px - half, 5.0)],
                        "asks": [(self.mid_px + half, 5.0)],
                        "ts": int(time.time() * 1000)}}

    def mid(self, coin):
        return self.mid_px

    def latest_candles(self, coin, interval, n=100):
        return self.candles[-n:]

    def seconds_since_msg(self):
        return self.feed_age


class FakeDB:
    def __init__(self):
        self.kv = {}
        self.trades = []

    def set_state(self, k, v):
        self.kv[k] = v

    def get_state(self, k):
        return self.kv.get(k)

    def log_trade(self, coin, side, action, size=None, price=None,
                  leverage=None, order_id=None, status=None, note=None):
        self.trades.append((coin, side, action, note))

    def funding_window(self, coin, since_ms):
        return []


class FakeInfo:
    def __init__(self, xc):
        self.xc = xc

    def user_state(self, addr):
        return {"marginSummary": {"accountValue": str(self.xc.equity)}}


class FakeXC:
    def __init__(self):
        self.equity = 10_000.0
        self.account_address = "0xTEST"
        self._positions = []
        self.closed = []
        self.cancelled = False
        self.info = FakeInfo(self)

    def positions(self):
        return self._positions

    def market_close(self, coin):
        self.closed.append(coin)
        return {"status": "ok"}

    def cancel_all(self):
        self.cancelled = True
        return 0


def fresh(hb_fresh=True):
    hb = Path("/tmp/hl_reaper_test_hb")
    hb.write_text(str(int(time.time()) if hb_fresh else 0))
    cfg = FakeCfg(str(hb))
    buf, db, xc = FakeBuf(), FakeDB(), FakeXC()
    rm = RiskManager(cfg, buf, db, xc)
    # disable retry sleeps in tests by making equity cache warm
    return rm, buf, db, xc


RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append(passed)
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}" +
          (f" — {detail}" if detail and not passed else ""))


def pos_entry(coin="BTC", szi=1.0, entry=100.0, upnl=0.0):
    return {"position": {"coin": coin, "szi": str(szi), "entryPx": str(entry),
                         "unrealizedPnl": str(upnl),
                         "leverage": {"type": "cross", "value": 3}}}


def main():
    print("== HL Reaper Phase 2 risk engine test ==\n")

    print("Baseline:")
    rm, buf, db, xc = fresh()
    report("starts ACTIVE, stays ACTIVE on green guards",
           rm.check() == BotState.ACTIVE, f"state={rm.state}")

    print("\nLayer 1 — drawdowns:")
    rm, buf, db, xc = fresh()
    rm.check()                       # sets day-open baseline at 10k
    xc.equity = 9_400.0              # -6%
    rm._equity_cache = (0.0, 0.0)    # bust cache
    report("daily drawdown >5% -> MANAGING",
           rm.check() == BotState.MANAGING, f"state={rm.state}")
    ok, why = rm.can_open("BTC", "LONG", 0.9, 8, 3.0)
    report("MANAGING blocks new entries", not ok, why)

    rm, buf, db, xc = fresh()
    rm.check()
    xc._positions = [pos_entry()]
    xc.equity = 8_900.0              # -11%
    rm._equity_cache = (0.0, 0.0)
    st = rm.check()
    report("severe drawdown >10% -> HALTED",
           st == BotState.HALTED, f"state={rm.state}")
    report("severe drawdown closes all positions",
           xc.closed == ["BTC"] and xc.cancelled, f"closed={xc.closed}")

    print("\nLayer 3 — kill switches:")
    rm, buf, db, xc = fresh()
    rm.check()
    xc._positions = [pos_entry()]
    # seed cascade history: 4 min ago OI=1000 px=100; now OI=800 px=96
    rm._oi_hist["BTC"].append((time.time() - 240, 1000.0, 100.0))
    buf.ctx["BTC"]["open_interest"] = 800.0   # -20%
    buf.mid_px = 96.0                          # -4%
    st = rm.check()
    report("cascade (OI -20%, px -4% in 5min) -> HALTED",
           st == BotState.HALTED, f"state={rm.state}")
    report("cascade closes all positions", "BTC" in xc.closed,
           f"closed={xc.closed}")
    report("cascade halt is ~2h",
           abs(rm._halted_until - time.time() - 7200) < 60)

    rm, buf, db, xc = fresh()
    buf.ctx["BTC"]["funding"] = 0.0002        # 0.0016/8h > +0.001
    rm.check()
    ok_l, why_l = rm.can_open("BTC", "LONG", 0.9, 8, 3.0)
    ok_s, _ = rm.can_open("BTC", "SHORT", 0.9, 8, 3.0)
    report("extreme +funding halts longs, allows shorts",
           (not ok_l) and ok_s, f"long={why_l}")

    rm, buf, db, xc = fresh()
    buf.ctx["BTC"]["funding"] = -0.0002       # -0.0016/8h < -0.001
    rm.check()
    ok_s, why_s = rm.can_open("BTC", "SHORT", 0.9, 8, 3.0)
    ok_l, _ = rm.can_open("BTC", "LONG", 0.9, 8, 3.0)
    report("extreme -funding halts shorts, allows longs",
           (not ok_s) and ok_l, f"short={why_s}")

    rm, buf, db, xc = fresh()
    buf.candles = make_candles(last_move=0.06)   # 6% candle
    rm.check()
    ok, why = rm.can_open("BTC", "LONG", 0.9, 8, 3.0)
    report("flash-crash candle pauses entries", not ok, why)

    rm, buf, db, xc = fresh()
    rm.check()                                   # week baseline 10k
    xc.equity = 8_950.0                          # -10.5% on week
    rm.day_open_equity = 8_950.0                 # keep daily guard quiet
    rm._equity_cache = (0.0, 0.0)
    st = rm.check()
    report("weekly drawdown >10% -> COOLDOWN",
           st == BotState.COOLDOWN, f"state={rm.state}")
    report("cooldown is ~48h",
           abs(rm._cooldown_until - time.time() - 48 * 3600) < 60)

    print("\nLayer 4 — infra:")
    rm, buf, db, xc = fresh()
    buf.feed_age = 99.0
    report("stale feed -> RECONNECTING",
           rm.check() == BotState.RECONNECTING, f"state={rm.state}")
    buf.feed_age = 1.0
    report("feed recovery -> ACTIVE", rm.check() == BotState.ACTIVE)
    rm, buf, db, xc = fresh(hb_fresh=False)
    report("stale heartbeat detected", not rm.heartbeat_ok())

    print("\nLayer 1 — pre-trade gates:")
    rm, buf, db, xc = fresh()
    rm.check()
    ok, why = rm.can_open("BTC", "LONG", 0.50, 8, 3.0)
    report("low confidence blocked", not ok and "confidence" in why, why)
    ok, why = rm.can_open("BTC", "LONG", 0.9, 3, 3.0)
    report("low model agreement blocked", not ok and "agreement" in why, why)
    xc._positions = [pos_entry("BTC"), pos_entry("ETH"), pos_entry("SOL")]
    ok, why = rm.can_open("BTC", "LONG", 0.9, 8, 3.0)
    report("max concurrent positions blocked", not ok, why)
    xc._positions = [pos_entry("BTC")]
    ok, why = rm.can_open("BTC", "LONG", 0.9, 8, 3.0)
    report("per-symbol cap blocked", not ok and "holding" in why, why)
    xc._positions = []
    buf.spread = 0.004                           # 0.4% spread
    ok, why = rm.can_open("BTC", "LONG", 0.9, 8, 3.0)
    report("wide spread blocked", not ok and "spread" in why, why)
    buf.spread = 0.0005
    ok, why = rm.can_open("BTC", "LONG", 0.9, 8, 3.0)
    report("clean signal allowed", ok, why)
    report("leverage clamped to ceiling", rm.clamp_leverage(20.0) == 5.0)

    print("\nLayer 2 — stops & in-trade management:")
    rm, buf, db, xc = fresh()
    sl, tp = rm.calc_sl_tp("BTC", 100.0, True, atr=2.0)
    report("long SL/TP: sl=entry-1.5*ATR, tp at 2R",
           abs(sl - 97.0) < 1e-9 and abs(tp - 106.0) < 1e-9,
           f"sl={sl} tp={tp}")
    sl, tp = rm.calc_sl_tp("BTC", 100.0, False, atr=2.0)
    report("short SL/TP mirrored",
           abs(sl - 103.0) < 1e-9 and abs(tp - 94.0) < 1e-9,
           f"sl={sl} tp={tp}")

    rm, buf, db, xc = fresh()
    rm.check()
    rm.register_entry("BTC", 100.0, 97.0, 106.0, True)
    buf.mid_px = 96.5                            # below SL
    acts = rm.check_open_positions([pos_entry(upnl=-35)])
    report("SL breach -> CLOSE",
           any(a["action"] == "CLOSE" and "stop" in a["reason"]
               for a in acts), str(acts))

    rm, buf, db, xc = fresh()
    rm.check()
    rm.register_entry("BTC", 100.0, 97.0, 106.0, True)
    buf.mid_px = 105.0                           # +1.67R -> trailing active
    acts = rm.check_open_positions([pos_entry(upnl=50)])
    tr = rm._pos_track["BTC"]
    report("trailing stop activates >=1.5R and raises SL",
           any(a["action"] == "UPDATE_SL" for a in acts)
           and tr["sl"] > 97.0, f"acts={acts} sl={tr['sl']:.2f}")

    rm, buf, db, xc = fresh()
    rm.check()
    rm.register_entry("BTC", 100.0, 97.0, 106.0, True)
    acts = rm.check_open_positions([pos_entry(upnl=-350)])  # -3.5% of 10k
    report("emergency per-position loss -> CLOSE",
           any("EMERGENCY" in a.get("reason", "") for a in acts), str(acts))

    rm, buf, db, xc = fresh()
    rm.check()
    rm.register_entry("BTC", 100.0, 97.0, 106.0, True)
    rm._pos_track["BTC"]["opened_ts"] = time.time() - 5 * 3600  # held 5h
    acts = rm.check_open_positions([pos_entry(upnl=5)])
    report("time expiry -> CLOSE",
           any("hold" in a.get("reason", "") for a in acts), str(acts))

    print("\nClose-pending guard (duplicate-close fix):")
    # 1. after a close is marked pending, a stale position (still showing open)
    #    is NOT re-evaluated -> no duplicate CLOSE action
    rm, buf, db, xc = fresh()
    rm.check()
    rm.register_entry("BTC", 100.0, 97.0, 106.0, True)
    buf.mid_px = 96.5                            # below SL -> would CLOSE
    rm.mark_close_pending("BTC")
    acts = rm.check_open_positions([pos_entry(upnl=-35)])
    report("close-pending suppresses duplicate CLOSE on stale position",
           not any(a["action"] == "CLOSE" for a in acts), str(acts))
    report("close-pending retains the position tracker while suppressed",
           "BTC" in rm._pos_track)

    # 2. once the position is actually gone, the pending flag is cleared
    acts = rm.check_open_positions([])           # position no longer present
    report("close-pending cleared once position confirmed gone",
           "BTC" not in rm._close_pending)

    # 3. after the timeout, a still-open position is re-evaluated (stuck retry)
    rm, buf, db, xc = fresh()
    rm.check()
    rm.register_entry("BTC", 100.0, 97.0, 106.0, True)
    buf.mid_px = 96.5
    rm.mark_close_pending("BTC")
    rm._close_pending["BTC"] = time.time() - (CLOSE_PENDING_TIMEOUT_S + 1)
    acts = rm.check_open_positions([pos_entry(upnl=-35)])
    report("close-pending timeout re-evaluates a genuinely stuck position",
           any(a["action"] == "CLOSE" for a in acts)
           and "BTC" not in rm._close_pending, str(acts))

    # 4. no pending flag -> normal SL close still fires (regression)
    rm, buf, db, xc = fresh()
    rm.check()
    rm.register_entry("BTC", 100.0, 97.0, 106.0, True)
    buf.mid_px = 96.5
    acts = rm.check_open_positions([pos_entry(upnl=-35)])
    report("no pending flag -> SL close fires normally",
           any(a["action"] == "CLOSE" for a in acts), str(acts))

    print("\nTrades-table CLOSE dedup:")
    import tempfile
    from reaper.db import DB as RealDB
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    rdb = RealDB(tmp.name)
    rdb.log_trade("ETH", "SHORT", "CLOSE", note="stop loss @ 1867.3714")
    rdb.log_trade("ETH", "SHORT", "CLOSE", note="stop loss @ 1867.3714")
    rdb.log_trade("ETH", "SHORT", "CLOSE", note="stop loss @ 1867.3714")
    n_dup = rdb._conn().execute(
        "SELECT COUNT(*) FROM trades WHERE coin='ETH' AND action='CLOSE'"
    ).fetchone()[0]
    report("identical CLOSE within 60s logged only once", n_dup == 1,
           f"rows={n_dup}")
    rdb.log_trade("ETH", "SHORT", "CLOSE", note="take profit @ 1900.0")
    n_diff = rdb._conn().execute(
        "SELECT COUNT(*) FROM trades WHERE coin='ETH' AND action='CLOSE'"
    ).fetchone()[0]
    report("a different CLOSE reason is still logged", n_diff == 2,
           f"rows={n_diff}")
    Path(tmp.name).unlink(missing_ok=True)

    print(f"\n{'=' * 40}")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"RESULT: {passed}/{total} guards passed")
    if passed != total:
        print("PHASE 2 TEST: FAIL")
        sys.exit(1)
    print("PHASE 2 TEST: PASS")


if __name__ == "__main__":
    main()
