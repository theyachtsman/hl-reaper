"""Liquidation event collector (Phase 8.6 research track — standalone).

Two acquisition paths into the liquidation_events store
(see docs/liquidation_data_sources.md for the full source survey):

1. Real-time WS watcher (free, official API): subscribes to the public
   `trades` channel and records every fill where a known liquidator-vault
   address is a counterparty. Per HL's two-tier liquidation flow these are
   BACKSTOP liquidations — the severe tail where the book couldn't absorb
   the market-order liquidation, i.e. exactly the cascade events this
   research targets. Routine market-order liquidations are indistinguishable
   from normal trades in public data and are NOT captured.
   source='hl_ws_backstop'

2. Optional Coinalyze backfill (free API key required, coverage unverified):
   aggregated long/short liquidation volume per interval. Symbols are
   discovered at runtime from /future-markets. source='coinalyze'

Runs as its own process (scripts/run_liquidation_poller.py) — never imported
by run_bot.py and writes only to data/liquidations.db.
"""
import os
import threading
import time

import requests
from hyperliquid.info import Info
from hyperliquid.utils import constants

from reaper.data import liquidation_store as store
from reaper.logger import get_logger

log = get_logger("liq_poller")

# HLP Liquidator vault — backstop liquidation counterparty. If HL rotates or
# adds vaults, extend this set (lowercase).
LIQUIDATOR_VAULTS = {
    "0x2e3d94f0562703b25c83308a05046ddaf9a8dd14",
}

COINALYZE_BASE = "https://api.coinalyze.net/v1"


class LiquidationPoller:
    """WS watcher: public trades -> backstop liquidation events."""

    def __init__(self, coins: list[str], db_path=None,
                 api_url: str = constants.MAINNET_API_URL,
                 stale_seconds: int = 120):
        self.coins = coins
        self.api_url = api_url
        self.stale_seconds = stale_seconds
        self.conn = store.connect(db_path)
        self._info: Info | None = None
        self._stop = threading.Event()
        self._last_msg = time.time()
        self._reconnects = 0
        self.events_recorded = 0
        self.trades_seen = 0

    # ---------- lifecycle ----------
    def start(self):
        self._connect()
        threading.Thread(target=self._monitor, daemon=True,
                         name="liq-monitor").start()
        log.info("watching %d coins for backstop liquidations -> %s",
                 len(self.coins), store.DEFAULT_DB_PATH)

    def stop(self):
        self._stop.set()
        self._teardown()

    # ---------- internals ----------
    def _connect(self):
        log.info("connecting websocket -> %s", self.api_url)
        self._info = Info(self.api_url)
        for coin in self.coins:
            self._info.subscribe(
                {"type": "trades", "coin": coin},
                lambda msg, c=coin: self._on_trades(c, msg),
            )

    def _teardown(self):
        if self._info is not None:
            try:
                self._info.disconnect_websocket()
            except Exception:
                pass
            self._info = None

    def _monitor(self):
        while not self._stop.is_set():
            time.sleep(5)
            stale = time.time() - self._last_msg
            if stale > self.stale_seconds:
                self._reconnects += 1
                backoff = min(60, 2 ** min(self._reconnects, 6))
                log.warning("feed stale %.0fs — reconnect #%d (backoff %ds)",
                            stale, self._reconnects, backoff)
                self._teardown()
                time.sleep(backoff)
                try:
                    self._connect()
                except Exception as e:
                    log.error("reconnect failed: %s", e)
            else:
                self._reconnects = 0

    def _on_trades(self, coin: str, msg: dict):
        try:
            trades = msg.get("data") or []
            self._last_msg = time.time()
            rows = []
            for t in trades:
                self.trades_seen += 1
                users = [u.lower() for u in t.get("users") or []]
                if len(users) != 2:
                    continue
                buyer_is_vault = users[0] in LIQUIDATOR_VAULTS
                seller_is_vault = users[1] in LIQUIDATOR_VAULTS
                if not (buyer_is_vault or seller_is_vault):
                    continue
                px, sz = float(t["px"]), float(t["sz"])
                # vault buying = it absorbs a closing long -> LONG liquidated
                side = "LONG" if buyer_is_vault else "SHORT"
                rows.append({
                    "coin": coin, "ts": int(t["time"]), "side": side,
                    "size_usd": px * sz, "price": px,
                    "source": "hl_ws_backstop",
                })
            if rows:
                n = store.insert_events(self.conn, rows)
                self.events_recorded += n
                for r in rows:
                    log.info("BACKSTOP LIQ %s %s $%.0f @ %s",
                             r["coin"], r["side"], r["size_usd"], r["price"])
        except Exception as e:
            log.error("trades cb error %s: %s", coin, e)

    def status_line(self) -> str:
        return (f"trades_seen={self.trades_seen} "
                f"liq_events={self.events_recorded} "
                f"reconnects={self._reconnects}")


