#!/usr/bin/env python3
"""Phase 4.6 parameter tuning: grid-search ATR stop multiplier × R:R ratio
on the TRAINING SPLIT ONLY (first 70% of the range). Validation/OOS splits
stay untouched — confirm any winner with run_backtest.py --split afterwards.

usage: tune_params.py --coin BTC --days 180 --interval 1h
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.backtester import Backtester, get_funding, get_history
from reaper.config import PROJECT_ROOT, Config
from reaper.logger import get_logger

log = get_logger("tune")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--coin", default="BTC")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--interval", default="1h",
                    choices=["1m", "5m", "15m", "1h"])
    ap.add_argument("--grid-atr", nargs="+", type=float,
                    default=[1.5, 2.0, 2.5, 3.0])
    ap.add_argument("--grid-rr", nargs="+", type=float, default=[2.0, 3.0])
    ap.add_argument("--step", type=int, default=None,
                    help="signal evaluation stride (default 3 on 1m/5m, 1 "
                         "on 15m/1h)")
    ap.add_argument("--min-agree", type=int, default=3)
    args = ap.parse_args()

    cfg = Config()
    step = args.step if args.step else (3 if args.interval in ("1m", "5m")
                                        else 1)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.days * 86_400_000

    # load once, slice the training split once — identical data per combo
    df = get_history(args.coin, args.interval, start_ms, end_ms, cfg.api_url)
    if df.empty:
        sys.exit(f"no data for {args.coin}")
    funding = get_funding(args.coin, start_ms - 86_400_000, end_ms,
                          cfg.api_url)
    df_train = df.iloc[:int(len(df) * 0.70)].reset_index(drop=True)
    span_d = (int(df_train['t'].iloc[-1]) - int(df_train['t'].iloc[0])) / 86_400_000
    print(f"== tuning {args.coin} on TRAINING split: {len(df_train)} "
          f"{args.interval} bars ({span_d:.0f} days), step={step} ==\n")

    rows = []
    for atr_mult in args.grid_atr:
        for rr in args.grid_rr:
            bt = Backtester(cfg, min_agreement=args.min_agree,
                            signal_step=step, interval=args.interval,
                            atr_sl_mult=atr_mult, rr=rr)
            bt._resolve_gate(args.coin)
            res = bt._simulate(args.coin, df_train, funding,
                               label=f"atr={atr_mult} rr={rr}")
            sl_n, sl_usd = res.exit_reasons.get("stop_loss", (0, 0.0))
            tp_n, _ = res.exit_reasons.get("take_profit", (0, 0.0))
            rows.append({
                "atr_mult": atr_mult, "rr": rr,
                "return_pct": round(res.total_return_pct, 2),
                "sharpe": round(res.sharpe_ratio, 2),
                "max_dd_pct": round(res.max_drawdown_pct, 2),
                "win_rate": round(res.win_rate, 3),
                "profit_factor": (round(res.profit_factor, 2)
                                  if res.profit_factor != float("inf")
                                  else None),
                "trades": res.total_trades,
                "avg_hold_min": round(res.avg_hold_minutes, 1),
                "sl_exits": sl_n, "tp_exits": tp_n,
            })
            r = rows[-1]
            print(f"atr={atr_mult:<4} rr={rr:<4} ret={r['return_pct']:+7.2f}% "
                  f"pf={r['profit_factor'] or 0:5.2f} win={r['win_rate']:5.1%} "
                  f"trades={r['trades']:<4} hold={r['avg_hold_min']:6.1f}m "
                  f"sl/tp={sl_n}/{tp_n}")

    rows.sort(key=lambda x: (x["profit_factor"] or 0, x["return_pct"]),
              reverse=True)
    best = rows[0]
    print(f"\nbest on training split: atr={best['atr_mult']} rr={best['rr']} "
          f"(pf={best['profit_factor']}, ret={best['return_pct']}%)")
    print("confirm with: run_backtest.py --split before trusting it.")

    out = PROJECT_ROOT / "data" / (
        f"tune_{args.coin}_{args.interval}_"
        f"{datetime.now(timezone.utc):%Y%m%d}.json")
    out.write_text(json.dumps({
        "coin": args.coin, "interval": args.interval, "days": args.days,
        "train_bars": len(df_train), "step": step,
        "min_agree": args.min_agree, "results": rows}, indent=1))
    print(f"saved {out}")


if __name__ == "__main__":
    main()
