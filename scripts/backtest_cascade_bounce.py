#!/usr/bin/env python3
"""Backtest the cascade-BOUNCE strategy (Phase 8.6, Task 3).

Unlike scripts/backtest_cascade_score.py (which tested whether a cascade was
PREDICTABLE), this tests the only thing that matters for trading: once a cascade
is detected and stabilizes, is FADING the overshoot profitable after fees?

Method — faithful replay, not a reimplementation:
  * Feeds the REAL reaper.models.cascade_bounce.CascadeBounceModel its 1m
    candles bar-by-bar exactly as run_bot.py does. The model's own detection /
    stabilization / cooldown / knife-abandon state machine decides every entry.
  * The model uses wall-clock time.time() for episode-age and cooldown timers;
    we monkeypatch it to the simulated bar time so replay is deterministic and
    matches live behaviour minute-for-minute.
  * OI/liquidation confirmation only boosts the logged confidence, never gates
    an entry or changes P&L, so we run with ctx empty (confidence stays at the
    base 0.60). This is documented and intentional.

Exit simulation per trade (from cascade_bounce config):
  * entry  = OPEN of the bar AFTER the signal bar (you cannot fill the bar that
             already closed) — conservative.
  * TP     = +profit_target_pct, SL = -stop_pct, MAX HOLD = max_hold_minutes.
  * within a bar, if BOTH the stop and target are touched, assume STOP first
    (conservative). Stop/TP checked against bar high/low.

Fees are reported at three levels so the cost structure is explicit:
  taker round-trip 0.070%, maker round-trip 0.045%, and zero (gross).

usage:
  backtest_cascade_bounce.py                       # all 7 coins, default cfg
  backtest_cascade_bounce.py --coins BTC ETH       # subset
  backtest_cascade_bounce.py --min-move 0.02 --vol-mult 4   # sensitivity
  backtest_cascade_bounce.py --json data/bt_cb.json
"""
import argparse
import csv
import json
import sys
import time as _time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.models import cascade_bounce as cb_mod
from reaper.models.cascade_bounce import CascadeBounceModel

HIST = Path(__file__).resolve().parent.parent / "data" / "history"
DEFAULT_COINS = ["BTC", "ETH", "SOL", "ARB", "AVAX", "DOGE", "WIF"]

TAKER_RT = 0.0007   # 0.035% * 2
MAKER_RT = 0.00045  # ~0.0225% * 2 (post-only entry + maker exit, optimistic)


def load_1m(coin: str) -> list[dict]:
    p = HIST / f"{coin}_1m.csv"
    if not p.exists():
        return []
    out = []
    with open(p) as f:
        for row in csv.DictReader(f):
            out.append({"t": int(row["t"]),
                        "o": row["o"], "h": row["h"],
                        "l": row["l"], "c": row["c"], "v": row["v"]})
    return out


class ReplayBuf:
    """Minimal buffer over a fixed candle list; serves a trailing window
    ending at index `i`. ctx empty (OI confirmation is confidence-only)."""

    def __init__(self, candles: list[dict]):
        self._c = candles
        self.i = 0
        self.ctx: dict = {}

    def latest_candles(self, coin: str, interval: str, n: int = 100):
        lo = max(0, self.i - n + 1)
        return self._c[lo:self.i + 1]

    def mid(self, coin):
        return float(self._c[self.i]["c"])


