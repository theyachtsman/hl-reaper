#!/usr/bin/env python3
"""Spot 1m OHLCV downloader (spot-perp lead/lag research).

Companion to download_history.py, which pulls Binance USD-M FUTURES candles
(the HL-perp price proxy). This pulls Binance SPOT candles for the same symbols
so the lead/lag backtest can compare the two venues. HL perp ≈ Binance futures
(HL oracle tracks CEX); "spot" = Binance spot.

Output (parallel to the futures convention):
  data/history/{COIN}_spot_1m.csv   columns t,o,h,l,c,v   (t = ms)

usage: download_spot_history.py --coins BTC ETH SOL ... --months 7
"""
import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.config import PROJECT_ROOT
from reaper.logger import get_logger
# reuse the futures downloader's zip/csv plumbing — identical archive format
from scripts.download_history import fetch_zip_csv, month_list, norm_ts

log = get_logger("download_spot")

BASE = "https://data.binance.vision/data/spot"
SYMBOL = lambda coin: f"{coin}USDT"  # noqa: E731


def download_spot_klines(coin: str, months, days_current, out_dir: Path) -> int:
    sym = SYMBOL(coin)
    rows: dict[int, tuple] = {}
    for y, m in months:
        url = f"{BASE}/monthly/klines/{sym}/1m/{sym}-1m-{y}-{m:02d}.zip"
        data = fetch_zip_csv(url)
        if data is None:
            log.warning("%s: no monthly spot file %d-%02d", coin, y, m)
            continue
        for r in data:
            t = norm_ts(r[0])
            rows[t] = (t, float(r[1]), float(r[2]), float(r[3]),
                       float(r[4]), float(r[5]))
        log.info("%s spot %d-%02d: %d candles", coin, y, m, len(data))
    for day in days_current:
        url = f"{BASE}/daily/klines/{sym}/1m/{sym}-1m-{day}.zip"
        data = fetch_zip_csv(url)
        if data is None:
            continue
        for r in data:
            t = norm_ts(r[0])
            rows[t] = (t, float(r[1]), float(r[2]), float(r[3]),
                       float(r[4]), float(r[5]))
    if not rows:
        log.error("%s: no spot kline data downloaded", coin)
        return 0
    out = out_dir / f"{coin}_spot_1m.csv"
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "o", "h", "l", "c", "v"])
        for t in sorted(rows):
            w.writerow(rows[t])
    log.info("%s: wrote %d spot 1m candles -> %s", coin, len(rows), out)
    return len(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--coins", nargs="+",
                    default=["BTC", "ETH", "SOL", "ARB", "AVAX", "DOGE", "WIF"])
    ap.add_argument("--months", type=int, default=7)
    ap.add_argument("--skip-current-month", action="store_true")
    args = ap.parse_args()

    out_dir = PROJECT_ROOT / "data" / "history"
    out_dir.mkdir(parents=True, exist_ok=True)
    months = month_list(args.months)

    days_current = []
    if not args.skip_current_month:
        now = datetime.now(timezone.utc)
        days_current = [f"{now.year}-{now.month:02d}-{d:02d}"
                        for d in range(1, now.day)]

    ok = 0
    for coin in args.coins:
        try:
            if download_spot_klines(coin, months, days_current, out_dir):
                ok += 1
        except Exception as e:
            log.error("%s failed: %s", coin, e)
    print(f"\ndone: {ok}/{len(args.coins)} coins -> {out_dir}")
    sys.exit(0 if ok == len(args.coins) else 1)


if __name__ == "__main__":
    main()
