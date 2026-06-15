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
import { useEffect, useRef, useState } from "react";
import clsx from "clsx";
import { usePoll } from "@/lib/api";
import { useStatusStore } from "@/lib/store";

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
    : value > 0 ? "#34d399" : "#f87171";
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

/* ---------- ticking UTC clock (isolated so it re-renders alone) --------- */
function Clock() {
  const [now, setNow] = useState("");
  useEffect(() => {
    const f = () => setNow(new Date().toISOString().slice(11, 19));
    f();
    const id = setInterval(f, 1000);
    return () => clearInterval(id);
  }, []);
  return (
    <span className="mono text-sm text-slate-300 tabular-nums">
      {now} <span className="text-[9px] text-slate-500">UTC</span>
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

/* ---------- open positions, live-marked against the 2.5s pulse ---------- */
function PositionsPanel({ positions, mids }: {
  positions: any[]; mids: Record<string, CoinPulse> | undefined;
}) {
  if (!positions.length)
    return <div className="h-24 flex items-center justify-center text-[10px] text-slate-600">
      no open positions — gates armed, waiting for a signal…</div>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full mono text-[11px] min-w-[500px]">
        <thead className="text-left text-slate-500 uppercase text-[8px] tracking-[0.15em]">
          <tr>
            <th className="py-1 pr-2">coin</th><th>side</th><th>size</th>
            <th>entry</th><th>mark</th><th>value</th><th>upnl</th>
            <th>lev</th><th>liq</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => {
            const mid = mids?.[p.coin]?.mid;
            const upnl = mid ? p.szi * (mid - p.entry_px) : p.unrealized_pnl;
            const value = mid ? Math.abs(p.szi) * mid : p.position_value;
            const long = p.szi > 0;
            return (
              <tr key={p.coin} className="border-t border-edge/60">
                <td className="py-1.5 pr-2 font-bold text-slate-200">{p.coin}</td>
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
                <td className={upnl >= 0 ? "text-emerald-400" : "text-red-400"}>
                  {upnl >= 0 ? "+" : "-"}${Math.abs(upnl).toFixed(2)}
                </td>
                <td>{p.leverage ?? "—"}x</td>
                <td className="text-slate-500">{p.liq_px ?? "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/* ---------- LED flow-event panel ----------------------------------------- */
type FlowEvent = { text: string; ts: number };
function Led({ ev }: { ev: FlowEvent | null }) {
  const liveish = ev && Date.now() - ev.ts < 30_000;
  return (
    <div className="bg-black/70 border border-sky-400/20 rounded-lg px-3 py-2 min-w-[210px]">
      <div className="text-[8px] uppercase tracking-[0.2em] text-sky-500/70 mb-0.5">
        flow event
      </div>
      <div key={ev?.ts ?? 0}
        className={clsx("mono text-xs tracking-widest uppercase led-in",
          liveish ? "text-sky-300" : "text-slate-600")}
        style={liveish ? { textShadow: "0 0 8px #38bdf8aa" } : undefined}>
        {liveish ? ev!.text : "scanning order flow…"}
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

  /* day PnL, re-marked live: swap the 5s-poll uPnL for one computed from
     the freshest 2.5s mids so the odometer moves with the market */
  let dayPnl: number | null = null;
  if (pos && status?.day_open_equity) {
    const polled = pos.positions.reduce((s, p) => s + p.unrealized_pnl, 0);
    const live = pos.positions.reduce((s, p) => {
      const mid = pulse?.coins?.[p.coin]?.mid;
      return s + (mid ? p.szi * (mid - p.entry_px) : p.unrealized_pnl);
    }, 0);
    dayPnl = pos.account_value - polled + live - status.day_open_equity;
  }

  /* fills-derived stats */
  const recent = fills?.recent ?? [];           // newest first
  const closes = recent.filter((f) => f.closed_pnl !== 0);
  let streak = 0;
  for (const f of closes) { if (f.closed_pnl > 0) streak++; else break; }
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

  return (
    <div className="card relative overflow-hidden">
      <div className="absolute -top-24 -left-24 w-72 h-72 rounded-full bg-glow/5 blur-3xl pointer-events-none" />

      {/* damage numbers: realized pnl floats up and fades like game text */}
      {dmgs.map((d) => (
        <span key={d.id}
          className={clsx(
            "dmg-float absolute z-20 mono font-bold pointer-events-none select-none",
            d.win ? "text-emerald-300" : "text-red-400")}
          style={{
            left: `${d.x}%`, top: "34%",
            fontSize: d.win ? "1.5rem" : "1.3rem",
            animationDelay: `${d.delay}s`, animationFillMode: "both",
            textShadow: d.win ? "0 0 14px #34d39999, 0 1px 0 #022c22"
                              : "0 0 14px #f8717199, 0 1px 0 #450a0a",
          }}>
          {d.text}
        </span>
      ))}

      {/* masthead */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mb-3">
        <span className="label">Profit Deck</span>
        <span className="text-[10px] mono text-amber-400/90 uppercase tracking-widest">
          testnet paper · live mark
        </span>
        <div className="ml-auto"><Clock /></div>
      </div>

      {/* row 1: big odometer + stats | LED panel | streak ring */}
      <div className="flex flex-wrap items-center gap-x-6 gap-y-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2 mb-1.5">
            <span className="text-[10px] uppercase tracking-[0.18em] text-slate-500">
              {month} — day pnl
            </span>
            <span className="flex items-center gap-1 border border-emerald-400/30 rounded-full px-2 py-px text-[8px] uppercase tracking-widest text-emerald-400">
              <span className="w-1 h-1 rounded-full bg-emerald-400 animate-pulse" /> live
            </span>
          </div>
          <Odometer value={dayPnl} className="h-10 md:h-14" />
          <div className="flex flex-wrap items-center gap-x-3 mt-2 mono text-[10px] text-slate-400">
            <span>{nCloses} closes</span>
            <span>{nCloses ? `${Math.round((nWins / nCloses) * 100)}% win` : "— win"}</span>
            <span className={realized >= 0 ? "text-emerald-400/90" : "text-red-400/90"}>
              realized {realized >= 0 ? "+" : "-"}${Math.abs(realized).toFixed(Math.abs(realized) >= 0.01 || realized === 0 ? 2 : 4)}
            </span>
            <span>acct ${pos ? pos.account_value.toFixed(2) : "—"}</span>
          </div>
          <div className="mt-2">
            <div className="text-[8px] uppercase tracking-[0.2em] text-slate-600 mb-0.5">
              trade stack — pnl per close
            </div>
            <TradeStack closes={stackPnls} />
          </div>
        </div>

        <div className="flex-1 min-w-[210px] max-w-[360px] md:ml-auto"><Led ev={ev} /></div>

        <div className="relative w-20 h-20 shrink-0">
          <div className="absolute inset-0 rounded-full core-ring" style={{ animationDuration: "5.5s" }} />
          <div className="absolute inset-[4px] rounded-full bg-ink border border-edge flex flex-col items-center justify-center">
            <span className="text-2xl font-bold mono text-glow tabular-nums">{streak}</span>
            <span className="text-[7px] uppercase tracking-[0.18em] text-slate-500">win streak</span>
          </div>
        </div>
      </div>

      {/* row 2: 24h pnl curve | open positions */}
      <div className="grid md:grid-cols-[1fr_1.6fr] gap-3 mt-4">
        <div className="border border-edge/70 rounded-xl p-3 min-w-0">
          <div className="flex items-baseline justify-between mb-1">
            <span className="text-[9px] uppercase tracking-wider text-slate-500">24h pnl</span>
            <span className="mono text-[10px]">
              <span className={last24 >= 0 ? "text-emerald-400" : "text-red-400"}>
                {last24 >= 0 ? "+" : "-"}${Math.abs(last24).toFixed(2)}
              </span>
              <span className="text-slate-600"> · peak +${peak24.toFixed(2)}</span>
            </span>
          </div>
          <PnlArea pts={pnl24} />
        </div>

        <div className="border border-edge/70 rounded-xl p-3 min-w-0">
          <div className="flex flex-wrap items-center gap-2 mb-1">
            <span className="text-[9px] uppercase tracking-wider text-slate-500">open positions</span>
            <span className="px-1.5 rounded-full border border-edge text-[9px] mono text-slate-400">
              {openPositions.length}
            </span>
            <span className="ml-auto text-[8px] uppercase tracking-wider text-slate-600">
              upnl marked vs 2.5s live mids
            </span>
          </div>
          <PositionsPanel positions={openPositions} mids={pulse?.coins} />
        </div>
      </div>
    </div>
  );
}
