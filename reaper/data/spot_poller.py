"""External spot price poller (spot-perp lead/lag research track).

Polls Binance spot ticker prices for the target coins and (a) writes them into
a MarketBuffer via buf.on_spot() for future live signal use, and (b) records
them to daily gzip JSONL so the lead/lag analysis can be repeated on live
HL-perp-vs-spot data later. Standalone — never imported by run_bot.py, no live
trading effect. Additive only.

Output (data/recorded/):
  spot_{COIN}_{YYYYMMDD}.jsonl.gz   {"ts", "px"}
"""
import gzip
import json
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from reaper.logger import get_logger

log = get_logger("spot_poller")

# public market-data mirror — api.binance.com is geo-blocked (451) from some
# regions, data-api.binance.vision serves the same unauthenticated endpoints
BINANCE_SPOT = "https://data-api.binance.vision/api/v3/ticker/price"
POLL_S = 5.0
MIN_FREE_GB = 2.0
SYMBOL = lambda coin: f"{coin}USDT"  # noqa: E731


class SpotPoller:
    def __init__(self, coins: list[str], out_dir: Path, buf=None,
                 poll_s: float = POLL_S, record: bool = True):
        self.coins = coins
        self.out_dir = out_dir
        self.record = record
        # only the recorder owns the gzip files; an in-process poller that just
        # feeds a live MarketBuffer (record=False) must not touch disk, or it
        # would corrupt the standalone recorder's daily files.
        if record:
            out_dir.mkdir(parents=True, exist_ok=True)
        self.buf = buf
        self.poll_s = poll_s
        self._sym_to_coin = {SYMBOL(c): c for c in coins}
        self._stop = threading.Event()
        self._files: dict[tuple[str, str], gzip.GzipFile] = {}
        self.polls = 0
        self.last_px: dict[str, float] = {}

    # ------------------------------------------------------------------
    def start(self):
        threading.Thread(target=self._loop, daemon=True,
                         name="spot-poll").start()
        log.info("polling Binance spot for %d coins every %.1fs -> %s",
                 len(self.coins), self.poll_s, self.out_dir)

    def stop(self):
        self._stop.set()
        for f in self._files.values():
            try:
                f.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _file(self, coin: str):
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        key = (coin, day)
        if key not in self._files:
            for old in [k for k in self._files
                        if k[0] == coin and k[1] != day]:
                try:
                    self._files.pop(old).close()
                except Exception:
                    pass
            path = self.out_dir / f"spot_{coin}_{day}.jsonl.gz"
            self._files[key] = gzip.open(path, "at")
        return self._files[key]

    def _disk_ok(self) -> bool:
        free_gb = shutil.disk_usage(self.out_dir).free / 1e9
        if free_gb < MIN_FREE_GB:
            log.error("low disk (%.1f GB free) — spot recording paused",
                      free_gb)
            return False
        return True

    def _loop(self):
        symbols = json.dumps(list(self._sym_to_coin.keys()),
                             separators=(",", ":"))
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self._poll_once(symbols)
            except Exception as e:
                log.warning("spot poll failed: %s", e)
            self._stop.wait(max(0.5, self.poll_s - (time.time() - t0)))
        for f in self._files.values():
            try:
                f.flush()
            except Exception:
                pass

    def _poll_once(self, symbols: str):
        r = requests.get(BINANCE_SPOT, params={"symbols": symbols}, timeout=15)
        r.raise_for_status()
        data = r.json()
        ts = int(time.time() * 1000)
        self.polls += 1
        record = self.record and self._disk_ok()
        for row in data:
            coin = self._sym_to_coin.get(row["symbol"])
            if not coin:
                continue
            px = float(row["price"])
            self.last_px[coin] = px
            if self.buf is not None:
                self.buf.on_spot(coin, px, ts)
            if record:
                self._file(coin).write(
                    json.dumps({"ts": ts, "px": px},
                               separators=(",", ":")) + "\n")

    def status_line(self) -> str:
        pxs = " ".join(f"{c}={self.last_px.get(c, 0):g}" for c in self.coins)
        return f"polls={self.polls} | {pxs}"
