#!/usr/bin/env python3
"""OFFLINE ML feature experiment — does adding spot/OI/divergence features let
the next-direction model clear the majority-class gate? (Phase 4.6 follow-up.)

This does NOT save or deploy anything. The new features (spot returns, OI
deltas, perp-spot divergence) are NOT computable by the live MLForecastModel —
the bot's buffer holds only the latest spot tick and latest OI, not the
bar-aligned series these need. So a model trained on them can't run live
without first wiring spot/OI history into the live feature pipeline (a real
live-path change, out of scope here). This script's only job: tell us whether
those features would be worth that wiring, using the same 80/20 split, same
XGBoost params, and the SAME honest gate as scripts/train_ml_model.py.

usage: train_ml_experiment.py [--coins ...] [--interval 5m] [--horizon 12]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score
from xgboost import XGBClassifier

from reaper.backtester import (DEFAULT_HISTORY_DIR, get_funding,
                               load_local_history)
from reaper.logger import get_logger
from reaper.models.ml_forecast import FEATURES, build_features

log = get_logger("train_exp")

NEW_FEATURES = ["spot_ret_1", "spot_ret_5", "oi_change_1", "oi_change_5",
                "perp_spot_divergence_1"]


def load_spot_5m(coin, interval, start_ms, end_ms):
    """Spot closes resampled to `interval`, indexed by bar ts (ms)."""
    p = DEFAULT_HISTORY_DIR / f"{coin}_spot_1m.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p)
    df = df[(df["t"] >= start_ms) & (df["t"] <= end_ms)]
    if df.empty:
        return None
    idx = pd.to_datetime(df["t"], unit="ms", utc=True)
    res = df.set_index(idx)["c"].resample(interval.replace("m", "min")).last()
    out = pd.DataFrame({"t": res.index.astype("int64") // 10 ** 6,
                        "spot_c": res.values}).dropna()
    return out


def load_oi_5m(coin, start_ms, end_ms):
    p = DEFAULT_HISTORY_DIR / f"{coin}_oi_5m.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p)
    df = df[(df["t"] >= start_ms) & (df["t"] <= end_ms)]
    return df[["t", "oi"]].reset_index(drop=True) if not df.empty else None


def run_coin(coin, days, interval, horizon, min_oos_acc):
    import time
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000

    perp = load_local_history(coin, interval, start_ms, end_ms)
    if perp is None or len(perp) < 3000:
        print(f"{coin}: insufficient perp history"); return
    funding = get_funding(coin, start_ms - 3_600_000, end_ms, "")
    fdf = pd.DataFrame(funding, columns=["t", "funding"])
    merged = pd.merge_asof(perp.sort_values("t"), fdf.sort_values("t"),
                           on="t", direction="backward")
    merged["funding"] = merged["funding"].fillna(0.0)

    spot = load_spot_5m(coin, interval, start_ms, end_ms)
    oi = load_oi_5m(coin, start_ms, end_ms)
    if spot is None or oi is None:
        print(f"{coin}: missing spot or OI history"); return
    merged = pd.merge_asof(merged.sort_values("t"), spot.sort_values("t"),
                           on="t", direction="backward")
    merged = pd.merge_asof(merged.sort_values("t"), oi.sort_values("t"),
                           on="t", direction="backward")

    base = build_features(merged, merged["funding"])    # the live-safe 15
    ext = pd.DataFrame(index=merged.index)
    sc = merged["spot_c"]
    ext["spot_ret_1"] = sc.pct_change(1)
    ext["spot_ret_5"] = sc.pct_change(5)
    oiv = merged["oi"].replace(0, np.nan)
    ext["oi_change_1"] = oiv.pct_change(1)
    ext["oi_change_5"] = oiv.pct_change(5)
    ext["perp_spot_divergence_1"] = base["ret_1"] - ext["spot_ret_1"]

    y = (merged["c"].shift(-horizon) > merged["c"]).astype(int)

    def evaluate(cols, label):
        X = pd.concat([base, ext], axis=1)[cols]
        valid = X.notna().all(axis=1) & y.notna()
        valid.iloc[-horizon:] = False
        Xv, yv = X[valid], y[valid]
        split = int(len(Xv) * 0.8)
        Xtr, Xte = Xv.iloc[:split], Xv.iloc[split:]
        ytr, yte = yv.iloc[:split], yv.iloc[split:]
        m = XGBClassifier(n_estimators=300, max_depth=5, learning_rate=0.05,
                          subsample=0.8, colsample_bytree=0.8,
                          min_child_weight=5, eval_metric="logloss",
                          n_jobs=4, random_state=42)
        m.fit(Xtr.values, ytr.values)
        acc_is = accuracy_score(ytr, m.predict(Xtr.values))
        acc_oos = accuracy_score(yte, m.predict(Xte.values))
        majority = max(yte.mean(), 1 - yte.mean())
        required = max(min_oos_acc, majority + 0.01)
        ok = acc_oos >= required
        print(f"  {label:18} IS {acc_is:.4f}  OOS {acc_oos:.4f}  "
              f"req {required:.4f}  -> {'PASS' if ok else 'fail'}")
        return m, list(cols), acc_oos

    print(f"\n=== {coin} ({interval}, horizon {horizon}, {len(merged)} bars) ===")
    evaluate(FEATURES, "base-15")
    m, cols, _ = evaluate(FEATURES + NEW_FEATURES, "base+new")
    imp = sorted(zip(cols, m.feature_importances_), key=lambda kv: -kv[1])
    print("  feature importances (base+new), new features marked *:")
    for name, v in imp:
        star = " *" if name in NEW_FEATURES else ""
        print(f"    {name:<24s} {v:.4f}{star}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--coins", nargs="+",
                    default=["BTC", "ETH", "SOL", "ARB", "AVAX", "DOGE", "WIF"])
    ap.add_argument("--days", type=int, default=230)
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--horizon", type=int, default=12)
    ap.add_argument("--min-oos-acc", type=float, default=0.52)
    args = ap.parse_args()
    print("OFFLINE EXPERIMENT — trains nothing for deployment. Tests whether "
          "spot/OI/divergence features clear the same honest gate.")
    for coin in args.coins:
        try:
            run_coin(coin, args.days, args.interval, args.horizon,
                     args.min_oos_acc)
        except Exception as e:
            log.error("%s failed: %s", coin, e)


if __name__ == "__main__":
    main()
