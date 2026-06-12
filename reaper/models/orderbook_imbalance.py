"""Orderbook imbalance model: bid/ask depth ratio over the top N levels."""
import time

from reaper.logger import get_logger
from reaper.models import BaseModel, LONG, SHORT, Ticket

log = get_logger("model.obimb")


class OrderbookImbalanceModel(BaseModel):
    name = "OrderbookImbalanceModel"

    def __init__(self, top_levels: int = 10, min_imbalance: float = 0.30,
                 max_age_s: float = 10.0):
        self.top_levels = top_levels
        self.min_imbalance = min_imbalance
        self.max_age_s = max_age_s

    def compute(self, coin: str, buf) -> Ticket:
        try:
            book = buf.books.get(coin)
            if not book or not book.get("bids") or not book.get("asks"):
                return self.flat(reason="no_book")
            age_s = time.time() - float(book.get("ts", 0)) / 1000
            if age_s > self.max_age_s:
                return self.flat(reason="stale_book", age_s=round(age_s, 1))

            bid_vol = sum(sz for _, sz in book["bids"][:self.top_levels])
            ask_vol = sum(sz for _, sz in book["asks"][:self.top_levels])
            total = bid_vol + ask_vol
            if total <= 0:
                return self.flat(reason="empty_book")
            imbalance = (bid_vol - ask_vol) / total

            meta = {"imbalance": round(imbalance, 4),
                    "bid_vol": round(bid_vol, 4), "ask_vol": round(ask_vol, 4)}
            if imbalance > self.min_imbalance:
                return Ticket(self.name, LONG,
                              min(0.90, 0.50 + imbalance * 0.40), meta)
            if imbalance < -self.min_imbalance:
                return Ticket(self.name, SHORT,
                              min(0.90, 0.50 + abs(imbalance) * 0.40), meta)
            return self.flat(**meta)
        except Exception as e:
            log.warning("compute failed for %s: %s", coin, e)
            return self.flat(error=str(e))
