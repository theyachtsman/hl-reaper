"""SQLite store for liquidation events (Phase 8.6 research track).

Deliberately a SEPARATE database file (data/liquidations.db) rather than a
new table inside data/hl_reaper.db: the live bot holds a WAL writer on the
main DB, and this research track must not introduce lock contention or any
schema change there. Everything here is additive and standalone.
"""
import sqlite3
from pathlib import Path

from reaper.config import PROJECT_ROOT

DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "liquidations.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS liquidation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin TEXT NOT NULL,
    ts INTEGER NOT NULL,
    side TEXT,              -- LONG (long got liquidated) | SHORT
    size_usd REAL,
    price REAL,
    source TEXT             -- where this record came from
);
CREATE INDEX IF NOT EXISTS idx_liq_coin_ts ON liquidation_events(coin, ts);
-- dedupe guard for WS replays / re-runs of backfills
CREATE UNIQUE INDEX IF NOT EXISTS idx_liq_dedupe
    ON liquidation_events(coin, ts, side, size_usd, price, source);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the liquidation events DB."""
    p = Path(path) if path else DEFAULT_DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def insert_events(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Insert events; duplicates (same coin/ts/side/size/price/source) are
    ignored. Returns number of new rows."""
    if not rows:
        return 0
    before = conn.total_changes
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO liquidation_events "
            "(coin, ts, side, size_usd, price, source) "
            "VALUES (:coin, :ts, :side, :size_usd, :price, :source)",
            rows,
        )
    return conn.total_changes - before


def events_window(conn: sqlite3.Connection, coin: str, since_ms: int,
                  until_ms: int | None = None) -> list[tuple]:
    """Events for a coin in [since_ms, until_ms), oldest first.
    Returns (ts, side, size_usd, price, source) tuples."""
    q = ("SELECT ts, side, size_usd, price, source FROM liquidation_events "
         "WHERE coin=? AND ts>=?")
    args: list = [coin, since_ms]
    if until_ms is not None:
        q += " AND ts<?"
        args.append(until_ms)
    return conn.execute(q + " ORDER BY ts", args).fetchall()


def latest_ts(conn: sqlite3.Connection, coin: str,
              source: str | None = None) -> int | None:
    q = "SELECT MAX(ts) FROM liquidation_events WHERE coin=?"
    args: list = [coin]
    if source:
        q += " AND source=?"
        args.append(source)
    row = conn.execute(q, args).fetchone()
    return row[0] if row and row[0] is not None else None
