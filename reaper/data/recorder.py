"""Live market microstructure recorder (Phase 4.6, Action 1).

Streams L2 books + trades over the existing WebSocketFeed and polls asset
contexts (funding / OI / mark), appending everything to daily gzip JSONL
files. Purpose: build the replayable history that the orderbook-imbalance
and liquidation-heatmap models need to ever be backtested — the exchange
exposes no historical L2/OI, so every day not recording is lost coverage.

Output (data/recorded/):
  l2_{COIN}_{YYYYMMDD}.jsonl.gz      {"ts", "bids": [[px,sz]..], "asks": [...]}
  trades_{COIN}_{YYYYMMDD}.jsonl.gz  {"ts", "px", "sz", "side"}
  ctx_{COIN}_{YYYYMMDD}.jsonl.gz     {"ts", "funding", "oi", "mark", "oracle"}
"""
import gzip
import json
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from hyperliquid.info import Info

from reaper.data.buffer import MarketBuffer
from reaper.data.websocket_feed import WebSocketFeed
from reaper.logger import get_logger

log = get_logger("recorder")

BOOK_LEVELS = 20
BOOK_SAMPLE_S = 2.0
CTX_POLL_S = 60.0
MIN_FREE_GB = 2.0


class Recorder:
    def __init__(self, api_url: str, coins: list[str], out_dir: Path):
        self.api_url = api_url
        self.coins = coins
        self.out_dir = out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        self.buf = MarketBuffer(coins, ["1m"], maxlen=10)
        self.feed = WebSocketFeed(api_url, self.buf, ["1m"])
        self.info = Info(api_url, skip_ws=True)
        self._stop = threading.Event()
        self._files: dict[tuple[str, str, str], gzip.GzipFile] = {}
        self._last_trade_key: dict[str, tuple] = {}
        self.lines_written = 0

    # ------------------------------------------------------------------
    def start(self):
        self.feed.start()
        threading.Thread(target=self._ctx_loop, daemon=True,
                         name="rec-ctx").start()
        threading.Thread(target=self._sample_loop, daemon=True,
                         name="rec-sample").start()
        log.info("recording %d coins -> %s (book every %.0fs, ctx every "
                 "%.0fs)", len(self.coins), self.out_dir, BOOK_SAMPLE_S,
                 CTX_POLL_S)

    def stop(self):
        self._stop.set()
        self.feed.stop()
        for f in self._files.values():
            try:
                f.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _file(self, kind: str, coin: str):
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        key = (kind, coin, day)
        if key not in self._files:
            # close any previous day's handle for this kind/coin
            for old in [k for k in self._files
                        if k[0] == kind and k[1] == coin and k[2] != day]:
                try:
                    self._files.pop(old).close()
                except Exception:
                    pass
            path = self.out_dir / f"{kind}_{coin}_{day}.jsonl.gz"
            self._files[key] = gzip.open(path, "at")
        return self._files[key]

    def _write(self, kind: str, coin: str, obj: dict):
        self._file(kind, coin).write(json.dumps(obj, separators=(",", ":"))
                                     + "\n")
        self.lines_written += 1

    def _disk_ok(self) -> bool:
        free_gb = shutil.disk_usage(self.out_dir).free / 1e9
        if free_gb < MIN_FREE_GB:
            log.error("low disk (%.1f GB free) — recording paused", free_gb)
            return False
        return True

    # ------------------------------------------------------------------
    def _sample_loop(self):
        """Every BOOK_SAMPLE_S: snapshot books, drain new trades."""
        while not self._stop.is_set():
            t0 = time.time()
            if self._disk_ok():
                try:
                    self._sample_once()
                except Exception as e:
                    log.error("sample failed: %s", e)
            self._stop.wait(max(0.2, BOOK_SAMPLE_S - (time.time() - t0)))
        # flush handles on shutdown
        for f in self._files.values():
            try:
                f.flush()
            except Exception:
                pass

    def _sample_once(self):
        for coin in self.coins:
            book = self.buf.books.get(coin)
            if book and book.get("bids"):
                self._write("l2", coin, {
                    "ts": book["ts"],
                    "bids": [[p, s] for p, s in book["bids"][:BOOK_LEVELS]],
                    "asks": [[p, s] for p, s in book["asks"][:BOOK_LEVELS]],
                })
            # drain trades newer than the last recorded one
            last = self._last_trade_key.get(coin)
            newest = last
            for tr in list(self.buf.trades.get(coin) or []):
                key = (tr["ts"], tr["px"], tr["sz"], tr["side"])
                if last is not None and key <= last:
                    continue
                self._write("trades", coin, {
                    "ts": tr["ts"], "px": tr["px"], "sz": tr["sz"],
                    "side": tr["side"]})
                if newest is None or key > newest:
                    newest = key
            if newest is not None:
                self._last_trade_key[coin] = newest

    def _ctx_loop(self):
        while not self._stop.is_set():
            try:
                meta, ctxs = self.info.meta_and_asset_ctxs()
                names = [u["name"] for u in meta["universe"]]
                ts = int(time.time() * 1000)
                for coin in self.coins:
                    if coin not in names:
                        continue
                    ctx = ctxs[names.index(coin)]
                    self._write("ctx", coin, {
                        "ts": ts,
                        "funding": float(ctx.get("funding", 0)),
                        "oi": float(ctx.get("openInterest", 0)),
                        "mark": float(ctx.get("markPx", 0)),
                        "oracle": float(ctx.get("oraclePx", 0)),
                    })
            except Exception as e:
                log.warning("ctx poll failed: %s", e)
            self._stop.wait(CTX_POLL_S)
