#!/usr/bin/env python3
"""Standalone Binance spot price poller/recorder (spot-perp lead/lag track).

Records spot prices to data/recorded/spot_{COIN}_{date}.jsonl.gz for later
lead/lag analysis on live HL-perp-vs-spot data. Never touches the live bot.

usage:
  run_spot_poller.py                 # default 7 coins, 5s poll
  run_spot_poller.py --coins BTC ETH --poll 10
"""
import argparse
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.config import PROJECT_ROOT
from reaper.data.spot_poller import SpotPoller
from reaper.logger import get_logger

log = get_logger("spot_main")
_running = True

DEFAULT_COINS = ["BTC", "ETH", "SOL", "ARB", "AVAX", "DOGE", "WIF"]


def _sig(_s, _f):
    global _running
    _running = False


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--coins", nargs="+", default=DEFAULT_COINS)
    ap.add_argument("--poll", type=float, default=5.0)
    args = ap.parse_args()

    out_dir = PROJECT_ROOT / "data" / "recorded"
    poller = SpotPoller(args.coins, out_dir, poll_s=args.poll)
    poller.start()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    hb = Path("/tmp/hl_spot_poller_heartbeat")
    last_status = 0.0
    try:
        while _running:
            time.sleep(1)
            hb.write_text(str(int(time.time())))
            if time.time() - last_status >= 300:
                last_status = time.time()
                log.info("STATUS | %s", poller.status_line())
    finally:
        log.info("shutting down...")
        poller.stop()
        import os
        os._exit(0)


if __name__ == "__main__":
    main()
