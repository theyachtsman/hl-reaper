"""REST pollers: asset contexts (funding / OI / mark px), funding rate
history, and account equity snapshots. Each runs in its own daemon thread
with retry + backoff."""
import threading
import time

from hyperliquid.info import Info

from reaper.data.buffer import MarketBuffer
from reaper.db import DB
from reaper.logger import get_logger

log = get_logger("pollers")


def _retry(fn, what: str, tries: int = 3):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            wait = 2 ** i
            log.warning("%s failed (%s) — retry in %ds", what, e, wait)
            time.sleep(wait)
    log.error("%s failed after %d tries", what, tries)
    return None


class RestPollers:
    def __init__(self, api_url: str, cfg, buf: MarketBuffer, db: DB):
        self.info = Info(api_url, skip_ws=True)
        self.cfg = cfg
        self.buf = buf
        self.db = db
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._ctx_loop, daemon=True,
                         name="poll-ctx").start()
        threading.Thread(target=self._funding_loop, daemon=True,
                         name="poll-funding").start()
        threading.Thread(target=self._equity_loop, daemon=True,
                         name="poll-equity").start()

    def stop(self):
        self._stop.set()

    # ---- asset contexts: funding, open interest, mark px ----
    def _ctx_loop(self):
        while not self._stop.is_set():
            res = _retry(lambda: self.info.meta_and_asset_ctxs(),
                         "meta_and_asset_ctxs")
            if res:
                meta, ctxs = res[0], res[1]
                names = [u["name"] for u in meta["universe"]]
                for coin in self.buf.coins:
                    if coin in names:
                        ctx = ctxs[names.index(coin)]
                        self.buf.on_ctx(coin, {
                            "funding": float(ctx.get("funding", 0)),
                            "open_interest": float(ctx.get("openInterest", 0)),
                            "mark_px": float(ctx.get("markPx", 0)),
                            "oracle_px": float(ctx.get("oraclePx", 0)),
                            "ts": int(time.time() * 1000),
                        })
                log.info("ctx updated: %s", {
                    c: round(self.buf.ctx[c].get("funding", 0), 8)
                    for c in self.buf.coins})
            self._stop.wait(self.cfg.asset_ctx_seconds)

    # ---- funding history (rolling window into SQLite) ----
    def _funding_loop(self):
        while not self._stop.is_set():
            since = int(time.time() * 1000) - \
                self.cfg.funding_lookback_hours * 3600 * 1000
            for coin in self.buf.coins:
                rows = _retry(
                    lambda c=coin: self.info.funding_history(c, since),
                    f"funding_history({coin})")
                if rows:
                    self.db.insert_funding(coin, rows)
                    log.info("funding history %s: %d rows (last rate %s)",
                             coin, len(rows), rows[-1]["fundingRate"])
            self._stop.wait(self.cfg.funding_history_minutes * 60)

    # ---- account equity snapshot (every 5 min) ----
    def _equity_loop(self):
        while not self._stop.is_set():
            state = _retry(
                lambda: self.info.user_state(self.cfg.account_address),
                "user_state")
            if state:
                ms = state.get("marginSummary", {})
                self.db.snapshot_equity(
                    float(ms.get("accountValue", 0)),
                    float(ms.get("totalMarginUsed", 0)),
                    float(state.get("withdrawable", 0)),
                )
                log.info("equity: %s USDC (margin used %s)",
                         ms.get("accountValue"), ms.get("totalMarginUsed"))
            self._stop.wait(300)
