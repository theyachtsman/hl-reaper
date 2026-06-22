"""Shared model primitives: Ticket dataclass, BaseModel ABC, candle helpers."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import pandas as pd

from reaper.data.buffer import MarketBuffer

LONG = "LONG"
SHORT = "SHORT"
FLAT = "FLAT"


@dataclass
class Ticket:
    model: str               # model name
    direction: str           # "LONG" | "SHORT" | "FLAT" (regime string for RegimeDetectorModel)
    confidence: float        # 0.0 – 1.0
    meta: dict = field(default_factory=dict)  # debug info


class BaseModel(ABC):
    """All models implement compute(); it must never raise — each model
    catches internally and returns a FLAT ticket via self.flat()."""

    name: str = "BaseModel"

    @abstractmethod
    def compute(self, coin: str, buf: MarketBuffer,
                interval: str | None = None) -> Ticket:
        """Compute and return a Ticket. Must never raise — catch internally.

        interval: optional candle resolution to evaluate on (dual-band: "5m"
        for the scalp band, "1h" for the trend band). When None each model uses
        its own historical default — preserves all legacy single-band callers.
        Interval-agnostic models (orderbook, funding) accept and ignore it."""

    def flat(self, **meta) -> Ticket:
        return Ticket(self.name, FLAT, 0.0, meta)


def candles_to_df(candles: list[dict]) -> pd.DataFrame:
    """Convert buffer candle dicts (string values from WS) to a float DataFrame
    with columns [t, o, h, l, c, v]."""
    if not candles:
        return pd.DataFrame(columns=["t", "o", "h", "l", "c", "v"])
    df = pd.DataFrame(candles)
    out = pd.DataFrame({
        "t": df["t"].astype("int64"),
        "o": df["o"].astype(float),
        "h": df["h"].astype(float),
        "l": df["l"].astype(float),
        "c": df["c"].astype(float),
        "v": df["v"].astype(float),
    })
    return out.reset_index(drop=True)


def atr_from_candles(candles: list[dict], period: int = 14) -> float | None:
    """Simple ATR (SMA of true range) from buffer candle dicts.
    Returns None if fewer than period+1 candles."""
    if not candles or len(candles) < period + 1:
        return None
    highs = [float(x["h"]) for x in candles]
    lows = [float(x["l"]) for x in candles]
    closes = [float(x["c"]) for x in candles]
    trs = []
    for i in range(1, len(candles)):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    window = trs[-period:]
    return sum(window) / len(window)
