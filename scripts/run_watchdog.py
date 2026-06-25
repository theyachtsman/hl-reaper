#!/usr/bin/env python3
"""Dead man's switch (Phase 6). Separate process + systemd unit.

Watches the bot heartbeat file; if it goes stale, cancels all orders and
closes all positions through its own ExchangeClient, then waits for the
heartbeat to recover before re-arming. The bot can die mid-position —
this process makes sure dead bot != open exposure."""
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Belt-and-suspenders: even with the ExchangeClient REST timeout, force a hard
# floor on every blocking socket op in this process so flatten() can NEVER hang
# forever. This process makes no websocket connections (skip_ws=True), so a
# global default timeout is safe here. On 2026-06-25 a timeout-less SDK socket
# wedged flatten() mid-trip and the dead-man's switch went silent for ~4.4h.
socket.setdefaulttimeout(30.0)

from reaper import alerts
from reaper.config import Config
from reaper.execution.exchange_client import ExchangeClient
from reaper.logger import get_logger

log = get_logger("watchdog")

CHECK_S = 15


def hb_age(cfg) -> float:
    try:
        return time.time() - float(Path(cfg.heartbeat_path).read_text())
    except Exception:
        return 1e9


def flatten(xc: ExchangeClient) -> int:
    n = 0
    try:
        xc.cancel_all()
    except Exception as e:
        log.error("cancel_all failed: %s", e)
    try:
        for p in xc.positions():
            coin = p["position"]["coin"]
            try:
                xc.market_close(coin)
                n += 1
            except Exception as e:
                log.error("market_close(%s) failed: %s", coin, e)
    except Exception as e:
        log.error("positions() failed: %s", e)
    return n


def main():
    cfg = Config()
    stale_after = max(120.0, 4.0 * cfg.heartbeat_interval)
    xc = ExchangeClient(cfg)
    log.info("watchdog armed: heartbeat=%s stale_after=%.0fs network=%s",
             cfg.heartbeat_path, stale_after, cfg.network)
    tripped = False
    while True:
        age = hb_age(cfg)
        if age > stale_after and not tripped:
            tripped = True
            log.error("HEARTBEAT STALE %.0fs — flattening account", age)
            n = flatten(xc)
            alerts.send(f"⚠️ WATCHDOG TRIPPED\nheartbeat stale {age:.0f}s\n"
                        f"closed {n} position(s), cancelled all orders")
        elif age <= stale_after and tripped:
            tripped = False
            log.warning("heartbeat recovered (%.0fs) — watchdog re-armed", age)
            alerts.send("✅ watchdog re-armed: bot heartbeat recovered")
        time.sleep(CHECK_S)


if __name__ == "__main__":
    main()
