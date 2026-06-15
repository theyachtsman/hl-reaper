#!/usr/bin/env python3
"""OB-confirmation experiment — REDESIGNED (non-tautological).

Original design bucketed shadow trades by `ob_direction == direction`. That is
degenerate: OB is 26% of the vote that *chooses* `direction`, so OB almost
always "agrees" with an entry it helped create (20/20 in the first sample). No
amount of accumulation produces a disagreed bucket.

Redesign: the base strategy is the NON-OB ensemble. For each logged trade we
re-run the REAL SignalAggregator on its logged tickets twice —
  (1) full weights      -> sanity check it reproduces the logged entry
  (2) OB weight zeroed  -> the non-OB base direction
— then bucket by OB's relationship to the non-OB base:

  OB_confirms   : non-OB base has a direction AND ob_direction matches it
  OB_contradicts: non-OB base has a direction AND ob_direction opposes it
  OB_decides    : non-OB base is FLAT, so OB is what carried the entry

OB_contradicts + OB_decides are the non-tautological buckets the original design
could never populate. The question this actually answers: does OB's marginal
push (overriding or breaking a tie in the rest of the ensemble) help or hurt?

Read-only on data/shadow.db. At small n this is a METHODOLOGY VALIDATION — the
point is to confirm the non-tautological buckets are non-empty; a verdict needs
the >=30/bucket gate.

usage: ob_confirmation_experiment.py [--db data/shadow.db]
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.aggregator import SignalAggregator
from reaper.config import PROJECT_ROOT
from reaper.models import Ticket

OB = "OrderbookImbalanceModel"


def tickets_from_json(tj: dict) -> list[Ticket]:
    return [Ticket(model=name, direction=d.get("dir", "FLAT"),
                   confidence=float(d.get("conf", 0.0) or 0.0))
            for name, d in tj.items()]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(PROJECT_ROOT / "data" / "shadow.db"))
    args = ap.parse_args()
    if not Path(args.db).exists():
        print(f"no shadow db at {args.db}")
        return

    full_agg = SignalAggregator()
    # non-OB aggregator: same weights, OB zeroed (re-normalized inside)
    no_ob_weights = dict(full_agg.base_weights)
    no_ob_weights[OB] = 0.0
    noob_agg = SignalAggregator(no_ob_weights)

    c = sqlite3.connect(args.db)
    rows = c.execute(
        "SELECT coin, direction, status, net_pnl_pct, ob_direction, "
        "tickets_json FROM shadow_trades WHERE tickets_json!=''").fetchall()

    buckets = {"OB_confirms": [], "OB_contradicts": [], "OB_decides": [],
               "base_and_ob_both_flat": []}
    repro_ok = repro_total = 0

    for coin, direction, status, net, ob_dir, tj in rows:
        tickets = tickets_from_json(json.loads(tj))
        full = full_agg.aggregate(coin, tickets)
        noob = noob_agg.aggregate(coin, tickets)
        # sanity: does re-aggregation reproduce the logged entry direction?
        repro_total += 1
        repro_ok += (full.direction == direction)

        base = noob.direction
        rec = {"coin": coin, "status": status, "net": net,
               "entry": direction, "ob": ob_dir, "base": base}
        if base == "FLAT":
            buckets["OB_decides" if ob_dir in ("LONG", "SHORT")
                    else "base_and_ob_both_flat"].append(rec)
        elif ob_dir == base:
            buckets["OB_confirms"].append(rec)
        else:
            buckets["OB_contradicts"].append(rec)

    print(f"\nreconstruction sanity: full-aggregator reproduced the logged "
          f"entry direction on {repro_ok}/{repro_total} trades")
    print(f"(non-OB base re-runs the real aggregator with {OB} weight -> 0)\n")

    print(f"  {'bucket':24}{'n':>4}{'closed':>8}{'win%':>7}{'net_pnl%':>11}"
          f"   tautological?")
    taut = {"OB_confirms": "yes (OB agreed w/ rest)",
            "OB_contradicts": "NO — OB overrode rest",
            "OB_decides": "NO — OB carried it",
            "base_and_ob_both_flat": "n/a"}
    for name, recs in buckets.items():
        closed = [r for r in recs if r["status"] == "CLOSED"]
        wins = sum(1 for r in closed if (r["net"] or 0) > 0)
        net = sum((r["net"] or 0) for r in closed)
        wr = (wins / len(closed)) if closed else 0
        print(f"  {name:24}{len(recs):>4}{len(closed):>8}"
              f"{wr:>7.2f}{100*net:>11.3f}   {taut[name]}")

    nontaut = (len(buckets["OB_contradicts"]) + len(buckets["OB_decides"]))
    print(f"\nNON-TAUTOLOGICAL trades (OB_contradicts + OB_decides): {nontaut}"
          f" of {repro_total}")
    if nontaut == 0:
        print("  -> STILL DEGENERATE: even removing OB from the base, OB never "
              "diverges. Redesign insufficient — rethink before waiting.")
    else:
        print("  -> redesign WORKS: the disagreed/decided buckets are "
              "populated. The 3-week wait will buy a real comparison.\n"
              "  (n still far below the >=30/bucket verdict gate — this run is "
              "methodology validation only.)")

    # show the actual non-tautological cases for inspection at small n
    show = buckets["OB_contradicts"] + buckets["OB_decides"]
    if show:
        print("\n  non-tautological cases so far:")
        for r in show:
            net_s = "open" if r["net"] is None else f"{r['net'] * 100:+.2f}%"
            print(f"    {r['coin']:5} entry={r['entry']:5} base(non-OB)="
                  f"{r['base']:5} ob={r['ob']:5} status={r['status']:6} "
                  f"net={net_s}")


if __name__ == "__main__":
    main()
