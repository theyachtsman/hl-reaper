"""Multi-factor liquidation cascade likelihood score (Phase 8.6 research).

Research module — NOT wired into the live ensemble. Scores where leverage is
trapped and how likely price is to reach and trigger a forced-liquidation
cascade there. Not a price-direction prediction.

    Cascade Score ~ OI_Expansion * Funding_Extremity * Cluster_Size *
                    Distance_to_Cluster(inv) * Volume_Confirmation

The scoring math lives in pure functions of plain series so the backtester
(scripts/backtest_cascade_score.py) can drive it from historical data with
no lookahead; compute_score() is the live wrapper reading buf/db.

Component scores are 0-10. Combined score is a weighted geometric mean
scaled 0-100 (multiplicative per the gameplan: a cascade needs ALL factors —
trapped leverage, crowding, proximity, and participation). Each component is
floored at COMPONENT_FLOOR before combining so one missing factor dampens
rather than zeroes the score (a hard zero would destroy lead time in the
backtest whenever e.g. volume hasn't confirmed yet).
"""
import math
import time

from reaper.logger import get_logger
from reaper.models import atr_from_candles

log = get_logger("model.cascade")

DEFAULT_WEIGHTS = {
    "oi_expansion": 0.20,
    "funding_extremity": 0.20,
    "cluster_size_est": 0.20,
    "distance_to_cluster": 0.25,
    "volume_confirmation": 0.15,
}
COMPONENT_FLOOR = 0.5

# normalization anchors (documented heuristics, tunable)
OI_EXPANSION_FULL = 0.08      # +8% OI vs rolling mean -> score 10
OI_FLAT_PX_LIMIT = 0.04       # price moved >4% in window -> no "quiet build"
FUNDING_FULL_8H = 0.001       # |0.1%/8h| -> score 10 (matches FundingRateModel extreme)
CLUSTER_FULL_FRAC_OI = 0.10   # cluster holding >=10% of OI notional -> score 10
DIST_MIN = 0.002              # closer than 0.2% = likely already trading through
DIST_MAX = 0.05               # farther than 5% = irrelevant
VOLUME_FULL_RATIO = 3.0       # recent vol >= 3x baseline -> score 10
EVENT_HALF_LIFE_MS = 24 * 3600 * 1000   # recency weight for real events
CLUSTER_BUCKET_PCT = 0.005    # 0.5% price buckets for event clustering

# OI-math fallback cluster geometry (no real events yet): crowd liquidation
# prices estimated at these distances from the recent extreme, splitting the
# crowded side's OI across typical leverage tiers (high lev liquidates first).
FALLBACK_TIERS = [(0.03, 0.5), (0.05, 0.3), (0.08, 0.2)]


# ---------------------------------------------------------------------------
# pure component scorers
# ---------------------------------------------------------------------------
def score_oi_expansion(oi_series: list[float],
                       px_change_pct: float) -> tuple[float, dict]:
    """Rate of OI growth vs the rolling window mean, dampened when price
    already moved a lot (we want leverage building QUIETLY)."""
    if len(oi_series) < 5 or oi_series[-1] <= 0:
        return 0.0, {"oi_growth_pct": None}
    mean = sum(oi_series) / len(oi_series)
    growth = oi_series[-1] / mean - 1 if mean > 0 else 0.0
    raw = max(0.0, min(1.0, growth / OI_EXPANSION_FULL))
    flat = max(0.3, min(1.0, 1 - abs(px_change_pct) / OI_FLAT_PX_LIMIT))
    return raw * flat * 10, {"oi_growth_pct": round(growth, 4),
                             "px_flatness": round(flat, 2)}


def score_funding_extremity(rate_8h: float) -> tuple[float, dict]:
    """|funding| normalized — extremes mean one side is crowded."""
    s = min(1.0, abs(rate_8h) / FUNDING_FULL_8H) * 10
    return s, {"rate_8h": round(rate_8h, 6),
               "crowded_side": "LONG" if rate_8h > 0 else "SHORT"}


