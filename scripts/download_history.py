#!/usr/bin/env python3
"""Deep historical data downloader (Phase 4.6, Problem 1 — data starvation).

The HL API retains only ~5000 candles/interval and the hyperliquid-archive
S3 bucket is requester-pays L2 snapshots, so deep OHLCV + funding history
comes from the Binance public futures archive (free, no auth). HL's oracle
tracks CEX prices, so {coin}USDT perp candles are a faithful price proxy.

Output (canonical, consumed by backtester/trainer):
  data/history/{COIN}_1m.csv       columns t,o,h,l,c,v   (t = ms)
  data/history/{COIN}_funding.csv  columns ts,rate       (rate = hourly, HL-style)

usage: download_history.py --coins BTC ETH SOL --months 6
"""
import argparse
import csv
import io
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from reaper.config import PROJECT_ROOT
from reaper.logger import get_logger

log = get_logger("download")

BASE = "https://data.binance.vision/data/futures/um"
SYMBOL = lambda coin: f"{coin}USDT"  # noqa: E731


def month_list(months: int) -> list[tuple[int, int]]:
    """Last `months` complete months, oldest first, excluding the current one."""
    now = datetime.now(timezone.utc)
    out = []
    y, m = now.year, now.month
    for _ in range(months):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append((y, m))
    return list(reversed(out))


def fetch_zip_csv(url: str) -> list[list[str]] | None:
    """Download a zip containing one CSV; returns data rows (header skipped)."""
    r = requests.get(url, timeout=120)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    with zf.open(zf.namelist()[0]) as f:
        rows = list(csv.reader(io.TextIOWrapper(f, "utf-8")))
    if rows and not rows[0][0].isdigit():
        rows = rows[1:]  # header row
    return rows


def norm_ts(raw: str) -> int:
    """Normalize a Binance timestamp to ms (newer files use microseconds)."""
    t = int(raw)
    return t // 1000 if t > 10 ** 14 else t


def download_klines(coin: str, months: list[tuple[int, int]],
                    days_current: list[str], out_dir: Path) -> int:
    sym = SYMBOL(coin)
    rows: dict[int, tuple] = {}
    for y, m in months:
        url = f"{BASE}/monthly/klines/{sym}/1m/{sym}-1m-{y}-{m:02d}.zip"
        data = fetch_zip_csv(url)
        if data is None:
            log.warning("%s: no monthly file %d-%02d (not listed yet?)",
                        coin, y, m)
            continue
        for r in data:
            t = norm_ts(r[0])
            rows[t] = (t, float(r[1]), float(r[2]), float(r[3]),
                       float(r[4]), float(r[5]))
        log.info("%s %d-%02d: %d candles", coin, y, m, len(data))
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
        log.error("%s: no kline data downloaded", coin)
        return 0
    out = out_dir / f"{coin}_1m.csv"
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "o", "h", "l", "c", "v"])
        for t in sorted(rows):
            w.writerow(rows[t])
    log.info("%s: wrote %d 1m candles -> %s", coin, len(rows), out)
    return len(rows)


def download_funding(coin: str, months: list[tuple[int, int]],
                     out_dir: Path) -> int:
    sym = SYMBOL(coin)
    rows: dict[int, float] = {}
    for y, m in months:
        url = (f"{BASE}/monthly/fundingRate/{sym}/"
               f"{sym}-fundingRate-{y}-{m:02d}.zip")
        data = fetch_zip_csv(url)
        if data is None:
            continue
        for r in data:
            # calc_time, funding_interval_hours, last_funding_rate
            ts = norm_ts(r[0])
            interval_h = float(r[1] or 8)
            rate_hourly = float(r[2]) / interval_h  # HL-style hourly rate
            rows[ts] = rate_hourly
    if not rows:
        log.error("%s: no funding data downloaded", coin)
        return 0
    out = out_dir / f"{coin}_funding.csv"
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "rate"])
        for ts in sorted(rows):
            w.writerow([ts, f"{rows[ts]:.10f}"])
    log.info("%s: wrote %d funding points -> %s", coin, len(rows), out)
    return len(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SOL"])
    ap.add_argument("--months", type=int, default=6,
                    help="complete months of history to fetch")
    ap.add_argument("--skip-current-month", action="store_true",
                    help="don't fetch the current month's daily files")
    args = ap.parse_args()

    out_dir = PROJECT_ROOT / "data" / "history"
    out_dir.mkdir(parents=True, exist_ok=True)
    months = month_list(args.months)

    days_current = []
    if not args.skip_current_month:
        now = datetime.now(timezone.utc)
        days_current = [f"{now.year}-{now.month:02d}-{d:02d}"
                        for d in range(1, now.day)]  # complete days only

    ok = 0
    for coin in args.coins:
        try:
            n_k = download_klines(coin, months, days_current, out_dir)
            n_f = download_funding(coin, months, out_dir)
            if n_k and n_f:
                ok += 1
        except Exception as e:
            log.error("%s failed: %s", coin, e)
    print(f"\ndone: {ok}/{len(args.coins)} coins -> {out_dir}")
    sys.exit(0 if ok == len(args.coins) else 1)


if __name__ == "__main__":
    main()
