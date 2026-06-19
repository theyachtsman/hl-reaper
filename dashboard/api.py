#!/usr/bin/env python3
"""HL Reaper dashboard bridge — FastAPI on 127.0.0.1:8801 (Phase 5).

(Gameplan said 8001, but Range Reaper's api_bridge.py already owns 8001 on
this server — 8801 avoids the collision. The user-facing port is 8888.)

Read side: SQLite (trades/signals/equity/bot_state) + its own light REST
pollers for live prices, funding, OI and account state — independent of the
bot process, so the dashboard works even when the bot is down.

Control side: writes control keys into the bot_state table, which
scripts/run_bot.py honors each loop (halt / resume / per-coin disable).
Manual position close goes straight through ExchangeClient.

Localhost only — bound to 127.0.0.1, no auth by design (gameplan rule:
no tunnel, no exposure). The Next.js frontend on :8888 proxies /api here.
"""
import copy
import json
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from hyperliquid.info import Info
from pydantic import BaseModel

from fastapi.responses import Response
from reaper.config import PROJECT_ROOT, Config
from reaper.data import fills_store
from reaper.logger import get_logger

log = get_logger("dashboard")
cfg = Config()
app = FastAPI(title="HL Reaper Dashboard Bridge")

DATA_DIR = PROJECT_ROOT / "data"
COINS = (cfg._raw.get("trading", {}) or {}).get("coins_active", cfg.coins)


# ---------------------------------------------------------------------------
# DB helpers (read-mostly; WAL allows concurrent access with the bot)
# ---------------------------------------------------------------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(cfg.db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def rows(sql: str, *params) -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def _json_safe(obj):
    """Recursively replace NaN/Inf floats with None. The bot publishes some
    model meta (e.g. TA macd_hist when candle history is short) as NaN, which
    json.dumps writes and json.loads reads back fine — but FastAPI's strict
    encoder 500s on it. Null them so passthrough endpoints stay valid JSON."""
    import math
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


def get_state(key: str) -> str | None:
    r = rows("SELECT value FROM bot_state WHERE key=?", key)
    return r[0]["value"] if r else None


def set_state(key: str, value: str):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO bot_state VALUES (?,?,?)",
                  (key, value, int(time.time() * 1000)))


# ---------------------------------------------------------------------------
# live_config — hot-reload overrides on top of config.yaml. The bridge can
# start before the bot has created these tables, so ensure them here too.
# ---------------------------------------------------------------------------
def _ensure_control_tables():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS live_config (
            key TEXT PRIMARY KEY, value TEXT NOT NULL,
            updated_ts INTEGER NOT NULL,
            updated_by TEXT DEFAULT 'dashboard')""")
        c.execute("""CREATE TABLE IF NOT EXISTS bot_commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT, command TEXT NOT NULL,
            issued_ts INTEGER NOT NULL, executed_ts INTEGER,
            status TEXT DEFAULT 'pending')""")
        # preset_log — attribution trail so CSV trade data can be tied to the
        # strategy preset that was active when each trade happened.
        c.execute("""CREATE TABLE IF NOT EXISTS preset_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, preset_name TEXT NOT NULL,
            applied_ts INTEGER NOT NULL, applied_by TEXT DEFAULT 'dashboard')""")


_ensure_control_tables()


def live_overrides() -> dict:
    """Active overrides as {dotted_key: decoded_value}, read fresh."""
    out: dict = {}
    for r in rows("SELECT key, value FROM live_config"):
        try:
            out[r["key"]] = json.loads(r["value"])
        except Exception:
            out[r["key"]] = r["value"]
    return out


def set_live_config(key: str, value, by: str = "dashboard"):
    """Upsert one live_config override (value JSON-encoded)."""
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO live_config "
            "(key, value, updated_ts, updated_by) VALUES (?,?,?,?)",
            (key, json.dumps(value), int(time.time() * 1000), by))


def get_live_config(key: str):
    """Single decoded override value, or None if unset."""
    r = rows("SELECT value FROM live_config WHERE key=?", key)
    if not r:
        return None
    try:
        return json.loads(r[0]["value"])
    except Exception:
        return r[0]["value"]


def effective_config() -> dict:
    """config.yaml base deep-merged with the current live_config overrides —
    the exact effective config the bot enforces each loop. Read fresh so the
    page never shows a stale value."""
    raw = copy.deepcopy(cfg._raw)
    for dotted, val in live_overrides().items():
        if dotted.startswith("system."):  # dashboard bookkeeping, not bot config
            continue
        parts = dotted.split(".")
        node = raw
        for p in parts[:-1]:
            nxt = node.get(p)
            if not isinstance(nxt, dict):
                nxt = {}
                node[p] = nxt
            node = nxt
        node[parts[-1]] = val
    return raw


def _flatten(d: dict, prefix: str = "") -> dict:
    """Flatten a nested config dict to {dotted_key: scalar/list}."""
    out: dict = {}
    for k, v in (d or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


# ---------------------------------------------------------------------------
# live market cache (own pollers — independent of the bot process)
# ---------------------------------------------------------------------------
class MarketCache:
    def __init__(self):
        self.info = Info(cfg.api_url, skip_ws=True)
        self.mids: dict = {}
        self.ctx: dict = {}
        self.user: dict = {}
        self.updated = 0.0
        # live microstructure pulse: real L2 snapshots every ~2.5s feed the
        # dashboard's "analysis core" (depth, spread, imbalance, mid history)
        self.pulse: dict = {}
        self.mid_hist: dict = {c: [] for c in COINS}
        self.pulse_n = 0
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._pulse_loop, daemon=True).start()

    def _pulse_loop(self):
        while True:
            t0 = time.time()
            for coin in COINS:
                try:
                    book = self.info.l2_snapshot(coin)
                    bids, asks = book["levels"][0][:10], book["levels"][1][:10]
                    if not bids or not asks:
                        continue
                    bb, ba = float(bids[0]["px"]), float(asks[0]["px"])
                    mid = (bb + ba) / 2
                    bid_szs = [float(x["sz"]) for x in bids]
                    ask_szs = [float(x["sz"]) for x in asks]
                    bv, av = sum(bid_szs), sum(ask_szs)
                    self.pulse[coin] = {
                        "mid": mid,
                        "spread_bps": (ba - bb) / mid * 10_000,
                        "imbalance": (bv - av) / (bv + av) if bv + av else 0,
                        "bid_szs": bid_szs[:6],
                        "ask_szs": ask_szs[:6],
                        "bid_notional": bv * mid,
                        "ask_notional": av * mid,
                        "ts": int(time.time() * 1000),
                    }
                    h = self.mid_hist[coin]
                    h.append(mid)
                    if len(h) > 150:
                        del h[: len(h) - 150]
                except Exception:
                    pass
            self.pulse_n += 1
            time.sleep(max(0.5, 2.5 - (time.time() - t0)))

    def _loop(self):
        n = 0
        while True:
            try:
                self.mids = {c: float(v) for c, v in
                             self.info.all_mids().items() if c in COINS}
                if n % 12 == 0:  # ctx every ~60s
                    meta, ctxs = self.info.meta_and_asset_ctxs()
                    names = [u["name"] for u in meta["universe"]]
                    for coin in COINS:
                        if coin in names:
                            x = ctxs[names.index(coin)]
                            self.ctx[coin] = {
                                "funding": float(x.get("funding", 0)),
                                "oi": float(x.get("openInterest", 0)),
                                "mark": float(x.get("markPx", 0)),
                            }
                if n % 2 == 0:  # account every ~10s
                    self.user = self.info.user_state(cfg.account_address)
                self.updated = time.time()
            except Exception as e:
                log.warning("market cache poll failed: %s", e)
            n += 1
            time.sleep(5)


cache = MarketCache()


# ---------------------------------------------------------------------------
# durable fill archive — keeps a permanent local copy of every fill so the
# History page survives HL's ~2000-fill user_fills cap, and reconstructs
# round-trip trades (the corrected, per-trade PnL — never per-fill).
# ---------------------------------------------------------------------------
def _fills_sync_loop():
    # own connection (sqlite handles are per-thread); first pass is a full
    # backfill, then incremental top-ups.
    conn = fills_store.connect()
    first = True
    while True:
        try:
            fills_store.sync(conn, cache.info, cfg.account_address, full=first)
            first = False
        except Exception as e:
            log.warning("fills sync loop error: %s", e)
        time.sleep(60)


threading.Thread(target=_fills_sync_loop, daemon=True).start()


def _history_trades() -> list[dict]:
    """All reconstructed round-trip trades from the durable archive."""
    conn = fills_store.connect()
    return fills_store.reconstruct_trades(conn)


# ---------------------------------------------------------------------------
# read endpoints
# ---------------------------------------------------------------------------
@app.get("/api/status")
def status():
    hb_age = None
    try:
        hb_age = round(time.time()
                       - float(Path(cfg.heartbeat_path).read_text()), 1)
    except Exception:
        pass
    rec_age = None
    try:
        rec_age = round(time.time()
                        - float(Path("/tmp/hl_recorder_heartbeat")
                                .read_text()), 1)
    except Exception:
        pass
    # derive an honest display state: the risk_state key only exists once
    # run_bot.py (the trading loop) has run — the Phase 1 data service
    # shares the heartbeat but never trades.
    phase = get_state("phase")
    risk_state = get_state("risk_state")
    risk_reason = get_state("risk_state_reason")
    hb_fresh = hb_age is not None and hb_age < 90
    if not hb_fresh:
        risk_state, risk_reason = "OFFLINE", "no bot heartbeat"
    elif phase != "5" or not risk_state:
        risk_state = "DATA_ONLY"
        risk_reason = ("Phase 1 data service running — trading loop "
                       "(run_bot.py) not started yet")
    # mode: what the running bot reports; config-file value as fallback
    # (suffixed so a config edit pending a restart can't masquerade as live)
    mode = get_state("trading_mode")
    if not mode:
        mode = ((cfg._raw.get("trading", {}) or {})
                .get("mode", "conservative")) + " (config)"
    _tr = effective_config().get("trading", {}) or {}
    return {
        "network": cfg.network,
        "risk_state": risk_state,
        "risk_reason": risk_reason,
        "trading_mode": mode,
        "directions": {
            "longs": bool(_tr.get("longs_enabled", True)),
            "shorts": bool(_tr.get("shorts_enabled", True)),
        },
        "bot_status": get_state("status"),
        "phase": phase,
        "control_request": get_state("control_request"),
        "coins_disabled": json.loads(get_state("control_coins_disabled")
                                     or "[]"),
        "day_open_equity": float(get_state("risk_day_open_equity") or 0),
        "week_open_equity": float(get_state("risk_week_open_equity") or 0),
        "heartbeat_age_s": hb_age,
        "recorder_heartbeat_age_s": rec_age,
        "cache_age_s": round(time.time() - cache.updated, 1),
        "coins": COINS,
    }


@app.get("/api/prices")
def prices():
    return {"mids": cache.mids, "ctx": cache.ctx,
            "ts": int(cache.updated * 1000)}


@app.get("/api/pulse")
def pulse():
    """Live microstructure for the Live page's analysis core."""
    return {"coins": cache.pulse, "hist": cache.mid_hist,
            "n": cache.pulse_n, "ts": int(time.time() * 1000)}


