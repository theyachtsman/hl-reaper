#!/usr/bin/env python3
"""Phase 4.6 TA audit: which TA sub-signals actually predict forward returns?

Mirrors TAModel's four components (RSI lean, MACD histogram, EMA cross,
Bollinger touch) vectorized over the TRAINING split (first 70%) and measures
mean forward return per signal state, net of round-trip taker fees. A
component is only worth its weight if its long-state forward return beats
fees AND its short-state is symmetric.

usage: audit_ta.py --coin BTC --interval 15m --horizon 16
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import BollingerBands

from reaper.backtester import get_history
from reaper.config import Config

FEE_RT = 0.0007  # taker round trip


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--coin", default="BTC")
    ap.add_argument("--interval", default="15m",
                    choices=["1m", "5m", "15m", "1h"])
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--horizon", type=int, default=16,
                    help="forward-return horizon in bars")
    args = ap.parse_args()

    cfg = Config()
    end_ms = int(time.time() * 1000)
    df = get_history(args.coin, args.interval,
                     end_ms - args.days * 86_400_000, end_ms, cfg.api_url)
    df = df.iloc[:int(len(df) * 0.70)].reset_index(drop=True)  # training only
    c = df["c"]
    fwd = c.shift(-args.horizon) / c - 1.0

    rsi = RSIIndicator(c, window=14).rsi()
    macd_hist = MACD(c).macd_diff()
    ema9 = EMAIndicator(c, window=9).ema_indicator()
    ema21 = EMAIndicator(c, window=21).ema_indicator()
    bb = BollingerBands(c, window=20, window_dev=2)
    bb_hi, bb_lo = bb.bollinger_hband(), bb.bollinger_lband()

    components = {
        "RSI<30 (long lean)":        rsi < 30,
        "RSI>70 (short lean)":       rsi > 70,
        "RSI<25 (long, tighter)":    rsi < 25,
        "RSI>75 (short, tighter)":   rsi > 75,
        "MACD hist > 0 (long)":      macd_hist > 0,
        "MACD hist < 0 (short)":     macd_hist < 0,
        "EMA9>EMA21 (long)":         ema9 > ema21,
        "EMA9<EMA21 (short)":        ema9 < ema21,
        "px<=BB lower (long)":       c <= bb_lo,
        "px>=BB upper (short)":      c >= bb_hi,
    }

    base = float(fwd.mean())
    print(f"== TA audit: {args.coin} {args.interval}, training split "
          f"({len(df)} bars), forward horizon {args.horizon} bars ==")
    print(f"baseline mean fwd return: {base * 100:+.4f}%   "
          f"round-trip fee: {FEE_RT * 100:.3f}%\n")
    print(f"{'signal state':<26s} {'n':>6s} {'mean fwd':>10s} "
          f"{'net of fee*':>11s} {'hit>0':>7s}")
    for name, mask in components.items():
        m = mask & fwd.notna()
        n = int(m.sum())
        if n < 20:
            print(f"{name:<26s} {n:>6d}   (too few samples)")
            continue
        is_short = "short" in name
        mean = float(fwd[m].mean()) * (-1 if is_short else 1)
        net = mean - FEE_RT
        hit = float(((fwd[m] < 0) if is_short else (fwd[m] > 0)).mean())
        verdict = "  <- edge" if net > 0 else ""
        print(f"{name:<26s} {n:>6d} {mean * 100:>+9.4f}% {net * 100:>+10.4f}% "
              f"{hit:>6.1%}{verdict}")
    print("\n* mean directional fwd return minus round-trip fee. Positive "
          "net = component worth keeping in this state.")


if __name__ == "__main__":
    main()
