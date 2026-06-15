#!/usr/bin/env python3
"""Spot-perp lead/lag backtest (primary hypothesis test).

Question: does WHERE a move originates (spot vs perp) predict its future?
  * spot leads (spot moved more than perp, same dir) -> "real demand",
    perp expected to catch up -> CONTINUATION hypothesis
  * perp leads (perp moved more than spot, same dir) -> "leverage-driven",
    spot hasn't sponsored it -> FADE/REVERSAL hypothesis
  * aligned (both moved similarly) -> baseline
  * divergent (opposite signs) -> basis dislocation

Data: Binance FUTURES candles (data/history/{COIN}_1m.csv) as the HL-perp
price proxy (HL oracle tracks CEX), vs Binance SPOT candles
(data/history/{COIN}_spot_1m.csv). Aligned on the shared 1m timestamp grid.

METHODOLOGY IS LOCKED BEFORE RESULTS (honesty): thresholds/bands below are
fixed defaults; do not tune them after seeing output.

Classification at each timestamp t, over a lookback window of N minutes:
  spot_ret = spot_close[t]/spot_close[t-N] - 1
  perp_ret = perp_close[t]/perp_close[t-N] - 1
  Require a real move: max(|spot_ret|,|perp_ret|) >= MOVE_THRESH[N];
    smaller -> 'flat' (excluded).
  ratio = |spot_ret| / |perp_ret|
  same sign:
    ratio >= 1/BAND  -> spot_leads        (BAND=0.83 => ratio>=1.2)
    ratio <= BAND    -> perp_leads         (ratio<=0.83)
    else             -> aligned
  opposite sign (both >= 0.7*MOVE_THRESH)  -> divergent

Forward return measured on PERP, signed by the recent move direction
(continuation convention): fwd_cont = (perp[t+h]/perp[t]-1) * sign(move).
  >0 => price continued the recent move;  <0 => it reversed.
  win_rate_continuation = P(fwd_cont > 0).
  spot_leads hypothesis predicts fwd_cont>0; perp_leads predicts fwd_cont<0.

Compare any edge magnitude vs ~0.045% maker round-trip fee.

usage:
  backtest_spot_perp_leadlag.py [--coins ...] [--N 1] [--json out.json]
"""
import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HIST = Path(__file__).resolve().parent.parent / "data" / "history"
DEFAULT_COINS = ["BTC", "ETH", "SOL", "ARB", "AVAX", "DOGE", "WIF"]

# locked thresholds (fraction). larger N -> larger move needed.
MOVE_THRESH = {1: 0.0005, 5: 0.0015, 15: 0.0030}
BAND = 0.83          # ratio band: >1.2 spot-led, <0.83 perp-led, else aligned
HORIZONS = [5, 15, 30, 60]
FEE_RT = 0.00045     # maker round-trip reference


def load_closes(path: Path) -> dict[int, float]:
    if not path.exists():
        return {}
    out = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            out[int(r["t"])] = float(r["c"])
    return out


def classify(spot_ret, perp_ret, thresh):
    big = max(abs(spot_ret), abs(perp_ret))
    if big < thresh:
        return None
    same = (spot_ret >= 0) == (perp_ret >= 0)
    if not same:
        if abs(spot_ret) >= 0.7 * thresh and abs(perp_ret) >= 0.7 * thresh:
            return "divergent"
        return None
    if abs(perp_ret) < 1e-12:
        return "spot_leads"
    ratio = abs(spot_ret) / abs(perp_ret)
    if ratio >= 1.0 / BAND:
        return "spot_leads"
    if ratio <= BAND:
        return "perp_leads"
    return "aligned"