@app.get("/api/positions")
def positions():
    u = cache.user or {}
    ms = u.get("marginSummary", {})
    pos = []
    for p in u.get("assetPositions", []):
        q = p.get("position", {})
        if float(q.get("szi") or 0) == 0:
            continue
        pos.append({
            "coin": q.get("coin"),
            "szi": float(q.get("szi") or 0),
            "entry_px": float(q.get("entryPx") or 0),
            "position_value": float(q.get("positionValue") or 0),
            "unrealized_pnl": float(q.get("unrealizedPnl") or 0),
            "leverage": (q.get("leverage") or {}).get("value"),
            "liq_px": q.get("liquidationPx"),
        })
    return {"positions": pos,
            "account_value": float(ms.get("accountValue") or 0),
            "margin_used": float(ms.get("totalMarginUsed") or 0),
            "withdrawable": float(u.get("withdrawable") or 0)}


@app.get("/api/equity")
def equity(hours: int = 168):
    since = int((time.time() - hours * 3600) * 1000)
    return rows("SELECT ts, account_value FROM equity_snapshots "
                "WHERE ts>=? ORDER BY ts", since)


# Filter decisions, not real trade actions — excluded from the audit log by
# default (long_unconfirmed = no microstructure confirmation; direction_disabled
# = side toggled off; taker_skipped_* / taker_failed = maker-timeout fallback
# bailed). The long_blocked_<reason> family (structural gate) is excluded
# separately by a NOT LIKE 'long_blocked%' prefix match. Visible only with
# include_skips.
SKIP_STATUSES = (
    "long_unconfirmed",
    "short_unconfirmed",
    "direction_disabled",
    "taker_skipped_degraded",
    "taker_skipped_exhausted",
    "taker_failed",
)


@app.get("/api/trades")
def trades(limit: int = 200, coin: str | None = None,
           action: str | None = None, status: str | None = None,
           include_skips: bool = False):
    """Trade audit log: real OPEN/CLOSE/TEST actions. Skip statuses are
    excluded by default. Returns {total, trades} so the UI can show how many
    rows matched the active filters vs how many are displayed."""
    clauses, params = [], []
    if not include_skips:
        ph = ",".join("?" * len(SKIP_STATUSES))
        clauses.append(f"(status IS NULL OR status NOT IN ({ph}))")
        params.extend(SKIP_STATUSES)
        # long_blocked_<reason> uses dynamic suffixes (structural gate), so a
        # fixed IN-list can't catch them — exclude the whole family by prefix.
        clauses.append("(status IS NULL OR status NOT LIKE 'long_blocked%')")
    if coin:
        clauses.append("coin = ?")
        params.append(coin)
    if action:
        clauses.append("action = ?")
        params.append(action)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    total = rows(f"SELECT COUNT(*) AS n FROM trades{where}", *params)[0]["n"]
    out = rows(f"SELECT * FROM trades{where} ORDER BY ts DESC LIMIT ?",
               *params, limit)
    return {"total": total, "trades": out}


@app.get("/api/signals")
def signals(limit: int = 200, coin: str | None = None):
    out = rows(
        "SELECT * FROM signals WHERE (?1 IS NULL OR coin=?1) "
        "ORDER BY ts DESC LIMIT ?2", coin, limit)
    for r in out:
        try:
            r["meta"] = json.loads(r["meta"]) if r["meta"] else {}
        except Exception:
            pass
    return _json_safe(out)


