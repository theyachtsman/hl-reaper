#!/usr/bin/env python3
"""Stacked leverage-fade backtest — do independent fade signals compound past
the fee? (follow-up to lead/lag + OI decomposition).

Thesis: leverage-driven moves get unwound. Three structurally independent
lenses on that one phenomenon were each found real-but-sub-fee:
  (A) perp_leads   — perp moved more than spot (cross-venue price)   [225d]
  (B) new_shorts   — price down + OI up = fresh leveraged sells (OI flow) [192d]
  (C) OB-imbalance-against — book pressure opposes the move (L2 depth) [~4d only]

DATA REALITY: (C) needs historical L2, of which only ~4 days exist (the
recorder's whole purpose). A large-sample 3-way is therefore NOT possible yet.
This script tests the scalable 2-way (A)+(B) — which is exactly the "2-of-3
agree" fallback in practice — on BTC/ETH/SOL, 5m bars, ~192d. The full 3-way
awaits more recorded L2.

If independent measurements of the same true signal are combined, shared signal
reinforces while idiosyncratic noise partly cancels — so the stack can clear
the fee even when each leg alone doesn't. The cost is sample size: requiring
agreement shrinks n hard. We report n (and per-week-per-coin frequency), hit
rate, and avg forward return for A-only, B-only, BOTH, and EITHER, at 5/15/30/
60m, for down-moves (fade->long), up-moves (fade->short), and combined.

"fade_return" is signed so >0 = the fade worked (price moved against the
original move). Compare avg fade_return vs the 0.045% maker round-trip fee.

usage: backtest_stacked_fade.py [--coins BTC ETH SOL] [--json out.json]
"""
import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HIST = Path(__file__).resolve().parent.parent / "data" / "history"
DEFAULT_COINS = ["BTC", "ETH", "SOL"]

MOVE_THRESH = 0.0015   # 0.15% 5m move = a real move to fade
OI_THRESH = 0.0010     # 0.10% OI change
BAND = 0.83            # perp_leads if |spot|/|perp| <= 0.83
HORIZONS = [5, 15, 30, 60]
FEE_RT = 0.00045
LEGS = ["A_only", "B_only", "BOTH", "EITHER", "ALL_MOVES"]


def load_closes(path: Path) -> dict[int, float]:
    out = {}
    if not path.exists():
        return out
    with open(path) as f:
        for r in csv.DictReader(f):
            out[int(r["t"])] = float(r["c"])
    return out


def load_oi(path: Path) -> list[tuple[int, float]]:
    out = []
    if not path.exists():
        return out
    with open(path) as f:
        for r in csv.DictReader(f):
            out.append((int(r["t"]), float(r["oi"])))
    return out


def _blank():
    return {leg: {"n": 0, **{h: {"sum": 0.0, "wins": 0, "n": 0}
                             for h in HORIZONS}} for leg in LEGS}


def _add(acc, leg, fade_returns):
    acc[leg]["n"] += 1
    for h, fr in fade_returns.items():
        acc[leg][h]["sum"] += fr
        acc[leg][h]["wins"] += 1 if fr > 0 else 0
        acc[leg][h]["n"] += 1


