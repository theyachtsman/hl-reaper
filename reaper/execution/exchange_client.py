"""Thin wrapper around the SDK Exchange object.

Signs with the API/agent wallet (HL_REAPER_SECRET) on behalf of the main
account_address. Handles size/price rounding to asset precision.
"""
import math

import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from reaper.logger import get_logger

log = get_logger("exchange")


class ExchangeClient:
    def __init__(self, cfg):
        cfg.require_secret()
        wallet = eth_account.Account.from_key(cfg.secret_key)
        log.info("API wallet: %s (acting for account %s)",
                 wallet.address, cfg.account_address)
        self.exchange = Exchange(
            wallet, cfg.api_url, account_address=cfg.account_address)
        self.info = Info(cfg.api_url, skip_ws=True)
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

    def cancel(self, coin: str, oid: int) -> dict:
        log.info("CANCEL %s oid=%s", coin, oid)
        return self.exchange.cancel(coin, oid)

    def cancel_all(self) -> int:
        n = 0
        for o in self.open_orders():
            self.cancel(o["coin"], o["oid"])
            n += 1
        return n