@app.get("/api/tickets")
def tickets():
    """Live per-model tickets, published by the bot each loop, plus the same
    aggregation the bot itself runs (real SignalAggregator, not a re-impl) so
    the UI can show the actual verdict and how close it is to the entry gates.
    """
    eff = effective_config()
    risk_cfg = eff.get("risk", {}) or {}
    # the effective config already has live_config overrides merged in, so the
    # gates shown are exactly what the bot enforces this loop (no mode toggle).
    gates = {
        "min_confidence": float(risk_cfg.get("min_confidence", 0.62)),
        "min_model_agreement": int(risk_cfg.get("min_model_agreement", 4)),
        "mode": "live_config",
    }
    empty = {"ts": None, "coins": {}, "verdicts": {}, "gates": gates}
    raw = get_state("live_tickets")
    if not raw:
        return empty
    try:
        data = json.loads(raw)
    except Exception:
        return empty

    from reaper.aggregator import SignalAggregator
    from reaper.models import Ticket

    trading_cfg = eff.get("trading", {}) or {}
    agg = SignalAggregator(
        funding_hard_block_enabled=bool(
            risk_cfg.get("funding_hard_block_enabled", True)),
        funding_hard_block_conf=float(
            risk_cfg.get("funding_hard_block_conf", 0.75)),
        funding_hard_block_short_enabled=bool(
            risk_cfg.get("funding_hard_block_short_enabled", False)),
        funding_hard_block_short_conf=float(
            risk_cfg.get("funding_hard_block_short_conf", 0.75)))
    long_conf_enabled = bool(trading_cfg.get("long_confirmation_enabled", True))
    long_conf_models = set(trading_cfg.get(
        "long_confirmation_models",
        ["OrderbookImbalanceModel", "VWAPModel"]))
    long_conf_min = int(trading_cfg.get("long_confirmation_min", 1))
    short_conf_enabled = bool(
        trading_cfg.get("short_confirmation_enabled", False))
    short_conf_models = set(trading_cfg.get(
        "short_confirmation_models",
        ["OrderbookImbalanceModel", "VWAPModel"]))
    short_conf_min = int(trading_cfg.get("short_confirmation_min", 1))
    # LONG structural gate (2026-06-17) — the bot publishes its per-coin
    # spot/OI/book status each loop (it needs the live MarketBuffer to compute
    # it; the bridge can't recompute it from tickets alone). We read it through.
    long_struct_enabled = bool(
        trading_cfg.get("long_structural_gate_enabled", True))
    long_gates_pub = data.get("long_gates") or {}
    STRUCT_REASON = {
        "spot_not_leading": "LONG blocked (spot not leading perp)",
        "oi_not_rising": "LONG blocked (OI not rising)",
        "book_not_bid_heavy": "LONG blocked (book not bid-heavy)",
        "recent_pump": "LONG blocked (recent pump — cooldown)",
    }
    # SHORT structural gate (2026-06-19) — mirror of the LONG gate
    short_struct_enabled = bool(
        trading_cfg.get("short_structural_gate_enabled", True))
    short_gates_pub = data.get("short_gates") or {}
    SHORT_STRUCT_REASON = {
        "spot_not_lagging": "SHORT blocked (spot not lagging perp)",
        "oi_not_rising": "SHORT blocked (OI not rising w/ falling price)",
        "book_not_ask_heavy": "SHORT blocked (book not ask-heavy)",
        "recent_dump": "SHORT blocked (recent dump — cooldown)",
    }
    verdicts: dict = {}
    for coin, tks in (data.get("coins") or {}).items():
        try:
            objs = [Ticket(model=t["model"], direction=t["direction"],
                           confidence=float(t.get("confidence") or 0),
                           meta=t.get("meta") or {}) for t in tks]
            sig = agg.aggregate(coin, objs)
            agreement = (sig.long_votes if sig.direction == "LONG"
                         else sig.short_votes if sig.direction == "SHORT"
                         else 0)
            fund = next((t for t in objs
                         if t.model == "FundingRateModel"), None)
            veto = bool(fund and sig.direction in ("LONG", "SHORT")
                        and fund.direction in ("LONG", "SHORT")
                        and fund.direction != sig.direction)
            # LONG structural gate (run_bot, 2026-06-17): would this LONG be
            # blocked for lack of {spot leading, OI rising, book bid-heavy}?
            # The bot publishes the computed status per coin. Funding hard-block
            # already lands inside sig (FLAT + meta.block_reason).
            gate = long_gates_pub.get(coin) or {}
            long_blocked = bool(
                long_struct_enabled and sig.direction == "LONG"
                and gate and not gate.get("allowed"))
            # SHORT structural gate (run_bot, 2026-06-19): would this SHORT be
            # blocked for lack of {spot lagging, OI rising w/ falling price,
            # book ask-heavy, dump cooldown}?
            short_gate = short_gates_pub.get(coin) or {}
            short_blocked = bool(
                short_struct_enabled and sig.direction == "SHORT"
                and short_gate and not short_gate.get("allowed"))
            # legacy OB/VWAP confirmation (kept for the SHORT mirror display)
            long_unconfirmed = False
            if long_conf_enabled and sig.direction == "LONG":
                confirming = sum(
                    1 for t in objs
                    if t.model in long_conf_models and t.direction == "LONG")
                long_unconfirmed = confirming < long_conf_min
            short_unconfirmed = False
            if short_conf_enabled and sig.direction == "SHORT":
                confirming = sum(
                    1 for t in objs
                    if t.model in short_conf_models and t.direction == "SHORT")
                short_unconfirmed = confirming < short_conf_min
            block_reason = sig.meta.get("block_reason")
            if long_blocked:
                block_reason = STRUCT_REASON.get(
                    gate.get("block_reason"), "LONG blocked (structural gate)")
            elif short_blocked:
                block_reason = SHORT_STRUCT_REASON.get(
                    short_gate.get("block_reason"),
                    "SHORT blocked (structural gate)")
            elif long_unconfirmed:
                block_reason = "LONG skipped (no microstructure confirmation)"
            elif short_unconfirmed:
                block_reason = "SHORT skipped (no microstructure confirmation)"
            verdicts[coin] = {
                "direction": sig.direction,
                "confidence": round(sig.confidence, 3),
                "long_votes": sig.long_votes,
                "short_votes": sig.short_votes,
                "flat_votes": sig.flat_votes,
                "agreement": agreement,
                "regime": sig.regime,
                "veto": veto,
                "block_reason": block_reason,
                "long_gate": gate or None,
                "short_gate": short_gate or None,
                "would_fire": (sig.direction in ("LONG", "SHORT")
                               and not long_blocked
                               and not short_blocked
                               and not long_unconfirmed
                               and not short_unconfirmed
                               and sig.confidence >= gates["min_confidence"]
                               and agreement >= gates["min_model_agreement"]),
            }
        except Exception as e:
            log.warning("verdict aggregation failed for %s: %s", coin, e)
    data["verdicts"] = verdicts
    data["gates"] = gates
    return _json_safe(data)