def backtest_coin(coin: str):
    perp = load_closes(HIST / f"{coin}_1m.csv")
    spot = load_closes(HIST / f"{coin}_spot_1m.csv")
    oi = load_oi(HIST / f"{coin}_oi_5m.csv")
    if not perp or not spot or len(oi) < 1000:
        return None

    buckets = {"down": _blank(), "up": _blank(), "combined": _blank()}
    span_ms = oi[-1][0] - oi[0][0]
    weeks = max(span_ms / (7 * 86400 * 1000), 1e-9)

    for k in range(1, len(oi)):
        t, oi_now = oi[k]
        t_prev, oi_prev = oi[k - 1]
        if t - t_prev != 5 * 60_000 or oi_prev <= 0:
            continue
        p_now, p_prev = perp.get(t), perp.get(t_prev)
        s_now, s_prev = spot.get(t), spot.get(t_prev)
        if None in (p_now, p_prev, s_now, s_prev):
            continue
        perp_ret = p_now / p_prev - 1
        spot_ret = s_now / s_prev - 1
        doi = oi_now / oi_prev - 1
        if abs(perp_ret) < MOVE_THRESH:
            continue
        down = perp_ret < 0
        # both legs require same-direction structure
        # A: perp_leads (perp magnitude dominates, same sign as the move)
        same_sign = (spot_ret >= 0) == (perp_ret >= 0)
        ratio = abs(spot_ret) / abs(perp_ret) if perp_ret else 9
        sigA = same_sign and ratio <= BAND
        # B: fresh leverage in the move direction (new_shorts on down,
        # new_longs on up) = OI rising
        sigB = doi >= OI_THRESH

        # forward fade returns: + = price moved against the original move
        fwd = {}
        for h in HORIZONS:
            pf = perp.get(t + h * 60_000)
            if pf is None:
                continue
            raw = pf / p_now - 1
            fwd[h] = -raw if not down else raw   # down->long wins if raw>0
        if not fwd:
            continue

        bkt = buckets["down" if down else "up"]
        for b in (bkt, buckets["combined"]):
            _add(b, "ALL_MOVES", fwd)
            if sigA:
                _add(b, "A_only", fwd)
            if sigB:
                _add(b, "B_only", fwd)
            if sigA and sigB:
                _add(b, "BOTH", fwd)
            if sigA or sigB:
                _add(b, "EITHER", fwd)

    return {"coin": coin, "weeks": round(weeks, 1), "buckets": buckets}


def fmt_leg(d, weeks):
    cells = []
    for h in HORIZONS:
        if d[h]["n"]:
            avg = d[h]["sum"] / d[h]["n"] * 100
            win = d[h]["wins"] / d[h]["n"] * 100
            cells.append(f"{avg:+.3f}%/{int(win)}%".rjust(15))
        else:
            cells.append(" " * 15)
    return cells


def print_bucket(coin, name, b, weeks):
    print(f"\n  [{coin} / {name}]  ({weeks:.1f} weeks of data)")
    print(f"    {'leg':10} {'n':>6} {'n/wk':>6} | "
          + " ".join(f"{'+'+str(h)+'m(avg/win)':>15}" for h in HORIZONS))
    for leg in LEGS:
        d = b[leg]
        npw = d["n"] / weeks
        cells = fmt_leg(d, weeks)
        print(f"    {leg:10} {d['n']:>6} {npw:>6.1f} | " + " ".join(cells))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--coins", nargs="+", default=DEFAULT_COINS)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    print("Stacked leverage-fade: A=perp_leads + B=new_shorts/new_longs.")
    print(f"move thr {MOVE_THRESH:.2%}, OI thr {OI_THRESH:.2%}, band {BAND}, "
          f"maker fee ref {FEE_RT:.3%} RT")
    print("fade_return >0 = fade worked. down=fade->long, up=fade->short.")
    print("NOTE: OB-imbalance (3rd signal) excluded — only ~4d L2 history "
          "exists; large-sample 3-way not yet possible.")

    results = []
    # pooled across coins
    pool = {nm: _blank() for nm in ("down", "up", "combined")}
    pool_weeks = 0.0
    for coin in args.coins:
        r = backtest_coin(coin)
        if r is None:
            print(f"  {coin}: missing/short data")
            continue
        results.append(r)
        for nm in ("down", "up", "combined"):
            print_bucket(coin, nm, r["buckets"][nm], r["weeks"])
            for leg in LEGS:
                src = r["buckets"][nm][leg]
                dst = pool[nm][leg]
                dst["n"] += src["n"]
                for h in HORIZONS:
                    dst[h]["sum"] += src[h]["sum"]
                    dst[h]["wins"] += src[h]["wins"]
                    dst[h]["n"] += src[h]["n"]
        pool_weeks += r["weeks"]

    print("\n" + "=" * 70)
    print("POOLED across coins (n/wk is summed across coins)")
    for nm in ("down", "up", "combined"):
        print_bucket("POOL", nm, pool[nm], pool_weeks / max(len(results), 1))

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
