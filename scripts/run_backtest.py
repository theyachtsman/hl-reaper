#!/usr/bin/env python3
"""Backtest CLI: replay historical data through the full signal pipeline.

usage: run_backtest.py --coin BTC --days 90 --split
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.backtester import Backtester
from reaper.config import PROJECT_ROOT, Config
from reaper.logger import get_logger

log = get_logger("backtest")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--coin", default="BTC")
    ap.add_argument("--days", type=int, default=17,
                    help="lookback days (API retains ~5000 candles/interval: "
                         "1m≈3.5d, 5m≈17d, 1h≈208d)")
    ap.add_argument("--interval", default="5m",
                    choices=["1m", "5m", "15m", "1h"],
                    help="bar size to replay (match the ML training interval "
                         "or the ML model votes FLAT)")
    ap.add_argument("--mainnet-data", action="store_true",
                    help="replay mainnet price history (read-only) even "
                         "when the bot trades testnet")
    ap.add_argument("--split", action="store_true",
                    help="walk-forward 70/15/15 train/validation/OOS report")
    ap.add_argument("--min-confidence", type=float, default=None,
                    help="override the replay-scaled confidence gate")
    ap.add_argument("--min-agree", type=int, default=3,
                    help="model quorum (candle-driven models only in replay)")
    ap.add_argument("--step", type=int, default=None,
                    help="evaluate signals every N candles "
                         "(default: 5 on 1m bars, 1 otherwise)")
    ap.add_argument("--equity", type=float, default=10_000.0)
    ap.add_argument("--atr-mult", type=float, default=None,
                    help="stop distance in ATRs (default: config risk value)")
    ap.add_argument("--rr", type=float, default=2.0,
                    help="take-profit at N × initial risk")
    ap.add_argument("--entry", default="taker", choices=["taker", "maker"],
                    help="maker = post-only entry at signal-bar close, "
                         "skip if not traded through within 2 bars")
    args = ap.parse_args()

    cfg = Config()
    data_url = ("https://api.hyperliquid.xyz" if args.mainnet_data
                else cfg.api_url)
    step = args.step if args.step else (5 if args.interval == "1m" else 1)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.days * 86_400_000

    bt = Backtester(cfg, min_confidence=args.min_confidence,
                    min_agreement=args.min_agree, signal_step=step,
                    start_equity=args.equity, interval=args.interval,
                    data_api_url=data_url, atr_sl_mult=args.atr_mult,
                    rr=args.rr, entry_style=args.entry)

    print(f"== HL Reaper backtest: {args.coin}, {args.days}d of "
          f"{args.interval} bars, data={data_url} ==\n")

    out = {"coin": args.coin, "days": args.days, "interval": args.interval,
           "data_api": data_url,
           "generated": datetime.now(timezone.utc).isoformat()}

    if args.split:
        res = bt.walk_forward(args.coin, start_ms, end_ms)
        for key in ("train", "validation", "test"):
            print(res[key].summary() + "\n")
        if res["oos_degraded"]:
            print("*** WARNING: out-of-sample results degrade >30% vs "
                  "training — likely overfit. DO NOT proceed to live. ***")
        else:
            print("OOS check: no >30% degradation vs training split.")
        out["splits"] = {
            key: {**_metrics_dict(res[key]),
                  "equity_curve": res[key].equity_curve}
            for key in ("train", "validation", "test")}
        out["oos_degraded"] = res["oos_degraded"]
    else:
        res = bt.run(args.coin, start_ms, end_ms)
        print(res.summary())
        out["results"] = _metrics_dict(res)
        out["equity_curve"] = res.equity_curve

    # gate is resolved per-coin inside run()/walk_forward(), so record it after
    out["params"] = {"min_confidence": bt.min_confidence,
                     "min_agreement": bt.min_agreement,
                     "signal_step": bt.signal_step,
                     "start_equity": bt.start_equity}

    data_dir = PROJECT_ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    fname = data_dir / (f"backtest_{args.coin}_"
                        f"{datetime.now(timezone.utc):%Y%m%d}.json")
    fname.write_text(json.dumps(out))
    print(f"\nsaved {fname}")


def _metrics_dict(r) -> dict:
    return {
        "total_return_pct": r.total_return_pct,
        "sharpe_ratio": r.sharpe_ratio,
        "max_drawdown_pct": r.max_drawdown_pct,
        "win_rate": r.win_rate,
        "profit_factor": (r.profit_factor
                          if r.profit_factor != float("inf") else None),
        "total_trades": r.total_trades,
        "avg_hold_minutes": r.avg_hold_minutes,
        "per_model_contribution": r.per_model_contribution,
    }


if __name__ == "__main__":
    main()
