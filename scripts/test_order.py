#!/usr/bin/env python3
"""Step 2 — testnet order test (exit criteria for Phase 1.7).

Default run (safe):
  1. Rests a limit BUY far below mid (will never fill)
  2. Verifies it shows in open orders
  3. Cancels it and verifies cancellation

With --roundtrip:
  4. Market-opens a tiny position, holds 5s, market-closes it

Refuses to run against mainnet.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.config import Config
from reaper.db import DB
from reaper.execution.exchange_client import ExchangeClient


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--roundtrip", action="store_true",
                    help="also do a tiny market open+close")
    args = ap.parse_args()

    cfg = Config()
    if cfg.network != "testnet":
        sys.exit("Refusing: this script only runs on testnet.")

    db = DB(cfg.db_path)
    xc = ExchangeClient(cfg)

    t = cfg.test_order
    coin = t.get("coin", "BTC")
    usd = float(t.get("usd_size", 12))
    offset = float(t.get("limit_offset_pct", 20)) / 100

    mid = xc.mid(coin)
    print(f"\n{coin} mid = {mid}")

    # ---- 1. far limit order ----
    px = mid * (1 - offset)
    res = xc.limit_order(coin, True, usd, px)
    print("order response:", res)
    status = res["response"]["data"]["statuses"][0]
    oid = status.get("resting", {}).get("oid")
    if not oid:
        sys.exit(f"limit order did not rest: {status}")
    db.log_trade(coin, "LONG", "TEST", usd / px, px,
                 order_id=oid, status="resting", note="phase1 limit test")
    print(f"[OK] limit order resting, oid={oid}")

    # ---- 2. verify in open orders ----
    time.sleep(2)
    oids = [o["oid"] for o in xc.open_orders()]
    if oid not in oids:
        sys.exit("[FAIL] order not found in open orders")
    print("[OK] order visible in open orders")

    # ---- 3. cancel ----
    cres = xc.cancel(coin, oid)
    print("cancel response:", cres)
    time.sleep(2)
    if oid in [o["oid"] for o in xc.open_orders()]:
        sys.exit("[FAIL] order still open after cancel")
    db.log_trade(coin, "LONG", "TEST", order_id=oid,
                 status="cancelled", note="phase1 cancel test")
    print("[OK] order cancelled\n")

    # ---- 4. optional market round trip ----
    if args.roundtrip:
        print("market round-trip: opening tiny long...")
        r = xc.market_open(coin, True, usd)
        print("open response:", r)
        db.log_trade(coin, "LONG", "OPEN", note="phase1 roundtrip")
        time.sleep(5)
        pos = xc.positions()
        print(f"positions after open: "
              f"{[(p['position']['coin'], p['position']['szi']) for p in pos]}")
        r = xc.market_close(coin)
        print("close response:", r)
        db.log_trade(coin, "LONG", "CLOSE", note="phase1 roundtrip")
        time.sleep(3)
        if any(p["position"]["coin"] == coin for p in xc.positions()):
            sys.exit("[FAIL] position still open after close")
        print("[OK] market round-trip complete, position closed")

    print("\nPHASE 1 ORDER TEST PASSED")


if __name__ == "__main__":
    main()
