"use client";
/**
 * ANALYSIS CORE — the Live page's heartbeat.
 *
 * Everything moving here is real: the bridge samples actual L2 order books
 * every 2.5s and this renders the stream — growing mid-price sparklines,
 * morphing depth bars, sliding imbalance needles, and a rolling decision
 * feed derived from tick-to-tick changes. No fake animation.
 */
import { useEffect, useRef, useState } from "react";
import clsx from "clsx";
import { usePoll } from "@/lib/api";

type CoinPulse = {
  mid: number; spread_bps: number; imbalance: number;
  bid_szs: number[]; ask_szs: number[];
  bid_notional: number; ask_notional: number; ts: number;
};

/* ---------- growing mid-price sparkline (SVG, glow + live dot) ---------- */
function Spark({ pts }: { pts: number[] }) {
  if (!pts || pts.length < 2) return <div className="h-9" />;
  const min = Math.min(...pts), max = Math.max(...pts);
  const r = max - min || max * 0.0001 || 1;
  const xy = (v: number, i: number): [number, number] =>
    [(i / (pts.length - 1)) * 100, 32 - ((v - min) / r) * 26 - 3];
  const path = pts.map((v, i) => xy(v, i).join(",")).join(" ");
  const [lx, ly] = xy(pts[pts.length - 1], pts.length - 1);
  const up = pts[pts.length - 1] >= pts[0];
  const c = up ? "#34d399" : "#f87171";
  return (
    <svg viewBox="0 0 100 32" preserveAspectRatio="none" className="w-full h-9">
      <polyline points={path} fill="none" stroke={c} strokeWidth="1.4"
        vectorEffect="non-scaling-stroke" opacity="0.9" />
      <polyline points={path} fill="none" stroke={c} strokeWidth="4"
        vectorEffect="non-scaling-stroke" opacity="0.12" />
      <circle cx={lx} cy={ly} r="2.4" fill={c}>
        <animate attributeName="opacity" values="1;0.2;1" dur="1.2s"
          repeatCount="indefinite" />
      </circle>
    </svg>
  );
}

/* ---------- mirrored order-book depth bars, morphing between ticks ------ */
function Depth({ bids, asks }: { bids: number[]; asks: number[] }) {
  const mx = Math.max(...bids, ...asks, 1e-9);
  return (
    <div className="flex items-end gap-[3px] h-8">
      {[...bids].reverse().map((s, i) => (
        <div key={`b${i}`}
          className="w-[7px] rounded-sm bg-emerald-400/70 transition-all duration-700 ease-out"
          style={{ height: `${Math.max(8, (s / mx) * 100)}%` }} />
      ))}
      <div className="w-px h-full bg-edge mx-0.5" />
      {asks.map((s, i) => (
        <div key={`a${i}`}
          className="w-[7px] rounded-sm bg-red-400/70 transition-all duration-700 ease-out"
          style={{ height: `${Math.max(8, (s / mx) * 100)}%` }} />
      ))}
    </div>
  );
}

/* ---------- imbalance needle sliding on a gradient track ---------------- */
function Imbalance({ v }: { v: number }) {
  const pct = ((v + 1) / 2) * 100;
  return (
    <div className="relative h-2 rounded-full bg-gradient-to-r from-red-500/50 via-edge to-emerald-500/50">
      <div
        className="absolute -top-[3px] w-[3px] h-3.5 rounded bg-white shadow-[0_0_6px_#22d3ee] transition-all duration-700 ease-out"
        style={{ left: `calc(${pct}% - 1px)` }} />
    </div>
  );
}

/* ---------- the spinning core ------------------------------------------- */
function Core({ ticks, books }: { ticks: number; books: number }) {
  return (
    <div className="relative w-36 h-36 mx-auto">
      <div className="absolute inset-0 rounded-full core-ring" />
      <div className="absolute inset-[5px] rounded-full core-ring opacity-50"
        style={{ animationDirection: "reverse", animationDuration: "4.6s" }} />
      <div className="absolute inset-[12px] rounded-full bg-ink border border-edge flex flex-col items-center justify-center">
        <div className="text-[9px] label">analyzing</div>
        <div className="text-xl font-bold mono text-glow tabular-nums">{ticks}</div>
        <div className="text-[9px] text-slate-500">book scans</div>
        <div className="text-[10px] mono text-slate-400 tabular-nums">{books} levels</div>
      </div>
    </div>
  );
}

