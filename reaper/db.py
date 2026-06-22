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
    order_id TEXT, status TEXT, note TEXT,
    band TEXT                       -- scalp | trend | NULL (legacy/manual)
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
-- live_config: hot-reload overrides on top of config.yaml. Each row is one
-- dotted key (e.g. "risk.min_confidence") whose JSON value overrides the
-- equivalent config.yaml default. Deleting a row restores the default.
CREATE TABLE IF NOT EXISTS live_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,          -- JSON-encoded value
    updated_ts INTEGER NOT NULL,
    updated_by TEXT DEFAULT 'dashboard'
);
-- bot_commands: one-shot control queue. run_bot.py drains pending rows each
-- loop and marks them done (pause/resume/close_all/close_coin/set_state).
CREATE TABLE IF NOT EXISTS bot_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command TEXT NOT NULL,
    issued_ts INTEGER NOT NULL,
    executed_ts INTEGER,
    status TEXT DEFAULT 'pending'
);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        with self._conn() as c:
            c.executescript(_SCHEMA)
            self._migrate(c)

    @staticmethod
    def _migrate(c: sqlite3.Connection):
        """Idempotent additive migrations for DBs created before a column
        existed. ALTER TABLE ADD COLUMN is a no-op-safe schema bump."""
        cols = {r[1] for r in c.execute("PRAGMA table_info(trades)").fetchall()}
        if "band" not in cols:
            c.execute("ALTER TABLE trades ADD COLUMN band TEXT")

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self.path, timeout=10)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
        return self._local.conn

    def log_trade(self, coin, side, action, size=None, price=None,
                  leverage=None, order_id=None, status=None, note=None,
                  band=None):
        now_ms = int(time.time() * 1000)
        with self._conn() as c:
            # Dedup ghost close re-evaluations: a single on-chain close can be
            # logged repeatedly if the position state lags. An identical CLOSE
            # (same coin + reason note) within the last 60s is a duplicate of
            # one real close, not a new one — drop it so the trades table stays
            # honest. (Belt-and-suspenders alongside the close-pending guard.)
            if action == "CLOSE":
                dup = c.execute(
                    "SELECT 1 FROM trades WHERE coin=? AND action='CLOSE' "
                    "AND IFNULL(note,'')=IFNULL(?,'') AND ts > ? LIMIT 1",
                    (coin, note, now_ms - 60_000)).fetchone()
                if dup:
                    return
            c.execute(
                "INSERT INTO trades (ts,coin,side,action,size,price,leverage,"
                "order_id,status,note,band) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (now_ms, coin, side, action, size, price,
                 leverage, str(order_id) if order_id else None, status, note,
                 band),
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

    # ------------------------------------------------------------------
    # live_config — hot-reload overrides (dotted key -> JSON value)
    # ------------------------------------------------------------------
    def get_live_config(self) -> dict:
        """All active overrides as {dotted_key: decoded_value}."""
        import json
        out: dict = {}
        for k, v in self._conn().execute(
                "SELECT key, value FROM live_config").fetchall():
            try:
                out[k] = json.loads(v)
            except Exception:
                out[k] = v
        return out

    def set_live_config(self, key: str, value, updated_by: str = "dashboard"):
        import json
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO live_config "
                "(key, value, updated_ts, updated_by) VALUES (?,?,?,?)",
                (key, json.dumps(value), int(time.time() * 1000), updated_by))

    def delete_live_config(self, key: str):
        with self._conn() as c:
            c.execute("DELETE FROM live_config WHERE key=?", (key,))

    def clear_live_config(self):
        with self._conn() as c:
            c.execute("DELETE FROM live_config")

    # ------------------------------------------------------------------
    # bot_commands — one-shot control queue
    # ------------------------------------------------------------------
    def enqueue_command(self, command: str) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO bot_commands (command, issued_ts, status) "
                "VALUES (?,?, 'pending')", (command, int(time.time() * 1000)))
            return cur.lastrowid

    def get_pending_commands(self) -> list[tuple]:
        """Pending commands oldest-first as (id, command) tuples."""
        return self._conn().execute(
            "SELECT id, command FROM bot_commands WHERE status='pending' "
            "ORDER BY id").fetchall()

    def mark_command_done(self, cmd_id: int, status: str = "done"):
        with self._conn() as c:
            c.execute(
                "UPDATE bot_commands SET status=?, executed_ts=? WHERE id=?",
                (status, int(time.time() * 1000), cmd_id))