def simulate_exit(candles, entry_i, side, entry_px, tp_pct, sl_pct,
                  max_hold_min):
    """Walk forward from the entry bar; return (exit_px, ret_frac, reason,
    bars_held). ret_frac is signed gross return (before fees)."""
    is_long = side == "LONG"
    if is_long:
        tp = entry_px * (1 + tp_pct)
        sl = entry_px * (1 - sl_pct)
    else:
        tp = entry_px * (1 - tp_pct)
        sl = entry_px * (1 + sl_pct)

    last = min(len(candles) - 1, entry_i + max_hold_min)
    for j in range(entry_i, last + 1):
        hi = float(candles[j]["h"])
        lo = float(candles[j]["l"])
        # conservative: check stop before target within the same bar
        if is_long:
            if lo <= sl:
                return sl, (sl / entry_px - 1), "STOP", j - entry_i
            if hi >= tp:
                return tp, (tp / entry_px - 1), "TP", j - entry_i
        else:
            if hi >= sl:
                return sl, -(sl / entry_px - 1), "STOP", j - entry_i
            if lo <= tp:
                return tp, -(tp / entry_px - 1), "TP", j - entry_i
    # timed out — exit at the last bar's close
    exit_px = float(candles[last]["c"])
    ret = (exit_px / entry_px - 1) * (1 if is_long else -1)
    return exit_px, ret, "TIME", last - entry_i


def backtest_coin(coin: str, cfg: dict, tp_pct, sl_pct, max_hold_min):
    candles = load_1m(coin)
    if len(candles) < 100:
        return None

    model = CascadeBounceModel(cfg)
    buf = ReplayBuf(candles)
    trades = []

    # patch the model's time source to the simulated bar time
    orig_time = cb_mod.time.time
    n = len(candles)
    try:
        for i in range(60, n - 1):
            buf.i = i
            bar_close_s = (candles[i]["t"] + 60_000) / 1000.0
            cb_mod.time.time = lambda _t=bar_close_s: _t
            sig = model.compute(coin, buf, None)
            if not sig:
                continue
            # enter at the OPEN of the next bar (can't fill the closed bar)
            entry_i = i + 1
            entry_px = float(candles[entry_i]["o"])
            exit_px, gross, reason, held = simulate_exit(
                candles, entry_i, sig["side"], entry_px,
                tp_pct, sl_pct, max_hold_min)
            # geometry-free diagnostic: signed forward return (in the bounce
            # direction) at fixed horizons — does price actually revert at all?
            sgn = 1 if sig["side"] == "LONG" else -1
            fwd = {}
            for h in (5, 15, 30, 60):
                k = entry_i + h
                if k < len(candles):
                    fwd[h] = (float(candles[k]["c"]) / entry_px - 1) * sgn
            trades.append({
                "ts": candles[entry_i]["t"], "side": sig["side"],
                "move_pct": sig["cascade_move_pct"], "entry": entry_px,
                "exit": exit_px, "gross": gross, "reason": reason,
                "bars_held": held, "conf": sig["confidence"], "fwd": fwd,
            })
    finally:
        cb_mod.time.time = orig_time

    return summarize(coin, trades, len(candles))


