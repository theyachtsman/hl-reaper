#!/usr/bin/env python3
"""Quick checkpoint readout for the horizon shadow run (data/shadow.db).

Tells you at a glance whether the OB-confirmation experiment has enough
samples yet (gate: >=30 CLOSED trades per OB bucket) and shows the running
PnL / win-rate split by whether OrderbookImbalance agreed with each entry.
Read-only — touches nothing live.
"""
import argparse
import datetime as dt
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from reaper.config import PROJECT_ROOT  # noqa: E402

READY_GATE = 30


def fmt_ts(ms):
    return dt.datetime.utcfromtimestamp(ms / 1000).strftime("%m-%d %H:%M") if ms else "—"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(PROJECT_ROOT / "data" / "shadow.db"))
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"no shadow db at {args.db} — is hl-shadow running?")
        return
    c = sqlite3.connect(args.db)

    total = c.execute("SELECT COUNT(*) FROM shadow_trades").fetchone()[0]
    n_open = c.execute(
        "SELECT COUNT(*) FROM shadow_trades WHERE status='OPEN'").fetchone()[0]
    n_closed = c.execute(
        "SELECT COUNT(*) FROM shadow_trades WHERE status='CLOSED'").fetchone()[0]
    rng = c.execute("SELECT MIN(entry_ts), MAX(entry_ts) FROM shadow_trades"
                    ).fetchone()
    print(f"shadow.db: {total} trades ({n_open} open, {n_closed} closed)  "
          f"span {fmt_ts(rng[0])} -> {fmt_ts(rng[1])} UTC")

    # per-coin closed summary
    print("\nclosed by coin:")
    print(f"  {'coin':6}{'n':>5}{'wins':>6}{'win%':>7}{'net_pnl%':>11}")
    for coin, n, w, net in c.execute(
            "SELECT coin, COUNT(*), SUM(net_pnl_pct>0), SUM(net_pnl_pct) "
            "FROM shadow_trades WHERE status='CLOSED' GROUP BY coin "
            "ORDER BY COUNT(*) DESC"):
        wr = (w / n) if n else 0
        print(f"  {coin:6}{n:>5}{w or 0:>6}{wr:>7.2f}{100*(net or 0):>11.3f}")

    # OB-confirmation readiness: split closed trades by OB agreement
    print("\nOB-confirmation buckets (CLOSED trades):")
    print(f"  {'bucket':22}{'n':>5}{'win%':>7}{'net_pnl%':>11}")
    rows = c.execute(
        "SELECT CASE WHEN ob_direction=direction THEN 'OB_agreed' "
        "            ELSE 'OB_disagreed_or_flat' END AS bucket, "
        "       COUNT(*), SUM(net_pnl_pct>0), SUM(net_pnl_pct) "
        "FROM shadow_trades WHERE status='CLOSED' GROUP BY bucket").fetchall()
    buckets = {r[0]: r for r in rows}
    ready = True
    for b in ("OB_agreed", "OB_disagreed_or_flat"):
        r = buckets.get(b)
        n = r[1] if r else 0
        if n < READY_GATE:
            ready = False
        if r:
            wr = (r[2] / r[1]) if r[1] else 0
            print(f"  {b:22}{r[1]:>5}{wr:>7.2f}{100*(r[3] or 0):>11.3f}")
        else:
            print(f"  {b:22}{0:>5}{'—':>7}{'—':>11}")

    print(f"\nexperiment readiness (>= {READY_GATE} closed/bucket): "
          f"{'READY ✅' if ready else 'not yet — keep accumulating'}")


if __name__ == "__main__":
    main()
