"""Diagnose MomentumModel sign/saturation bugs against logged signals.

Loads a signal_history CSV and, for the logged `vote_momentum` decisions,
computes mean forward returns conditioned on the momentum vote direction.

Interpretation (see dev prompt Step 1):
  - LONG votes -> NEGATIVE mean fwd return AND SHORT votes -> POSITIVE
    => the sign is inverted (anti-predictive). Fix the sign.
  - Directions correct on average but conf railed at ~0.95 => pure saturation.
  - Possibly both.

Step 4 re-validation: pass --live-validate to fetch recent Hyperliquid candles
and re-simulate the CURRENTLY INSTALLED MomentumModel over them, reporting mean
forward returns by vote direction and the confidence spread. This re-runs the
fixed model logic on fresh data so the before/after improvement is auditable.

Usage:
  python scripts/diagnose_momentum.py <signals.csv> [--coin ETH] [--band trend]
  python scripts/diagnose_momentum.py --live-validate [--coin ETH] [--interval 1h] [--days 20]
"""
import argparse
import csv
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def parse_vote(s):
    """'SHORT:0.95' -> ('SHORT', 0.95); 'FLAT:0.00' -> ('FLAT', 0.0)."""
    if not s or ":" not in s:
        return None, None
    d, c = s.split(":", 1)
    try:
        return d, float(c)
    except ValueError:
        return d, None


def fnum(s):
    if s is None or s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_ts(s):
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else float("nan")


def summarize(rows, label):
    """Mean fwd returns for momentum LONG vs SHORT (conf >= 0.5)."""
    horizons = ("fwd_ret_5m", "fwd_ret_15m", "fwd_ret_30m")
    buckets = {"LONG": {h: [] for h in horizons},
               "SHORT": {h: [] for h in horizons}}
    conf_vals = []
    dir_counter = Counter()
    for r in rows:
        d, c = parse_vote(r["vote_momentum"])
        if d in ("LONG", "SHORT") and c is not None:
            conf_vals.append(c)
        if d in ("LONG", "SHORT") and c is not None and c >= 0.5:
            dir_counter[d] += 1
            for h in horizons:
                buckets[d][h].append(fnum(r.get(h)))

    print(f"\n=== {label} ===")
    print(f"rows: {len(rows)}  (momentum conf>=0.5 -> "
          f"LONG {dir_counter['LONG']}, SHORT {dir_counter['SHORT']})")
    if conf_vals:
        conf_vals.sort()
        railed = sum(1 for c in conf_vals if c >= 0.949) / len(conf_vals)
        print(f"non-FLAT momentum conf: n={len(conf_vals)} "
              f"min={conf_vals[0]:.2f} med={conf_vals[len(conf_vals)//2]:.2f} "
              f"max={conf_vals[-1]:.2f}  frac@0.95={railed:.1%}")
    print("  mean forward return by momentum vote (conf>=0.5):")
    for h in horizons:
        ml = mean(buckets["LONG"][h])
        ms = mean(buckets["SHORT"][h])
        print(f"    {h:11s}  LONG={ml*100:+.4f}%   SHORT={ms*100:+.4f}%")
    # verdict
    ml5 = mean(buckets["LONG"]["fwd_ret_15m"])
    ms5 = mean(buckets["SHORT"]["fwd_ret_15m"])
    if ml5 == ml5 and ms5 == ms5:  # not nan
        if ml5 < 0 and ms5 > 0:
            print("  >> VERDICT: ANTI-PREDICTIVE (sign likely inverted)")
        elif ml5 > 0 and ms5 < 0:
            print("  >> VERDICT: predictive (sign correct)")
        else:
            print("  >> VERDICT: mixed / weak")


def live_validate(coin, interval, days, horizons=(1, 2, 3)):
    """Re-simulate the INSTALLED MomentumModel over fresh HL candles and report
    mean forward returns by vote direction + the confidence spread."""
    import statistics as st

    import requests

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from reaper.data.buffer import MarketBuffer
    from reaper.models.momentum_model import MomentumModel

    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    start = end - days * 86400 * 1000
    r = requests.post("https://api.hyperliquid.xyz/info",
                      json={"type": "candleSnapshot",
                            "req": {"coin": coin, "interval": interval,
                                    "startTime": start, "endTime": end}},
                      timeout=20)
    closes = [float(x["c"]) for x in r.json()]
    m = MomentumModel()  # installed live defaults
    buf = MarketBuffer([coin], [interval], maxlen=max(600, len(closes) + 5))
    t0 = 1_700_000_000_000
    res = {h: ([], []) for h in horizons}
    confs, railed = [], 0
    for i, c in enumerate(closes):
        buf.on_candle(coin, interval, {"t": t0 + i * 60000, "o": c, "h": c,
                                       "l": c, "c": c, "v": 100.0})
        if i < m.min_candles + 5 or i >= len(closes) - max(horizons):
            continue
        tk = m.compute(coin, buf, interval=interval)
        if tk.direction in ("LONG", "SHORT") and tk.confidence >= 0.5:
            confs.append(tk.confidence)
            railed += tk.confidence >= 0.949
            for h in horizons:
                fr = (closes[i + h] - closes[i]) / closes[i] * 100
                res[h][0 if tk.direction == "LONG" else 1].append(fr)
    print(f"\n=== LIVE re-validation: {coin} {interval} ({days}d, "
          f"{len(closes)} candles) ===")
    if confs:
        print(f"  installed model votes (conf>=0.5): n={len(confs)} "
              f"min={min(confs):.2f} med={st.median(confs):.2f} "
              f"max={max(confs):.2f}  frac@0.95={railed/len(confs):.0%}")
    print("  mean forward return by vote direction:")
    for h in horizons:
        L, S = res[h]
        mL = st.mean(L) if L else float("nan")
        mS = st.mean(S) if S else float("nan")
        v = ("ANTI-PRED" if (mL < 0 and mS > 0) else
             "predictive" if (mL > 0 and mS < 0) else "mixed")
        print(f"    h={h}c  LONG={mL:+.4f}% (n{len(L)})  "
              f"SHORT={mS:+.4f}% (n{len(S)})  -> {v}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="?", help="signal_history CSV (omit with --live-validate)")
    ap.add_argument("--coin", default="ETH")
    ap.add_argument("--band", default=None, help="scalp|trend (default: all)")
    ap.add_argument("--live-validate", action="store_true",
                    help="re-simulate the installed model on fresh HL candles")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--days", type=int, default=20)
    args = ap.parse_args()

    if args.live_validate:
        live_validate(args.coin, args.interval, args.days)
        return
    if not args.csv:
        ap.error("a signals CSV is required unless --live-validate is given")

    rows = list(csv.DictReader(open(args.csv)))
    sel = [r for r in rows if r["coin"] == args.coin
           and (args.band is None or r["band"] == args.band)]

    summarize(sel, f"{args.coin} ALL bands" if not args.band
              else f"{args.coin} {args.band}")

    # The specific failure window: ETH 2026-06-26 07:00-09:00 UTC
    win = []
    for r in sel:
        ts = parse_ts(r["ts_utc"])
        if ts and ts.strftime("%Y-%m-%d") == "2026-06-26" \
                and 7 <= ts.hour < 9:
            win.append(r)
    if win:
        summarize(win, f"{args.coin} 2026-06-26 07:00-09:00 UTC (slide window)")
        c = Counter(r["vote_momentum"] for r in win)
        print("  momentum vote distribution in window:")
        for k, v in c.most_common(8):
            print(f"    {k:14s} {v}")


if __name__ == "__main__":
    main()
