#!/usr/bin/env python3
"""Step 1 — connection smoke test. Run this FIRST.

Verifies: API reachable, account exists, balance visible, candles +
funding pull works. No orders are placed. Exit code 0 = all good.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hyperliquid.info import Info
from reaper.config import Config

OK, FAIL = "\033[92m[OK]\033[0m  ", "\033[91m[FAIL]\033[0m"


def main():
    cfg = Config()
    print(f"network={cfg.network}  api={cfg.api_url}")
    print(f"account={cfg.account_address}\n")
    failures = 0

    info = Info(cfg.api_url, skip_ws=True)

    # 1 — mids
    try:
        mids = info.all_mids()
        print(OK + f"all_mids: BTC={mids.get('BTC')} ETH={mids.get('ETH')}")
    except Exception as e:
        print(FAIL, "all_mids:", e); failures += 1

    # 2 — account state
    try:
        st = info.user_state(cfg.account_address)
        av = st["marginSummary"]["accountValue"]
        print(OK + f"user_state: accountValue={av} USDC")
        if float(av) <= 0:
            print(FAIL, "account value is 0 — did the faucet drip arrive?")
            failures += 1
    except Exception as e:
        print(FAIL, "user_state:", e); failures += 1

    # 3 — candles
    try:
        now = int(time.time() * 1000)
        candles = info.candles_snapshot("BTC", "1m", now - 3600_000, now)
        print(OK + f"candles_snapshot: {len(candles)} x 1m BTC candles")
    except Exception as e:
        print(FAIL, "candles_snapshot:", e); failures += 1

    # 4 — funding history
    try:
        rows = info.funding_history("BTC", int(time.time() * 1000) - 86400_000)
        last = rows[-1]["fundingRate"] if rows else "n/a"
        print(OK + f"funding_history: {len(rows)} rows, last rate {last}")
    except Exception as e:
        print(FAIL, "funding_history:", e); failures += 1

    # 5 — secret configured (not validated against chain here)
    try:
        cfg.require_secret()
        print(OK + "HL_REAPER_SECRET is set")
    except Exception as e:
        print(FAIL, str(e)); failures += 1

    print("\n" + ("ALL CHECKS PASSED — proceed to test_order.py"
                  if failures == 0 else f"{failures} CHECK(S) FAILED"))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
