"use client";
/**
 * PROFIT DECK — flip-board style live PnL console above the Analysis Core.
 *
 * Every number is real: day PnL re-marks open positions against the 2.5s L2
 * mid stream (so it moves between account polls), the trade stack / win
 * streak come from actual exchange fills, the 24h curve from recorded equity
 * snapshots, the candle feed is built from sampled mids, and the LED panel
 * flags genuine order-flow events (price bursts, resting book walls). No
 * fake animation, no invented whales.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import clsx from "clsx";
import { usePoll, post } from "@/lib/api";
import { useStatusStore, useBandStore, type Band } from "@/lib/store";
import ThreeCanvas, { ColorMode, Intensity } from "@/components/ThreeCanvas";
import SignalConsole from "@/components/SignalConsole";

/* analysis-core accent palette (shared with CoinCard3D / ThreeCanvas) */
const PROFIT = "#1D9E75", LOSS = "#E24B4A", NEUTRAL = "#888880";

/* ---------- 5x7 dot-matrix glyphs --------------------------------------- */
const FONT: Record<string, string[]> = {
  "0": ["01110","10001","10011","10101","11001","10001","01110"],
  "1": ["00100","01100","00100","00100","00100","00100","01110"],
  "2": ["01110","10001","00001","00010","00100","01000","11111"],
  "3": ["11111","00010","00100","00010","00001","10001","01110"],
  "4": ["00010","00110","01010","10010","11111","00010","00010"],
  "5": ["11111","10000","11110","00001","00001","10001","01110"],
  "6": ["00110","01000","10000","11110","10001","10001","01110"],
  "7": ["11111","00001","00010","00100","01000","01000","01000"],
  "8": ["01110","10001","10001","01110","10001","10001","01110"],
  "9": ["01110","10001","10001","01111","00001","00010","01100"],
  "$": ["00100","01111","10100","01110","00101","11110","00100"],
  "+": ["00000","00100","00100","11111","00100","00100","00000"],
  "-": ["00000","00000","00000","11111","00000","00000","00000"],
  ".": ["00000","00000","00000","00000","00000","01100","01100"],
  ",": ["00000","00000","00000","00000","00110","00100","01000"],
};

function DotGlyph({ ch, lit }: { ch: string; lit: string }) {
  const rows = FONT[ch] ?? FONT["-"];
  return (
    <svg viewBox="0 0 10 14" className="h-full w-auto block"
      style={{ filter: `drop-shadow(0 0 3px ${lit}55)` }}>
      {rows.map((row, r) =>
        row.split("").map((c, x) => (
          <rect key={`${r}-${x}`} x={x * 2 + 0.25} y={r * 2 + 0.25}
            width="1.5" height="1.5" rx="0.35"
            fill={c === "1" ? lit : "#1a2433"} />
        )))}
    </svg>
  );
}

/* odometer column: digits roll vertically through a dot-matrix strip */
function RollDigit({ ch, lit }: { ch: string; lit: string }) {
  if (!/\d/.test(ch)) return <div className="h-full"><DotGlyph ch={ch} lit={lit} /></div>;
  const n = Number(ch);
  return (
    <div className="relative h-full overflow-hidden" style={{ aspectRatio: "10 / 14" }}>
      <div className="absolute inset-x-0 top-0 transition-transform duration-700 ease-out"
        style={{ height: "1000%", transform: `translateY(-${n * 10}%)` }}>
        {Array.from({ length: 10 }).map((_, d) => (
          <div key={d} className="flex items-center justify-center" style={{ height: "10%" }}>
            <DotGlyph ch={String(d)} lit={lit} />
          </div>
        ))}
      </div>
    </div>
  );
}

