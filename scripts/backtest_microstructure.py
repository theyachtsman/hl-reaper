#!/usr/bin/env python3
"""TASK 1 — Signal-quality test of OrderbookImbalance + LiquidationHeatmap
on recorded L2/OI/trades data.

These two models are 26% of the ensemble weight and have NEVER been backtested
(they can't vote in candle replay — they were the whole reason the recorder was
built in Phase 4.6 Action 1). The live attribution (TASK 2) showed LiqHeatmap
casts zero directional votes and OB shows only a noise-level tilt. This script
tests them directly: when the model says LONG/SHORT with confidence X, what does
price actually do over the next 1 / 5 / 15 minutes?

NOT a P&L backtest — no fees, no sizing. Pure signal quality:
  hit_rate            : did the forward mid-price move in the model's direction?
  avg_dir_return      : average forward return signed toward the model's call
                        (positive = model was right on average)
Bucketed by confidence, per coin, per model.

Data (data/recorded/*.jsonl.gz):
  l2_<COIN>_<DATE>     top-20 book every ~2s  -> drives OB model + forward mids
  ctx_<COIN>_<DATE>    funding/oi/mark every ~60s -> drives LiqHeatmap
  trades_<COIN>_<DATE> trade prints           -> reconstruct 1m candles for LiqHeatmap
"""
import argparse
import bisect
import glob
import gzip
import json
import sys
import zlib
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.models import LONG, SHORT, FLAT  # noqa: E402
from reaper.models.orderbook_imbalance import OrderbookImbalanceModel  # noqa: E402
from reaper.models.liquidation_heatmap import LiquidationHeatmapModel  # noqa: E402

REC_DIR = Path(__file__).resolve().parent.parent / "data" / "recorded"
DEFAULT_COINS = ["BTC", "ETH", "SOL", "ARB", "AVAX", "DOGE", "WIF"]
HORIZONS_MIN = [1, 5, 15]
CONF_BUCKETS = [(0.0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 1.01)]
EVAL_STRIDE_S = 30        # evaluate a signal at most every 30s (reduce autocorr)


def read_jsonl_gz(pattern: str):
    for fp in sorted(glob.glob(str(REC_DIR / pattern))):
        try:
            with gzip.open(fp, "rt") as fh:
                while True:
                    try:
                        line = fh.readline()
                    except (zlib.error, EOFError, OSError):
                        break      # truncated gzip tail of a live-written file
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue   # truncated final line
        except (OSError, EOFError):
            continue


# ---------------------------------------------------------------------------
# fake buffer exposing only what the two models read
# ---------------------------------------------------------------------------
class FakeBuf:
    def __init__(self):
        self.books: dict = {}
        self.ctx: dict = {}
        self._candles: list = []   # 1m candle dicts {t,o,h,l,c,v}, chronological
        self._ctimes: list = []    # candle end-times (ms) for bisect

    def mid(self, coin):
        b = self.books.get(coin)
        if not b or not b["bids"] or not b["asks"]:
            return None
        return (b["bids"][0][0] + b["asks"][0][0]) / 2

    def latest_candles(self, coin, interval, n=100):
        # candles strictly before the current sim time (set via set_now)
        idx = bisect.bisect_right(self._ctimes, self._now)
        return self._candles[max(0, idx - n):idx]

    def set_candles(self, candles):
        self._candles = candles
        self._ctimes = [c["t_end"] for c in candles]

    def set_now(self, ts):
        self._now = ts


