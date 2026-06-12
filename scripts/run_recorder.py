#!/usr/bin/env python3
"""Standalone L2/trades/OI recorder entrypoint (own systemd unit:
hl-recorder.service). Separate process so the stable hl-reaper data
service is never touched. Records MAINNET microstructure by default —
that's the market the models will eventually trade, and testnet books
are too thin to be representative."""
import argparse
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.config import PROJECT_ROOT
from reaper.data.recorder import Recorder
from reaper.logger import get_logger

log = get_logger("recorder_main")
_running = True

DEFAULT_COINS = ["BTC", "ETH", "SOL", "ARB", "AVAX", "DOGE", "WIF"]


def _sig(_s, _f):
    global _running
    _running = False


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--coins", nargs="+", default=DEFAULT_COINS)
    ap.add_argument("--testnet", action="store_true",
                    help="record testnet instead of mainnet")
    args = ap.parse_args()

    api_url = ("https://api.hyperliquid-testnet.xyz" if args.testnet
               else "https://api.hyperliquid.xyz")
    out_dir = PROJECT_ROOT / "data" / "recorded"
    rec = Recorder(api_url, args.coins, out_dir)
    rec.start()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    hb = Path("/tmp/hl_recorder_heartbeat")
    last_status = 0.0
    try:
        while _running:
            time.sleep(1)
            hb.write_text(str(int(time.time())))
            if time.time() - last_status >= 300:
                last_status = time.time()
                log.info("STATUS | %d lines written | feed_age=%.1fs",
                         rec.lines_written, rec.buf.seconds_since_msg())
    finally:
        log.info("shutting down...")
        rec.stop()


if __name__ == "__main__":
    main()