function Odometer({ value, className }: { value: number | null; className?: string }) {
  const lit = value == null || Math.abs(value) < 0.005 ? "#64748b"
    : value > 0 ? "#22c98e" : "#f0625f";
  const s = value == null ? "$-.--"
    : `${value > 0 ? "+" : value < 0 ? "-" : ""}$${Math.abs(value)
        .toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  const chars = s.split("");
  return (
    <div className={clsx("flex gap-[3px]", className)}>
      {chars.map((ch, i) => (
        /* key by distance from the right so trailing digits keep their
           columns when a comma or extra digit appears on the left */
        <RollDigit key={chars.length - i} ch={ch} lit={lit} />
      ))}
    </div>
  );
}

/* ---------- ticking clock (isolated so it re-renders alone) -------------
   Local browser time, matching the History page's fmtTs() so every timestamp
   across the site reads in the same zone (was UTC, which didn't match). */
const TZ = (() => {
  try {
    return new Intl.DateTimeFormat("en-US", { timeZoneName: "short" })
      .formatToParts(new Date())
      .find((p) => p.type === "timeZoneName")?.value ?? "LOCAL";
  } catch { return "LOCAL"; }
})();
function Clock() {
  const [now, setNow] = useState("");
  useEffect(() => {
    const f = () => setNow(new Date().toLocaleTimeString("en-US", { hour12: true }));
    f();
    const id = setInterval(f, 1000);
    return () => clearInterval(id);
  }, []);
  return (
    <span className="mono text-sm text-slate-300 tabular-nums">
      {now} <span className="text-[9px] text-slate-500">{TZ}</span>
    </span>
  );
}

/* ---------- trade stack: realized pnl per close, oldest → newest -------- */
function TradeStack({ closes }: { closes: number[] }) {
  if (!closes.length)
    return <div className="text-[10px] text-slate-600">no closed trades yet</div>;
  const mx = Math.max(...closes.map(Math.abs), 1e-9);
  const n = closes.length;
  return (
    <svg viewBox={`0 0 ${n * 3} 20`} preserveAspectRatio="none"
      className="h-8" style={{ width: Math.min(220, n * 12) }}>
      <line x1="0" y1="10" x2={n * 3} y2="10" stroke="#1e2a3a" strokeWidth="0.5" />
      {closes.map((p, i) => {
        const h = Math.max(1, (Math.abs(p) / mx) * 9);
        return (
          <rect key={i} x={i * 3 + 0.4} width="2.2" rx="0.6"
            y={p >= 0 ? 10 - h : 10} height={h}
            className="stack-bar"
            style={{ animationDelay: `${i * 0.04}s`,
                     transformOrigin: p >= 0 ? "bottom" : "top" }}
            fill={p >= 0 ? "#34d399" : "#f87171"} opacity="0.85">
            <title>{`${p >= 0 ? "+" : ""}$${p.toFixed(4)}`}</title>
          </rect>
        );
      })}
    </svg>
  );
}

/* ---------- 24h pnl area chart ------------------------------------------ */
function PnlArea({ pts }: { pts: number[] }) {
  if (pts.length < 2)
    return <div className="h-20 flex items-center justify-center text-[10px] text-slate-600">
      collecting equity snapshots…</div>;
  const min = Math.min(...pts, 0), max = Math.max(...pts, 0);
  const r = max - min || 1;
  const y = (v: number) => 38 - ((v - min) / r) * 32 - 3;
  const path = pts.map((v, i) => `${(i / (pts.length - 1)) * 100},${y(v)}`).join(" ");
  const last = pts[pts.length - 1];
  const c = last >= 0 ? "#34d399" : "#f87171";
  return (
    <svg viewBox="0 0 100 40" preserveAspectRatio="none" className="w-full h-20">
      <defs>
        <linearGradient id="pnl24" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={c} stopOpacity="0.3" />
          <stop offset="100%" stopColor={c} stopOpacity="0" />
        </linearGradient>
      </defs>
      <line x1="0" x2="100" y1={y(0)} y2={y(0)} stroke="#334155"
        strokeWidth="0.5" strokeDasharray="2 3" vectorEffect="non-scaling-stroke" />
      <polygon points={`0,${y(0)} ${path} 100,${y(0)}`} fill="url(#pnl24)" />
      <polyline points={path} fill="none" stroke={c} strokeWidth="1.4"
        vectorEffect="non-scaling-stroke" />
      <circle cx="100" cy={y(last)} r="2" fill={c}>
        <animate attributeName="opacity" values="1;0.2;1" dur="1.2s" repeatCount="indefinite" />
      </circle>
    </svg>
  );
}

/* per-position band tag (cyan = scalp, purple = trend) — shown on every row so
   it's always clear which band owns a position at a glance. */
function BandTag({ band }: { band?: string | null }) {
  if (!band) return <span className="text-slate-600 text-[8px]">—</span>;
  return (
    <span className={clsx(
      "px-1 rounded text-[8px] uppercase tracking-wider font-semibold",
      band === "scalp" ? "bg-cyan-500/20 text-cyan-300"
                       : "bg-purple-500/20 text-purple-300")}>
      {band}
    </span>
  );
}

/* ---------- open positions, live-marked against the 2.5s pulse ----------
   `band` is the active Live-page context: the panel shows only positions owned
   by that band, but every row still carries its band tag. */
function PositionsPanel({ positions, mids, band }: {
  positions: any[]; mids: Record<string, CoinPulse> | undefined; band: Band;
}) {
  // manual close: a row's "close" arms a one-click confirm, which queues a
  // close_coin command to the bot (it executes the market close within one
  // loop and logs the round-trip to history as a manual stop — see api.py
  // _exit_result). `sent` shows "closing…" until the position clears the book.
  const [pending, setPending] = useState<string | null>(null);
  const [sent, setSent] = useState<Set<string>>(new Set());
  const [err, setErr] = useState<string | null>(null);

  // once a closed position drops out of the feed, forget it so the same coin
  // reopening later shows a fresh "close" button (not a stale "closing…").
  useEffect(() => {
    const live = new Set(positions.map((p) => p.coin));
    setSent((prev) => {
      const next = new Set([...prev].filter((c) => live.has(c)));
      return next.size === prev.size ? prev : next;
    });
  }, [positions]);

  const doClose = (coin: string) => {
    setErr(null);
    setSent((s) => new Set(s).add(coin));
    setPending(null);
    post("/api/bot/command", { command: `close_coin/${coin}` }).catch((e) => {
      setErr(`${coin}: ${e}`);
      setSent((s) => { const n = new Set(s); n.delete(coin); return n; });
    });
  };

  if (!positions.length)
    return <div className="h-24 flex items-center justify-center gap-1.5 text-[10px] text-slate-600">
      <span className="pulse-icon inline-block w-1.5 h-1.5 rounded-full bg-emerald-500/80" />
      no open {band} positions — gates armed, waiting for a signal…</div>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full mono text-[11px] min-w-[620px]">
        <thead className="text-left text-slate-500 uppercase text-[8px] tracking-[0.15em]">
          <tr>
            <th className="py-1 pr-2">coin</th><th>band</th><th>side</th><th>size</th>
            <th>entry</th><th>mark</th><th>value</th><th>upnl</th>
            <th>lev</th><th>liq</th><th className="text-right pr-1">action</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => {
            const mid = mids?.[p.coin]?.mid;
            const upnl = mid ? p.szi * (mid - p.entry_px) : p.unrealized_pnl;
            const value = mid ? Math.abs(p.szi) * mid : p.position_value;
            const long = p.szi > 0;
            const inProfit = upnl >= 0;
            return (
              <tr key={p.coin} className="border-t border-edge/60 feed-in">
                <td className="py-1.5 pr-2 font-bold text-slate-200">{p.coin}</td>
                <td><BandTag band={p.band} /></td>
                <td className={long ? "text-emerald-400" : "text-red-400"}>
                  {long ? "LONG" : "SHORT"}
                </td>
                <td>{Math.abs(p.szi)}</td>
                <td>{p.entry_px}</td>
                <td className="text-slate-300">{mid ? mid.toLocaleString() : "—"}</td>
                <td className="text-slate-200">
                  {value != null ? `$${value.toLocaleString("en-US",
                    { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : "—"}
                </td>
                <td className={inProfit ? "text-emerald-400" : "text-red-400"}>
                  {inProfit ? "+" : "-"}${Math.abs(upnl).toFixed(2)}
                </td>
                <td>{p.leverage ?? "—"}x</td>
                <td className="text-slate-500">{p.liq_px ?? "—"}</td>
                <td className="text-right pr-1 whitespace-nowrap">
                  {sent.has(p.coin) ? (
                    <span className="text-[9px] uppercase tracking-wider text-sky-400">closing…</span>
                  ) : pending === p.coin ? (
                    <span className="inline-flex items-center gap-1">
                      <button onClick={() => doClose(p.coin)}
                        title={inProfit ? "market close — lock in this profit"
                                        : "market close — take this loss now"}
                        className={clsx(
                          "px-1.5 py-0.5 rounded text-[9px] font-semibold uppercase tracking-wider",
                          inProfit ? "bg-emerald-500/25 text-emerald-200 hover:bg-emerald-500/40"
                                   : "bg-red-500/25 text-red-200 hover:bg-red-500/40")}>
                        {inProfit ? "lock profit" : "take loss"}
                      </button>
                      <button onClick={() => setPending(null)}
                        className="px-1 py-0.5 rounded text-[9px] text-slate-500 hover:text-slate-300"
                        title="cancel">✕</button>
                    </span>
                  ) : (
                    <button onClick={() => { setErr(null); setPending(p.coin); }}
                      className="px-1.5 py-0.5 rounded text-[9px] font-semibold uppercase tracking-wider border border-edge text-slate-400 hover:text-slate-100 hover:border-slate-500">
                      close
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {err && <div className="text-[10px] text-red-400 mt-1.5 mono">close failed — {err}</div>}
    </div>
  );
}

/* ---------- LED flow-event panel ----------------------------------------- */
type FlowEvent = { text: string; ts: number };
function Led({ ev }: { ev: FlowEvent | null }) {
  const liveish = ev && Date.now() - ev.ts < 30_000;
  /* atmospheric particles: random positions/timings, fixed once on mount */
  const particles = useMemo(
    () => Array.from({ length: 8 }).map(() => ({
      left: `${10 + Math.random() * 80}%`,
      delay: `${Math.random() * 6}s`,
      duration: `${5 + Math.random() * 3}s`,
    })), []);
  return (
    <div className="core-card relative overflow-hidden px-4 py-3 flex flex-col justify-center">
      {particles.map((p, i) => (
        <span key={i} className="flow-particle"
          style={{ left: p.left, bottom: 0,
                   animationDelay: p.delay, animationDuration: p.duration }} />
      ))}
      <div className="relative flex items-center gap-2 mb-1.5">
        <span className="pulse-icon inline-block w-[5px] h-[5px] rounded-full"
          style={{ background: PROFIT, boxShadow: `0 0 6px ${PROFIT}` }} />
        <span className="label">Flow Event</span>
      </div>
      <div key={ev?.ts ?? 0}
        className={clsx("relative mono text-xs tracking-widest uppercase led-in",
          liveish ? "" : "text-slate-500")}
        style={liveish ? { color: PROFIT, textShadow: `0 0 8px ${PROFIT}aa` } : undefined}>
        {liveish ? ev!.text : (
          <span className="inline-flex items-center gap-1.5">
            <span className="inline-flex gap-0.5" style={{ color: PROFIT }}>
              <span className="scan-dot">·</span>
              <span className="scan-dot">·</span>
              <span className="scan-dot">·</span>
            </span>
            scanning order flow…
          </span>
        )}
      </div>
    </div>
  );
}

/* ======================================================================== */
type CoinPulse = {
  mid: number; spread_bps: number; imbalance: number;
  bid_notional: number; ask_notional: number;
};

export default function ProfitDeck() {
  const status = useStatusStore((s) => s.status);
  const { data: pulse } = usePoll<{ coins: Record<string, CoinPulse>;
    hist: Record<string, number[]> }>("/api/pulse", 2500);
  const { data: pos } = usePoll<{ positions: any[]; account_value: number }>(
    "/api/positions", 5000);
  const { data: fills } = usePoll<{ per_coin: Record<string, any>; recent: any[] }>(
    "/api/fills", 10000);
  const { data: equity } = usePoll<{ ts: number; account_value: number }[]>(
    "/api/equity?hours=24", 30000);

  const [ev, setEv] = useState<FlowEvent | null>(null);
  const prevMids = useRef<Record<string, number>>({});

  /* flash-burst on day-PnL change: keyed remount replays the halo even on
     consecutive ticks; direction colours it green (up) / red (down) */
  const [burst, setBurst] = useState<{ key: number; dir: "up" | "down" } | null>(null);
  const prevPnl = useRef<number | null>(null);

  /* RPG damage numbers: one popup per freshly-closed fill */
  type Dmg = { id: number; text: string; win: boolean; x: number;
               delay: number };
  const [dmgs, setDmgs] = useState<Dmg[]>([]);
  const seenFills = useRef<Set<string> | null>(null);
  useEffect(() => {
    if (!fills?.recent) return;
    const closes = fills.recent.filter((f) => f.closed_pnl !== 0);
    const key = (f: any) => `${f.ts}|${f.coin}|${f.px}|${f.closed_pnl}`;
    if (seenFills.current === null) {
      // first load: everything is history, not news — no popup storm
      seenFills.current = new Set(closes.map(key));
      return;
    }
    const fresh = closes.filter((f) => !seenFills.current!.has(key(f)));
    closes.forEach((f) => seenFills.current!.add(key(f)));
    if (!fresh.length) return;
    const spawned = fresh.slice(0, 5).map((f, i) => ({
      id: Date.now() + i,
      text: `${f.closed_pnl > 0 ? "+" : "-"}$${Math.abs(f.closed_pnl).toFixed(2)}`,
      win: f.closed_pnl > 0,
      x: 12 + Math.random() * 60,
      delay: i * 0.3,
    }));
    setDmgs((d) => [...d, ...spawned]);
    spawned.forEach((s, i) => setTimeout(
      () => setDmgs((d) => d.filter((x) => x.id !== s.id)), 2800 + i * 300));
  }, [fills]);

  /* real flow-event detection: tick bursts and resting book walls */
  useEffect(() => {
    if (!pulse?.coins) return;
    let best: { score: number; text: string } | null = null;
    for (const [coin, p] of Object.entries(pulse.coins)) {
      const pm = prevMids.current[coin];
      prevMids.current[coin] = p.mid;
      if (pm) {
        const bps = ((p.mid - pm) / pm) * 10_000;
        if (Math.abs(bps) >= 6 && (!best || Math.abs(bps) > best.score))
          best = { score: Math.abs(bps),
            text: `${coin} ${bps > 0 ? "▲" : "▼"} ${bps > 0 ? "+" : ""}${bps.toFixed(1)}bp burst` };
      }
      const skew = p.imbalance;
      const wall = skew > 0 ? p.bid_notional : p.ask_notional;
      if (Math.abs(skew) >= 0.65 && wall >= 5000 && (!best || Math.abs(skew) * 4 > best.score))
        best = { score: Math.abs(skew) * 4,
          text: `${coin} ${skew > 0 ? "bid" : "ask"} wall $${(wall / 1000).toFixed(1)}k · skew ${skew > 0 ? "+" : ""}${(skew * 100).toFixed(0)}%` };
    }
    if (best) setEv({ text: best.text, ts: Date.now() });
  }, [pulse]);

  /* day-open baseline anchored to the VIEWER'S LOCAL midnight (e.g. EST),
     NOT the bot's UTC day_open_equity (that stays UTC — it drives the drawdown
     guards and must not move). Baseline = account equity at the most recent
     snapshot at/before local midnight; fall back to the earliest snapshot in
     the 24h window (dashboard opened mid-day), then to the bot's UTC baseline
     if no equity history is available yet. Recomputed when equity polls (30s),
     so it rolls over within 30s of local midnight. */
  const dayOpenLocal = useMemo(() => {
    const eq = equity ?? [];
    if (!eq.length) return null;
    const lm = new Date(); lm.setHours(0, 0, 0, 0);
    const localMidnight = lm.getTime();
    let base: number | null = null;
    for (const e of eq) {                       // ascending by ts
      if (e.ts <= localMidnight) base = e.account_value;
      else break;
    }
    return base ?? eq[0].account_value;         // window starts after midnight
  }, [equity]);

  /* day PnL, re-marked live: swap the 5s-poll uPnL for one computed from
     the freshest 2.5s mids so the odometer moves with the market */
  let dayPnl: number | null = null;
  const dayOpen = dayOpenLocal ?? status?.day_open_equity ?? null;
  if (pos && dayOpen) {
    const polled = pos.positions.reduce((s, p) => s + p.unrealized_pnl, 0);
    const live = pos.positions.reduce((s, p) => {
      const mid = pulse?.coins?.[p.coin]?.mid;
      return s + (mid ? p.szi * (mid - p.entry_px) : p.unrealized_pnl);
    }, 0);
    dayPnl = pos.account_value - polled + live - dayOpen;
  }

  /* fire a flash-burst whenever the marked day-PnL moves */
  useEffect(() => {
    if (dayPnl == null) return;
    const prev = prevPnl.current;
    if (prev != null && Math.abs(dayPnl - prev) >= 0.005) {
      setBurst({ key: Date.now(), dir: dayPnl > prev ? "up" : "down" });
    }
    prevPnl.current = dayPnl;
  }, [dayPnl]);

  /* fills-derived stats */
  const recent = fills?.recent ?? [];           // newest first
  const closes = recent.filter((f) => f.closed_pnl !== 0);
  const agg = Object.values(fills?.per_coin ?? {});
  const nCloses = agg.reduce((s: number, c: any) => s + c.closes, 0);
  const nWins = agg.reduce((s: number, c: any) => s + c.wins, 0);
  const realized = agg.reduce((s: number, c: any) => s + c.realized_pnl, 0);
  const stackPnls = closes.slice(0, 40).reverse().map((f) => f.closed_pnl);

  /* 24h pnl relative to the window's first snapshot */
  const eq = equity ?? [];
  const pnl24 = eq.length ? eq.map((e) => e.account_value - eq[0].account_value) : [];
  const peak24 = pnl24.length ? Math.max(...pnl24) : 0;
  const last24 = pnl24.length ? pnl24[pnl24.length - 1] : 0;

  const month = new Date().toLocaleString("en-US", { month: "long" }).toUpperCase();
  const openPositions = pos?.positions ?? [];
  // account-wide pnl/deck calcs above stay over ALL positions; the Open
  // Positions panel below shows only the active band's positions.
  const activeBand = useBandStore((s) => s.activeBand);
  const setActiveBand = useBandStore((s) => s.setActiveBand);
  const bandPositions = openPositions.filter((p) => p.band === activeBand);

  /* analysis-core 3D backdrop: a live ribbon of the account's equity path,
     colour-keyed to the day's direction (green up / red down) so the deck
     glows the same way CoinCard3D does. memoised on equity so the WebGL
     geometry only rebuilds when a fresh snapshot lands, not every render. */
  const deckColor: ColorMode = dayPnl == null || Math.abs(dayPnl) < 0.005 ? "neutral"
    : dayPnl > 0 ? "long" : "short";
  const deckIntensity: Intensity = openPositions.length ? "position" : "armed";
  const deckAccent = deckColor === "long" ? PROFIT : deckColor === "short" ? LOSS : NEUTRAL;
  const deckGlow = deckColor !== "neutral";

  return (
    <div className="grid gap-4">
      {/* ── row 1: profit deck (wide) | flow event + win streak stacked ── */}
      <div className="grid lg:grid-cols-[1.7fr_1fr] gap-4">
        {/* PROFIT DECK — live 3D core backdrop + glowing pnl-keyed edge -- */}
        <div className="relative rounded-xl overflow-hidden deck-accent"
          style={{
            border: `1px solid ${deckGlow ? deckAccent + "cc" : "#1e2a3a"}`,
            boxShadow: deckGlow
              ? `0 0 26px -8px ${deckAccent}99, inset 0 0 60px -30px ${deckAccent}`
              : "none",
          }}>
          {/* 3D scene — particle field only (the equity-ribbon "chart" is
              removed); colour-keyed to the day's pnl direction */}
          <div className="absolute inset-0">
            <ThreeCanvas prices={[]} colorMode={deckColor} intensity={deckIntensity} />
          </div>
          {/* legibility scrim: clearer at the headline, darker over dense text */}
          <div className="absolute inset-0 pointer-events-none bg-gradient-to-b
            from-[#070a0e]/70 via-[#070a0e]/55 to-[#070a0e]/90" />

          <div className="relative p-4" style={{ textShadow: "0 1px 6px rgba(0,0,0,0.9)" }}>

          {/* damage numbers: realized pnl floats up and fades like game text */}
          {dmgs.map((d) => (
            <span key={d.id}
              className={clsx(
                "dmg-float absolute z-20 mono font-bold pointer-events-none select-none",
                d.win ? "text-emerald-300" : "text-red-400")}
              style={{
                left: `${d.x}%`, top: "40%",
                fontSize: d.win ? "1.5rem" : "1.3rem",
                animationDelay: `${d.delay}s`, animationFillMode: "both",
                textShadow: d.win ? "0 0 14px #34d39999, 0 1px 0 #022c22"
                                  : "0 0 14px #f8717199, 0 1px 0 #450a0a",
              }}>
              {d.text}
            </span>
          ))}

          {/* masthead */}
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mb-4">
            <span className="flex items-center gap-2">
              <span className="pulse-icon inline-block w-[5px] h-[5px] rounded-full"
                style={{ background: PROFIT, boxShadow: `0 0 6px ${PROFIT}` }} />
              <span className="label">Profit Deck</span>
            </span>
            <span className="text-[10px] mono text-amber-400/90 uppercase tracking-widest">
              testnet paper · live mark
            </span>
            <div className="ml-auto"><Clock /></div>
          </div>

          {/* day pnl headline */}
          <div className="flex items-center gap-2 mb-1.5">
            <span className="text-[10px] uppercase tracking-[0.18em] text-slate-500">
              {month} — day pnl
            </span>
            <span className="flex items-center gap-1 border border-emerald-400/30 rounded-full px-2 py-px text-[8px] uppercase tracking-widest text-emerald-400">
              <span className="w-1 h-1 rounded-full bg-emerald-400 animate-pulse" /> live
            </span>
          </div>
          <div className="relative inline-block">
            {burst && (
              <span key={burst.key} className={clsx("flash-burst", burst.dir)} />
            )}
            <Odometer value={dayPnl} className="h-12 md:h-16" />
          </div>

          {/* stats row */}
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 mt-3 mono text-[11px] text-slate-400">
            <span>{nCloses} closes</span>
            <span>{nCloses ? `${Math.round((nWins / nCloses) * 100)}% win` : "— win"}</span>
            <span className={realized >= 0 ? "text-emerald-400/90" : "text-red-400/90"}>
              realized {realized >= 0 ? "+" : "-"}${Math.abs(realized).toFixed(Math.abs(realized) >= 0.01 || realized === 0 ? 2 : 4)}
            </span>
            <span>acct ${pos ? pos.account_value.toFixed(2) : "—"}</span>
          </div>

          {/* trade stack */}
          <div className="mt-4">
            <div className="text-[8px] uppercase tracking-[0.2em] text-slate-500 mb-1">
              trade stack — pnl per close
            </div>
            <TradeStack closes={stackPnls} />
          </div>
          </div>
        </div>

        {/* FLOW EVENT + SIGNAL CONSOLE (stacked right column) ---------- */}
        <div className="grid grid-rows-[auto_1fr] gap-4 min-h-0">
          <Led ev={ev} />
          {/* relative cell — the console is absolutely positioned to fill it so
              its (growing) line list never feeds height back into the grid */}
          <div className="relative min-h-[220px] lg:min-h-0">
            <SignalConsole positions={openPositions} pulse={pulse}
              fills={fills ?? undefined} />
          </div>
        </div>
      </div>

      {/* ── row 2: 24h pnl | open positions ─────────────────────────── */}
      <div className="grid lg:grid-cols-[1fr_1.7fr] gap-4">
        <div className="core-card p-4 min-w-0">
          <div className="flex items-baseline justify-between mb-2">
            <span className="label">24h pnl</span>
            <span className="mono text-[11px]">
              <span style={last24 >= 0 ? { color: PROFIT } : { color: LOSS }}>
                {last24 >= 0 ? "+" : "-"}${Math.abs(last24).toFixed(2)}
              </span>
              <span className="text-slate-600"> · peak +${peak24.toFixed(2)}</span>
            </span>
          </div>
          <PnlArea pts={pnl24} />
        </div>

        <div className="core-card p-4 min-w-0">
          <div className="flex flex-wrap items-center gap-2 mb-2">
            <span className="label">open positions</span>
            {/* band selector — shares the global Live-page band context with the
                Analysis Core toggle, so switching here swaps both in sync */}
            <div className="flex items-center gap-1 rounded-full border border-edge p-0.5">
              {(["scalp", "trend"] as Band[]).map((b) => (
                <button key={b} onClick={() => setActiveBand(b)}
                  className={clsx(
                    "px-2 py-0.5 rounded-full text-[9px] font-semibold uppercase tracking-wider transition",
                    activeBand === b
                      ? b === "scalp"
                        ? "bg-cyan-500/25 text-cyan-200"
                        : "bg-purple-500/25 text-purple-200"
                      : "text-slate-500 hover:text-slate-300")}>
                  {b}
                </button>
              ))}
            </div>
            <span className="px-1.5 rounded-full border border-edge text-[9px] mono text-slate-400">
              {bandPositions.length}
              {openPositions.length > bandPositions.length
                && <span className="text-slate-600"> / {openPositions.length}</span>}
            </span>
            <span className="ml-auto text-[8px] uppercase tracking-wider text-slate-600">
              upnl marked vs 2.5s live mids
            </span>
          </div>
          <PositionsPanel positions={bandPositions} mids={pulse?.coins}
            band={activeBand} />
        </div>
      </div>
    </div>
  );
}
