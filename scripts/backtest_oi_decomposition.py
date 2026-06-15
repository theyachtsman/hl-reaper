#!/usr/bin/env python3
"""OI decomposition backtest (secondary hypothesis).

Decompose each 5m bar by the joint sign of price change and OI change, then
measure forward perp returns. Hypothesis: "exhaustible" moves (price down +
OI down = long liquidation, leverage flushing out) bounce more than "fresh"
moves (price down + OI up = new shorts entering).

  px up + OI up   = new_longs       (fresh buying)
  px up + OI down = short_covering   (not fresh demand)
  px down + OI up = new_shorts       (fresh selling)
  px down + OI down = long_liq       (exhaustible)

Data: Binance futures OI 5m (data/history/{COIN}_oi_5m.csv) + perp close from
the 1m futures csv. BTC/ETH/SOL only (the coins with OI history). ~192 days.

Forward return reported RAW on perp (+ = price rose) so each category's
natural direction is visible, plus P(fwd>0). LOCKED thresholds below.

usage: backtest_oi_decomposition.py [--coins BTC ETH SOL] [--json out.json]
"""
import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HIST = Path(__file__).resolve().parent.parent / "data" / "history"
DEFAULT_COINS = ["BTC", "ETH", "SOL"]

PX_THRESH = 0.0015    # 0.15% over the 5m bar = a real price move
OI_THRESH = 0.0010    # 0.10% OI change = a real OI move
HORIZONS = [5, 15, 30, 60]   # minutes
FEE_RT = 0.00045

CLASSES = ["new_longs", "short_covering", "new_shorts", "long_liq"]


def load_closes(path: Path) -> dict[int, float]:
    out = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            out[int(r["t"])] = float(r["c"])
    return out


def load_oi(path: Path) -> list[tuple[int, float]]:
    out = []
    with open(path) as f:
        for r in csv.DictReader(f):
            out.append((int(r["t"]), float(r["oi"])))
    return out


def classify(dpx, doi):
    if abs(dpx) < PX_THRESH or abs(doi) < OI_THRESH:
        return None
    if dpx > 0:
        return "new_longs" if doi > 0 else "short_covering"
    return "new_shorts" if doi > 0 else "long_liq"


def backtest_coin(coin: str):
    closes = load_closes(HIST / f"{coin}_1m.csv")
    oi = load_oi(HIST / f"{coin}_oi_5m.csv")
    if not closes or len(oi) < 1000:
        return None

    acc = {c: {"n": 0, **{h: {"sum": 0.0, "wins": 0, "n": 0}
                          for h in HORIZONS}} for c in CLASSES}

    for k in range(1, len(oi)):
        t, oi_now = oi[k]
        t_prev, oi_prev = oi[k - 1]
        if t - t_prev != 5 * 60_000 or oi_prev <= 0:
            continue
        px_now = closes.get(t)
        px_prev = closes.get(t_prev)
        if px_now is None or px_prev is None:
            continue
        dpx = px_now / px_prev - 1
        doi = oi_now / oi_prev - 1
        cls = classify(dpx, doi)
        if cls is None:
            continue
        a = acc[cls]
        a["n"] += 1
        for h in HORIZONS:
            tf = t + h * 60_000
            px_f = closes.get(tf)
            if px_f is None:
                continue
            fwd = px_f / px_now - 1     # raw: + = price rose
            a[h]["sum"] += fwd
            a[h]["wins"] += 1 if fwd > 0 else 0
            a[h]["n"] += 1

    out = {"coin": coin, "classes": {}}
    for c in CLASSES:
        a = acc[c]
        cd = {"n": a["n"]}
        for h in HORIZONS:
            if a[h]["n"]:
                cd[h] = {"avg_pct": round(a[h]["sum"] / a[h]["n"] * 100, 4),
                         "up_rate": round(a[h]["wins"] / a[h]["n"], 3),
                         "n": a[h]["n"]}
        out["classes"][c] = cd
    return out


def print_coin(r):
    print(f"\n=== {r['coin']} — OI Decomposition (5m bars) ===")
    print(f"{'class':16} {'n':>7} | "
          + " ".join(f"{'+'+str(h)+'m(avg/up)':>15}" for h in HORIZONS))
    for c in CLASSES:
        cd = r["classes"][c]
        if cd["n"] == 0:
            print(f"{c:16} {0:>7} | (none)")
            continue
        cells = []
        for h in HORIZONS:
            if h in cd:
                cells.append(f"{cd[h]['avg_pct']:+.3f}%/{int(cd[h]['up_rate']*100)}%"
                             .rjust(15))
            else:
                cells.append(" " * 15)
        print(f"{c:16} {cd['n']:>7} | " + " ".join(cells))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--coins", nargs="+", default=DEFAULT_COINS)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    print("OI decomposition. perp=Binance futures, OI=Binance futures 5m.")
    print(f"px thresh {PX_THRESH:.2%}, oi thresh {OI_THRESH:.2%}, "
          f"maker fee ref {FEE_RT:.3%} RT. fwd = RAW perp return (+=up).")

    results = []
    pool = {c: {h: {"sum": 0.0, "wins": 0, "n": 0} for h in HORIZONS}
            for c in CLASSES}
    for coin in args.coins:
        r = backtest_coin(coin)
        if r is None:
            print(f"  {coin}: missing/short data")
            continue
        results.append(r)
        print_coin(r)
        for c in CLASSES:
            cd = r["classes"][c]
            for h in HORIZONS:
                if isinstance(cd.get(h), dict):
                    pool[c][h]["sum"] += cd[h]["avg_pct"] * cd[h]["n"]
                    pool[c][h]["wins"] += cd[h]["up_rate"] * cd[h]["n"]
                    pool[c][h]["n"] += cd[h]["n"]

    print(f"\n--- POOLED (all coins) ---")
    print(f"{'class':16} | "
          + " ".join(f"{'+'+str(h)+'m(avg/up)':>15}" for h in HORIZONS))
    for c in CLASSES:
        cells = []
        for h in HORIZONS:
            p = pool[c][h]
            if p["n"]:
                cells.append(f"{p['sum']/p['n']:+.3f}%/{int(p['wins']/p['n']*100)}%"
                             .rjust(15))
            else:
                cells.append(" " * 15)
        print(f"{c:16} | " + " ".join(cells))

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
