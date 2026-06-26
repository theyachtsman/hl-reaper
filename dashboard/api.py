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
        # signal_history — the bot owns this table (reaper/db.py), but the bridge
        # may start first on a brand-new DB; ensure it so the export endpoints
        # never 500 before the bot's first run. Schema must match db.py.
        c.execute("""CREATE TABLE IF NOT EXISTS signal_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL,
            coin TEXT NOT NULL, band TEXT NOT NULL, regime TEXT,
            vote_ta TEXT, vote_meanrev TEXT, vote_vwap TEXT, vote_funding TEXT,
            vote_ob TEXT, vote_momentum TEXT, vote_regime TEXT, vote_liqmap TEXT,
            vote_ml TEXT, final_direction TEXT, final_conf REAL,
            active_voters INTEGER, cleared_gate INTEGER NOT NULL,
            gate_block_reason TEXT, trade_id INTEGER,
            ts_inserted TEXT DEFAULT (datetime('now')))""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_signal_history_ts "
                  "ON signal_history(ts_utc)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_signal_history_coin "
                  "ON signal_history(coin, band, ts_utc)")


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
        time.sleep(20)


threading.Thread(target=_fills_sync_loop, daemon=True).start()


def _attach_bands(trips: list[dict]) -> list[dict]:
    """Best-effort band attribution for reconstructed round-trips. Fills carry
    no band (the exchange doesn't know our bands); the band lives on our
    trades-table OPEN rows. Match each round-trip to the nearest same-coin,
    same-direction OPEN within a 10-minute window and copy its band. Anything
    unmatched (legacy/manual entries) gets band=None -> shown as '—'."""
    opens: dict[tuple, list[tuple]] = {}
    for o in rows("SELECT ts, coin, side, band FROM trades "
                  "WHERE action='OPEN' AND band IS NOT NULL ORDER BY ts"):
        opens.setdefault((o["coin"], o["side"]), []).append((o["ts"], o["band"]))
    for t in trips:
        cands = opens.get((t["coin"], t["direction"]), [])
        best_band, best_d = None, None
        for ts, band in cands:
            d = abs(ts - t["entry_ts"])
            if best_d is None or d < best_d:
                best_d, best_band = d, band
        t["band"] = best_band if (best_d is not None and best_d <= 600_000) \
            else None
    return trips


def _history_trades() -> list[dict]:
    """All reconstructed round-trip trades from the durable archive, with
    best-effort per-band attribution from the trades table."""
    conn = fills_store.connect()
    return _attach_bands(fills_store.reconstruct_trades(conn))


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
        "regime_history": json.loads(get_state("regime_history") or "null"),
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
    # band attribution: one band owns a coin at a time, so the latest OPEN row
    # for the coin tells us which band the live position belongs to.
    last_band: dict[str, str] = {}
    for r in rows("SELECT coin, band FROM trades WHERE action='OPEN' "
                  "AND band IS NOT NULL ORDER BY ts DESC LIMIT 200"):
        last_band.setdefault(r["coin"], r["band"])
    pos = []
    for p in u.get("assetPositions", []):
        q = p.get("position", {})
        if float(q.get("szi") or 0) == 0:
            continue
        pos.append({
            "coin": q.get("coin"),
            "band": last_band.get(q.get("coin")),
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
           band: str | None = None, include_skips: bool = False,
           order: str = "desc", start: int | None = None,
           end: int | None = None):
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
    if band:
        clauses.append("band = ?")
        params.append(band)
    if start is not None:
        clauses.append("ts >= ?")
        params.append(start)
    if end is not None:
        clauses.append("ts <= ?")
        params.append(end)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    direction = "ASC" if str(order).lower() == "asc" else "DESC"
    total = rows(f"SELECT COUNT(*) AS n FROM trades{where}", *params)[0]["n"]
    out = rows(f"SELECT * FROM trades{where} ORDER BY ts {direction} LIMIT ?",
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
    trading_cfg = eff.get("trading", {}) or {}
    # Dual band (2026-06-20): the bot publishes a per-band verdict per coin —
    # {coin: {scalp: {...}, trend: {...}}} — each already aggregated with the
    # band's fixed weight set (and, for scalp, the 1h regime bias already
    # applied). The bridge does NOT re-aggregate (that would drop the band
    # weights + bias); it reads the published verdict and only computes
    # would_fire against each band's gate.
    scalp_struct = (bool(risk_cfg.get("scalp_structural_gates_enabled", True)))
    gates = {
        "scalp": {
            "min_confidence": float(risk_cfg.get("scalp_min_confidence", 0.40)),
            "min_model_agreement": int(
                risk_cfg.get("scalp_min_model_agreement", 2)),
            "structural_gates_enabled": scalp_struct,
            "long_structural_gate_enabled": bool(
                trading_cfg.get("long_structural_gate_enabled", True)),
            "short_structural_gate_enabled": bool(
                trading_cfg.get("short_structural_gate_enabled", True)),
        },
        "trend": {
            "min_confidence": float(risk_cfg.get("trend_min_confidence", 0.55)),
            "min_model_agreement": int(
                risk_cfg.get("trend_min_model_agreement", 3)),
            "structural_gates_enabled": False,
        },
        "funding_hard_block_enabled": bool(
            risk_cfg.get("funding_hard_block_enabled", True)),
        "regime_counter_trend_penalty": float(
            risk_cfg.get("regime_counter_trend_penalty", 0.7)),
        "mode": "live_config",
    }
    bands_enabled = {
        "scalp": bool(trading_cfg.get("scalp_band_enabled", True)),
        "trend": bool(trading_cfg.get("trend_band_enabled", True)),
    }
    empty = {"ts": None, "coins": {}, "verdicts": {}, "gates": gates,
             "bands": bands_enabled}
    raw = get_state("live_tickets")
    if not raw:
        return empty
    try:
        data = json.loads(raw)
    except Exception:
        return empty

    long_gates_pub = data.get("long_gates") or {}
    short_gates_pub = data.get("short_gates") or {}
    STRUCT_REASON = {
        "spot_not_leading": "LONG blocked (spot not leading perp)",
        "oi_not_rising": "LONG blocked (OI not rising)",
        "book_not_bid_heavy": "LONG blocked (book not bid-heavy)",
        "recent_pump": "LONG blocked (recent pump — cooldown)",
    }
    SHORT_STRUCT_REASON = {
        "spot_not_lagging": "SHORT blocked (spot not lagging perp)",
        "oi_not_rising": "SHORT blocked (OI not rising w/ falling price)",
        "book_not_ask_heavy": "SHORT blocked (book not ask-heavy)",
        "recent_dump": "SHORT blocked (recent dump — cooldown)",
    }

    # parked non-voters (+ the meta router) are excluded from the reported vote
    # tally so the dashboard shows the real active-voter count (e.g. 1/1/3, not
    # 1/1/5). Recomputed here from the published tickets so it's correct even
    # before the bot restarts onto the matching aggregator change.
    NON_VOTERS = {"MLForecastModel", "LiquidationHeatmapModel",
                  "RegimeDetectorModel"}

    def _band_verdict(coin: str, band: str, packed: dict) -> dict:
        g = gates[band]
        direction = packed.get("direction", "FLAT")
        confidence = float(packed.get("confidence") or 0)
        meta = packed.get("meta") or {}
        tks = packed.get("tickets") or []
        active = [t for t in tks if t.get("model") not in NON_VOTERS]
        if active:
            long_votes = sum(1 for t in active if t.get("direction") == "LONG")
            short_votes = sum(1 for t in active if t.get("direction") == "SHORT")
            flat_votes = sum(1 for t in active if t.get("direction") == "FLAT")
        else:  # no ticket list published — fall back to the packed counts
            long_votes = int(packed.get("long") or 0)
            short_votes = int(packed.get("short") or 0)
            flat_votes = int(packed.get("flat") or 0)
        agreement = (long_votes if direction == "LONG"
                     else short_votes if direction == "SHORT" else 0)
        fund = next((t for t in tks if t["model"] == "FundingRateModel"), None)
        veto = bool(fund and direction in ("LONG", "SHORT")
                    and fund["direction"] in ("LONG", "SHORT")
                    and fund["direction"] != direction)
        block_reason = meta.get("block_reason")
        long_gate = short_gate = None
        long_blocked = short_blocked = False
        # structural gates apply to the SCALP band only. The signal DETAIL is
        # always surfaced (the bot computes it every loop regardless of the
        # toggle) so the dashboard gate rings fill as consensus forms even when a
        # gate is switched off — only the BLOCKING is gated on the enabled flags.
        if band == "scalp":
            long_gate = long_gates_pub.get(coin) or None
            short_gate = short_gates_pub.get(coin) or None
            if g["structural_gates_enabled"]:
                if (g["long_structural_gate_enabled"] and direction == "LONG"
                        and long_gate and not long_gate.get("allowed")):
                    long_blocked = True
                    block_reason = STRUCT_REASON.get(
                        long_gate.get("block_reason"),
                        "LONG blocked (structural gate)")
                elif (g["short_structural_gate_enabled"] and direction == "SHORT"
                        and short_gate and not short_gate.get("allowed")):
                    short_blocked = True
                    block_reason = SHORT_STRUCT_REASON.get(
                        short_gate.get("block_reason"),
                        "SHORT blocked (structural gate)")
        would_fire = (bands_enabled[band] and direction in ("LONG", "SHORT")
                      and not long_blocked and not short_blocked
                      and confidence >= g["min_confidence"]
                      and agreement >= g["min_model_agreement"])
        return {
            "direction": direction,
            "confidence": round(confidence, 3),
            "long_votes": long_votes, "short_votes": short_votes,
            "flat_votes": flat_votes, "agreement": agreement,
            "regime": packed.get("regime"), "veto": veto,
            "block_reason": block_reason,
            "regime_bias": meta.get("regime_bias"),
            "funding_dampen": meta.get("funding_dampen"),
            "long_gate": long_gate or None, "short_gate": short_gate or None,
            "would_fire": would_fire, "enabled": bands_enabled[band],
        }

    verdicts: dict = {}
    for coin, bands in (data.get("coins") or {}).items():
        try:
            # tolerate the legacy flat-list shape during a bot/bridge restart
            if isinstance(bands, list):
                continue
            verdicts[coin] = {
                "scalp": _band_verdict(coin, "scalp", bands.get("scalp") or {}),
                "trend": _band_verdict(coin, "trend", bands.get("trend") or {}),
            }
        except Exception as e:
            log.warning("verdict aggregation failed for %s: %s", coin, e)
    data["verdicts"] = verdicts
    data["gates"] = gates
    data["bands"] = bands_enabled
    return _json_safe(data)


@app.get("/api/fills")
def fills():
    """Realized per-coin PnL + win rate from reconstructed ROUND-TRIP trades.

    Same source of truth as the History page (reaper.data.fills_store) — one
    win/loss per round-trip, net of fees. NOT per-fill: counting each partial
    closing fill's closedPnl separately double-counts a single profitable close
    and inflates the win streak / win-rate (the documented per-fill artifact).
    Response shape is unchanged so the Profit Deck and Risk page need no edits;
    `recent` is now newest-first round-trip closes, `closed_pnl` is net realized.
    """
    try:
        trades = _history_trades()
    except Exception as e:
        raise HTTPException(502, f"fills reconstruct failed: {e}")
    per: dict[str, dict] = {}
    for t in trades:
        s = per.setdefault(t["coin"], {"realized_pnl": 0.0, "fees": 0.0,
                                       "closes": 0, "wins": 0})
        s["realized_pnl"] += t["realized_pnl"]
        s["fees"] += t["fees"]
        s["closes"] += 1
        s["wins"] += 1 if t["realized_pnl"] > 0 else 0
    for s in per.values():
        s["win_rate"] = (s["wins"] / s["closes"]) if s["closes"] else None
        s["realized_pnl"] = round(s["realized_pnl"], 4)
        s["fees"] = round(s["fees"], 4)
    # newest-first round-trip closes for the deck's streak / stack / popups
    recent = [{
        "ts": t["exit_ts"], "coin": t["coin"], "side": t["direction"],
        "px": t.get("exit_px"), "sz": round(t["qty"], 6),
        "closed_pnl": round(t["realized_pnl"], 4), "fee": round(t["fees"], 4),
    } for t in sorted(trades, key=lambda x: x["exit_ts"], reverse=True)[:50]]
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
    # manual close from the dashboard (Live/Controls "close" button -> bot
    # close_coin command, reason note "dashboard close_coin command"). Surface
    # it as an explicit exit reason so history shows it was manually stopped.
    if "close_coin" in n or "manual" in n:
        return "manual stop"
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
        "SELECT ts, side, action, size, price, status, note, band FROM trades "
        "WHERE coin=? AND ts>=? AND action IN ('OPEN','CLOSE','BE_LOCK') "
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
                "band": t["band"], "note": note,
            }
            markers.append(entry)
            open_stack.append(entry)
        elif t["action"] == "BE_LOCK":
            # breakeven profit lock fired — SL snapped to entry+buffer. Does not
            # touch open_stack pairing; just a visual "BE" marker on the chart.
            markers.append({
                "time": bucket(t["ts"]), "type": "be",
                "direction": t["side"],
                "price": float(t["price"]) if t["price"] is not None else None,
                "band": t["band"], "note": note,
            })
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
                "result": _exit_result(note), "pnl": pnl,
                "band": t["band"] or (matched.get("band") if matched else None),
                "note": note,
            })

    # open position (if any) from the account-state poller.
    # live in-trade tracker (entry/sl/tp/band) published by the bot each loop —
    # sl/tp move on trailing-stop / breakeven-lock, so this is the source of
    # truth for the chart's TP/SL overlay (the bot owns it in-memory; the bridge
    # can only see it via bot_state).
    ptrack: dict = {}
    try:
        raw_pt = get_state("pos_track")
        if raw_pt:
            ptrack = json.loads(raw_pt)
    except Exception:
        ptrack = {}
    open_position = None
    for p in (cache.user or {}).get("assetPositions", []):
        q = p.get("position", {})
        if q.get("coin") != coin or float(q.get("szi") or 0) == 0:
            continue
        szi = float(q.get("szi") or 0)
        pt = ptrack.get(coin) or {}
        # entry time: the most recent unmatched filled OPEN, if we have one
        entry_time = open_stack[-1]["time"] if open_stack else None
        open_position = {
            "direction": "LONG" if szi > 0 else "SHORT",
            "entry_price": float(q.get("entryPx") or 0),
            "entry_time": entry_time,
            "unrealized_pnl": float(q.get("unrealizedPnl") or 0),
            "conf": open_stack[-1].get("conf") if open_stack else None,
            "band": (open_stack[-1].get("band") if open_stack
                     else pt.get("band")),
            # TP/SL overlay inputs; sl/tp may be None if the tracker hasn't
            # published yet (e.g. just after a bot restart) — the UI guards that.
            "sl": pt.get("sl"),
            "tp": pt.get("tp"),
            "size": abs(szi),
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
                 start, end, sort, order, band=None):
    out = trades
    if coin:
        out = [t for t in out if t["coin"] == coin]
    if direction:
        out = [t for t in out if t["direction"] == direction.upper()]
    if band:
        out = [t for t in out if (t.get("band") or "") == band]
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
    # per-band breakdown (scalp / trend / unattributed) for the History page
    per_band: dict[str, dict] = {}
    for t in trades:
        b = t.get("band") or "unattributed"
        s = per_band.setdefault(b, {"n": 0, "net": 0.0, "wins": 0, "fees": 0.0})
        s["n"] += 1
        s["net"] += t["realized_pnl"]
        s["fees"] += t["fees"]
        s["wins"] += 1 if t["realized_pnl"] > 0 else 0
    for s in per_band.values():
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
        "per_band": per_band,
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
def history_daily(tz_offset: int = 0):
    """Realized PnL per day (by exit date) + running cumulative.

    tz_offset is the viewer's JS getTimezoneOffset() in minutes (UTC minus
    local; e.g. EDT = 240). Shifting the timestamp by -tz_offset buckets trades
    on the viewer's LOCAL calendar day instead of UTC, so the table matches the
    local timestamps shown everywhere else. Default 0 = UTC.
    """
    import datetime as _dt
    trades = _history_trades()
    shift = tz_offset * 60_000
    days: dict[str, dict] = {}
    for t in trades:
        day = _dt.datetime.utcfromtimestamp(
            (t["exit_ts"] - shift) / 1000).strftime("%Y-%m-%d")
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
                   order: str = "desc", limit: int = 500, offset: int = 0,
                   band: str | None = None):
    """Filtered/sorted/paginated round-trip trades."""
    flt = _filter_sort(_history_trades(), coin, direction, result, start, end,
                       sort, order, band)
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
                   order: str = "desc", band: str | None = None):
    """CSV of round-trip trades honoring the same filters as the table —
    hand this to evaluation."""
    import csv as _csv
    import datetime as _dt
    import io as _io
    flt = _filter_sort(_history_trades(), coin, direction, result, start, end,
                       sort, order, band)
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["entry_utc", "exit_utc", "coin", "direction", "band",
                "hold_minutes", "entry_px", "exit_px", "qty", "n_fills",
                "gross_pnl", "fees", "net_pnl"])
    iso = lambda ms: _dt.datetime.utcfromtimestamp(ms / 1000).strftime(
        "%Y-%m-%d %H:%M:%S")  # noqa: E731
    for t in flt:
        w.writerow([iso(t["entry_ts"]), iso(t["exit_ts"]), t["coin"],
                    t["direction"], t.get("band") or "",
                    t["hold_minutes"],
                    round(t.get("entry_px", 0), 6), round(t.get("exit_px", 0), 6),
                    round(t.get("qty", 0), 6), t["n_fills"],
                    round(t["gross_pnl"], 4), round(t["fees"], 4),
                    round(t["realized_pnl"], 4)])
    fname = f"hl_reaper_trades_{int(time.time())}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="{fname}"'})


@app.get("/api/trades/export.csv")
def trades_export(coin: str | None = None, action: str | None = None,
                  status: str | None = None, include_skips: bool = False,
                  band: str | None = None, start: int | None = None,
                  end: int | None = None):
    """CSV of the raw trade audit log (OPEN/CLOSE/TEST actions), honoring the
    same filters as the audit table. Separate from the round-trip trades CSV —
    this is the per-action bot log, not reconstructed PnL. No row limit: exports
    every matching action."""
    import csv as _csv
    import datetime as _dt
    import io as _io
    clauses, params = [], []
    if not include_skips:
        ph = ",".join("?" * len(SKIP_STATUSES))
        clauses.append(f"(status IS NULL OR status NOT IN ({ph}))")
        params.extend(SKIP_STATUSES)
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
    if band:
        clauses.append("band = ?")
        params.append(band)
    if start is not None:
        clauses.append("ts >= ?")
        params.append(start)
    if end is not None:
        clauses.append("ts <= ?")
        params.append(end)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    out = rows(f"SELECT * FROM trades{where} ORDER BY ts DESC", *params)
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["ts_utc", "coin", "side", "action", "size", "price",
                "leverage", "status", "note"])
    iso = lambda ms: _dt.datetime.utcfromtimestamp(ms / 1000).strftime(
        "%Y-%m-%d %H:%M:%S")  # noqa: E731
    for t in out:
        w.writerow([iso(t["ts"]), t["coin"], t["side"], t["action"],
                    t.get("size"), t.get("price"), t.get("leverage"),
                    t.get("status"), t.get("note")])
    fname = f"hl_reaper_audit_{int(time.time())}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="{fname}"'})


# ---------------------------------------------------------------------------
# signal_history — every aggregator evaluation, traded or not (diagnostic). The
# CSV is the deliverable; the JSON/count endpoints feed the History page filters.
# ---------------------------------------------------------------------------
SIGNAL_HISTORY_CSV_COLS = (
    "ts_utc", "coin", "band", "regime",
    "vote_ta", "vote_meanrev", "vote_vwap", "vote_funding", "vote_ob",
    "vote_momentum", "vote_regime", "vote_liqmap", "vote_ml",
    "final_direction", "final_conf", "active_voters", "cleared_gate",
    "gate_block_reason", "trade_id",
)


def _signal_history_where(coin, band, from_ts, to_ts, cleared_only):
    """Build (where_sql, params) shared by the list/count/export endpoints.
    ts_utc is stored as ISO8601 UTC, so lexicographic >=/<= on the ISO strings
    is chronological."""
    clauses, params = [], []
    if coin:
        clauses.append("coin = ?")
        params.append(coin)
    if band:
        clauses.append("band = ?")
        params.append(band)
    if from_ts:
        clauses.append("ts_utc >= ?")
        params.append(from_ts)
    if to_ts:
        clauses.append("ts_utc <= ?")
        params.append(to_ts)
    if cleared_only:
        clauses.append("cleared_gate = 1")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


@app.get("/api/signal-history")
def signal_history(coin: str | None = None, band: str | None = None,
                   from_ts: str | None = None, to_ts: str | None = None,
                   cleared_only: bool = False, limit: int = 500):
    """Filtered signal_history rows, newest first. limit clamped to 5000."""
    limit = max(1, min(int(limit), 5000))
    where, params = _signal_history_where(coin, band, from_ts, to_ts,
                                          cleared_only)
    out = rows(f"SELECT * FROM signal_history{where} "
               f"ORDER BY ts_utc DESC LIMIT ?", *params, limit)
    return {"count": len(out), "rows": _json_safe(out)}


@app.get("/api/signal-history/count")
def signal_history_count(coin: str | None = None, band: str | None = None,
                         from_ts: str | None = None, to_ts: str | None = None,
                         cleared_only: bool = False):
    """Row count for the current filters — drives the 'N signals in range'
    preview without shipping the rows."""
    where, params = _signal_history_where(coin, band, from_ts, to_ts,
                                          cleared_only)
    r = rows(f"SELECT COUNT(*) AS n FROM signal_history{where}", *params)
    return {"count": r[0]["n"] if r else 0}


def _candles_range(coin: str, interval: str, start_ms: int,
                   end_ms: int) -> list[dict]:
    """Candle closes for an explicit [start, end] window (ms). Unlike _candles
    (now-anchored, for the live chart) this serves historical export windows."""
    raw = cache.info.candles_snapshot(coin, interval, int(start_ms), int(end_ms))
    out = [{"time": int(c["t"]) // 1000, "close": float(c["c"])} for c in raw]
    out.sort(key=lambda c: c["time"])
    return out


# forward-return columns appended to the signal_history export (export-only —
# never stored, computed from HL candle closes at request time).
FWD_RET_COLS = ("fwd_ret_5m", "fwd_ret_15m", "fwd_ret_30m")


def _signal_forward_returns(out: list[dict]) -> dict:
    """{row id: {fwd_ret_5m, fwd_ret_15m, fwd_ret_30m}} for the given signal
    rows, as % price change from the candle containing the signal to the candle
    N minutes later. Scalp -> 5m grid (all three windows); trend -> 1h grid
    (only the next 1h close, mapped to fwd_ret_30m; the other two stay None).

    A forward window is None when its candle isn't fully closed yet (recent
    signals) or when candle data is unavailable — never a partial/derived value.
    This runs in the bridge (separate process from the bot), so closes come from
    HL's REST candles, not the bot's in-memory TA buffers — same underlying data.
    Any failure degrades to None for the affected rows; the export never errors."""
    import datetime as _dt

    def _ts(s):
        try:
            return _dt.datetime.fromisoformat(s).timestamp()
        except Exception:
            return None

    band_interval = lambda b: "5m" if b == "scalp" else "1h"  # noqa: E731
    now_sec = time.time()

    # 1. group signal times by (coin, interval) so we fetch ONE candle series per
    #    pair across the whole window (+30m forward headroom), not per row.
    groups: dict = {}
    for r in out:
        t = _ts(r.get("ts_utc"))
        if t is None:
            continue
        groups.setdefault((r["coin"], band_interval(r.get("band"))), []).append(t)

    # 2. fetch + build {candle_open_sec: close} per pair (best-effort per pair).
    close_maps: dict = {}
    for (coin, interval), times in groups.items():
        sec = INTERVAL_SEC[interval]
        try:
            cs = _candles_range(coin, interval,
                                int((min(times) - sec) * 1000),
                                int((max(times) + 1800 + sec) * 1000))
            close_maps[(coin, interval)] = {c["time"]: c["close"] for c in cs}
        except Exception as e:
            log.warning("forward-return candle fetch failed %s %s: %s",
                        coin, interval, e)
            close_maps[(coin, interval)] = {}

    # 3. per-row lookup. A forward candle is only used once fully closed
    #    (fwd_open + interval <= now) so recent signals yield None, not partials.
    res: dict = {}
    for r in out:
        vals = {c: None for c in FWD_RET_COLS}
        res[r.get("id")] = vals
        t = _ts(r.get("ts_utc"))
        scalp = r.get("band") == "scalp"
        interval = "5m" if scalp else "1h"
        sec = INTERVAL_SEC[interval]
        cmap = close_maps.get((r["coin"], interval), {})
        if t is None or not cmap:
            continue
        t_open = int(t // sec) * sec
        base = cmap.get(t_open)
        if not base:
            continue
        windows = (("fwd_ret_5m", 300), ("fwd_ret_15m", 900),
                   ("fwd_ret_30m", 1800)) if scalp else (("fwd_ret_30m", sec),)
        for col, offset in windows:
            fwd_open = t_open + offset
            if fwd_open + sec > now_sec:      # forward candle not closed yet
                continue
            fwd = cmap.get(fwd_open)
            if fwd:
                vals[col] = round((fwd - base) / base * 100, 4)
    return res


@app.get("/api/signal-history/export.csv")
def signal_history_export(coin: str | None = None, band: str | None = None,
                          from_ts: str | None = None, to_ts: str | None = None,
                          cleared_only: bool = False):
    """CSV of signal_history honoring the same filters. No row limit — the
    whole point is to analyze the blocked signals externally. Hand to Claude.
    Appends forward-return columns (computed from candle closes at export time,
    never stored) so the CSV is a self-contained model-validation dataset."""
    import csv as _csv
    import io as _io
    where, params = _signal_history_where(coin, band, from_ts, to_ts,
                                          cleared_only)
    out = rows(f"SELECT * FROM signal_history{where} ORDER BY ts_utc DESC",
               *params)
    try:
        fwd = _signal_forward_returns(out)
    except Exception as e:
        log.warning("forward-return computation failed: %s", e)
        fwd = {}
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(SIGNAL_HISTORY_CSV_COLS + FWD_RET_COLS)
    for r in out:
        fr = fwd.get(r.get("id")) or {}
        w.writerow([r.get(c) for c in SIGNAL_HISTORY_CSV_COLS]
                   + [fr.get(c) for c in FWD_RET_COLS])
    tag = lambda s: (s or "").replace(":", "").replace(" ", "")[:19] or "all"  # noqa: E731
    fname = f"hl_reaper_signals_{tag(from_ts)}_{tag(to_ts)}.csv"
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
    # FundingRateModel mapping A/B switch (2026-06-20). false = original binary
    # zones (fallback), true = smoothed continuous mapping. Stamped on OPEN
    # trade notes as fmap=binary|smooth for attribution.
    "risk.funding_smooth_mapping_enabled": {"type": "bool"},
    # FundingRate counter-1h-trend weight dampening (2026-06-23). Cuts FUNDING's
    # aggregator weight when it votes against a sustained trend. 0.40 default.
    "aggregator.funding_counter_trend_damp": {"type": "float", "min": 0.0,
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
    # breakeven profit lock (2026-06-20) — move SL to entry+buffer at this R
    "risk.breakeven_lock_enabled": {"type": "bool"},
    "risk.breakeven_lock_r": {"type": "float", "min": 0.0, "max": 2.0},
    "risk.breakeven_lock_buffer_pct": {"type": "float", "min": 0.0, "max": 0.5},
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
    # ---- Dual band (2026-06-20): 5m SCALP + 1h TREND -----------------------
    # Band master switches (at least one must stay enabled — both-false rejected
    # in set_config, same as longs/shorts) and per-band direction toggles.
    "trading.scalp_band_enabled": {"type": "bool"},
    "trading.trend_band_enabled": {"type": "bool"},
    "trading.scalp_longs_enabled": {"type": "bool"},
    "trading.scalp_shorts_enabled": {"type": "bool"},
    "trading.trend_longs_enabled": {"type": "bool"},
    "trading.trend_shorts_enabled": {"type": "bool"},
    # SCALP band geometry + gate
    "risk.scalp_min_confidence": {"type": "float", "min": 0.25, "max": 0.80},
    "risk.scalp_min_model_agreement": {"type": "int", "min": 1, "max": 6},
    "risk.scalp_atr_sl_multiplier": {"type": "float", "min": 0.5, "max": 3.0},
    "risk.scalp_take_profit_r": {"type": "float", "min": 1.0, "max": 4.0},
    "risk.scalp_trail_activation_r": {"type": "float", "min": 0.5, "max": 3.0},
    "risk.scalp_max_hold_hours": {"type": "float", "min": 0.1, "max": 12.0},
    "risk.scalp_breakeven_lock_r": {"type": "float", "min": 0.0, "max": 2.0},
    "risk.scalp_max_concurrent_positions": {"type": "int", "min": 1, "max": 7},
    "risk.scalp_position_size_usd": {"type": "float", "min": 10, "max": 500},
    "risk.scalp_structural_gates_enabled": {"type": "bool"},
    # TREND band geometry + gate (wider/longer than scalp)
    "risk.trend_min_confidence": {"type": "float", "min": 0.30, "max": 0.80},
    "risk.trend_min_model_agreement": {"type": "int", "min": 1, "max": 6},
    "risk.trend_atr_sl_multiplier": {"type": "float", "min": 0.5, "max": 5.0},
    "risk.trend_take_profit_r": {"type": "float", "min": 1.0, "max": 8.0},
    "risk.trend_trail_activation_r": {"type": "float", "min": 0.5, "max": 5.0},
    "risk.trend_max_hold_hours": {"type": "float", "min": 1.0, "max": 168.0},
    "risk.trend_breakeven_lock_r": {"type": "float", "min": 0.0, "max": 3.0},
    "risk.trend_max_concurrent_positions": {"type": "int", "min": 1, "max": 7},
    "risk.trend_position_size_usd": {"type": "float", "min": 10, "max": 1000},
    # Regime bias connector: 1h regime dampens counter-trend scalp confidence
    "risk.regime_counter_trend_penalty": {"type": "float", "min": 0.3,
                                          "max": 1.0},
    # Regime memory (2026-06-26): trend-band pre-entry suppression when the
    # recent dominant 1h regime opposes the proposed direction.
    "trading.regime_memory_enabled": {"type": "bool"},
    "trading.regime_memory_window": {"type": "int", "min": 2, "max": 8},
    "trading.regime_memory_threshold": {"type": "float", "min": 0.30,
                                        "max": 0.80},
    # ---- TAModel regime-aware trending RSI thresholds (2026-06-24) ----------
    # In TRENDING_UP/DOWN, TA uses these relaxed RSI thresholds so it agrees with
    # the trend at moderate RSI instead of abstaining (FLAT). RANGING/HIGH_VOL
    # keep the original blend. Re-read by run_bot each loop (hot-reload), same
    # pattern as aggregator.funding_counter_trend_damp. Thresholds are stated in
    # TRENDING_DOWN space; TRENDING_UP mirrors them around RSI 50.
    "models.ta.trending.rsi_short": {"type": "float", "min": 40, "max": 70},
    "models.ta.trending.rsi_long": {"type": "float", "min": 20, "max": 50},
    "models.ta.trending.rsi_neutral_low": {"type": "float", "min": 40,
                                           "max": 60},
    "models.ta.trending.rsi_neutral_high": {"type": "float", "min": 45,
                                            "max": 70},
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

# Shared per-band fragments (dual-band redesign 2026-06-20). Presets spread
# these and then override what makes them distinct. Keys are the real
# risk.scalp_*/trend_* CONFIG_SCHEMA keys the bot honors.
_SCALP_TIGHT = {
    "risk.scalp_min_confidence": 0.40,
    "risk.scalp_min_model_agreement": 2,
    "risk.scalp_atr_sl_multiplier": 1.0,
    "risk.scalp_take_profit_r": 1.5,
    "risk.scalp_trail_activation_r": 1.0,
    "risk.scalp_max_hold_hours": 0.5,
    "risk.scalp_breakeven_lock_r": 0.4,
    "risk.scalp_max_concurrent_positions": 3,
    "risk.scalp_position_size_usd": 30,
    "risk.scalp_structural_gates_enabled": True,
}
_TREND_WIDE = {
    "risk.trend_min_confidence": 0.55,
    "risk.trend_min_model_agreement": 3,
    "risk.trend_atr_sl_multiplier": 2.5,
    "risk.trend_take_profit_r": 4.0,
    "risk.trend_trail_activation_r": 2.0,
    "risk.trend_max_hold_hours": 48.0,
    "risk.trend_breakeven_lock_r": 0.8,
    "risk.trend_max_concurrent_positions": 2,
    "risk.trend_position_size_usd": 75,
}

PRESETS: dict[str, dict] = {
    "DUAL_BAND": {
        "display_name": "DUAL BAND",
        "description": "Both bands active — 5m scalp + 1h trend simultaneously. "
                       "The flagship dual-band configuration.",
        "warning": None,
        "settings": {
            "trading.scalp_band_enabled": True,
            "trading.trend_band_enabled": True,
            "trading.longs_enabled": True,
            "trading.shorts_enabled": True,
            "trading.scalp_longs_enabled": True,
            "trading.scalp_shorts_enabled": True,
            "trading.trend_longs_enabled": True,
            "trading.trend_shorts_enabled": True,
            **_SCALP_TIGHT, **_TREND_WIDE,
            "risk.regime_counter_trend_penalty": 0.7,
            "risk.funding_hard_block_enabled": True,
        },
    },
    "SCALPER": {
        "display_name": "SCALPER",
        "description": "Pure 5m scalp band. Trend band disabled. Tight stops, "
                       "fast exits, high frequency.",
        "warning": None,
        "settings": {
            "trading.scalp_band_enabled": True,
            "trading.trend_band_enabled": False,
            "trading.longs_enabled": True,
            "trading.shorts_enabled": True,
            "trading.scalp_longs_enabled": True,
            "trading.scalp_shorts_enabled": True,
            **_SCALP_TIGHT,
            "risk.funding_hard_block_enabled": True,
        },
    },
    "TREND_RIDER": {
        "display_name": "TREND RIDER",
        "description": "Pure 1h trend band. Scalp band disabled. Wide stops, "
                       "lets winners run, low frequency.",
        "warning": None,
        "settings": {
            "trading.scalp_band_enabled": False,
            "trading.trend_band_enabled": True,
            "trading.longs_enabled": True,
            "trading.shorts_enabled": True,
            "trading.trend_longs_enabled": True,
            "trading.trend_shorts_enabled": True,
            **_TREND_WIDE,
            # tuned trend-band defaults (captured from live config 2026-06-23):
            # bigger size, looser confidence bar, no counter-trend penalty.
            "risk.trend_position_size_usd": 175,
            "risk.trend_min_confidence": 0.49,
            "risk.regime_counter_trend_penalty": 1.0,
            "risk.funding_hard_block_enabled": True,
        },
    },
    "SHORT_HUNTER": {
        "display_name": "SHORT HUNTER",
        "description": "Both bands, shorts only. Optimized for downtrends.",
        "warning": None,
        "settings": {
            "trading.scalp_band_enabled": True,
            "trading.trend_band_enabled": True,
            "trading.longs_enabled": False,    # global master: no longs
            "trading.shorts_enabled": True,
            "trading.scalp_shorts_enabled": True,
            "trading.trend_shorts_enabled": True,
            **_SCALP_TIGHT, **_TREND_WIDE,
            "risk.regime_counter_trend_penalty": 0.7,
            "risk.funding_hard_block_enabled": True,
        },
    },
    "CONSERVATIVE": {
        "display_name": "CONSERVATIVE",
        "description": "Both bands, highest-quality entries only. Higher "
                       "confidence/agreement bars on each band.",
        "warning": None,
        "settings": {
            "trading.scalp_band_enabled": True,
            "trading.trend_band_enabled": True,
            "trading.longs_enabled": True,
            "trading.shorts_enabled": True,
            "trading.scalp_longs_enabled": True,
            "trading.scalp_shorts_enabled": True,
            "trading.trend_longs_enabled": True,
            "trading.trend_shorts_enabled": True,
            **_SCALP_TIGHT, **_TREND_WIDE,
            "risk.scalp_min_confidence": 0.55,
            "risk.scalp_min_model_agreement": 3,
            "risk.trend_min_confidence": 0.62,
            "risk.trend_min_model_agreement": 4,
            "risk.regime_counter_trend_penalty": 0.6,
            "risk.funding_hard_block_enabled": True,
        },
    },
    "BASELINE": {
        "display_name": "BASELINE",
        "description": "Both bands, structural gates off. Max frequency. Best "
                       "for strong trends only.",
        "warning": ("This disables structural gates and increases trade "
                    "frequency. Use only in trending markets."),
        "settings": {
            "trading.scalp_band_enabled": True,
            "trading.trend_band_enabled": True,
            "trading.longs_enabled": True,
            "trading.shorts_enabled": True,
            "trading.scalp_longs_enabled": True,
            "trading.scalp_shorts_enabled": True,
            "trading.trend_longs_enabled": True,
            "trading.trend_shorts_enabled": True,
            **_SCALP_TIGHT, **_TREND_WIDE,
            "risk.scalp_min_confidence": 0.35,
            "risk.scalp_structural_gates_enabled": False,
            "trading.long_structural_gate_enabled": False,
            "trading.short_structural_gate_enabled": False,
            "trading.long_pump_cooldown_enabled": False,
            "trading.short_dump_cooldown_enabled": False,
            "risk.regime_counter_trend_penalty": 0.85,
            "risk.funding_hard_block_enabled": True,
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
    # safety: at least one BAND must stay enabled — disabling both halts all new
    # entries (use pause for that). Mirrors the direction guard above.
    if body.key in ("trading.scalp_band_enabled", "trading.trend_band_enabled") \
            and coerced is False:
        other = ("trading.trend_band_enabled"
                 if body.key == "trading.scalp_band_enabled"
                 else "trading.scalp_band_enabled")
        other_val = _flatten(effective_config()).get(other, True)
        if not other_val:
            raise HTTPException(
                400, "at least one band (SCALP or TREND) must stay enabled — "
                     "re-enable the other band first, or use pause")
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
    # manual SL/TP override from the chart sliders: set_sltp/<coin>/<sl>/<tp>.
    # Light validation here (shape + known coin + parseable floats); the bot
    # enforces the side constraints (tp/sl on the correct sides of entry).
    if not ok and cmd.startswith("set_sltp/"):
        parts = cmd.split("/")
        if len(parts) == 4 and parts[1] in COINS:
            try:
                float(parts[2]); float(parts[3])
                ok = True
            except ValueError:
                ok = False
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