@app.get("/api/fills")
def fills():
    """Realized per-coin PnL + win rate from exchange fill history."""
    try:
        fl = cache.info.user_fills(cfg.account_address) or []
    except Exception as e:
        raise HTTPException(502, f"user_fills failed: {e}")
    per: dict[str, dict] = {}
    recent = []
    for f in fl:
        coin = f.get("coin")
        pnl = float(f.get("closedPnl") or 0)
        fee = float(f.get("fee") or 0)
        s = per.setdefault(coin, {"realized_pnl": 0.0, "fees": 0.0,
                                  "closes": 0, "wins": 0})
        s["realized_pnl"] += pnl
        s["fees"] += fee
        if pnl != 0:
            s["closes"] += 1
            s["wins"] += 1 if pnl > 0 else 0
        if len(recent) < 50:
            recent.append({"ts": f.get("time"), "coin": coin,
                           "side": f.get("side"), "px": f.get("px"),
                           "sz": f.get("sz"), "closed_pnl": pnl, "fee": fee})
    for s in per.values():
        s["win_rate"] = (s["wins"] / s["closes"]) if s["closes"] else None
        s["realized_pnl"] = round(s["realized_pnl"], 4)
        s["fees"] = round(s["fees"], 4)
    return {"per_coin": per, "recent": recent}


# ---------------------------------------------------------------------------
# Live candlestick chart — candles (HL REST, same data the bot's WS feed gets)
# + entry/exit markers reconstructed from the trades table.
# ---------------------------------------------------------------------------
INTERVAL_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}

# small TTL cache so multiple browser tabs / the 10s refresh don't each hit
# the REST candle endpoint — keyed by (coin, interval).
_candle_cache: dict[tuple, tuple] = {}
_candle_lock = threading.Lock()


def _candles(coin: str, interval: str, limit: int) -> list[dict]:
    sec = INTERVAL_SEC[interval]
    key = (coin, interval)
    now = time.time()
    with _candle_lock:
        hit = _candle_cache.get(key)
        if hit and now - hit[0] < 4 and len(hit[1]) >= limit:
            return hit[1][-limit:]
    end = int(now * 1000)
    start = end - (limit + 2) * sec * 1000
    raw = cache.info.candles_snapshot(coin, interval, start, end)
    out = [{
        "time": int(c["t"]) // 1000,
        "open": float(c["o"]), "high": float(c["h"]),
        "low": float(c["l"]), "close": float(c["c"]),
        "volume": float(c["v"]),
    } for c in raw]
    out.sort(key=lambda c: c["time"])
    with _candle_lock:
        _candle_cache[key] = (now, out)
    return out[-limit:]


_CONF_RE = re.compile(r"conf=([0-9.]+)")
_VOTES_RE = re.compile(r"votes=(\d+)")
_EXIT_PX_RE = re.compile(r"@\s*([0-9.]+)")


def _exit_result(note: str) -> str:
    """Human exit reason from a CLOSE note ('take profit @ 1831.88' ->
    'take profit'; 'max hold time expired' -> 'max hold')."""
    n = (note or "").lower()
    if "take profit" in n:
        return "take profit"
    if "trailing" in n:
        return "trailing stop"
    if "stop loss" in n:
        return "stop loss"
    if "max hold" in n:
        return "max hold"
    if "roundtrip" in n:
        return "roundtrip"
    head = (note or "").split("@")[0].strip()
    return head or "close"


