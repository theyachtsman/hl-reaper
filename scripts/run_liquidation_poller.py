#!/usr/bin/env python3
"""Standalone liquidation event collector entrypoint (Phase 8.6 research).

Separate process — never touches the live bot, the recorder, or the main
hl_reaper.db. Watches MAINNET public trades for backstop-liquidation fills
(liquidator vault as counterparty) and appends them to data/liquidations.db.

Optionally backfills aggregated history from the Coinalyze free API first
(--backfill-coinalyze, requires COINALYZE_API_KEY in the environment).

usage:
  run_liquidation_poller.py                    # watch live, default 7 coins
  run_liquidation_poller.py --backfill-coinalyze --since 2025-05-01
"""
import argparse
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.data.liquidation_poller import (LiquidationPoller,
                                            backfill_coinalyze)
from reaper.logger import get_logger

log = get_logger("liq_main")
_running = True

DEFAULT_COINS = ["BTC", "ETH", "SOL", "ARB", "AVAX", "DOGE", "WIF"]


def _sig(_s, _f):
    global _running
    _running = False


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--coins", nargs="+", default=DEFAULT_COINS)
    ap.add_argument("--backfill-coinalyze", action="store_true",
                    help="pull aggregated liquidation history first "
                         "(needs COINALYZE_API_KEY)")
    ap.add_argument("--since", default="2025-05-01",
                    help="backfill start date YYYY-MM-DD")
    ap.add_argument("--backfill-only", action="store_true",
                    help="exit after backfill, don't watch live")
    args = ap.parse_args()

    poller = LiquidationPoller(args.coins)

    if args.backfill_coinalyze:
        since_ms = int(datetime.strptime(args.since, "%Y-%m-%d")
                       .replace(tzinfo=timezone.utc).timestamp() * 1000)
        n = backfill_coinalyze(poller.conn, args.coins, since_ms)
        log.info("backfill complete: %d rows", n)
        if args.backfill_only:
            return

    poller.start()
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    hb = Path("/tmp/hl_liq_poller_heartbeat")
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
        # the SDK's ws threads are non-daemon and can outlive us — make
        # SIGTERM shutdown deterministic for systemd / scripted runs
        import os
        os._exit(0)


if __name__ == "__main__":
    main()
