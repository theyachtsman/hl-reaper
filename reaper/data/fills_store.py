"""Durable fill archive + round-trip trade reconstruction (History page).

HL's `user_fills` only returns the most recent ~2000 fills, so "all-time"
history would silently truncate over time. This module persists every fill
locally (data/fills.db, keyed by tid so re-syncs are idempotent) and
reconstructs completed round-trip trades from them.

CRITICAL: realized PnL is per ROUND-TRIP trade (position opened -> back to
flat), taking closedPnl/fee from the fills — NOT per-fill. Counting each
partial fill as a trade is the exact artifact that produced the false ETH
"edge" (see docs/live_attribution_report.md). This is the single source of
truth for the History page; the `trades` table (per-action rows) is not used
for PnL.

Mirrors scripts/analyze_live_signals.py reconstruct_trades() logic.
"""
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

from reaper.config import PROJECT_ROOT
from reaper.logger import get_logger

log = get_logger("fills_store")

DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "fills.db"
LONG, SHORT = "LONG", "SHORT"


def connect(db_path=None) -> sqlite3.Connection:
    path = str(db_path or DEFAULT_DB_PATH)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fills (
            tid            INTEGER PRIMARY KEY,
            ts             INTEGER NOT NULL,
            coin           TEXT,
            side           TEXT,        -- B | A
            px             REAL,
            sz             REAL,
            dir            TEXT,        -- Open/Close Long/Short
            closed_pnl     REAL,
            fee            REAL,
            hash           TEXT,
            oid            INTEGER,
            start_position REAL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fills_coin_ts "
                 "ON fills(coin, ts)")
    conn.commit()
    return conn


def _row(f: dict) -> tuple:
    return (
        int(f.get("tid")), int(f.get("time")), f.get("coin"), f.get("side"),
        float(f.get("px") or 0), float(f.get("sz") or 0), f.get("dir"),
        float(f.get("closedPnl") or 0), float(f.get("fee") or 0),
        f.get("hash"), int(f.get("oid") or 0),
        float(f.get("startPosition") or 0),
    )


def insert_fills(conn: sqlite3.Connection, fills: list[dict]) -> int:
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO fills "
        "(tid, ts, coin, side, px, sz, dir, closed_pnl, fee, hash, oid, "
        " start_position) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [_row(f) for f in fills if f.get("tid") is not None],
    )
    conn.commit()
    return conn.total_changes - before


def sync(conn: sqlite3.Connection, info, address: str,
         full: bool = False) -> int:
    """Pull fills from the exchange into the local archive (idempotent).

    Incremental by default: fetches from just after the newest stored fill.
    `full=True` re-walks from epoch (dedupe by tid makes this safe). Pages via
    user_fills_by_time (~2000/page) advancing startTime past the last batch.
    """
    row = conn.execute("SELECT MAX(ts) AS m FROM fills").fetchone()
    start = 0 if full or not row or row["m"] is None else int(row["m"])
    now = int(time.time() * 1000)
    inserted = 0
    guard = 0
    while start <= now and guard < 500:
        guard += 1
        try:
            batch = info.user_fills_by_time(address, start, now) or []
        except Exception as e:
            log.warning("user_fills_by_time failed at start=%d: %s", start, e)
            break
        if not batch:
            break
        inserted += insert_fills(conn, batch)
        max_ts = max(int(f["time"]) for f in batch)
        if max_ts < start + 1 or len(batch) < 2:
            break               # no forward progress / drained
        start = max_ts + 1
        if len(batch) < 2000:
            break               # last (partial) page
    if inserted:
        log.info("fills sync: +%d new (archive now %d)", inserted,
                 conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0])
    return inserted


def all_fills(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM fills ORDER BY ts, tid")]


def reconstruct_trades(conn: sqlite3.Connection) -> list[dict]:
    """Group archived fills into completed round-trip trades, per coin.

    A trade = position opened from flat until it returns to flat. realized_pnl
    is net of fees. Unclosed (currently open) positions are omitted (no
    realized PnL yet).
    """
    by_coin: dict[str, list[dict]] = defaultdict(list)
    for f in all_fills(conn):
        by_coin[f["coin"]].append(f)

    trades: list[dict] = []
    for coin, fl in by_coin.items():
        fl.sort(key=lambda x: (x["ts"], x["tid"]))
        pos = 0.0
        cur: dict | None = None
        for f in fl:
            sz = float(f["sz"])
            signed = sz if f["side"] == "B" else -sz
            is_open = (f["dir"] or "").startswith("Open")
            if cur is None and is_open:
                cur = {
                    "coin": coin,
                    "direction": LONG if "Long" in (f["dir"] or "") else SHORT,
                    "entry_ts": f["ts"], "exit_ts": f["ts"],
                    "entry_px": float(f["px"]),
                    "gross_pnl": 0.0, "fees": 0.0, "n_fills": 0, "qty": 0.0,
                }
            if cur is not None:
                cur["gross_pnl"] += float(f["closed_pnl"] or 0)
                cur["fees"] += float(f["fee"] or 0)
                cur["n_fills"] += 1
                cur["exit_ts"] = f["ts"]
                cur["exit_px"] = float(f["px"])
                if is_open:
                    cur["qty"] += sz
            pos += signed
            if cur is not None and abs(pos) < 1e-9:
                cur["realized_pnl"] = cur["gross_pnl"] - cur["fees"]
                cur["hold_minutes"] = round(
                    (cur["exit_ts"] - cur["entry_ts"]) / 60000, 1)
                trades.append(cur)
                cur = None
    trades.sort(key=lambda t: t["entry_ts"])
    return trades
