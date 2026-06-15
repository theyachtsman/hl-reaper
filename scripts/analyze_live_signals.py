#!/usr/bin/env python3
"""TASK 2 — Per-model attribution from live paper trades.

Answers: is ETH's win streak coming from real model signal or one regime, and
which of the 8 models are actually pulling their weight in the live ensemble?

Data sources (no live config touched — pure read/analysis):
  * Realized PnL  -> exchange user_fills (closedPnl/fee). This is the SAME
    authoritative source the dashboard /api/fills endpoint uses, so the
    per-coin win rates printed here reconcile with the dashboard.
  * Model votes   -> the `signals` table. run_bot.py logs all 8 models'
    tickets (model, direction, confidence) immediately before each gate-passing
    entry attempt (see run_bot.py ~line 353). Those per-model rows cluster at
    one timestamp per entry = one "ticket batch".

Method:
  1. Pull user_fills, reconstruct round-trip trades per coin by tracking the
     signed position (Open* fills build it, Close* fills unwind it). A trade's
     realized PnL = sum(closedPnl) over its closing fills; entry direction from
     the opening fill's `dir`.
  2. For each trade, match the model ticket batch logged at/just-before the
     entry fill time, same coin.
  3. Per coin, per model, compute:
       - agree_rate              : % of trades the model voted the trade's dir
       - win_rate_when_agree     : win rate of trades where it agreed
       - win_rate_when_disagree  : win rate when it was FLAT or opposite
"""
import argparse
import datetime as dt
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hyperliquid.info import Info  # noqa: E402

from reaper.config import Config  # noqa: E402

LONG, SHORT, FLAT = "LONG", "SHORT", "FLAT"
# how far before an entry fill a ticket batch may sit and still be its cause.
# run_bot logs tickets, then the maker order rests up to entry_timeout (~30s)
# before filling — allow generous slack but reject stale matches.
MATCH_WINDOW_MS = 5 * 60 * 1000


# ---------------------------------------------------------------------------
# 1. reconstruct round-trip trades from fills
# ---------------------------------------------------------------------------
def reconstruct_trades(fills: list[dict]) -> list[dict]:
    """Group raw fills into completed round-trip trades, per coin.

    Returns dicts: coin, direction (LONG/SHORT), entry_ts, exit_ts,
    realized_pnl (net of fees), gross_pnl, fees, n_fills.
    """
    by_coin: dict[str, list[dict]] = defaultdict(list)
    for f in fills:
        by_coin[f["coin"]].append(f)

    trades: list[dict] = []
    for coin, fl in by_coin.items():
        fl.sort(key=lambda x: (x["time"], x.get("tid", 0)))
        pos = 0.0          # signed position size
        cur: dict | None = None
        for f in fl:
            sz = float(f["sz"])
            signed = sz if f["side"] == "B" else -sz
            is_open = f["dir"].startswith("Open")
            if cur is None and is_open:
                cur = {
                    "coin": coin,
                    "direction": LONG if "Long" in f["dir"] else SHORT,
                    "entry_ts": f["time"],
                    "exit_ts": f["time"],
                    "gross_pnl": 0.0,
                    "fees": 0.0,
                    "n_fills": 0,
                }
            if cur is not None:
                cur["gross_pnl"] += float(f.get("closedPnl") or 0.0)
                cur["fees"] += float(f.get("fee") or 0.0)
                cur["n_fills"] += 1
                cur["exit_ts"] = f["time"]
            pos += signed
            # position flat (within a hair) -> round trip complete
            if cur is not None and abs(pos) < 1e-9:
                cur["realized_pnl"] = cur["gross_pnl"] - cur["fees"]
                trades.append(cur)
                cur = None
        # an unclosed position (still open right now) is simply dropped —
        # it has no realized PnL yet.
    trades.sort(key=lambda t: t["entry_ts"])
    return trades


# ---------------------------------------------------------------------------
# 2. load model ticket batches from the signals table
# ---------------------------------------------------------------------------
def load_ticket_batches(db_path: str) -> dict[str, list[dict]]:
    """Per coin, a time-sorted list of {ts, votes:{model:direction}} batches.

    Per-model ticket rows logged in the same cluster (run_bot writes all 8 in
    one loop pass) are grouped by proximity in time.
    """
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ts, coin, model, direction FROM signals "
        "WHERE model != 'AGGREGATOR' AND model != 'CASCADE_BOUNCE' "
        "ORDER BY coin, ts"
    ).fetchall()
    conn.close()

    batches: dict[str, list[dict]] = defaultdict(list)
    cur: dict | None = None
    cur_coin = None
    for r in rows:
        if (cur is None or r["coin"] != cur_coin
                or r["ts"] - cur["ts"] > 2000):  # >2s gap = new batch
            cur = {"ts": r["ts"], "votes": {}}
            cur_coin = r["coin"]
            batches[r["coin"]].append(cur)
        cur["votes"][r["model"]] = r["direction"]
        cur["ts"] = r["ts"]  # track latest ts in the cluster
    return batches


def match_batch(batches: list[dict], entry_ts: int) -> dict | None:
    """Latest ticket batch at or before entry_ts, within MATCH_WINDOW_MS."""
    best = None
    for b in batches:
        if b["ts"] <= entry_ts and entry_ts - b["ts"] <= MATCH_WINDOW_MS:
            if best is None or b["ts"] > best["ts"]:
                best = b
    return best


