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
