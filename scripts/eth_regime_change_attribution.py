#!/usr/bin/env python3
"""ETH regime-change attribution — did any model see the 6/14 reversal coming?

ETH ranged/declined ~1670-1720 through most of 2026-06-14, then broke out
upward (~21:00 UTC, 1680 -> 1753). The live aggressive bot kept SHORTing into
the rally and got stopped out (SL @ 1750/1758/1763). This splits ETH's
round-trip trades at the inflection point and runs per-model attribution on
each side, with a focus on whether OrderbookImbalance (now 26% weight) diverged
from the losing SHORT aggregate before the others.

Pure analysis — same round-trip / fill methodology as analyze_live_signals.py.
"""
import datetime as dt
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hyperliquid.info import Info  # noqa: E402

from reaper.config import Config  # noqa: E402
from scripts.analyze_live_signals import (  # noqa: E402
    LONG, SHORT, reconstruct_trades, load_ticket_batches, match_batch,
    fmt_rate)

OPEN_MATCH_MS = 3 * 60 * 1000      # round-trip entry <-> trades.OPEN row
CLOSE_MATCH_MS = 6 * 60 * 1000     # round-trip exit  <-> trades.CLOSE row


def find_inflection(info, day=dt.date(2026, 6, 14)):
    """Step-change detector on testnet 1m ETH closes (what the bot saw):
    the minute maximizing mean(next 60m) - mean(prev 120m) = sharpest
    down->up transition. Returns (ts_ms, price_series, chosen_price)."""
    start = int(dt.datetime(day.year, day.month, day.day).timestamp() * 1000)
    end = start + 24 * 3600 * 1000
    cs = info.candles_snapshot("ETH", "1m", start, end) or []
    series = [(int(c["t"]), float(c["c"])) for c in cs]
    series.sort()
    times = [t for t, _ in series]
    px = [p for _, p in series]

    def mean(a, b):
        seg = px[a:b]
        return sum(seg) / len(seg) if seg else None

    best_ts, best_score, best_px = None, -1e18, None
    for i in range(len(series)):
        # only consider the afternoon/evening transition window
        h = dt.datetime.utcfromtimestamp(times[i] / 1000).hour
        if not (12 <= h <= 22):
            continue
        prev = mean(max(0, i - 120), i)
        nxt = mean(i, min(len(px), i + 60))
        if prev is None or nxt is None:
            continue
        score = nxt - prev
        if score > best_score:
            best_score, best_ts, best_px = score, times[i], px[i]
    return best_ts, series, best_px