def summarize(coin, trades, n_bars):
    if not trades:
        return {"coin": coin, "n_bars": n_bars, "trades": 0}

    def stats(fee):
        nets = [t["gross"] - fee for t in trades]
        wins = [x for x in nets if x > 0]
        losses = [x for x in nets if x <= 0]
        total = sum(nets)
        gp = sum(wins)
        gl = -sum(losses)
        pf = (gp / gl) if gl > 0 else float("inf")
        return {
            "net_sum_pct": round(total * 100, 3),
            "avg_pct": round(total / len(nets) * 100, 4),
            "win_rate": round(len(wins) / len(nets), 3),
            "profit_factor": round(pf, 2) if pf != float("inf") else None,
        }

    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    longs = sum(1 for t in trades if t["side"] == "LONG")

    # geometry-free forward-return diagnostic (mean signed return + hit rate)
    fwd_diag = {}
    for h in (5, 15, 30, 60):
        vals = [t["fwd"][h] for t in trades if h in t["fwd"]]
        if vals:
            fwd_diag[h] = {
                "mean_pct": round(sum(vals) / len(vals) * 100, 4),
                "hit_rate": round(sum(1 for v in vals if v > 0) / len(vals), 3),
                "n": len(vals),
            }

    days = n_bars / 1440
    return {
        "fwd": fwd_diag,
        "coin": coin,
        "n_bars": n_bars,
        "days": round(days, 1),
        "trades": len(trades),
        "trades_per_week": round(len(trades) / days * 7, 2),
        "long": longs, "short": len(trades) - longs,
        "exit_reasons": reasons,
        "avg_bars_held": round(sum(t["bars_held"] for t in trades)
                               / len(trades), 1),
        "avg_cascade_move_pct": round(
            sum(abs(t["move_pct"]) for t in trades) / len(trades) * 100, 2),
        "gross": stats(0.0),
        "maker": stats(MAKER_RT),
        "taker": stats(TAKER_RT),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--coins", nargs="+", default=DEFAULT_COINS)
    ap.add_argument("--min-move", type=float, default=0.015)
    ap.add_argument("--vol-mult", type=float, default=3.0)
    ap.add_argument("--window", type=int, default=5)
    ap.add_argument("--stab-bars", type=int, default=2)
    ap.add_argument("--tp", type=float, default=0.010)
    ap.add_argument("--sl", type=float, default=0.0075)
    ap.add_argument("--max-hold", type=int, default=20)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    cfg = {
        "min_cascade_move_pct": args.min_move,
        "cascade_window_minutes": args.window,
        "min_volume_mult": args.vol_mult,
        "stabilization_bars": args.stab_bars,
        # generous so replay isn't throttled by wall-clock-style caps;
        # these are candle/elapsed based via the patched clock
        "cascade_stale_minutes": 15,
        "retrigger_cooldown_minutes": 30,
    }

    print(f"\nCascade-BOUNCE backtest — replay of real CascadeBounceModel")
    print(f"trigger: move >={args.min_move:.1%} in {args.window}m, "
          f"vol >={args.vol_mult}x, stabilize {args.stab_bars} bars")
    print(f"exit:    TP +{args.tp:.2%} / SL -{args.sl:.2%} / "
          f"max hold {args.max_hold}m")
    print(f"fees:    gross 0% | maker {MAKER_RT:.3%} | taker {TAKER_RT:.3%} "
          f"round-trip\n")

    results = []
    t0 = _time.time()
    for coin in args.coins:
        r = backtest_coin(coin, cfg, args.tp, args.sl, args.max_hold)
        if r is None:
            print(f"  {coin}: no history")
            continue
        results.append(r)
        if r["trades"] == 0:
            print(f"  {coin}: 0 cascade events in {r['n_bars']} bars")
            continue
        print(f"  {coin}: {r['trades']} trades "
              f"({r['trades_per_week']}/wk over {r['days']}d), "
              f"{r['long']}L/{r['short']}S, "
              f"avg move {r['avg_cascade_move_pct']}%, "
              f"hold {r['avg_bars_held']}m")
        print(f"      exits {r['exit_reasons']}")
        for lvl in ("gross", "maker", "taker"):
            s = r[lvl]
            print(f"      {lvl:5s}: net {s['net_sum_pct']:+.2f}% "
                  f"| avg {s['avg_pct']:+.4f}%/trade "
                  f"| win {s['win_rate']:.1%} | PF {s['profit_factor']}")

    # portfolio roll-up
    traded = [r for r in results if r["trades"] > 0]
    if traded:
        tot = sum(r["trades"] for r in traded)
        print(f"\n  ── PORTFOLIO ({tot} trades, {len(traded)} coins) ──")
        for lvl in ("gross", "maker", "taker"):
            net = sum(r[lvl]["net_sum_pct"] for r in traded)
            # trade-weighted win rate
            wr = sum(r[lvl]["win_rate"] * r["trades"] for r in traded) / tot
            print(f"    {lvl:5s}: net {net:+.2f}%  | win {wr:.1%}")

    print(f"\n  ({_time.time() - t0:.1f}s)")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"config": vars(args), "results": results}, indent=2))
        print(f"  wrote {args.json}")


if __name__ == "__main__":
    main()
