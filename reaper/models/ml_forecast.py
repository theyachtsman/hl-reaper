"""ML forecast model: XGBoost next-candle direction classifier.
The feature pipeline here is shared with scripts/train_ml_model.py so
training and inference can never drift apart."""
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import MACD
from ta.volatility import AverageTrueRange

from reaper.logger import get_logger
from reaper.models import BaseModel, LONG, SHORT, Ticket, candles_to_df

log = get_logger("model.ml")

FEATURES = [
    "ret_1", "ret_5", "ret_15", "ret_48",
    "vol_10", "vol_20", "vol_ratio",
    "rsi_14", "macd_hist", "atr_ratio",
    "hour_sin", "hour_cos", "dow_sin", "dow_cos", "funding",
]
MIN_CANDLES = 60


def build_features(df: pd.DataFrame, funding) -> pd.DataFrame:
    """Full feature superset from a [t,o,h,l,c,v] DataFrame. `funding` is a
    scalar (live inference) or a Series aligned to df.index (training).
    Callers select the column subset a given model was trained on."""
    out = pd.DataFrame(index=df.index)
    c = df["c"]
    out["ret_1"] = c.pct_change(1)
    out["ret_5"] = c.pct_change(5)
    out["ret_15"] = c.pct_change(15)
    out["ret_48"] = c.pct_change(48)   # multi-timeframe momentum
    out["vol_10"] = out["ret_1"].rolling(10).std()
    out["vol_20"] = out["ret_1"].rolling(20).std()
    out["vol_ratio"] = df["v"] / df["v"].rolling(20).mean().replace(0, np.nan)
    out["rsi_14"] = RSIIndicator(c, window=14).rsi()
    out["macd_hist"] = MACD(c).macd_diff()
    out["atr_ratio"] = AverageTrueRange(
        df["h"], df["l"], c, window=14).average_true_range() / c
    ts = pd.to_datetime(df["t"], unit="ms", utc=True)
    out["hour_sin"] = np.sin(2 * np.pi * ts.dt.hour / 24)
    out["hour_cos"] = np.cos(2 * np.pi * ts.dt.hour / 24)
    out["dow_sin"] = np.sin(2 * np.pi * ts.dt.dayofweek / 7)
    out["dow_cos"] = np.cos(2 * np.pi * ts.dt.dayofweek / 7)
    out["funding"] = funding
    return out[FEATURES]


class MLForecastModel(BaseModel):
    name = "MLForecastModel"

    def __init__(self, model_dir: str = "models/", min_confidence: float = 0.55):
        self.model_dir = Path(model_dir)
        self.min_confidence = min_confidence
        self._models: dict[str, object] = {}      # coin -> loaded model
        self._missing_logged: set[str] = set()

    def _load(self, coin: str) -> dict | None:
        """Returns {"model": clf, "interval": str} or None if not trained.
        The training interval is stored inside the pickle so inference always
        builds features from the same bar size the model was fit on."""
        if coin in self._models:
            return self._models[coin]
        path = self.model_dir / f"xgb_{coin}.pkl"
        if not path.exists():
            return None
        payload = joblib.load(path)
        if isinstance(payload, dict):
            entry = {"model": payload["model"],
                     "interval": payload.get("interval", "1m"),
                     "features": payload.get("features", FEATURES),
                     "horizon": payload.get("horizon", 1)}
        else:  # legacy pickle: bare classifier trained on 1m bars
            entry = {"model": payload, "interval": "1m",
                     "features": FEATURES, "horizon": 1}
        self._models[coin] = entry
        log.info("loaded %s (interval=%s horizon=%d bars)",
                 path, entry["interval"], entry["horizon"])
        return entry

    def compute(self, coin: str, buf, interval: str | None = None) -> Ticket:
        try:
            entry = self._load(coin)
            if entry is None:
                if coin not in self._missing_logged:
                    log.warning("no trained model for %s — run "
                                "scripts/train_ml_model.py", coin)
                    self._missing_logged.add(coin)
                return self.flat(reason="model not trained", coin=coin)
            model, interval = entry["model"], entry["interval"]

            df = candles_to_df(buf.latest_candles(coin, interval, 100))
            if len(df) < MIN_CANDLES:
                return self.flat(reason="insufficient_candles", n=len(df),
                                 interval=interval)
            funding = float((buf.ctx.get(coin) or {}).get("funding") or 0.0)
            feats = build_features(df, funding)[entry["features"]].iloc[[-1]]
            if feats.isna().any().any():
                return self.flat(reason="nan_features")

            proba = model.predict_proba(feats.values)[0]
            p_up = float(proba[1]) if len(proba) > 1 else float(proba[0])
            confidence = max(p_up, 1 - p_up)
            meta = {"p_up": round(p_up, 4)}
            if confidence < self.min_confidence:
                return self.flat(reason="below_min_confidence", **meta)
            direction = LONG if p_up >= 0.5 else SHORT
            return Ticket(self.name, direction, confidence, meta)
        except Exception as e:
            log.warning("compute failed for %s: %s", coin, e)
            return self.flat(error=str(e))