@app.get("/api/chart/{coin}")
def chart(coin: str, interval: str = "5m", limit: int = 200):
    """Live candles + entry/exit markers for one coin, for the Live chart."""
    if coin not in COINS:
        raise HTTPException(404, f"unknown coin: {coin}")
    if interval not in INTERVAL_SEC:
        raise HTTPException(400, f"bad interval: {interval}")
    limit = max(20, min(limit, 500))
    sec = INTERVAL_SEC[interval]
    try:
        candles = _candles(coin, interval, limit)
    except Exception as e:
        raise HTTPException(502, f"candle fetch failed: {e}")

    # markers from the trades table — real OPEN/CLOSE actions in the last 48h.
    since = int((time.time() - 48 * 3600) * 1000)
    _skip_ph = ",".join("?" * len(SKIP_STATUSES))
    trs = rows(
        "SELECT ts, side, action, size, price, status, note FROM trades "
        "WHERE coin=? AND ts>=? AND action IN ('OPEN','CLOSE') "
        f"AND (status IS NULL OR status NOT IN ({_skip_ph})) "
        "AND (status IS NULL OR status NOT LIKE 'long_blocked%') "
        "ORDER BY ts ASC",
        coin, since, *SKIP_STATUSES)

    # snap a marker ms-timestamp to its candle bucket so lightweight-charts
    # can place it on an existing bar.
    def bucket(ts_ms: int) -> int:
        return (int(ts_ms) // 1000 // sec) * sec

    markers: list[dict] = []
    open_stack: list[dict] = []  # unmatched entries, FIFO, for PnL pairing
    sign = {"LONG": 1, "SHORT": -1}
    last_close_note = None  # collapse repeated CLOSE logs of one exit event
    for t in trs:
        note = t["note"] or ""
        if t["action"] == "CLOSE" and note == last_close_note:
            continue
        last_close_note = note if t["action"] == "CLOSE" else None
        if t["action"] == "OPEN":
            if t["price"] is None:  # unfilled (resting/cancelled) — skip
                continue
            conf = _CONF_RE.search(note)
            votes = _VOTES_RE.search(note)
            entry = {
                "time": bucket(t["ts"]), "type": "entry",
                "direction": t["side"], "price": float(t["price"]),
                "size": float(t["size"]) if t["size"] is not None else None,
                "conf": float(conf.group(1)) if conf else None,
                "votes": int(votes.group(1)) if votes else None,
                "note": note,
            }
            markers.append(entry)
            open_stack.append(entry)
        else:  # CLOSE
            px = _EXIT_PX_RE.search(note)
            exit_px = float(px.group(1)) if px else None
            matched = open_stack.pop(0) if open_stack else None
            direction = matched["direction"] if matched else None
            pnl = None
            if (matched and exit_px is not None
                    and matched.get("size") and matched.get("price")):
                pnl = round((exit_px - matched["price"]) * matched["size"]
                            * sign.get(direction, 0), 4)
            markers.append({
                "time": bucket(t["ts"]), "type": "exit",
                "direction": direction, "price": exit_px,
                "result": _exit_result(note), "pnl": pnl, "note": note,
            })

    # open position (if any) from the account-state poller.
    open_position = None
    for p in (cache.user or {}).get("assetPositions", []):
        q = p.get("position", {})
        if q.get("coin") != coin or float(q.get("szi") or 0) == 0:
            continue
        szi = float(q.get("szi") or 0)
        # entry time: the most recent unmatched filled OPEN, if we have one
        entry_time = open_stack[-1]["time"] if open_stack else None
        open_position = {
            "direction": "LONG" if szi > 0 else "SHORT",
            "entry_price": float(q.get("entryPx") or 0),
            "entry_time": entry_time,
            "unrealized_pnl": float(q.get("unrealizedPnl") or 0),
            "conf": open_stack[-1].get("conf") if open_stack else None,
        }
        break

    # only return markers that fall within the visible candle window (pairing
    # above still walks the full 48h so cross-window exits get direction+PnL).
    if candles:
        lo = candles[0]["time"]
        markers = [m for m in markers if m["time"] >= lo]

    return {"coin": coin, "interval": interval, "candles": candles,
            "markers": markers, "open_position": open_position}


# ---------------------------------------------------------------------------
# History page — round-trip trade archive (corrected per-trade PnL)
# ---------------------------------------------------------------------------
def _filter_sort(trades: list[dict], coin, direction, result,
                 start, end, sort, order):
    out = trades
    if coin:
        out = [t for t in out if t["coin"] == coin]
    if direction:
        out = [t for t in out if t["direction"] == direction.upper()]
    if result == "win":
        out = [t for t in out if t["realized_pnl"] > 0]
    elif result == "loss":
        out = [t for t in out if t["realized_pnl"] <= 0]
    if start is not None:
        out = [t for t in out if t["exit_ts"] >= start]
    if end is not None:
        out = [t for t in out if t["exit_ts"] <= end]
    valid = {"entry_ts", "exit_ts", "coin", "direction", "realized_pnl",
             "gross_pnl", "fees", "hold_minutes", "n_fills"}
    key = sort if sort in valid else "exit_ts"
    out = sorted(out, key=lambda t: (t.get(key) is None, t.get(key)),
                 reverse=(order != "asc"))
    return out


def _summarize(trades: list[dict]) -> dict:
    if not trades:
        return {"n_trades": 0}
    net = sum(t["realized_pnl"] for t in trades)
    gross = sum(t["gross_pnl"] for t in trades)
    fees = sum(t["fees"] for t in trades)
    wins = [t for t in trades if t["realized_pnl"] > 0]
    gl = -sum(t["realized_pnl"] for t in trades if t["realized_pnl"] <= 0)
    per_coin: dict[str, dict] = {}
    for t in trades:
        s = per_coin.setdefault(t["coin"], {"n": 0, "net": 0.0, "wins": 0,
                                            "fees": 0.0})
        s["n"] += 1
        s["net"] += t["realized_pnl"]
        s["fees"] += t["fees"]
        s["wins"] += 1 if t["realized_pnl"] > 0 else 0
    for s in per_coin.values():
        s["net"] = round(s["net"], 4)
        s["fees"] = round(s["fees"], 4)
        s["win_rate"] = s["wins"] / s["n"] if s["n"] else None
    best = max(trades, key=lambda t: t["realized_pnl"])
    worst = min(trades, key=lambda t: t["realized_pnl"])
    return {
        "n_trades": len(trades),
        "net_pnl": round(net, 4),
        "gross_pnl": round(gross, 4),
        "fees": round(fees, 4),
        "win_rate": len(wins) / len(trades),
        "wins": len(wins), "losses": len(trades) - len(wins),
        "profit_factor": round(sum(t["realized_pnl"] for t in wins) / gl, 3)
        if gl > 0 else None,
        "avg_pnl": round(net / len(trades), 4),
        "avg_hold_min": round(sum(t["hold_minutes"] for t in trades)
                              / len(trades), 1),
        "first_ts": min(t["entry_ts"] for t in trades),
        "last_ts": max(t["exit_ts"] for t in trades),
        "per_coin": per_coin,
        "best": {"coin": best["coin"], "pnl": round(best["realized_pnl"], 4),
                 "ts": best["exit_ts"]},
        "worst": {"coin": worst["coin"], "pnl": round(worst["realized_pnl"], 4),
                  "ts": worst["exit_ts"]},
    }


@app.get("/api/history/summary")
def history_summary():
    """All-time totals + per-coin breakdown from reconstructed round-trips."""
    return _summarize(_history_trades())


@app.get("/api/history/daily")
def history_daily():
    """Realized PnL per UTC day (by exit date) + running cumulative."""
    import datetime as _dt
    trades = _history_trades()
    days: dict[str, dict] = {}
    for t in trades:
        day = _dt.datetime.utcfromtimestamp(
            t["exit_ts"] / 1000).strftime("%Y-%m-%d")
        d = days.setdefault(day, {"date": day, "net": 0.0, "gross": 0.0,
                                  "fees": 0.0, "n": 0, "wins": 0})
        d["net"] += t["realized_pnl"]
        d["gross"] += t["gross_pnl"]
        d["fees"] += t["fees"]
        d["n"] += 1
        d["wins"] += 1 if t["realized_pnl"] > 0 else 0
    out = []
    cum = 0.0
    for day in sorted(days):
        d = days[day]
        cum += d["net"]
        d["net"] = round(d["net"], 4)
        d["gross"] = round(d["gross"], 4)
        d["fees"] = round(d["fees"], 4)
        d["win_rate"] = d["wins"] / d["n"] if d["n"] else None
        d["cumulative"] = round(cum, 4)
        out.append(d)
    return out


@app.get("/api/history/trades")
def history_trades(coin: str | None = None, direction: str | None = None,
                   result: str | None = None, start: int | None = None,
                   end: int | None = None, sort: str = "exit_ts",
                   order: str = "desc", limit: int = 500, offset: int = 0):
    """Filtered/sorted/paginated round-trip trades."""
    flt = _filter_sort(_history_trades(), coin, direction, result, start, end,
                       sort, order)
    page = flt[offset:offset + limit]
    for t in page:
        t["realized_pnl"] = round(t["realized_pnl"], 4)
        t["gross_pnl"] = round(t["gross_pnl"], 4)
        t["fees"] = round(t["fees"], 4)
    return {"total": len(flt), "count": len(page), "offset": offset,
            "trades": page}


@app.get("/api/history/export.csv")
def history_export(coin: str | None = None, direction: str | None = None,
                   result: str | None = None, start: int | None = None,
                   end: int | None = None, sort: str = "exit_ts",
                   order: str = "desc"):
    """CSV of round-trip trades honoring the same filters as the table —
    hand this to evaluation."""
    import csv as _csv
    import datetime as _dt
    import io as _io
    flt = _filter_sort(_history_trades(), coin, direction, result, start, end,
                       sort, order)
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["entry_utc", "exit_utc", "coin", "direction", "hold_minutes",
                "entry_px", "exit_px", "qty", "n_fills", "gross_pnl", "fees",
                "net_pnl"])
    iso = lambda ms: _dt.datetime.utcfromtimestamp(ms / 1000).strftime(
        "%Y-%m-%d %H:%M:%S")  # noqa: E731
    for t in flt:
        w.writerow([iso(t["entry_ts"]), iso(t["exit_ts"]), t["coin"],
                    t["direction"], t["hold_minutes"],
                    round(t.get("entry_px", 0), 6), round(t.get("exit_px", 0), 6),
                    round(t.get("qty", 0), 6), t["n_fills"],
                    round(t["gross_pnl"], 4), round(t["fees"], 4),
                    round(t["realized_pnl"], 4)])
    fname = f"hl_reaper_trades_{int(time.time())}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="{fname}"'})


@app.get("/api/risk")
def risk():
    # effective guard params = config.yaml floor + live_config overrides, the
    # exact values the bot's RiskManager enforces this loop.
    r = dict(effective_config().get("risk", {}) or {})
    mode = "live_config"
    acct = float((cache.user.get("marginSummary", {}) or {})
                 .get("accountValue") or 0)
    day_open = float(get_state("risk_day_open_equity") or 0)
    week_open = float(get_state("risk_week_open_equity") or 0)
    st = status()  # same honest state derivation as /api/status
    return {
        "params": r,
        "mode": mode,
        "state": st["risk_state"],
        "reason": st["risk_reason"],
        "halted_until": float(get_state("risk_halted_until") or 0),
        "cooldown_until": float(get_state("risk_cooldown_until") or 0),
        "account_value": acct,
        "daily_drawdown": (1 - acct / day_open) if day_open and acct else None,
        "weekly_drawdown": (1 - acct / week_open) if week_open and acct else None,
    }


@app.get("/api/backtests")
def backtests():
    out = []
    for p in sorted(DATA_DIR.glob("*.json"), reverse=True):
        out.append({"name": p.name, "size": p.stat().st_size,
                    "mtime": int(p.stat().st_mtime * 1000)})
    return out


