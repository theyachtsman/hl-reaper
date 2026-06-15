#!/usr/bin/env python3
"""Open-interest history downloader (Binance futures metrics archive).

The futures metrics archive (daily files, 5m cadence) carries
sum_open_interest. Produces data/history/{COIN}_oi_5m.csv with columns
t,oi,oi_usd — the same format the BTC/ETH/SOL OI files already use — so the
stacked-fade / OI-decomposition backtests can cover the illiquid coins where
the perp-leads signal actually fires.

usage: download_oi_history.py --coins ARB AVAX DOGE WIF --start 2025-12-01
"""
import argparse
import csv
import io
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from reaper.config import PROJECT_ROOT
from reaper.logger import get_logger

log = get_logger("download_oi")

BASE = "https://data.binance.vision/data/futures/um/daily/metrics"
SYMBOL = lambda coin: f"{coin}USDT"  # noqa: E731


def day_range(start: str, end: datetime):
    d = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    while d < end:
        yield d.strftime("%Y-%m-%d")
        d += timedelta(days=1)


def fetch_day(sym: str, day: str):
    url = f"{BASE}/{sym}/{sym}-metrics-{day}.zip"
    r = requests.get(url, timeout=60)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    rows = list(csv.reader(io.TextIOWrapper(zf.open(zf.namelist()[0]), "utf-8")))
    return rows[1:] if rows and not rows[0][0][0].isdigit() else rows


def download_oi(coin: str, start: str, end: datetime, out_dir: Path) -> int:
    sym = SYMBOL(coin)
    rows: dict[int, tuple] = {}
    for day in day_range(start, end):
        data = fetch_day(sym, day)
        if not data:
            continue
        for r in data:
            # create_time, symbol, sum_open_interest, sum_open_interest_value
            ts = int(datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S")
                     .replace(tzinfo=timezone.utc).timestamp() * 1000)
            rows[ts] = (ts, float(r[2]), float(r[3]))
    if not rows:
        log.error("%s: no OI data", coin)
        return 0
    out = out_dir / f"{coin}_oi_5m.csv"
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "oi", "oi_usd"])
        for t in sorted(rows):
            w.writerow(rows[t])
    log.info("%s: wrote %d OI points -> %s", coin, len(rows), out)
    return len(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--coins", nargs="+", default=["ARB", "AVAX", "DOGE", "WIF"])
    ap.add_argument("--start", default="2025-12-01")
    args = ap.parse_args()
    out_dir = PROJECT_ROOT / "data" / "history"
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                             microsecond=0)
    ok = 0
    for coin in args.coins:
        try:
            if download_oi(coin, args.start, end, out_dir):
                ok += 1
        except Exception as e:
            log.error("%s failed: %s", coin, e)
    print(f"\ndone: {ok}/{len(args.coins)} coins")
    sys.exit(0 if ok == len(args.coins) else 1)


if __name__ == "__main__":
    main()
