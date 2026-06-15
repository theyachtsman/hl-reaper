#!/usr/bin/env python3
"""Cascade score backtest (Phase 8.6, Task 4).

Did CascadeScoreModel's combined score rise BEFORE historical cascade
events, or does it only describe them after the fact?

Real per-event HL liquidation history isn't freely available (see
docs/liquidation_data_sources.md), so cascade events are DERIVED from free
Binance archive data: a forced-deleveraging signature = sharp open-interest
contraction + outsized price displacement + volume spike inside a trailing
30m window. OI comes from the Binance `metrics` archive (5m), price/volume
from the 1m candles already in data/history/, funding from the funding CSVs.

No lookahead: the score at bar t uses only trailing windows ending at t;
event detection at bar t likewise uses trailing windows, so an event's
onset bar is the first bar where the signature is fully visible.

Evaluation per threshold T:
  precision  = alert episodes followed by an event onset within HORIZON
  recall     = events with score >= T at some bar in the HORIZON before onset
  lead time  = onset - first above-threshold bar in that window
  base rate  = P(random bar has an event onset within HORIZON) — precision
               must beat this clearly, otherwise the score is hindsight.

usage: backtest_cascade_score.py --coins BTC ETH SOL
"""
import argparse
import csv
import io
import json
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import requests

from reaper.config import PROJECT_ROOT
from reaper.logger import get_logger
from reaper.models.cascade_score import score_from_series

log = get_logger("cascade_bt")

HIST = PROJECT_ROOT / "data" / "history"
BASE = "https://data.binance.vision/data/futures/um/daily/metrics"

BAR_MS = 5 * 60 * 1000
W_30M, W_4H, W_24H = 6, 48, 288          # in 5m bars
HORIZON_BARS = 72                        # 6h alert horizon
EVENT_MERGE_BARS = 24                    # events within 2h merge into one


# ---------------------------------------------------------------------------
# data loading
# ---------------------------------------------------------------------------
def download_oi(coin: str, start: datetime, end: datetime,
                refresh: bool = False) -> Path:
    """Fetch Binance 5m OI metrics day-by-day into one cached CSV."""
    out = HIST / f"{coin}_oi_5m.csv"
    if out.exists() and not refresh:
        return out
    sym = f"{coin}USDT"
    rows = []
    day = start
    while day <= end:
        url = (f"{BASE}/{sym}/{sym}-metrics-"
               f"{day:%Y-%m-%d}.zip")
        r = requests.get(url, timeout=60)
        if r.status_code == 200:
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            with zf.open(zf.namelist()[0]) as f:
                rdr = csv.reader(io.TextIOWrapper(f, "utf-8"))
                next(rdr)
                for rec in rdr:
                    ts = int(datetime.strptime(
                        rec[0], "%Y-%m-%d %H:%M:%S")
                        .replace(tzinfo=timezone.utc).timestamp() * 1000)
                    rows.append((ts, float(rec[2]), float(rec[3])))
        elif r.status_code != 404:
            r.raise_for_status()
        day += timedelta(days=1)
    if not rows:
        raise RuntimeError(f"{coin}: no OI metrics downloaded")
    rows.sort()
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "oi", "oi_usd"])
        w.writerows(rows)
    log.info("%s: wrote %d OI points -> %s", coin, len(rows), out)
    return out