# ---------------------------------------------------------------------------
# Optional Coinalyze backfill (aggregated, needs free API key)
# ---------------------------------------------------------------------------
def _coinalyze_get(path: str, api_key: str, **params):
    r = requests.get(f"{COINALYZE_BASE}/{path}", params=params,
                     headers={"api_key": api_key}, timeout=60)
    r.raise_for_status()
    return r.json()


def discover_hl_symbols(api_key: str, coins: list[str]) -> dict[str, str]:
    """Map our coin names to Coinalyze Hyperliquid perp symbols via the
    future-markets catalog. Returns only the coins actually found."""
    markets = _coinalyze_get("future-markets", api_key)
    out: dict[str, str] = {}
    for m in markets:
        if "hyperliquid" not in str(m.get("exchange", "")).lower() \
                and "hyperliquid" not in str(m.get("exchange_name", "")).lower():
            continue
        base = str(m.get("base_asset", "")).upper()
        if base in coins and m.get("is_perpetual", True):
            out.setdefault(base, m["symbol"])
    return out


def backfill_coinalyze(conn, coins: list[str], since_ms: int,
                       api_key: str | None = None,
                       interval: str = "5min") -> int:
    """Pull aggregated liquidation history per coin since `since_ms`.
    Each interval becomes up to two events (LONG and SHORT volume) at the
    interval timestamp. Returns rows inserted. No-op with a warning if no
    API key or no Hyperliquid symbols are found."""
    api_key = api_key or os.environ.get("COINALYZE_API_KEY", "")
    if not api_key:
        log.warning("coinalyze backfill skipped: COINALYZE_API_KEY not set")
        return 0
    try:
        symbols = discover_hl_symbols(api_key, coins)
    except Exception as e:
        log.error("coinalyze symbol discovery failed: %s", e)
        return 0
    if not symbols:
        log.warning("coinalyze: no Hyperliquid symbols found for %s — "
                    "their HL liquidation coverage may be limited", coins)
        return 0
    log.info("coinalyze symbols: %s", symbols)

    total = 0
    for coin, sym in symbols.items():
        frm = since_ms // 1000
        to = int(time.time())
        try:
            data = _coinalyze_get("liquidation-history", api_key,
                                  symbols=sym, interval=interval,
                                  **{"from": frm, "to": to},
                                  convert_to_usd="true")
        except Exception as e:
            log.error("coinalyze liquidation-history %s failed: %s", coin, e)
            continue
        rows = []
        for series in data:
            for p in series.get("history", []):
                ts = int(p["t"]) * 1000
                # 'l' = longs liquidated volume, 's' = shorts liquidated
                if float(p.get("l") or 0) > 0:
                    rows.append({"coin": coin, "ts": ts, "side": "LONG",
                                 "size_usd": float(p["l"]), "price": None,
                                 "source": "coinalyze"})
                if float(p.get("s") or 0) > 0:
                    rows.append({"coin": coin, "ts": ts, "side": "SHORT",
                                 "size_usd": float(p["s"]), "price": None,
                                 "source": "coinalyze"})
        n = store.insert_events(conn, rows)
        log.info("coinalyze %s: %d aggregated rows inserted", coin, n)
        total += n
        time.sleep(1.6)  # free tier ~40 req/min
    return total