def load_trade_meta(db_path):
    """ETH OPEN-confidence (from note) and CLOSE-reason (from note), by ts."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    opens, closes = [], []
    for r in conn.execute(
            "SELECT ts,note FROM trades WHERE coin='ETH' AND action='OPEN' "
            "AND status='filled'"):
        conf = None
        for tok in (r["note"] or "").split():
            if tok.startswith("conf="):
                try:
                    conf = float(tok.split("=")[1])
                except ValueError:
                    pass
        opens.append((r["ts"], conf))
    for r in conn.execute(
            "SELECT ts,note FROM trades WHERE coin='ETH' AND action='CLOSE'"):
        note = (r["note"] or "").lower()
        if "take profit" in note:
            reason = "TP"
        elif "trailing" in note:
            reason = "trail"
        elif "stop loss" in note:
            reason = "SL"
        elif "hold" in note:
            reason = "maxhold"
        else:
            reason = "other"
        closes.append((r["ts"], reason))
    conn.close()
    return opens, closes


def nearest(rows, ts, tol):
    best, bestd = None, tol + 1
    for rts, val in rows:
        d = abs(rts - ts)
        if d <= tol and d < bestd:
            best, bestd = val, d
    return best


def attribution_table(trades, models):
    base = sum(t["win"] for t in trades) / len(trades) if trades else 0
    print(f"{'model':26} {'agree_rate':>11} {'win|agree':>14} "
          f"{'win|flat/disagree':>20}")
    for m in models:
        agree = [t for t in trades if t["votes"].get(m) == t["direction"]]
        dis = [t for t in trades if t["votes"].get(m) != t["direction"]]
        ar = len(agree) / len(trades) if trades else 0
        print(f"{m:26} {ar:>11.2f} "
              f"{fmt_rate(sum(t['win'] for t in agree), len(agree)):>14} "
              f"{fmt_rate(sum(t['win'] for t in dis), len(dis)):>20}")
    return base


def main():
    cfg = Config()
    info = Info(cfg.api_url, skip_ws=True)

    infl_ts, series, infl_px = find_inflection(info)
    infl_str = dt.datetime.utcfromtimestamp(infl_ts / 1000).strftime(
        "%Y-%m-%d %H:%M") if infl_ts else "n/a"

    fills = [f for f in (info.user_fills(cfg.account_address) or [])
             if f["coin"] == "ETH"]
    trades = reconstruct_trades(fills)
    batches = load_ticket_batches(cfg.db_path).get("ETH", [])
    opens, closes = load_trade_meta(cfg.db_path)

    models = sorted({m for b in batches for m in b["votes"]})

    matched = []
    for t in trades:
        b = match_batch(batches, t["entry_ts"])
        if b is None:
            continue
        t["votes"] = b["votes"]
        t["win"] = t["realized_pnl"] > 0
        t["conf"] = nearest(opens, t["entry_ts"], OPEN_MATCH_MS)
        t["exit_reason"] = nearest(closes, t["exit_ts"], CLOSE_MATCH_MS)
        matched.append(t)

    pre = [t for t in matched if t["entry_ts"] < infl_ts]
    post = [t for t in matched if t["entry_ts"] >= infl_ts]

    out = []

    def p(s=""):
        print(s)
        out.append(s)

    p("=== ETH regime-change attribution — 2026-06-14 ===")
    p(f"Inflection point (step-detector on testnet 1m): ~{infl_str} UTC "
      f"(~{infl_px:.0f})")
    p(f"ETH round-trips: {len(trades)} reconstructed, {len(matched)} matched "
      f"to a ticket batch  | pre={len(pre)} post={len(post)}")
    p("")

    for label, bucket in (("PRE-REVERSAL", pre), ("POST-REVERSAL", post)):
        if not bucket:
            p(f"{label}: no matched trades"); p(""); continue
        wr = sum(t["win"] for t in bucket) / len(bucket)
        longs = sum(1 for t in bucket if t["direction"] == LONG)
        shorts = sum(1 for t in bucket if t["direction"] == SHORT)
        net = sum(t["realized_pnl"] for t in bucket)
        p(f"{label} (N={len(bucket)}, win rate {wr:.2f}, "
          f"{longs}L/{shorts}S, net ${net:.3f})")
        attribution_table(bucket, models)
        p("")

    # confidence comparison
    def avg_conf(b):
        cs = [t["conf"] for t in b if t["conf"] is not None]
        return sum(cs) / len(cs) if cs else None
    pc, qc = avg_conf(pre), avg_conf(post)
    p("Confidence comparison (aggregate conf at entry, from trade log):")
    p(f"  Pre-reversal  avg entry confidence: "
      f"{pc:.3f}" if pc is not None else "  Pre-reversal: n/a")
    p(f"  Post-reversal avg entry confidence: "
      f"{qc:.3f}" if qc is not None else "  Post-reversal: n/a")
    p("")

    # OB divergence check on post-reversal SHORT trades
    post_shorts = [t for t in post if t["direction"] == SHORT]
    ob_dis = [t for t in post_shorts
              if t["votes"].get("OrderbookImbalanceModel") != SHORT]
    ob_agr = [t for t in post_shorts
              if t["votes"].get("OrderbookImbalanceModel") == SHORT]

    def sl_tp(b):
        n = len(b)
        sl = sum(1 for t in b if t["exit_reason"] == "SL")
        tp = sum(1 for t in b if t["exit_reason"] == "TP")
        tr = sum(1 for t in b if t["exit_reason"] == "trail")
        return n, sl, tp, tr

    p("OB divergence check (post-reversal SHORT trades):")
    for name, b in (("OB disagreed (voted LONG/FLAT)", ob_dis),
                    ("OB agreed (voted SHORT)", ob_agr)):
        n, sl, tp, tr = sl_tp(b)
        if n:
            p(f"  {name}: N={n} -> SL {sl}/{n} ({sl/n:.0%}), "
              f"TP {tp}/{n} ({tp/n:.0%}), trail {tr}/{n} ({tr/n:.0%})")
        else:
            p(f"  {name}: N=0")
    p("")

    # interpretation
    p("INTERPRETATION:")
    if not post_shorts:
        p("- No matched post-reversal SHORT trades to judge OB divergence.")
    elif len(ob_dis) >= 3 and len(ob_agr) >= 1:
        d_sl = sum(1 for t in ob_dis if t["exit_reason"] == "SL") / len(ob_dis)
        a_sl = (sum(1 for t in ob_agr if t["exit_reason"] == "SL")
                / len(ob_agr))
        if d_sl > a_sl + 0.1:
            p("- OB-disagreed shorts stopped out MORE than OB-agreed -> OB "
              "carried early reversal info the aggregator under-weighted.")
        else:
            p("- OB-disagreed shorts did NOT stop out more -> no evidence OB "
              "saw the reversal earlier than the aggregate.")
    else:
        p("- Samples too thin to separate OB-divergent vs OB-agreed shorts "
          "(need >=3 per side).")
    if pc is not None and qc is not None:
        if qc < pc - 0.02:
            p(f"- Entry confidence DROPPED post-reversal ({pc:.3f}->{qc:.3f}) "
              "yet trades still fired -> the 0.35/3 aggressive gate is too "
              "loose to filter degrading signal. [DESIGN NOTE for Phase 8: "
              "consider a gate that scales with recent realized vol / win-rate "
              "— flag only, do not implement.]")
        else:
            p(f"- Entry confidence ~flat across the reversal ({pc:.3f}->"
              f"{qc:.3f}) -> signal did not degrade in the score; the gate "
              "isn't the issue, the models simply didn't detect the flip.")
    p("- RegimeDetector classifies trend/range/vol, NOT direction-of-trend-"
      "change; nothing in the ensemble does directional regime detection. "
      "Consistent with Phase 4.6: performance tracks whichever regime runs.")

    report = Path(__file__).resolve().parent.parent / "docs" / \
        "eth_regime_change_attribution.md"
    report.write_text("# ETH Regime-Change Attribution — 2026-06-14\n\n"
                      "Generated by `scripts/eth_regime_change_attribution.py` "
                      "(round-trip fills + ticket-batch matching).\n\n```\n"
                      + "\n".join(out) + "\n```\n")
    print(f"\nwrote {report}")


if __name__ == "__main__":
    main()