@app.get("/api/backtests/{name}")
def backtest_file(name: str):
    p = (DATA_DIR / name).resolve()
    if p.parent != DATA_DIR.resolve() or not p.exists():
        raise HTTPException(404, "no such file")
    data = json.loads(p.read_text())
    # equity curves can be 100k+ points — downsample for the browser
    def slim(d):
        if isinstance(d, dict):
            for k, v in d.items():
                if k == "equity_curve" and isinstance(v, list) and len(v) > 2000:
                    step = len(v) // 2000 + 1
                    d[k] = v[::step]
                else:
                    slim(v)
    slim(data)
    return data


# ---------------------------------------------------------------------------
# control endpoints (honored by run_bot.py each loop)
#
# The UI is reachable from the LAN, and the Next proxy makes every request
# look like 127.0.0.1 to this bridge — so write operations require a shared
# PIN (HL_REAPER_DASH_TOKEN in .env), sent as X-Dash-Token by the frontend.
# ---------------------------------------------------------------------------
DASH_TOKEN = os.environ.get("HL_REAPER_DASH_TOKEN", "")


def require_token(token: str | None):
    if not DASH_TOKEN:
        log.warning("HL_REAPER_DASH_TOKEN not set — controls UNPROTECTED")
        return
    if token != DASH_TOKEN:
        raise HTTPException(401, "bad or missing control PIN")


class CoinsBody(BaseModel):
    disabled: list[str]


class CloseBody(BaseModel):
    coin: str


@app.post("/api/control/halt")
def control_halt(x_dash_token: str | None = Header(default=None)):
    require_token(x_dash_token)
    set_state("control_request", "halt")
    return {"ok": True, "note": "bot will close all and HALT next loop"}


@app.post("/api/control/resume")
def control_resume(x_dash_token: str | None = Header(default=None)):
    require_token(x_dash_token)
    set_state("control_request", "resume")
    return {"ok": True, "note": "bot will clear HALT/COOLDOWN next loop"}


@app.post("/api/control/coins")
def control_coins(body: CoinsBody,
                  x_dash_token: str | None = Header(default=None)):
    require_token(x_dash_token)
    bad = [c for c in body.disabled if c not in COINS]
    if bad:
        raise HTTPException(400, f"unknown coins: {bad}")
    set_state("control_coins_disabled", json.dumps(body.disabled))
    return {"ok": True, "disabled": body.disabled}


@app.post("/api/control/close")
def control_close(body: CloseBody,
                  x_dash_token: str | None = Header(default=None)):
    require_token(x_dash_token)
    if body.coin not in COINS:
        raise HTTPException(400, f"unknown coin: {body.coin}")
    try:
        from reaper.execution.exchange_client import ExchangeClient
        xc = ExchangeClient(cfg)
        res = xc.market_close(body.coin)
        return {"ok": True, "result": str(res)[:500]}
    except Exception as e:
        raise HTTPException(502, f"close failed: {e}")


# ---------------------------------------------------------------------------
# live config (controls page) — every tunable on the page maps to one dotted
# key with a server-side validated range. The bot merges these onto config.yaml
# at the top of each loop, so changes take effect within one cycle (<=10s).
# ---------------------------------------------------------------------------
# {dotted_key: {"type": int|float|bool, "min": .., "max": ..}}
CONFIG_SCHEMA: dict[str, dict] = {
    # Section 2 — position sizing
    "trading.default_usd_size": {"type": "float", "min": 10, "max": 500},
    "risk.max_concurrent_positions": {"type": "int", "min": 1, "max": 7},
    "risk.max_leverage": {"type": "float", "min": 1, "max": 10},
    # Section 3 — signal gate
    "risk.min_confidence": {"type": "float", "min": 0.30, "max": 0.80},
    "risk.min_model_agreement": {"type": "int", "min": 2, "max": 6},
    # direction master switches (both-false rejected in set_config)
    "trading.longs_enabled": {"type": "bool"},
    "trading.shorts_enabled": {"type": "bool"},
    # Section 4 — entry filters
    "risk.funding_hard_block_enabled": {"type": "bool"},
    "risk.funding_hard_block_conf": {"type": "float", "min": 0.0, "max": 1.0},
    "trading.long_structural_gate_enabled": {"type": "bool"},
    "trading.long_spot_lead_threshold": {"type": "float", "min": 0.0,
                                         "max": 0.01},
    "trading.long_oi_rise_threshold": {"type": "float", "min": 0.0, "max": 0.05},
    "trading.long_ob_bid_threshold": {"type": "float", "min": 0.0, "max": 0.9},
    "trading.long_spot_lookback_minutes": {"type": "float", "min": 1, "max": 10},
    "trading.long_oi_lookback_minutes": {"type": "float", "min": 1, "max": 10},
    # LONG momentum cooldown (2026-06-18, anti-pump-top) — Signal 4
    "trading.long_pump_cooldown_enabled": {"type": "bool"},
    "trading.long_pump_threshold_1": {"type": "float", "min": 0.001,
                                      "max": 0.02},
    "trading.long_pump_threshold_2": {"type": "float", "min": 0.001,
                                      "max": 0.03},
    "trading.long_pump_threshold_3": {"type": "float", "min": 0.001,
                                      "max": 0.04},
    "trading.long_confirmation_enabled": {"type": "bool"},
    "trading.long_confirmation_min": {"type": "int", "min": 0, "max": 5},
    "risk.funding_hard_block_short_enabled": {"type": "bool"},
    "risk.funding_hard_block_short_conf": {"type": "float", "min": 0.0,
                                           "max": 1.0},
    "trading.short_confirmation_enabled": {"type": "bool"},
    "trading.short_confirmation_min": {"type": "int", "min": 0, "max": 5},
    # SHORT structural gate (2026-06-19) — mirror of the LONG gate
    "trading.short_structural_gate_enabled": {"type": "bool"},
    "trading.short_spot_lag_threshold": {"type": "float", "min": 0.0,
                                         "max": 0.01},
    "trading.short_oi_rise_threshold": {"type": "float", "min": 0.0,
                                        "max": 0.05},
    "trading.short_ob_ask_threshold": {"type": "float", "min": 0.0, "max": 0.9},
    "trading.short_spot_lookback_minutes": {"type": "float", "min": 1,
                                            "max": 10},
    "trading.short_oi_lookback_minutes": {"type": "float", "min": 1, "max": 10},
    # SHORT dump cooldown (anti-dump-bottom) — Signal 4
    "trading.short_dump_cooldown_enabled": {"type": "bool"},
    "trading.short_dump_threshold_1": {"type": "float", "min": 0.001,
                                       "max": 0.02},
    "trading.short_dump_threshold_2": {"type": "float", "min": 0.001,
                                       "max": 0.03},
    "trading.short_dump_threshold_3": {"type": "float", "min": 0.001,
                                       "max": 0.04},
    # Section 5 — risk / stops
    "risk.atr_sl_multiplier": {"type": "float", "min": 0.5, "max": 3.0},
    "risk.take_profit_r": {"type": "float", "min": 1.0, "max": 4.0},
    "risk.trail_activation_r": {"type": "float", "min": 0.5, "max": 3.0},
    "risk.max_hold_hours_scalp": {"type": "float", "min": 0.5, "max": 48},
    # Section 6 — taker fallback
    "trading.maker_timeout_fallback_enabled": {"type": "bool"},
    "trading.maker_timeout_fallback_n": {"type": "int", "min": 1, "max": 10},
    "trading.maker_timeout_fallback_window_s": {"type": "float", "min": 30,
                                                "max": 600},
    "trading.maker_timeout_exhaustion_atr_mult": {"type": "float", "min": 0.5,
                                                  "max": 3.0},
    # Section 8 — circuit breakers
    "risk.daily_drawdown_limit": {"type": "float", "min": 0.01, "max": 0.20},
    "risk.weekly_drawdown_limit": {"type": "float", "min": 0.01, "max": 0.50},
    "risk.max_loss_per_trade_pct": {"type": "float", "min": 0.005, "max": 0.20},
    "risk.cascade_detection_enabled": {"type": "bool"},
    "risk.cascade_oi_drop_pct": {"type": "float", "min": 0.05, "max": 0.50},
    "risk.cascade_window_minutes": {"type": "float", "min": 1, "max": 60},
    "risk.cascade_price_move_pct": {"type": "float", "min": 0.01, "max": 0.20},
}