def build_1m_candles(coin: str) -> list:
    """Reconstruct 1m OHLC from the recorded trade prints."""
    buckets: dict[int, dict] = {}
    for t in read_jsonl_gz(f"trades_{coin}_*.jsonl.gz"):
        m = (t["ts"] // 60000) * 60000
        px = float(t["px"])
        b = buckets.get(m)
        if b is None:
            buckets[m] = {"t": m, "t_end": m + 60000, "o": px, "h": px,
                          "l": px, "c": px, "v": float(t["sz"])}
        else:
            b["h"] = max(b["h"], px); b["l"] = min(b["l"], px)
            b["c"] = px; b["v"] += float(t["sz"])
    return [buckets[k] for k in sorted(buckets)]


def load_l2(coin: str):
    """Return (snaps, mid_times, mid_vals). snaps = [(ts, bids, asks)]."""
    snaps, mt, mv = [], [], []
    for r in read_jsonl_gz(f"l2_{coin}_*.jsonl.gz"):
        bids = [(float(p), float(s)) for p, s in r.get("bids", [])]
        asks = [(float(p), float(s)) for p, s in r.get("asks", [])]
        if not bids or not asks:
            continue
        snaps.append((r["ts"], bids, asks))
        mt.append(r["ts"]); mv.append((bids[0][0] + asks[0][0]) / 2)
    return snaps, mt, mv


def load_ctx(coin: str):
    """Return (times, ctx_dicts) sorted, mapped to model's expected keys."""
    times, ctxs = [], []
    for r in read_jsonl_gz(f"ctx_{coin}_*.jsonl.gz"):
        times.append(r["ts"])
        ctxs.append({"funding": r.get("funding"),
                     "open_interest": r.get("oi"),
                     "mark_px": r.get("mark")})
    order = sorted(range(len(times)), key=lambda i: times[i])
    return [times[i] for i in order], [ctxs[i] for i in order]


def fwd_return(mt, mv, ts, horizon_ms):
    """Forward return of mid from ts to ts+horizon_ms (None if off the end)."""
    i0 = bisect.bisect_right(mt, ts) - 1
    if i0 < 0:
        return None
    target = ts + horizon_ms
    i1 = bisect.bisect_left(mt, target)
    if i1 >= len(mt):
        return None              # not enough future data
    p0, p1 = mv[i0], mv[i1]
    if p0 <= 0:
        return None
    return (p1 - p0) / p0


def bucket_of(conf):
    for lo, hi in CONF_BUCKETS:
        if lo <= conf < hi:
            return (lo, hi)
    return None


def run_coin(coin: str, ob_model, liq_model):
    snaps, mt, mv = load_l2(coin)
    if not snaps:
        return None
    ctx_t, ctx_v = load_ctx(coin)
    candles = build_1m_candles(coin)

    buf = FakeBuf()
    buf.set_candles(candles)

    # stats[model][bucket][horizon] = [hits, dir_return_sum, n]
    stats = {m: defaultdict(lambda: defaultdict(lambda: [0, 0.0, 0]))
             for m in ("OrderbookImbalanceModel", "LiquidationHeatmapModel")}
    flat_count = {"OrderbookImbalanceModel": 0, "LiquidationHeatmapModel": 0}

    last_eval = 0
    for ts, bids, asks in snaps:
        if ts - last_eval < EVAL_STRIDE_S * 1000:
            continue
        last_eval = ts
        buf.set_now(ts)
        buf.books[coin] = {"bids": bids, "asks": asks, "ts": ts}
        # latest ctx at/before ts for the liq model
        ci = bisect.bisect_right(ctx_t, ts) - 1
        buf.ctx[coin] = ctx_v[ci] if ci >= 0 else {}

        for mdl in (ob_model, liq_model):
            tk = mdl.compute(coin, buf)
            if tk.direction == FLAT:
                flat_count[tk.model] += 1
                continue
            b = bucket_of(tk.confidence)
            if b is None:
                continue
            sign = 1.0 if tk.direction == LONG else -1.0
            for h in HORIZONS_MIN:
                fr = fwd_return(mt, mv, ts, h * 60000)
                if fr is None:
                    continue
                dir_ret = fr * sign
                cell = stats[tk.model][b][h]
                cell[0] += 1 if dir_ret > 0 else 0
                cell[1] += dir_ret
                cell[2] += 1
    return stats, flat_count, len(snaps)


def print_coin(coin, stats, flat_count, n_snaps):
    for model in ("OrderbookImbalanceModel", "LiquidationHeatmapModel"):
        print(f"\n=== {coin} — {model} "
              f"(FLAT on {flat_count[model]} evals) ===")
        sd = stats[model]
        if not sd:
            print("  no directional signals emitted")
            continue
        hdr = f"{'conf_bucket':12} {'n':>6}"
        for h in HORIZONS_MIN:
            hdr += f" {'hit_'+str(h)+'m':>9} {'dirret_'+str(h)+'m':>12}"
        print(hdr)
        for (lo, hi) in CONF_BUCKETS:
            if (lo, hi) not in sd:
                continue
            cells = sd[(lo, hi)]
            n = max(cells[h][2] for h in HORIZONS_MIN) if cells else 0
            row = f"{lo:.2f}-{hi:.2f}   {n:>6}"
            for h in HORIZONS_MIN:
                hits, drsum, cnt = cells[h]
                if cnt:
                    row += f" {hits/cnt:>9.2f} {100*drsum/cnt:>11.4f}%"
                else:
                    row += f" {'—':>9} {'—':>12}"
            print(row)


def summarize(all_stats):
    print("\n" + "=" * 72)
    print("INTERPRETATION")
    print("=" * 72)
    print(
        "* hit_rate ~0.50 across all buckets regardless of confidence -> no\n"
        "  signal; extends the Phase 4.6 'candle signal is neutral' finding to\n"
        "  microstructure.\n"
        "* hit_rate climbing meaningfully >0.50 in higher-conf buckets AND\n"
        "  avg dir_return positive (model's predicted direction) -> real signal,\n"
        "  worth weighting up.\n"
        "* dir_return is the forward mid move signed toward the model's call;\n"
        "  positive = right on average. It is the metric that matters most\n"
        "  (hit_rate ignores magnitude).\n"
        "* Any bucket with n<30 in the highest-confidence row is too small to\n"
        "  conclude — flagged below.")
    # thin-sample flags
    for coin, (stats, _fc, _n) in all_stats.items():
        for model, sd in stats.items():
            top = sd.get((0.7, 1.01))
            if top:
                n = max(top[h][2] for h in HORIZONS_MIN)
                if 0 < n < 30:
                    print(f"  [thin] {coin} {model} conf>0.70 n={n}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", nargs="*", default=DEFAULT_COINS)
    args = ap.parse_args()

    # max_age_s huge: recorded book ts vs wall-clock now is days stale, but the
    # snapshot was fresh when recorded — disable the live staleness guard.
    ob_model = OrderbookImbalanceModel(max_age_s=1e15)
    liq_model = LiquidationHeatmapModel()

    print(f"Microstructure signal-quality backtest — coins={args.coins}")
    print(f"eval stride {EVAL_STRIDE_S}s · horizons {HORIZONS_MIN}min · "
          f"recorded data in {REC_DIR}")

    all_stats = {}
    for coin in args.coins:
        # fresh liq model per coin so its OI deque doesn't bleed across coins
        liq_model = LiquidationHeatmapModel()
        res = run_coin(coin, ob_model, liq_model)
        if res is None:
            print(f"\n=== {coin}: no L2 data, skipped ===")
            continue
        stats, flat_count, n_snaps = res
        print("\n" + "#" * 72)
        print(f"# {coin}  ({n_snaps} L2 snapshots)")
        print("#" * 72)
        print_coin(coin, stats, flat_count, n_snaps)
        all_stats[coin] = res

    summarize(all_stats)


if __name__ == "__main__":
    main()
