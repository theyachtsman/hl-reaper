"""WebSocket market data feed via the official SDK's Info websocket.

Subscribes to candles (per coin x interval), l2Book, and trades.
A monitor thread detects a stale feed and rebuilds the connection.
"""
import threading
import time

from hyperliquid.info import Info

from reaper.data.buffer import MarketBuffer
from reaper.logger import get_logger

log = get_logger("ws_feed")

# Candle interval string -> milliseconds (for REST backfill window math).
_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}


class WebSocketFeed:
    def __init__(self, api_url: str, buffer: MarketBuffer,
                 intervals: list[str], stale_seconds: int = 30):
        self.api_url = api_url
        self.buf = buffer
        self.intervals = intervals
        self.stale_seconds = stale_seconds
        self._info: Info | None = None
        self._stop = threading.Event()
        self._reconnects = 0

    # ---------- lifecycle ----------
    def backfill(self, per_interval: int = 500) -> int:
        """Prime the candle buffers with recent history over REST.

        Hyperliquid's candle WS subscription streams only live candles — it
        sends no historical snapshot — so after a (re)start each deque fills
        one candle per period: a 5m series needs ~2.5h to reach the 30 candles
        TAModel requires, and a 1h series needs ~30h. Backfilling on startup
        makes every model work immediately instead. Best-effort: a failed
        coin/interval just falls back to filling live. Returns total candles
        loaded. Call before start() so the buffer is warm before the loop runs.
        """
        rest = Info(self.api_url, skip_ws=True)
        now_ms = int(time.time() * 1000)
        total = 0
        for iv in self.intervals:
            span = _INTERVAL_MS.get(iv)
            if span is None:
                log.warning("backfill: unknown interval %s — skipped", iv)
                continue
            start = now_ms - per_interval * span
            for coin in self.buf.coins:
                try:
                    candles = rest.candles_snapshot(coin, iv, start, now_ms)
                except Exception as e:
                    log.warning("backfill %s/%s failed: %s", coin, iv, e)
                    continue
                for c in candles or []:
                    self.buf.on_candle(coin, iv, c)
                total += len(candles or [])
        log.info("candle backfill: %d candles over %d coins x %d intervals "
                 "(<=%d each)", total, len(self.buf.coins),
                 len(self.intervals), per_interval)
        return total

    def start(self):
        self._connect()
        threading.Thread(target=self._monitor, daemon=True,
                         name="ws-monitor").start()

    def stop(self):
        self._stop.set()
        self._teardown()

    # ---------- internals ----------
    def _connect(self):
        log.info("connecting websocket -> %s", self.api_url)
        self._info = Info(self.api_url)  # skip_ws=False -> ws manager runs
        for coin in self.buf.coins:
            self._info.subscribe(
                {"type": "l2Book", "coin": coin},
                lambda msg, c=coin: self._on_book(c, msg),
            )
            self._info.subscribe(
                {"type": "trades", "coin": coin},
                lambda msg, c=coin: self._on_trades(c, msg),
            )
            for iv in self.intervals:
                self._info.subscribe(
                    {"type": "candle", "coin": coin, "interval": iv},
                    lambda msg, c=coin, i=iv: self._on_candle(c, i, msg),
                )
        log.info("subscribed: %d coins x (book + trades + %d candle feeds)",
                 len(self.buf.coins), len(self.intervals))

    def _teardown(self):
        if self._info is not None:
            try:
                self._info.disconnect_websocket()
            except Exception:
                pass
            self._info = None

    def _monitor(self):
        while not self._stop.is_set():
            time.sleep(5)
            stale = self.buf.seconds_since_msg()
            if stale > self.stale_seconds:
                self._reconnects += 1
                backoff = min(60, 2 ** min(self._reconnects, 6))
                log.warning(
                    "feed stale %.0fs — reconnect #%d (backoff %ds)",
                    stale, self._reconnects, backoff)
                self._teardown()
                time.sleep(backoff)
                try:
                    self._connect()
                except Exception as e:
                    log.error("reconnect failed: %s", e)
            else:
                self._reconnects = 0

    # ---------- callbacks ----------
    def _on_candle(self, coin: str, interval: str, msg: dict):
        try:
            data = msg.get("data")
            if data:
                self.buf.on_candle(coin, interval, data)
        except Exception as e:
            log.error("candle cb error %s/%s: %s", coin, interval, e)

    def _on_book(self, coin: str, msg: dict):
        try:
            data = msg.get("data", {})
            levels = data.get("levels")
            if levels:
                self.buf.on_book(coin, levels, int(data.get("time", 0)))
        except Exception as e:
            log.error("book cb error %s: %s", coin, e)

    def _on_trades(self, coin: str, msg: dict):
        try:
            data = msg.get("data")
            if data:
                self.buf.on_trades(coin, data)
        except Exception as e:
            log.error("trades cb error %s: %s", coin, e)
