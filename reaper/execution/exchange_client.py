"""Thin wrapper around the SDK Exchange object.

Signs with the API/agent wallet (HL_REAPER_SECRET) on behalf of the main
account_address. Handles size/price rounding to asset precision.
"""
import math
import time

import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from reaper.logger import get_logger

log = get_logger("exchange")

# The hyperliquid SDK defaults its requests timeout to None (block forever).
# A single stalled socket then wedges the whole process — this is what froze
# the trading loop AND the watchdog's flatten() on 2026-06-25, leaving an
# underwater position open with no stop-loss enforcement for ~4.4h. Bound
# every REST call so a hung connection raises instead of hanging.
REST_TIMEOUT_S = 20.0


class ExchangeClient:
    def __init__(self, cfg):
        cfg.require_secret()
        wallet = eth_account.Account.from_key(cfg.secret_key)
        log.info("API wallet: %s (acting for account %s)",
                 wallet.address, cfg.account_address)
        self.exchange = Exchange(
            wallet, cfg.api_url, account_address=cfg.account_address)
        self.info = Info(cfg.api_url, skip_ws=True)
        # SDK Exchange/Info both subclass API, whose post() passes self.timeout
        # straight to requests. The constructors don't expose it, so set it
        # here — bounds order placement, cancels, market_close, user_state, etc.
        self.exchange.timeout = REST_TIMEOUT_S
        self.info.timeout = REST_TIMEOUT_S
        self.account_address = cfg.account_address
        # asset metadata: szDecimals per coin
        meta = self.info.meta()
        self.sz_decimals = {
            u["name"]: u["szDecimals"] for u in meta["universe"]
        }

    # ---------- helpers ----------
    def round_sz(self, coin: str, sz: float) -> float:
        d = self.sz_decimals.get(coin, 3)
        return math.floor(sz * 10 ** d) / 10 ** d

    @staticmethod
    def round_px(px: float) -> float:
        # HL prices: max 5 significant figures
        if px <= 0:
            return px
        mag = math.floor(math.log10(px))
        decimals = max(0, 4 - mag)
        return round(px, decimals)

    def mid(self, coin: str) -> float:
        return float(self.info.all_mids()[coin])

    def open_orders(self) -> list:
        return self.info.open_orders(self.account_address)

    def positions(self) -> list:
        st = self.info.user_state(self.account_address)
        return [p for p in st.get("assetPositions", [])
                if float(p["position"]["szi"]) != 0]

    # ---------- orders ----------
    def limit_order(self, coin: str, is_buy: bool, usd_size: float,
                    px: float) -> dict:
        px = self.round_px(px)
        sz = self.round_sz(coin, usd_size / px)
        log.info("LIMIT %s %s sz=%s px=%s (~$%.2f)",
                 "BUY" if is_buy else "SELL", coin, sz, px, sz * px)
        return self.exchange.order(
            coin, is_buy, sz, px, {"limit": {"tif": "Gtc"}})

    def market_open(self, coin: str, is_buy: bool, usd_size: float,
                    slippage: float = 0.01) -> dict:
        px = self.mid(coin)
        sz = self.round_sz(coin, usd_size / px)
        log.info("MARKET %s %s sz=%s (~$%.2f, slippage %.1f%%)",
                 "BUY" if is_buy else "SELL", coin, sz, sz * px,
                 slippage * 100)
        return self.exchange.market_open(coin, is_buy, sz, None, slippage)

    def market_close(self, coin: str) -> dict:
        log.info("MARKET CLOSE %s", coin)
        return self.exchange.market_close(coin)

    def best_px(self, coin: str, is_buy: bool) -> float:
        """Best bid (buy) / best ask (sell) from a fresh L2 snapshot."""
        book = self.info.l2_snapshot(coin)
        side = book["levels"][0 if is_buy else 1]
        return float(side[0]["px"])

    def _order_status(self, oid: int) -> dict | None:
        try:
            r = self.info.query_order_by_oid(self.account_address, oid)
            return r.get("order") if isinstance(r, dict) else None
        except Exception as e:
            log.debug("query_order_by_oid(%s) failed: %s", oid, e)
            return None

    def try_limit_entry(self, coin: str, is_buy: bool, usd_size: float,
                        timeout_s: float = 30.0,
                        px: float | None = None) -> dict:
        """Maker (post-only) entry: rest at best bid/ask, poll until filled
        or timeout, cancel the remainder. Maker fees are a fraction of
        taker — with near-zero gross edge the fee side decides the sign.

        Returns {"status": "filled"|"partial"|"timeout"|"rejected"|"error",
                 "avg_px": float|None, "filled_sz": float}."""
        if px is None:
            px = self.best_px(coin, is_buy)
        px = self.round_px(px)
        sz = self.round_sz(coin, usd_size / px)
        log.info("MAKER %s %s sz=%s px=%s (~$%.2f, timeout %.0fs)",
                 "BUY" if is_buy else "SELL", coin, sz, px, sz * px,
                 timeout_s)
        res = self.exchange.order(
            coin, is_buy, sz, px, {"limit": {"tif": "Alo"}})
        try:
            st = res["response"]["data"]["statuses"][0]
        except Exception:
            return {"status": "error", "avg_px": None, "filled_sz": 0.0,
                    "raw": res}
        if "error" in st:
            # post-only that would cross is rejected — caller retries later
            return {"status": "rejected", "avg_px": None, "filled_sz": 0.0,
                    "reason": st["error"]}
        if "filled" in st:
            f = st["filled"]
            return {"status": "filled", "avg_px": float(f["avgPx"]),
                    "filled_sz": float(f["totalSz"])}
        oid = (st.get("resting") or {}).get("oid")
        if oid is None:
            return {"status": "error", "avg_px": None, "filled_sz": 0.0,
                    "raw": res}

        deadline = time.time() + timeout_s
        filled_sz = 0.0
        while time.time() < deadline:
            time.sleep(2)
            o = self._order_status(oid)
            if not o:
                continue
            status = o.get("status", "")
            inner = o.get("order") or {}
            orig = float(inner.get("origSz") or sz)
            remaining = float(inner.get("sz") or 0)
            filled_sz = max(filled_sz, orig - remaining)
            if status == "filled" or (status == "open" and remaining == 0):
                return {"status": "filled", "avg_px": px, "filled_sz": orig}
            if status in ("canceled", "rejected", "marginCanceled"):
                break
        try:
            self.cancel(coin, oid)
        except Exception as e:
            log.warning("cancel after timeout failed for %s oid=%s: %s",
                        coin, oid, e)
        if filled_sz > 0:
            return {"status": "partial", "avg_px": px, "filled_sz": filled_sz}
        return {"status": "timeout", "avg_px": None, "filled_sz": 0.0}

    def cancel(self, coin: str, oid: int) -> dict:
        log.info("CANCEL %s oid=%s", coin, oid)
        return self.exchange.cancel(coin, oid)

    def cancel_all(self) -> int:
        n = 0
        for o in self.open_orders():
            self.cancel(o["coin"], o["oid"])
            n += 1
        return n