# ---------------------------------------------------------------------------
# 3. attribution
# ---------------------------------------------------------------------------
def attribute(trades: list[dict], batches: dict[str, list[dict]]):
    models: set[str] = set()
    for bl in batches.values():
        for b in bl:
            models.update(b["votes"].keys())

    matched, unmatched = [], 0
    for t in trades:
        b = match_batch(batches.get(t["coin"], []), t["entry_ts"])
        if b is None:
            unmatched += 1
            continue
        t["votes"] = b["votes"]
        t["win"] = t["realized_pnl"] > 0
        matched.append(t)
    return matched, unmatched, sorted(models)


def fmt_rate(num, den):
    return f"{num/den:.2f} (n={den})" if den else "—   (n=0)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="override SQLite path")
    args = ap.parse_args()

    cfg = Config()
    db_path = args.db or cfg.db_path

    info = Info(cfg.api_url, skip_ws=True)
    fills = info.user_fills(cfg.account_address) or []
    print(f"pulled {len(fills)} fills from {cfg.network} "
          f"({cfg.account_address})")

    trades = reconstruct_trades(fills)
    batches = load_ticket_batches(db_path)
    matched, unmatched, models = attribute(trades, batches)

    if trades:
        span = (f"{dt.datetime.utcfromtimestamp(trades[0]['entry_ts']/1000):%Y-%m-%d %H:%M}"
                f" -> {dt.datetime.utcfromtimestamp(trades[-1]['entry_ts']/1000):%Y-%m-%d %H:%M} UTC")
    else:
        span = "n/a"
    print(f"reconstructed {len(trades)} round-trip trades  ({span})")
    print(f"matched to a ticket batch: {len(matched)}   "
          f"unmatched (no ticket within {MATCH_WINDOW_MS//60000}min): {unmatched}")

    # ---- per-coin realized summary (reconciles with dashboard /api/fills) ---
    print("\n" + "=" * 72)
    print("PER-COIN REALIZED SUMMARY (all reconstructed trades)")
    print("=" * 72)
    print(f"{'coin':6} {'trades':>6} {'wins':>5} {'win_rate':>9} "
          f"{'net_pnl':>10} {'fees':>9} {'gross':>10}")
    by_coin = defaultdict(list)
    for t in trades:
        by_coin[t["coin"]].append(t)
    tot = defaultdict(float)
    for coin in sorted(by_coin):
        ts = by_coin[coin]
        wins = sum(1 for t in ts if t["realized_pnl"] > 0)
        net = sum(t["realized_pnl"] for t in ts)
        fees = sum(t["fees"] for t in ts)
        gross = sum(t["gross_pnl"] for t in ts)
        tot["trades"] += len(ts); tot["wins"] += wins
        tot["net"] += net; tot["fees"] += fees; tot["gross"] += gross
        print(f"{coin:6} {len(ts):>6} {wins:>5} {wins/len(ts):>9.2f} "
              f"{net:>10.4f} {fees:>9.4f} {gross:>10.4f}")
    print("-" * 72)
    print(f"{'TOTAL':6} {int(tot['trades']):>6} {int(tot['wins']):>5} "
          f"{tot['wins']/tot['trades'] if tot['trades'] else 0:>9.2f} "
          f"{tot['net']:>10.4f} {tot['fees']:>9.4f} {tot['gross']:>10.4f}")

    # ---- per-coin per-model attribution -------------------------------------
    mcoin = defaultdict(list)
    for t in matched:
        mcoin[t["coin"]].append(t)

    for coin in sorted(mcoin):
        ts = mcoin[coin]
        print("\n" + "=" * 72)
        print(f"{coin} MODEL ATTRIBUTION  ({len(ts)} matched trades, "
              f"base win rate {sum(t['win'] for t in ts)/len(ts):.2f})")
        print("=" * 72)
        print(f"{'model':26} {'agree_rate':>11} {'win|agree':>14} "
              f"{'win|disagree/flat':>20}")
        for m in models:
            agree = [t for t in ts if t["votes"].get(m) == t["direction"]]
            disagree = [t for t in ts if t["votes"].get(m) != t["direction"]]
            ar = len(agree) / len(ts) if ts else 0
            wa = sum(t["win"] for t in agree)
            wd = sum(t["win"] for t in disagree)
            print(f"{m:26} {ar:>11.2f} {fmt_rate(wa,len(agree)):>14} "
                  f"{fmt_rate(wd,len(disagree)):>20}")

    # ---- interpretation -----------------------------------------------------
    print("\n" + "=" * 72)
    print("INTERPRETATION")
    print("=" * 72)
    print(
        "* High agree_rate AND win|agree meaningfully > win|disagree  -> model\n"
        "  is pulling its weight; the edge is concentrated in its signal.\n"
        "* agree_rate ~0 or win|agree ~ win|disagree                  -> dead\n"
        "  weight / noise in the current ensemble, candidate to down-weight.\n"
        "* MLForecastModel / LiquidationHeatmapModel are expected near-FLAT\n"
        "  (agree_rate ~0) — this confirms whether they contribute at all.\n"
        "* Any model row with n<10 in the agree column is too thin to trust.")


if __name__ == "__main__":
    main()
