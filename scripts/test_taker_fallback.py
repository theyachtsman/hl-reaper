#!/usr/bin/env python3
"""Intelligent taker fallback test: drives MakerTimeoutTracker streak logic
and run_taker_fallback's three decision paths (signal-degraded skip, move-
exhausted skip, live-signal fire) with mocked aggregator/exchange/buffer.
Mirrors run_phase2_test.py style — no network, no live services touched."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_bot import MakerTimeoutTracker, run_taker_fallback  # noqa: E402

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
def candle_list(n=30, hl=1.0, close=100.0) -> list[dict]:
    """Flat 1m candles with a fixed high-low range -> ATR ~= hl."""
    t0 = int(time.time() * 1000)
    return [{"t": t0 + i * 60_000, "T": t0 + (i + 1) * 60_000,
             "o": str(close), "h": str(close + hl / 2),
             "l": str(close - hl / 2), "c": str(close), "v": "10", "n": 5}
            for i in range(n)]


class Sig:
    def __init__(self, direction, confidence, long_votes=0, short_votes=0):
        self.direction = direction
        self.confidence = confidence
        self.long_votes = long_votes
        self.short_votes = short_votes


class FakeAgg:
    def __init__(self, sig): self.sig = sig
    def aggregate(self, coin, tickets, weights=None, regime_routing=True):
        return self.sig


class FakeXC:
    def __init__(self, fill_px="100.5"):
        self.calls = []
        self.fill_px = fill_px
    def market_open(self, coin, is_buy, usd_size, slippage: float = 0.01):
        self.calls.append((coin, is_buy, usd_size))
        return {"response": {"data": {"statuses": [
            {"filled": {"avgPx": self.fill_px, "totalSz": "0.5"}}]}}}


class FakeDB:
    def __init__(self): self.trades = []
    def log_trade(self, coin, side, action, **kw):
        self.trades.append({"coin": coin, "side": side,
                            "action": action, **kw})


class FakeBuf:
    def __init__(self, mid, candles=None, books=None):
        self._mid = mid
        self._candles = candles if candles is not None else candle_list()
        self.books = books or {}
    def mid(self, coin): return self._mid
    def latest_candles(self, coin, interval, n=100): return self._candles[-n:]


def fallback(buf, xc, db, sig, *, is_long, start_mid):
    return run_taker_fallback(
        "BTC", is_long, 50.0, models=[], aggregator=FakeAgg(sig), buf=buf,
        xc=xc, db=db, min_confidence=0.35, min_model_agreement=3,
        exhaustion_atr_mult=1.5, start_mid=start_mid)


# ---------------------------------------------------------------------------
print("\n--- MakerTimeoutTracker: streak counting ---")
trk = MakerTimeoutTracker(n=3, window_s=180)
t0 = 1_000.0
s = trk.record_timeout("BTC", "SHORT", 100.0, now=t0)
check("first timeout -> count 1", s["count"] == 1)
check("start_mid anchored at first timeout", s["start_mid"] == 100.0)
s = trk.record_timeout("BTC", "SHORT", 99.0, now=t0 + 30)
s = trk.record_timeout("BTC", "SHORT", 98.0, now=t0 + 60)
check("third consecutive timeout -> count 3", s["count"] == 3)
check("start_mid unchanged through the streak", s["start_mid"] == 100.0)

# direction flip resets the streak
s = trk.record_timeout("BTC", "LONG", 98.0, now=t0 + 90)
check("direction flip resets count to 1", s["count"] == 1)
check("direction flip re-anchors start_mid", s["start_mid"] == 98.0)

# window expiry resets the streak
trk2 = MakerTimeoutTracker(n=3, window_s=180)
trk2.record_timeout("BTC", "SHORT", 100.0, now=t0)
trk2.record_timeout("BTC", "SHORT", 99.0, now=t0 + 60)
s = trk2.record_timeout("BTC", "SHORT", 98.0, now=t0 + 300)  # > window
check("timeout past window resets count to 1", s["count"] == 1)

# explicit reset (called on any fill / skip)
trk.reset("BTC")
check("reset clears the coin's streak", "BTC" not in trk._streaks)

# ---------------------------------------------------------------------------
print("\n--- run_taker_fallback: signal-degraded SKIP ---")
# confidence fell below min_confidence on re-check
xc = FakeXC(); db = FakeDB()
buf = FakeBuf(mid=99.5)
res = fallback(buf, xc, db, Sig("SHORT", 0.20, short_votes=5),
               is_long=False, start_mid=100.0)
check("low confidence -> taker_skipped_degraded",
      res["status"] == "taker_skipped_degraded")
check("degraded skip fires NO market order", len(xc.calls) == 0)
check("degraded skip logged to trades table",
      any(t["status"] == "taker_skipped_degraded" for t in db.trades))

# direction flipped on re-check
xc = FakeXC(); db = FakeDB()
res = fallback(FakeBuf(mid=100.5), xc, db, Sig("LONG", 0.9, long_votes=5),
               is_long=False, start_mid=100.0)
check("direction flip -> taker_skipped_degraded",
      res["status"] == "taker_skipped_degraded" and len(xc.calls) == 0)

# agreement fell below quorum on re-check
xc = FakeXC(); db = FakeDB()
res = fallback(FakeBuf(mid=99.5), xc, db, Sig("SHORT", 0.9, short_votes=1),
               is_long=False, start_mid=100.0)
check("agreement below quorum -> taker_skipped_degraded",
      res["status"] == "taker_skipped_degraded" and len(xc.calls) == 0)

# ---------------------------------------------------------------------------
print("\n--- run_taker_fallback: move-exhausted SKIP ---")
# price already ran > 1.5x ATR in the signal direction (LONG, atr~1.0)
xc = FakeXC(); db = FakeDB()
res = fallback(FakeBuf(mid=102.0), xc, db, Sig("LONG", 0.9, long_votes=5),
               is_long=True, start_mid=100.0)
check("price moved > 1.5xATR our way -> taker_skipped_exhausted",
      res["status"] == "taker_skipped_exhausted")
check("exhausted skip fires NO market order", len(xc.calls) == 0)
check("exhausted skip logged to trades table",
      any(t["status"] == "taker_skipped_exhausted" for t in db.trades))

# orderbook flipped against a SHORT (bid-heavy = reversal up)
xc = FakeXC(); db = FakeDB()
buf = FakeBuf(mid=99.0,  # only 1.0 move, NOT price-exhausted
              books={"BTC": {"bids": [(99.0, 50.0)], "asks": [(99.1, 2.0)]}})
res = fallback(buf, xc, db, Sig("SHORT", 0.9, short_votes=5),
               is_long=False, start_mid=100.0)
check("book turned bid-heavy vs SHORT -> taker_skipped_exhausted",
      res["status"] == "taker_skipped_exhausted" and len(xc.calls) == 0)

# ---------------------------------------------------------------------------
print("\n--- run_taker_fallback: live signal FIRES taker ---")
xc = FakeXC(fill_px="100.5"); db = FakeDB()
# only 0.5 move (< 1.5xATR), neutral book, conf + votes still pass
buf = FakeBuf(mid=100.5,
              books={"BTC": {"bids": [(100.4, 10.0)], "asks": [(100.6, 10.0)]}})
res = fallback(buf, xc, db, Sig("LONG", 0.90, long_votes=5),
               is_long=True, start_mid=100.0)
check("live signal + live move -> taker_fallback",
      res["status"] == "taker_fallback")
check("taker_fallback fires exactly one market order", len(xc.calls) == 1)
check("market order is a BUY for a LONG", xc.calls and xc.calls[0][1] is True)
check("fill price returned to caller", res["fill_px"] == 100.5)
check("taker_fallback logged with fill price",
      any(t["status"] == "taker_fallback" and t.get("price") == 100.5
          for t in db.trades))

# ---------------------------------------------------------------------------
print("\n" + "=" * 40)
print(f"RESULT: {PASS}/{PASS + FAIL} taker fallback checks passed")
print("TAKER FALLBACK TEST:", "PASS" if FAIL == 0 else "FAIL")
sys.exit(0 if FAIL == 0 else 1)
