"""In-memory market data store. All writes come from WS callbacks /
pollers; all reads from the (future) signal loop. Thread-safe."""
import threading
import time
from collections import deque


class MarketBuffer:
    def __init__(self, coins: list[str], intervals: list[str], maxlen: int):
        self._lock = threading.RLock()
        self.coins = coins
        # candles[coin][interval] -> deque of dicts:
        #   {t, T, o, h, l, c, v, n}  (HL candle fields)
        self.candles = {
            c: {iv: deque(maxlen=maxlen) for iv in intervals} for c in coins
        }
        # latest l2 book per coin: {"bids":[(px,sz),..], "asks":[...], "ts":ms}
        self.books = {c: None for c in coins}
        # recent trades per coin
        self.trades = {c: deque(maxlen=200) for c in coins}
        # asset contexts (funding, OI, mark px) per coin
        self.ctx = {c: {} for c in coins}
        # external SPOT reference (Binance spot) per coin, for spot-perp
        # lead/lag — additive, written by the spot poller, read by future
        # signal code. {"px": float, "ts": ms}
        self.spot = {c: {} for c in coins}
        # rolling history for the LONG structural gate (2026-06-17). Spot poller
        # fires ~every 10s and ctx poller ~every 60s, so 60 samples covers ~10min
        # of spot and ~60min of OI — enough to look back 5 minutes either way.
        # Entries are (wall_ts_seconds, value).
        self.spot_history = {c: deque(maxlen=60) for c in coins}
        self.oi_history = {c: deque(maxlen=60) for c in coins}
        # staleness tracking
        self.last_msg_ts = 0.0
        self.msg_count = 0

    # ---------- writers ----------
    def on_candle(self, coin: str, interval: str, candle: dict):
        with self._lock:
            dq = self.candles[coin][interval]
            if dq and dq[-1]["t"] == candle["t"]:
                dq[-1] = candle          # update in-progress candle
            else:
                dq.append(candle)        # new candle started
            self._touch()

    def on_book(self, coin: str, levels: list, ts: int):
        # levels = [bids, asks]; each entry {"px","sz","n"}
        with self._lock:
            self.books[coin] = {
                "bids": [(float(x["px"]), float(x["sz"])) for x in levels[0]],
                "asks": [(float(x["px"]), float(x["sz"])) for x in levels[1]],
                "ts": ts,
            }
            self._touch()

    def on_trades(self, coin: str, fills: list):
        with self._lock:
            for t in fills:
                self.trades[coin].append({
                    "px": float(t["px"]), "sz": float(t["sz"]),
                    "side": t["side"], "ts": int(t["time"]),
                })
            self._touch()

    def on_ctx(self, coin: str, ctx: dict):
        with self._lock:
            self.ctx[coin] = ctx
            oi = ctx.get("open_interest")
            if oi:
                self.oi_history[coin].append((time.time(), float(oi)))

    def on_spot(self, coin: str, px: float, ts: int):
        """External spot reference price (Binance spot). Additive — does not
        affect existing readers."""
        with self._lock:
            self.spot[coin] = {"px": px, "ts": ts}
            self.spot_history[coin].append((time.time(), float(px)))

    def _touch(self):
        self.last_msg_ts = time.time()
        self.msg_count += 1

    # ---------- readers ----------
    def mid(self, coin: str) -> float | None:
        with self._lock:
            b = self.books.get(coin)
            if not b or not b["bids"] or not b["asks"]:
                return None
            return (b["bids"][0][0] + b["asks"][0][0]) / 2

    def spot_price(self, coin: str) -> float | None:
        with self._lock:
            return (self.spot.get(coin) or {}).get("px")

    @staticmethod
    def _value_n_minutes_ago(hist, minutes: float, tol_s: float = 120):
        """Closest recorded value to ~`minutes` ago. None if no sample within
        `tol_s` of the target (insufficient / stale history -> fail safe)."""
        target_ts = time.time() - minutes * 60
        best = None
        best_gap = None
        for ts, val in hist:
            gap = abs(ts - target_ts)
            if best_gap is None or gap < best_gap:
                best_gap, best = gap, val
        if best is None or best_gap > tol_s:
            return None
        return best

    def spot_price_n_minutes_ago(self, coin: str, minutes: float = 5):
        """Spot price from ~N minutes ago, or None if insufficient history."""
        with self._lock:
            return self._value_n_minutes_ago(
                list(self.spot_history.get(coin, [])), minutes)

    def oi_n_minutes_ago(self, coin: str, minutes: float = 5):
        """Open interest from ~N minutes ago, or None if insufficient history."""
        with self._lock:
            return self._value_n_minutes_ago(
                list(self.oi_history.get(coin, [])), minutes)

    def latest_candles(self, coin: str, interval: str, n: int = 100):
        with self._lock:
            return list(self.candles[coin][interval])[-n:]

    def seconds_since_msg(self) -> float:
        return time.time() - self.last_msg_ts if self.last_msg_ts else 1e9

    def status_line(self) -> str:
        with self._lock:
            parts = []
            for c in self.coins:
                mid = self.mid(c)
                n1m = len(self.candles[c].get("1m", []))
                fr = self.ctx[c].get("funding")
                parts.append(
                    f"{c}: mid={mid:.2f} 1m_candles={n1m} funding={fr}"
                    if mid else f"{c}: (no book yet)"
                )
            return " | ".join(parts) + f" | msgs={self.msg_count}"