# ---------------------------------------------------------------------------
# Strategy presets — named bundles of live_config overrides applied in one
# click. Keys are the REAL CONFIG_SCHEMA dotted keys the bot honors (the spec's
# shorthand — take_profit_r, max_hold_hours, *_structural_gate_enabled,
# pump/dump_cooldown_enabled — maps to risk.take_profit_r,
# risk.max_hold_hours_scalp, trading.long/short_structural_gate_enabled,
# trading.long_pump_cooldown_enabled, trading.short_dump_cooldown_enabled).
# Applied via live_config upsert → effective within one bot loop (<=10s).
# ---------------------------------------------------------------------------
ACTIVE_PRESET_KEY = "system.active_preset"
LAST_PRESET_KEY = "system.last_preset"

PRESETS: dict[str, dict] = {
    "BASELINE": {
        "display_name": "BASELINE",
        "description": "Pre-gate config. Max frequency. Best for strong trends.",
        "warning": ("This disables structural gates and increases trade "
                    "frequency. Use only in trending markets."),
        "settings": {
            "risk.min_confidence": 0.35,
            "risk.min_model_agreement": 3,
            "trading.longs_enabled": True,
            "trading.shorts_enabled": True,
            "risk.atr_sl_multiplier": 1.5,
            "risk.take_profit_r": 2.0,
            "risk.trail_activation_r": 1.5,
            "risk.max_hold_hours_scalp": 4.0,
            "trading.long_structural_gate_enabled": False,
            "trading.short_structural_gate_enabled": False,
            "risk.funding_hard_block_enabled": True,
            "trading.long_pump_cooldown_enabled": False,
            "trading.short_dump_cooldown_enabled": False,
        },
    },
    "SCALPER": {
        "display_name": "SCALPER",
        "description": "Tight stops, fast exits. Current configuration.",
        "warning": None,
        "settings": {
            "risk.min_confidence": 0.40,
            "risk.min_model_agreement": 3,
            "trading.longs_enabled": True,
            "trading.shorts_enabled": True,
            "risk.atr_sl_multiplier": 1.0,
            "risk.take_profit_r": 1.5,
            "risk.trail_activation_r": 1.0,
            "risk.max_hold_hours_scalp": 0.5,
            "trading.long_structural_gate_enabled": True,
            "trading.short_structural_gate_enabled": True,
            "risk.funding_hard_block_enabled": True,
            "trading.long_pump_cooldown_enabled": True,
            "trading.short_dump_cooldown_enabled": True,
        },
    },
    "SHORT_HUNTER": {
        "display_name": "SHORT HUNTER",
        "description": "Shorts only. Optimized for downtrending markets.",
        "warning": None,
        "settings": {
            "risk.min_confidence": 0.40,
            "risk.min_model_agreement": 3,
            "trading.longs_enabled": False,
            "trading.shorts_enabled": True,
            "risk.atr_sl_multiplier": 1.2,
            "risk.take_profit_r": 2.0,
            "risk.trail_activation_r": 1.5,
            "risk.max_hold_hours_scalp": 2.0,
            "trading.long_structural_gate_enabled": False,
            "trading.short_structural_gate_enabled": True,
            "risk.funding_hard_block_enabled": True,
            "trading.long_pump_cooldown_enabled": False,
            "trading.short_dump_cooldown_enabled": True,
        },
    },
    "TREND_RIDER": {
        "display_name": "TREND RIDER",
        "description": "Wider stops, lets winners run. Lower frequency.",
        "warning": None,
        "settings": {
            "risk.min_confidence": 0.50,
            "risk.min_model_agreement": 4,
            "trading.longs_enabled": True,
            "trading.shorts_enabled": True,
            "risk.atr_sl_multiplier": 2.0,
            "risk.take_profit_r": 3.0,
            "risk.trail_activation_r": 2.0,
            "risk.max_hold_hours_scalp": 8.0,
            "trading.long_structural_gate_enabled": True,
            "trading.short_structural_gate_enabled": True,
            "risk.funding_hard_block_enabled": True,
            "trading.long_pump_cooldown_enabled": True,
            "trading.short_dump_cooldown_enabled": True,
        },
    },
    "CONSERVATIVE": {
        "display_name": "CONSERVATIVE",
        "description": "Highest quality entries only. Minimal losses.",
        "warning": None,
        "settings": {
            "risk.min_confidence": 0.62,
            "risk.min_model_agreement": 4,
            "trading.longs_enabled": True,
            "trading.shorts_enabled": True,
            "risk.atr_sl_multiplier": 1.5,
            "risk.take_profit_r": 2.0,
            "risk.trail_activation_r": 1.5,
            "risk.max_hold_hours_scalp": 2.0,
            "trading.long_structural_gate_enabled": True,
            "trading.short_structural_gate_enabled": True,
            "risk.funding_hard_block_enabled": True,
            "trading.long_pump_cooldown_enabled": True,
            "trading.short_dump_cooldown_enabled": True,
        },
    },
}

_PER_COIN_RE = re.compile(r"^per_coin\.([A-Z0-9]+)\.(usd_size|leverage)$")
_PER_COIN_RANGE = {"usd_size": (10, 500), "leverage": (1, 10)}