def load_frame(coin: str, refresh_oi: bool = False) -> pd.DataFrame:
    """5m frame: o,h,l,c,v + oi + funding (hourly rate, ffilled)."""
    m1 = pd.read_csv(HIST / f"{coin}_1m.csv")
    m1["dt"] = pd.to_datetime(m1["t"], unit="ms", utc=True)
    df = (m1.set_index("dt")
            .resample("5min")
            .agg({"o": "first", "h": "max", "l": "min",
                  "c": "last", "v": "sum"})
            .dropna(subset=["c"]))

    start = m1["dt"].iloc[0].to_pydatetime()
    end = m1["dt"].iloc[-1].to_pydatetime()
    oi = pd.read_csv(download_oi(coin, start, end, refresh_oi))
    oi["dt"] = pd.to_datetime(oi["t"], unit="ms", utc=True)
    df = df.join(oi.set_index("dt")[["oi", "oi_usd"]])
    df["oi"] = df["oi"].ffill()

    fund = pd.read_csv(HIST / f"{coin}_funding.csv")
    fund["dt"] = pd.to_datetime(fund["ts"], unit="ms", utc=True)
    df = df.join(fund.set_index("dt")[["rate"]])
    df["rate_8h"] = df["rate"].ffill() * 8
    df = df.dropna(subset=["oi", "rate_8h"])
    log.info("%s: %d aligned 5m bars (%s -> %s)", coin, len(df),
             df.index[0], df.index[-1])
    return df


# ---------------------------------------------------------------------------
# derived cascade events
# ---------------------------------------------------------------------------
def derive_events(df: pd.DataFrame, oi_drop: float, px_move: float,
                  vol_ratio: float) -> list[int]:
    """Bar indices of cascade onsets: trailing 30m OI contraction >= oi_drop,
    abs price move >= px_move, 30m volume >= vol_ratio x trailing 24h avg."""
    oi_chg = df["oi"] / df["oi"].shift(W_30M) - 1
    px_chg = (df["c"] / df["c"].shift(W_30M) - 1).abs()
    vol_30m = df["v"].rolling(W_30M).mean()
    vol_24h = df["v"].rolling(W_24H).mean().shift(W_30M)
    sig = ((oi_chg <= -oi_drop) & (px_chg >= px_move)
           & (vol_30m >= vol_ratio * vol_24h))
    idxs = np.flatnonzero(sig.to_numpy())
    events, last = [], -10 ** 9
    for i in idxs:
        if i - last >= EVENT_MERGE_BARS:
            events.append(int(i))
        last = int(i)
    return [e for e in events if e >= W_24H]