def estimate_clusters(mark: float, oi_usd: float, rate_8h: float,
                      recent_high: float, recent_low: float,
                      events: list[tuple] | None = None,
                      now_ms: int | None = None) -> list[dict]:
    """Estimated liquidation clusters: [{price, side, size_usd}].
    side = which positions get liquidated if price reaches the level.

    With real liquidation_events history: bucket events by price (0.5%
    buckets), recency-weighted (24h half-life) — levels that recently
    liquidated tend to be re-populated leverage zones.
    Without: OI-math fallback — split the funding-crowded side's OI across
    leverage tiers below the recent high (longs) / above the recent low
    (shorts), refined version of the live LIQMAP heuristic."""
    if events:
        now_ms = now_ms or int(time.time() * 1000)
        buckets: dict[tuple[int, str], float] = {}
        for ts, side, size_usd, price, *_ in events:
            if not price or not size_usd or not side:
                continue
            w = 0.5 ** ((now_ms - ts) / EVENT_HALF_LIFE_MS)
            b = int(math.log(price / mark) / CLUSTER_BUCKET_PCT)
            buckets[(b, side)] = buckets.get((b, side), 0) + size_usd * w
        return [{"price": mark * math.exp(b * CLUSTER_BUCKET_PCT),
                 "side": side, "size_usd": usd, "source": "events"}
                for (b, side), usd in buckets.items() if usd > 0]

    # fallback: funding-implied crowd share of OI
    skew = 0.5 + min(0.4, abs(rate_8h) / (2 * FUNDING_FULL_8H) * 0.4)
    crowd_usd = oi_usd * skew
    out = []
    for dist, frac in FALLBACK_TIERS:
        if rate_8h >= 0:   # crowded longs liquidate below the recent high
            out.append({"price": recent_high * (1 - dist), "side": "LONG",
                        "size_usd": crowd_usd * frac, "source": "oi_math"})
        if rate_8h <= 0:   # crowded shorts liquidate above the recent low
            out.append({"price": recent_low * (1 + dist), "side": "SHORT",
                        "size_usd": crowd_usd * frac, "source": "oi_math"})
    return out


def score_cluster_size(clusters: list[dict], mark: float,
                       oi_usd: float) -> tuple[float, dict]:
    """$ concentration within reach (DIST_MAX) relative to total OI."""
    if not clusters or oi_usd <= 0 or mark <= 0:
        return 0.0, {"cluster_usd_in_reach": 0}
    in_reach = sum(c["size_usd"] for c in clusters
                   if abs(c["price"] - mark) / mark <= DIST_MAX)
    s = min(1.0, in_reach / (oi_usd * CLUSTER_FULL_FRAC_OI)) * 10
    return s, {"cluster_usd_in_reach": round(in_reach)}


def score_distance(clusters: list[dict],
                   mark: float) -> tuple[float, dict]:
    """Inverse distance to the nearest meaningful cluster, with a too-close
    cutoff (already trading through the level)."""
    if not clusters or mark <= 0:
        return 0.0, {"nearest_cluster_price": None,
                     "nearest_cluster_side": None}
    nearest = min(clusters, key=lambda c: abs(c["price"] - mark))
    d = abs(nearest["price"] - mark) / mark
    meta = {"nearest_cluster_price": round(nearest["price"], 6),
            "nearest_cluster_side": nearest["side"],
            "nearest_dist_pct": round(d, 4)}
    if d >= DIST_MAX:
        return 0.0, meta
    if d <= DIST_MIN:
        return 5.0, meta  # inside the zone — partially triggered already
    s = (DIST_MAX - d) / (DIST_MAX - DIST_MIN) * 10
    return s, meta


def score_volume(recent_vol_per_bar: float,
                 baseline_vol_per_bar: float) -> tuple[float, dict]:
    """Is the market actually moving with conviction, not drifting."""
    if baseline_vol_per_bar <= 0:
        return 0.0, {"vol_ratio": None}
    r = recent_vol_per_bar / baseline_vol_per_bar
    s = max(0.0, min(1.0, (r - 1) / (VOLUME_FULL_RATIO - 1))) * 10
    return s, {"vol_ratio": round(r, 2)}


def combine(components: dict[str, float],
            weights: dict[str, float] | None = None) -> float:
    """Weighted geometric mean of floored components, scaled 0-100."""
    w = weights or DEFAULT_WEIGHTS
    total_w = sum(w.values())
    acc = 0.0
    for k, wk in w.items():
        s = max(COMPONENT_FLOOR, components.get(k, 0.0))
        acc += wk * math.log(s / 10.0)
    return 100.0 * math.exp(acc / total_w)


