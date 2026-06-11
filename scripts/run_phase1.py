#!/usr/bin/env python3
"""Phase 1 main loop — data layer only, no trading.

Starts the WebSocket feed + REST pollers, writes a heartbeat file, and
logs a status line every 30s. This is what runs 24h for the Phase 1
stability exit criteria.
"""
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.config import Config
from reaper.data.buffer import MarketBuffer
from reaper.data.rest_pollers import RestPollers
from reaper.data.websocket_feed import WebSocketFeed
from reaper.db import DB
from reaper.logger import get_logger

log = get_logger("main")
_running = True


def _sig(_s, _f):
    global _running
    _running = False


def main():
    cfg = Config()
    log.setLevel(cfg.log_level)
    log.info("HL Reaper Phase 1 starting — network=%s coins=%s",
             cfg.network, cfg.coins)

    db = DB(cfg.db_path)
    db.set_state("phase", "1")
    db.set_state("status", "starting")

    buf = MarketBuffer(cfg.coins, cfg.candle_intervals,
                       cfg.candle_buffer_size)
    feed = WebSocketFeed(cfg.api_url, buf, cfg.candle_intervals,
                         cfg.stale_feed_seconds)
    pollers = RestPollers(cfg.api_url, cfg, buf, db)

    feed.start()
    pollers.start()
    db.set_state("status", "running")

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    hb_path = Path(cfg.heartbeat_path)
    last_status = 0.0
    try:
        while _running:
            time.sleep(1)
            now = time.time()
            # heartbeat (watchdog in Phase 2 will consume this)
            hb_path.write_text(str(int(now)))
            # status line
            if now - last_status >= 30:
                last_status = now
                stale = buf.seconds_since_msg()
                log.info("STATUS | %s | feed_age=%.1fs",
                         buf.status_line(), stale)
    finally:
        log.info("shutting down...")
        db.set_state("status", "stopped")
        pollers.stop()
        feed.stop()


if __name__ == "__main__":
    main()