# ---------------------------------------------------------------------------
# scores (trailing windows only)
# ---------------------------------------------------------------------------
def compute_scores(df: pd.DataFrame) -> np.ndarray:
    c = df["c"].to_numpy()
    h = df["h"].to_numpy()
    lo = df["l"].to_numpy()
    v = df["v"].to_numpy()
    oi = df["oi"].to_numpy()
    r8 = df["rate_8h"].to_numpy()
    n = len(df)
    scores = np.zeros(n)
    for i in range(W_24H, n):
        oi_series = oi[i - W_4H + 1:i + 1]
        out = score_from_series(
            mark=c[i],
            oi_series=list(oi_series),
            oi_usd=oi[i] * c[i],
            rate_8h=r8[i],
            recent_high=h[i - W_4H + 1:i + 1].max(),
            recent_low=lo[i - W_4H + 1:i + 1].min(),
            px_change_pct=c[i] / c[i - W_4H + 1] - 1,
            recent_vol_per_bar=v[i - W_30M + 1:i + 1].mean(),
            baseline_vol_per_bar=v[i - W_24H + 1:i + 1].mean(),
        )
        scores[i] = out["combined_score"]
    return scores


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------
def evaluate(scores: np.ndarray, events: list[int],
             thresholds: list[float]) -> dict:
    n = len(scores)
    valid = np.arange(W_24H, n)
    ev = np.zeros(n, bool)
    for e in events:
        ev[e] = True
    # base rate: random bar has an event onset within the next HORIZON
    has_ev_ahead = np.array(
        [ev[i + 1:i + 1 + HORIZON_BARS].any() for i in valid])
    base_rate = float(has_ev_ahead.mean()) if len(valid) else 0.0

    rows = []
    for t in thresholds:
        above = scores >= t
        starts = [i for i in valid
                  if above[i] and (i == valid[0] or not above[i - 1])]
        hits = sum(1 for s in starts
                   if ev[s + 1:s + 1 + HORIZON_BARS].any())
        precision = hits / len(starts) if starts else None

        rec_hits, leads = 0, []
        for e in events:
            win = np.flatnonzero(above[max(W_24H, e - HORIZON_BARS):e])
            if len(win):
                rec_hits += 1
                first = max(W_24H, e - HORIZON_BARS) + win[0]
                leads.append((e - first) * 5)  # minutes
        recall = rec_hits / len(events) if events else None
        rows.append({
            "threshold": t,
            "n_alert_episodes": len(starts),
            "precision": round(precision, 3) if precision is not None else None,
            "lift_vs_base": (round(precision / base_rate, 2)
                             if precision is not None and base_rate else None),
            "recall": round(recall, 3) if recall is not None else None,
            "median_lead_min": (float(np.median(leads)) if leads else None),
            "lead_min_p25_p75": ([float(np.percentile(leads, 25)),
                                  float(np.percentile(leads, 75))]
                                 if leads else None),
        })
    s = scores[valid]
    return {
        "n_bars": int(len(valid)),
        "n_events": len(events),
        "base_rate_event_within_6h": round(base_rate, 4),
        "score_pcts": {p: round(float(np.percentile(s, p)), 1)
                       for p in (50, 75, 90, 95, 99)},
        "thresholds": rows,
    }


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SOL"])
    ap.add_argument("--oi-drop", type=float, default=0.015,
                    help="min 30m OI contraction (fraction)")
    ap.add_argument("--px-move", type=float, default=0.0125,
                    help="min 30m abs price move (fraction)")
    ap.add_argument("--vol-ratio", type=float, default=2.5,
                    help="min 30m/24h volume ratio")
    ap.add_argument("--thresholds", nargs="+", type=float,
                    default=[20, 30, 40, 50, 60, 70, 80])
    ap.add_argument("--refresh-oi", action="store_true")
    ap.add_argument("--out", default=None,
                    help="JSON output path (default data/backtest_cascade_"
                         "<date>.json)")
    args = ap.parse_args()

    results = {"params": {"oi_drop": args.oi_drop, "px_move": args.px_move,
                          "vol_ratio": args.vol_ratio,
                          "horizon_hours": HORIZON_BARS / 12},
               "coins": {}}
    for coin in args.coins:
        log.info("=== %s ===", coin)
        df = load_frame(coin, args.refresh_oi)
        events = derive_events(df, args.oi_drop, args.px_move,
                               args.vol_ratio)
        log.info("%s: %d derived cascade events", coin, len(events))
        if len(events) < 5:
            log.warning("%s: very few events — consider loosening "
                        "--oi-drop/--px-move/--vol-ratio", coin)
        scores = compute_scores(df)
        res = evaluate(scores, events, args.thresholds)
        res["event_times"] = [str(df.index[e]) for e in events]
        results["coins"][coin] = res
        print(f"\n{coin}: {res['n_events']} events over {res['n_bars']} bars"
              f" | base rate (event within 6h) = "
              f"{res['base_rate_event_within_6h']:.2%}"
              f" | score percentiles {res['score_pcts']}")
        print(f"{'thr':>5} {'alerts':>7} {'prec':>6} {'lift':>6} "
              f"{'recall':>7} {'med_lead':>9}")
        for r in res["thresholds"]:
            print(f"{r['threshold']:>5.0f} {r['n_alert_episodes']:>7} "
                  f"{str(r['precision']):>6} {str(r['lift_vs_base']):>6} "
                  f"{str(r['recall']):>7} {str(r['median_lead_min']):>9}")

    out = Path(args.out) if args.out else (
        PROJECT_ROOT / "data" /
        f"backtest_cascade_{datetime.now():%Y%m%d}.json")
    out.write_text(json.dumps(results, indent=2))
    print(f"\nresults -> {out}")


if __name__ == "__main__":
    main()