def _coerce_config(key: str, value):
    """Validate + coerce one override. Raises HTTPException(400) on bad input.
    Dangerous values are rejected here — this is the only write path."""
    # Section 7 — active coin set
    if key == "trading.coins_active":
        if not isinstance(value, list) or not value:
            raise HTTPException(400, "coins_active must be a non-empty list")
        bad = [c for c in value if c not in COINS]
        if bad:
            raise HTTPException(400, f"unknown coins: {bad}")
        return list(value)
    # Section 7 — per-coin size/leverage overrides
    m = _PER_COIN_RE.match(key)
    if m:
        coin, field = m.groups()
        if coin not in COINS:
            raise HTTPException(400, f"unknown coin: {coin}")
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{key} must be a number")
        lo, hi = _PER_COIN_RANGE[field]
        if not lo <= v <= hi:
            raise HTTPException(400, f"{key} {v} outside {lo}..{hi}")
        return v
    spec = CONFIG_SCHEMA.get(key)
    if not spec:
        raise HTTPException(400, f"unknown or non-tunable key: {key}")
    if spec["type"] == "bool":
        if not isinstance(value, bool):
            raise HTTPException(400, f"{key} must be a boolean")
        return value
    try:
        v = int(value) if spec["type"] == "int" else float(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{key} must be a number")
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and v < lo:
        raise HTTPException(400, f"{key} {v} below minimum {lo}")
    if hi is not None and v > hi:
        raise HTTPException(400, f"{key} {v} above maximum {hi}")
    return v


class ConfigBody(BaseModel):
    key: str
    value: object


class CommandBody(BaseModel):
    command: str


@app.get("/api/config")
def get_config():
    """Effective config (floor + overrides), the floor defaults, the active
    overrides, and the slider ranges — everything the Controls page needs."""
    overrides = {k: v for k, v in live_overrides().items()
                 if not k.startswith("system.")}
    return {
        "effective": _flatten(effective_config()),
        "defaults": _flatten(cfg._raw),
        "overrides": overrides,
        "schema": CONFIG_SCHEMA,
        "coins": COINS,
    }


@app.get("/api/config/defaults")
def get_config_defaults():
    """config.yaml floor values only (what a full reset restores)."""
    return {"defaults": _flatten(cfg._raw)}


@app.post("/api/config")
def set_config(body: ConfigBody,
               x_dash_token: str | None = Header(default=None)):
    require_token(x_dash_token)
    coerced = _coerce_config(body.key, body.value)
    # safety: never let both trading directions be disabled at once — that would
    # silently halt all new entries. Enforced server-side (not just in the UI).
    if body.key in ("trading.longs_enabled", "trading.shorts_enabled") \
            and coerced is False:
        other = ("trading.shorts_enabled"
                 if body.key == "trading.longs_enabled"
                 else "trading.longs_enabled")
        other_val = _flatten(effective_config()).get(other, True)
        if not other_val:
            raise HTTPException(
                400, "at least one direction (LONG or SHORT) must stay "
                     "enabled — re-enable the other side first")
    old = live_overrides().get(body.key, _flatten(cfg._raw).get(body.key))
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO live_config "
            "(key, value, updated_ts, updated_by) VALUES (?,?,?,?)",
            (body.key, json.dumps(coerced), int(time.time() * 1000),
             "dashboard"))
    log.warning("LIVE CONFIG: %s changed from %s to %s by dashboard",
                body.key, old, coerced)
    # any manual tunable change diverges from the named preset → CUSTOM. Remember
    # which preset we came from (for the "modified from SCALPER" indicator).
    cur = get_live_config(ACTIVE_PRESET_KEY)
    if cur and cur != "CUSTOM":
        set_live_config(LAST_PRESET_KEY, cur)
    set_live_config(ACTIVE_PRESET_KEY, "CUSTOM")
    return {"ok": True, "key": body.key, "value": coerced,
            "note": "applies on next bot loop (<=10s)"}


@app.delete("/api/config/{key}")
def delete_config(key: str, x_dash_token: str | None = Header(default=None)):
    require_token(x_dash_token)
    with db() as c:
        c.execute("DELETE FROM live_config WHERE key=?", (key,))
    log.warning("LIVE CONFIG: %s override cleared by dashboard — default "
                "restored", key)
    return {"ok": True, "key": key, "note": "default restored next bot loop"}


@app.delete("/api/config")
def reset_config(x_dash_token: str | None = Header(default=None)):
    require_token(x_dash_token)
    n = len(live_overrides())
    with db() as c:
        c.execute("DELETE FROM live_config")
    log.warning("LIVE CONFIG: ALL %d overrides cleared by dashboard — "
                "config.yaml defaults restored", n)
    return {"ok": True, "cleared": n,
            "note": "all defaults restored next bot loop"}


@app.post("/api/bot/command")
def bot_command(body: CommandBody,
                x_dash_token: str | None = Header(default=None)):
    require_token(x_dash_token)
    cmd = (body.command or "").strip()
    ok = (cmd in ("pause", "resume", "close_all")
          or (cmd.startswith("close_coin/") and cmd.split("/", 1)[1] in COINS)
          or cmd.startswith("set_state/"))
    if not ok:
        raise HTTPException(400, f"unknown or unsafe command: {cmd!r}")
    with db() as c:
        cur = c.execute(
            "INSERT INTO bot_commands (command, issued_ts, status) "
            "VALUES (?,?, 'pending')", (cmd, int(time.time() * 1000)))
        cmd_id = cur.lastrowid
    log.warning("BOT COMMAND queued: %s (id=%d)", cmd, cmd_id)
    return {"ok": True, "id": cmd_id, "command": cmd,
            "note": "executes on next bot loop (<=10s)"}


# ---------------------------------------------------------------------------
# strategy presets — named bundles applied in one click via live_config
# ---------------------------------------------------------------------------
class PresetBody(BaseModel):
    preset_id: str


@app.get("/api/presets")
def list_presets():
    """Preset catalog for the Controls page (no raw settings needed there —
    the sliders/toggles re-read /api/config after apply)."""
    return {
        "presets": [
            {"id": pid, "display_name": p["display_name"],
             "description": p["description"], "warning": p.get("warning"),
             "settings": p["settings"]}
            for pid, p in PRESETS.items()
        ]
    }


@app.get("/api/presets/active")
def active_preset():
    """Current active preset name (a PRESETS id, or 'CUSTOM' once any setting
    has been changed by hand), plus the last named preset diverged from."""
    active = get_live_config(ACTIVE_PRESET_KEY) or "CUSTOM"
    last = get_live_config(LAST_PRESET_KEY)
    display = (PRESETS[active]["display_name"]
               if active in PRESETS else "CUSTOM")
    last_display = (PRESETS[last]["display_name"]
                    if last in PRESETS else None)
    return {"active": active, "display_name": display,
            "last_applied": last, "last_display_name": last_display}


@app.post("/api/presets/apply")
def apply_preset(body: PresetBody,
                 x_dash_token: str | None = Header(default=None)):
    require_token(x_dash_token)
    preset = PRESETS.get(body.preset_id)
    if not preset:
        raise HTTPException(400, f"unknown preset: {body.preset_id!r}")
    # validate every value through the same coercion the manual path uses, so a
    # malformed preset can never write an out-of-range override.
    coerced = {k: _coerce_config(k, v) for k, v in preset["settings"].items()}
    ts = int(time.time() * 1000)
    with db() as c:
        for key, value in coerced.items():
            c.execute(
                "INSERT OR REPLACE INTO live_config "
                "(key, value, updated_ts, updated_by) VALUES (?,?,?,?)",
                (key, json.dumps(value), ts, "preset"))
        c.execute(
            "INSERT OR REPLACE INTO live_config "
            "(key, value, updated_ts, updated_by) VALUES (?,?,?,?)",
            (ACTIVE_PRESET_KEY, json.dumps(body.preset_id), ts, "preset"))
        c.execute("INSERT INTO preset_log (preset_name, applied_ts, applied_by) "
                  "VALUES (?,?,?)", (body.preset_id, ts, "dashboard"))
    log.warning("PRESET applied: %s (%d settings) by dashboard",
                body.preset_id, len(coerced))
    return {"ok": True, "preset_id": body.preset_id,
            "applied": len(coerced),
            "note": "applies on next bot loop (<=10s)"}


@app.get("/api/presets/log")
def preset_log(limit: int = 50):
    """Recent preset-apply events (attribution trail for CSV trade analysis)."""
    return rows("SELECT id, preset_name, applied_ts, applied_by FROM preset_log "
                "ORDER BY applied_ts DESC LIMIT ?", limit)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8801, log_level="warning")