def backtest_coin(coin: str, N: int):
    perp = load_closes(HIST / f"{coin}_1m.csv")
    spot = load_closes(HIST / f"{coin}_spot_1m.csv")
    if not perp or not spot:
        return None
    ts = sorted(set(perp) & set(spot))
    if len(ts) < 1000:
        return None
    idx = {t: i for i, t in enumerate(ts)}
    perp_c = [perp[t] for t in ts]
    spot_c = [spot[t] for t in ts]
    n = len(ts)
    thresh = MOVE_THRESH[N]
    maxh = max(HORIZONS)

    # accumulators per class
    classes = ["spot_leads", "perp_leads", "aligned", "divergent"]
    acc = {c: {"n": 0, **{h: {"sum": 0.0, "wins": 0, "n": 0}
                          for h in HORIZONS}} for c in classes}

    for i in range(N, n - maxh):
        # require contiguous 1m bars (no gaps) across the lookback window
        if ts[i] - ts[i - N] != N * 60_000:
            continue
        spot_ret = spot_c[i] / spot_c[i - N] - 1
        perp_ret = perp_c[i] / perp_c[i - N] - 1
        cls = classify(spot_ret, perp_ret, thresh)
        if cls is None:
            continue
        # move direction: use perp for perp_leads/aligned/divergent, spot for
        # spot_leads (the leading venue defines the move)
        ref = spot_ret if cls == "spot_leads" else perp_ret
        sgn = 1 if ref >= 0 else -1
        a = acc[cls]
        a["n"] += 1
        for h in HORIZONS:
            j = i + h
            # forward window must also be contiguous
            if ts[j] - ts[i] != h * 60_000:
                continue
            fwd_cont = (perp_c[j] / perp_c[i] - 1) * sgn
            a[h]["sum"] += fwd_cont
            a[h]["wins"] += 1 if fwd_cont > 0 else 0
            a[h]["n"] += 1

    out = {"coin": coin, "N": N, "n_bars": n, "classes": {}}
    for c in classes:
        a = acc[c]
        if a["n"] == 0:
            out["classes"][c] = {"n": 0}
            continue
        cd = {"n": a["n"]}
        for h in HORIZONS:
            if a[h]["n"]:
                cd[h] = {
                    "avg_pct": round(a[h]["sum"] / a[h]["n"] * 100, 4),
                    "win": round(a[h]["wins"] / a[h]["n"], 3),
                    "n": a[h]["n"],
                }
        out["classes"][c] = cd
    return out


def print_coin(r):
    print(f"\n=== {r['coin']} — Spot-Perp Lead/Lag, {r['N']}m classification "
          f"({r['n_bars']} bars) ===")
    print(f"{'class':12} {'n':>7} | "
          + " ".join(f"{'+'+str(h)+'m(avg/win)':>16}" for h in HORIZONS))
    for c in ["spot_leads", "perp_leads", "aligned", "divergent"]:
        cd = r["classes"][c]
        if cd.get("n", 0) == 0:
            print(f"{c:12} {0:>7} | (none)")
            continue
        cells = []
        for h in HORIZONS:
            if h in cd:
                cells.append(f"{cd[h]['avg_pct']:+.3f}%/{int(cd[h]['win']*100)}%"
                             .rjust(16))
            else:
                cells.append(" " * 16)
        print(f"{c:12} {cd['n']:>7} | " + " ".join(cells))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--coins", nargs="+", default=DEFAULT_COINS)
    ap.add_argument("--N", type=int, nargs="+", default=[1, 5, 15],
                    help="lookback window(s) in minutes")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    print("Spot-perp lead/lag. perp=Binance futures (HL proxy), "
          "spot=Binance spot.")
    print(f"move thresholds {MOVE_THRESH}, ratio band {BAND}, "
          f"maker fee ref {FEE_RT:.3%} RT")
    print("fwd return is CONTINUATION-signed on perp: >0 continues, <0 reverts")

    results = []
    for N in args.N:
        print(f"\n{'#'*70}\n# LOOKBACK N = {N}m\n{'#'*70}")
        # pooled accumulators across coins
        pool = {c: {h: {"sum": 0.0, "wins": 0, "n": 0} for h in HORIZONS}
                for c in ["spot_leads", "perp_leads", "aligned", "divergent"]}
        for coin in args.coins:
            r = backtest_coin(coin, N)
            if r is None:
                print(f"  {coin}: missing/short data")
                continue
            results.append(r)
            print_coin(r)
            for c in pool:
                cd = r["classes"][c]
                for h in HORIZONS:
                    if isinstance(cd.get(h), dict):
                        pool[c][h]["sum"] += cd[h]["avg_pct"] * cd[h]["n"]
                        pool[c][h]["wins"] += cd[h]["win"] * cd[h]["n"]
                        pool[c][h]["n"] += cd[h]["n"]
        print(f"\n--- POOLED (all coins), N={N}m ---")
        print(f"{'class':12} | "
              + " ".join(f"{'+'+str(h)+'m(avg/win)':>16}" for h in HORIZONS))
        for c in ["spot_leads", "perp_leads", "aligned", "divergent"]:
            cells = []
            for h in HORIZONS:
                p = pool[c][h]
                if p["n"]:
                    cells.append(f"{p['sum']/p['n']:+.3f}%/"
                                 f"{int(p['wins']/p['n']*100)}%".rjust(16))
                else:
                    cells.append(" " * 16)
            print(f"{c:12} | " + " ".join(cells))

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
