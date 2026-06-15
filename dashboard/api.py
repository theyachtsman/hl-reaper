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
import json
import os
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


def get_state(key: str) -> str | None:
    r = rows("SELECT value FROM bot_state WHERE key=?", key)
    return r[0]["value"] if r else None


def set_state(key: str, value: str):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO bot_state VALUES (?,?,?)",
                  (key, value, int(time.time() * 1000)))


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
    return {
        "network": cfg.network,
        "risk_state": risk_state,
        "risk_reason": risk_reason,
        "trading_mode": mode,
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


@app.get("/api/trades")
def trades(limit: int = 100):
    return rows("SELECT * FROM trades ORDER BY ts DESC LIMIT ?", limit)


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
    return out


@app.get("/api/tickets")
def tickets():
    """Live per-model tickets, published by the bot each loop, plus the same
    aggregation the bot itself runs (real SignalAggregator, not a re-impl) so
    the UI can show the actual verdict and how close it is to the entry gates.
    """
    risk_cfg = cfg._raw.get("risk", {}) or {}
    gates = {
        "min_confidence": float(risk_cfg.get("min_confidence", 0.62)),
        "min_model_agreement": int(risk_cfg.get("min_model_agreement", 5)),
    }
    # apply the same mode overrides the bot's RiskManager enforces, so the
    # UI never shows conservative gates while paper_aggressive is live
    mode = get_state("trading_mode") or \
        (cfg._raw.get("trading", {}) or {}).get("mode", "conservative")
    if mode == "paper_aggressive":
        from reaper.risk.manager import PAPER_AGGRESSIVE_GATES
        gates["min_confidence"] = PAPER_AGGRESSIVE_GATES["min_confidence"]
        gates["min_model_agreement"] = \
            PAPER_AGGRESSIVE_GATES["min_model_agreement"]
    gates["mode"] = mode
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

    agg = SignalAggregator()
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
            verdicts[coin] = {
                "direction": sig.direction,
                "confidence": round(sig.confidence, 3),
                "long_votes": sig.long_votes,
                "short_votes": sig.short_votes,
                "flat_votes": sig.flat_votes,
                "agreement": agreement,
                "regime": sig.regime,
                "veto": veto,
                "would_fire": (sig.direction in ("LONG", "SHORT")
                               and sig.confidence >= gates["min_confidence"]
                               and agreement >= gates["min_model_agreement"]),
            }
        except Exception as e:
            log.warning("verdict aggregation failed for %s: %s", coin, e)
    data["verdicts"] = verdicts
    data["gates"] = gates
    return data


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
    # effective guard params: start from config, then apply the same
    # trading.mode overrides the bot's RiskManager enforces, so the page
    # shows what's actually gating trades — not the raw conservative base
    r = dict(cfg._raw.get("risk", {}) or {})
    mode = get_state("trading_mode") or \
        (cfg._raw.get("trading", {}) or {}).get("mode", "conservative")
    if mode == "paper_aggressive":
        from reaper.risk.manager import PAPER_AGGRESSIVE_GATES
        r.update(PAPER_AGGRESSIVE_GATES)
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


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8801, log_level="warning")