def score_from_series(mark: float, oi_series: list[float], oi_usd: float,
                      rate_8h: float, recent_high: float, recent_low: float,
                      px_change_pct: float, recent_vol_per_bar: float,
                      baseline_vol_per_bar: float,
                      events: list[tuple] | None = None,
                      now_ms: int | None = None,
                      weights: dict[str, float] | None = None) -> dict:
    """Pure scoring path shared by live wrapper and backtester."""
    meta: dict = {}
    oi_s, m = score_oi_expansion(oi_series, px_change_pct)
    meta.update(m)
    fund_s, m = score_funding_extremity(rate_8h)
    meta.update(m)
    clusters = estimate_clusters(mark, oi_usd, rate_8h, recent_high,
                                 recent_low, events, now_ms)
    size_s, m = score_cluster_size(clusters, mark, oi_usd)
    meta.update(m)
    dist_s, m = score_distance(clusters, mark)
    nearest_px = m.pop("nearest_cluster_price")
    nearest_side = m.pop("nearest_cluster_side")
    meta.update(m)
    vol_s, m = score_volume(recent_vol_per_bar, baseline_vol_per_bar)
    meta.update(m)
    meta["cluster_source"] = clusters[0]["source"] if clusters else None
    meta["n_clusters"] = len(clusters)

    comps = {"oi_expansion": oi_s, "funding_extremity": fund_s,
             "cluster_size_est": size_s, "distance_to_cluster": dist_s,
             "volume_confirmation": vol_s}
    return {
        **{k: round(v, 2) for k, v in comps.items()},
        "combined_score": round(combine(comps, weights), 2),
        "nearest_cluster_price": nearest_px,
        "nearest_cluster_side": nearest_side,
        "meta": meta,
    }


# ---------------------------------------------------------------------------
class CascadeScoreModel:
    """Multi-factor liquidation cascade likelihood score.
    Research module — not yet integrated into live ensemble."""

    OI_WINDOW_BARS = 240      # 1m ctx samples ~ 4h (ctx polls every 60s)
    VOL_RECENT_BARS = 30      # 30m of 1m candles
    VOL_BASELINE_BARS = 240   # 4h baseline (buffer depth permitting)
    RANGE_BARS = 240          # recent high/low anchor window
    EVENT_LOOKBACK_MS = 7 * 24 * 3600 * 1000

    def __init__(self, weights: dict[str, float] | None = None):
        self.weights = weights or DEFAULT_WEIGHTS
        self._oi_hist: dict[str, list[float]] = {}

    def compute_score(self, coin: str, buf, db=None) -> dict:
        """Live scoring from the market buffer + optional liquidation store
        connection (sqlite3.Connection from reaper.data.liquidation_store).
        Returns component scores + combined score; never raises."""
        try:
            ctx = buf.ctx.get(coin) or {}
            mark = float(ctx.get("mark_px") or 0) or (buf.mid(coin) or 0)
            oi = float(ctx.get("open_interest") or 0)
            funding = ctx.get("funding")
            if mark <= 0 or oi <= 0 or funding is None:
                return self._empty("missing_ctx")
            rate_8h = float(funding) * 8

            hist = self._oi_hist.setdefault(coin, [])
            hist.append(oi)
            del hist[:-self.OI_WINDOW_BARS]

            candles = buf.latest_candles(coin, "1m", self.VOL_BASELINE_BARS)
            if len(candles) < self.VOL_RECENT_BARS + 5:
                return self._empty("insufficient_candles")
            closes = [float(x["c"]) for x in candles]
            vols = [float(x["v"]) for x in candles]
            rng = candles[-self.RANGE_BARS:]
            recent_high = max(float(x["h"]) for x in rng)
            recent_low = min(float(x["l"]) for x in rng)
            px_change = closes[-1] / closes[0] - 1
            recent_vol = (sum(vols[-self.VOL_RECENT_BARS:])
                          / self.VOL_RECENT_BARS)
            baseline_vol = sum(vols) / len(vols)

            events = None
            if db is not None:
                from reaper.data.liquidation_store import events_window
                now_ms = int(time.time() * 1000)
                events = events_window(db, coin,
                                       now_ms - self.EVENT_LOOKBACK_MS)
            out = score_from_series(
                mark, list(hist), oi * mark, rate_8h, recent_high,
                recent_low, px_change, recent_vol, baseline_vol,
                events=events or None, weights=self.weights)
            out["meta"]["atr_1m"] = atr_from_candles(candles)
            return out
        except Exception as e:
            log.warning("compute_score failed for %s: %s", coin, e)
            return self._empty(str(e))

    @staticmethod
    def _empty(reason: str) -> dict:
        return {"oi_expansion": 0.0, "funding_extremity": 0.0,
                "cluster_size_est": 0.0, "distance_to_cluster": 0.0,
                "volume_confirmation": 0.0, "combined_score": 0.0,
                "nearest_cluster_price": None, "nearest_cluster_side": None,
                "meta": {"reason": reason}}