/* ======================================================================== */
export default function AnalysisCore() {
  const { data } = usePoll<{ coins: Record<string, CoinPulse>;
    hist: Record<string, number[]>; n: number }>("/api/pulse", 2500);
  const prev = useRef<Record<string, number>>({});
  const [feed, setFeed] = useState<{ id: number; t: string; line: string; dir: number }[]>([]);
  const feedId = useRef(0);

  useEffect(() => {
    if (!data?.coins) return;
    const t = new Date().toLocaleTimeString("en-US", { hour12: false });
    const fresh: typeof feed = [];
    for (const [coin, p] of Object.entries(data.coins)) {
      const last = prev.current[coin];
      const d = last == null ? 0 : Math.sign(p.mid - last);
      prev.current[coin] = p.mid;
      fresh.push({
        id: feedId.current++,
        t,
        dir: d,
        line: `${coin.padEnd(4)} ${p.mid.toLocaleString()} ${d > 0 ? "▲" : d < 0 ? "▼" : "·"} imb ${p.imbalance >= 0 ? "+" : ""}${p.imbalance.toFixed(2)} spr ${p.spread_bps.toFixed(1)}bp`,
      });
    }
    setFeed((f) => [...fresh, ...f].slice(0, 14));
  }, [data]);

  const coins = Object.entries(data?.coins ?? {});
  const totalLevels = coins.length * 20;

  return (
    <div className="card relative overflow-hidden">
      <div className="absolute -top-20 -right-20 w-64 h-64 rounded-full bg-glow/5 blur-3xl pointer-events-none" />
      <div className="flex items-center gap-2 mb-3">
        <span className="label">Analysis Core</span>
        <span className="relative flex h-1.5 w-1.5">
          <span className="animate-ping absolute h-full w-full rounded-full bg-glow opacity-60" />
          <span className="relative rounded-full h-1.5 w-1.5 bg-glow" />
        </span>
        <span className="text-[10px] text-slate-500">
          live L2 sampling · 2.5s cadence · real order flow
        </span>
      </div>

      <div className="grid md:grid-cols-[180px_1fr] gap-4 items-start">
        {/* left: spinning core + decision feed */}
        <div className="grid gap-3">
          <Core ticks={data?.n ?? 0} books={totalLevels} />
          <div className="mono text-[10px] leading-relaxed h-40 overflow-hidden relative">
            <div className="absolute inset-x-0 bottom-0 h-10 bg-gradient-to-t from-panel to-transparent z-[1]" />
            {feed.map((f) => (
              <div key={f.id} className={clsx("feed-in whitespace-nowrap",
                f.dir > 0 ? "text-emerald-300/90" : f.dir < 0 ? "text-red-300/90" : "text-slate-400")}>
                <span className="text-slate-600">{f.t}</span> {f.line}
              </div>
            ))}
            {!feed.length && <div className="text-slate-600">waiting for first book scan…</div>}
          </div>
        </div>

        {/* right: per-coin live rows */}
        <div className="grid gap-2 min-w-0">
          {coins.map(([coin, p]) => {
            const hist = data?.hist?.[coin] ?? [];
            const d = hist.length > 1 ? Math.sign(hist[hist.length - 1] - hist[hist.length - 2]) : 0;
            return (
              <div key={coin}
                className="grid grid-cols-2 md:grid-cols-[64px_1fr_auto_120px] gap-x-3 gap-y-1 items-center border border-edge/60 rounded-lg px-3 py-2 min-w-0">
                <div>
                  <div className="font-bold text-sm">{coin}</div>
                  <div key={`${coin}-${p.mid}`}
                    className={clsx("mono text-xs tabular-nums",
                      d > 0 ? "flash-up" : d < 0 ? "flash-down" : "text-slate-300")}>
                    {p.mid.toLocaleString()}
                  </div>
                </div>
                <div className="min-w-0"><Spark pts={hist} /></div>
                <div className="hidden md:block"><Depth bids={p.bid_szs} asks={p.ask_szs} /></div>
                <div className="col-span-2 md:col-span-1">
                  <div className="flex justify-between text-[9px] text-slate-500 mb-1">
                    <span>sell wall</span>
                    <span className="mono">{p.spread_bps.toFixed(1)}bp</span>
                    <span>buy wall</span>
                  </div>
                  <Imbalance v={p.imbalance} />
                </div>
              </div>
            );
          })}
          {!coins.length && (
            <div className="text-slate-500 text-sm py-8 text-center">
              spinning up book sampler…
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
