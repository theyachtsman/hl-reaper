"""SQLite storage. One connection per thread via thread-local."""
import sqlite3
import threading
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    coin TEXT NOT NULL,
    side TEXT NOT NULL,             -- LONG | SHORT
    action TEXT NOT NULL,           -- OPEN | CLOSE | TEST
    size REAL, price REAL, leverage REAL,
    order_id TEXT, status TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    coin TEXT NOT NULL,
    model TEXT NOT NULL,
    direction TEXT, confidence REAL, meta TEXT
);
CREATE TABLE IF NOT EXISTS funding_history (
    coin TEXT NOT NULL,
    ts INTEGER NOT NULL,
    funding_rate REAL NOT NULL,
    premium REAL,
    PRIMARY KEY (coin, ts)
);
CREATE TABLE IF NOT EXISTS equity_snapshots (
    ts INTEGER PRIMARY KEY,
    account_value REAL, margin_used REAL, withdrawable REAL
);
CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT, updated_ts INTEGER
);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self.path, timeout=10)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
        return self._local.conn

    def log_trade(self, coin, side, action, size=None, price=None,
                  leverage=None, order_id=None, status=None, note=None):
        with self._conn() as c:
            c.execute(
                "INSERT INTO trades (ts,coin,side,action,size,price,leverage,"
                "order_id,status,note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (int(time.time() * 1000), coin, side, action, size, price,
                 leverage, str(order_id) if order_id else None, status, note),
            )

    def insert_funding(self, coin: str, rows: list[dict]):
        with self._conn() as c:
            c.executemany(
                "INSERT OR IGNORE INTO funding_history "
                "(coin, ts, funding_rate, premium) VALUES (?,?,?,?)",
                [(coin, int(r["time"]), float(r["fundingRate"]),
                  float(r.get("premium") or 0)) for r in rows],
            )

    def funding_window(self, coin: str, since_ms: int) -> list[tuple]:
        cur = self._conn().execute(
            "SELECT ts, funding_rate FROM funding_history "
            "WHERE coin=? AND ts>=? ORDER BY ts", (coin, since_ms))
        return cur.fetchall()

    def snapshot_equity(self, account_value, margin_used, withdrawable):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO equity_snapshots VALUES (?,?,?,?)",
                (int(time.time() * 1000), account_value, margin_used,
                 withdrawable),
            )

    def set_state(self, key: str, value: str):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO bot_state VALUES (?,?,?)",
                (key, value, int(time.time() * 1000)),
            )

    def get_state(self, key: str) -> str | None:
        row = self._conn().execute(
            "SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
