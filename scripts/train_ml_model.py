#!/usr/bin/env python3
"""Train the per-coin XGBoost next-candle direction models.

Downloads 1m candles + funding history, builds the shared feature pipeline
from reaper.models.ml_forecast, trains on the first 80% and evaluates on the
final 20% (out-of-sample, time-ordered). Refuses to save any model whose OOS
accuracy is below the coin-flip-plus threshold."""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import joblib
import pandas as pd
from sklearn.metrics import accuracy_score
from xgboost import XGBClassifier

from reaper.backtester import get_funding, get_history
from reaper.config import PROJECT_ROOT, Config
from reaper.logger import get_logger
from reaper.models.ml_forecast import FEATURES, build_features

log = get_logger("train_ml")

MIN_OOS_ACC = 0.52  # below this the model is not worth deploying


def train_coin(coin: str, days: int, interval: str, horizon: int,
               data_url: str, out_dir: Path, min_oos_acc: float) -> bool:
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000

    df = get_history(coin, interval, start_ms, end_ms, data_url)
    if len(df) < 3000:
        log.error("%s: only %d candles — not enough to train", coin, len(df))
        return False
    funding = get_funding(coin, start_ms - 3_600_000, end_ms, data_url)

    # merge hourly funding onto candles (last known rate at candle time)
    fdf = pd.DataFrame(funding, columns=["t", "funding"])
    merged = pd.merge_asof(df.sort_values("t"), fdf.sort_values("t"),
                           on="t", direction="backward")
    merged["funding"] = merged["funding"].fillna(0.0)

    X = build_features(merged, merged["funding"])
    # target: direction of the close `horizon` bars ahead
    y = (merged["c"].shift(-horizon) > merged["c"]).astype(int)

    valid = X.notna().all(axis=1) & y.notna()
    valid.iloc[-horizon:] = False  # tail rows have no future target
    X, y = X[valid], y[valid]
    log.info("%s: %d samples, %d features, horizon=%d bars, up-ratio=%.3f",
             coin, len(X), len(FEATURES), horizon, y.mean())

    split = int(len(X) * 0.8)
    X_tr, X_te = X.iloc[:split], X.iloc[split:]
    y_tr, y_te = y.iloc[:split], y.iloc[split:]

    model = XGBClassifier(
        n_estimators=300, max_depth=5, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8, min_child_weight=5,
        eval_metric="logloss", n_jobs=4, random_state=42,
    )
    model.fit(X_tr.values, y_tr.values)

    acc_is = accuracy_score(y_tr, model.predict(X_tr.values))
    acc_oos = accuracy_score(y_te, model.predict(X_te.values))
    # the honest baseline is the majority class, not a 50/50 coin flip:
    # with up-ratio 0.42, always predicting "down" already scores 0.58
    majority = max(y_te.mean(), 1 - y_te.mean())
    required = max(min_oos_acc, majority + 0.01)
    print(f"\n=== {coin} ({interval} bars, horizon {horizon}) ===")
    print(f"in-sample accuracy:     {acc_is:.4f}")
    print(f"out-of-sample accuracy: {acc_oos:.4f}")
    print(f"majority-class baseline: {majority:.4f} -> required >= {required:.4f}")
    if acc_is - acc_oos > 0.15:
        print(f"note: IS/OOS gap {acc_is - acc_oos:.2f} — model is "
              f"memorizing the training window")
    print("feature importances:")
    for name, imp in sorted(zip(FEATURES, model.feature_importances_),
                            key=lambda kv: -kv[1]):
        print(f"  {name:<12s} {imp:.4f}")

    if acc_oos < required:
        print(f"REFUSING to save {coin}: OOS accuracy {acc_oos:.4f} < "
              f"{required:.4f} (no edge over the majority class)")
        return False

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"xgb_{coin}.pkl"
    joblib.dump({"model": model, "interval": interval,
                 "features": FEATURES, "horizon": horizon}, path)
    print(f"saved {path}")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--coins", nargs="+", default=None,
                    help="coins to train (default: config coins)")
    ap.add_argument("--days", type=int, default=180,
                    help="lookback days (served from data/history/ when "
                         "downloaded; the API alone retains ~5000 "
                         "candles/interval)")
    ap.add_argument("--interval", default="5m", choices=["1m", "5m", "1h"],
                    help="bar size; must exist in the live candle buffer")
    ap.add_argument("--horizon", type=int, default=12,
                    help="predict direction N bars ahead (12 × 5m = 1h); "
                         "next-bar (1) is near-unpredictable noise")
    ap.add_argument("--mainnet-data", action="store_true",
                    help="train on mainnet price history (read-only) even "
                         "when the bot trades testnet")
    ap.add_argument("--min-oos-acc", type=float, default=MIN_OOS_ACC)
    args = ap.parse_args()

    cfg = Config()
    data_url = ("https://api.hyperliquid.xyz" if args.mainnet_data
                else cfg.api_url)
    coins = args.coins or cfg.coins
    m = (cfg._raw.get("models", {}) or {})
    out_dir = (PROJECT_ROOT / m.get("ml_model_dir", "models/")).resolve()
    print(f"training on {data_url} ({args.interval} bars, {args.days}d)")

    saved = 0
    for coin in coins:
        try:
            if train_coin(coin, args.days, args.interval, args.horizon,
                          data_url, out_dir, args.min_oos_acc):
                saved += 1
        except Exception as e:
            log.error("training %s failed: %s", coin, e)
    print(f"\ndone: {saved}/{len(coins)} models saved to {out_dir}")
    sys.exit(0 if saved else 1)


if __name__ == "__main__":
    main()
