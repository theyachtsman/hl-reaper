"use client";
/**
 * AnalysisCoreSection — the Live page's per-coin decision grid, rendered as
 * live Three.js "analysis cores". Replaces the old SVG AnalysisCore. Polls the
 * same data sources (/api/tickets for verdicts+gates, /api/pulse for live mids +
 * book-scan count, /api/positions for open trades) and fans them out to one
 * CoinCard3D per active coin, with a scrolling tick tape underneath. The Signals
 * page keeps the legacy detailed view.
 */
import { useEffect, useRef, useState } from "react";
import clsx from "clsx";
import { usePoll, useActiveCoins } from "@/lib/api";
import { useBandStore } from "@/lib/store";
import CoinCard3D, { Verdict } from "@/components/CoinCard3D";

type Ticket = { model: string; direction: string; confidence: number; meta: any };
type BandPack = { tickets: Ticket[] };
type BandGate = {
  min_confidence: number; min_model_agreement: number;
  structural_gates_enabled?: boolean;
  long_structural_gate_enabled?: boolean;
  short_structural_gate_enabled?: boolean;
};
type TicketsResp = {
  ts: number | null;
  coins: Record<string, { scalp: BandPack; trend: BandPack }>;
  verdicts?: Record<string, { scalp: Verdict; trend: Verdict }>;
  gates?: { scalp: BandGate; trend: BandGate };
  bands?: { scalp: boolean; trend: boolean };
};
type Band = "scalp" | "trend";
type Pulse = {
  mid: number; imbalance: number; spread_bps?: number;
  bid_szs?: number[]; ask_szs?: number[];
  bid_notional?: number; ask_notional?: number;
};
type PulseResp = { coins: Record<string, Pulse>; n: number };
type PositionsResp = { positions?: { coin: string; szi: number }[] };

export default function AnalysisCoreSection() {
  const { data: live } = usePoll<TicketsResp>("/api/tickets", 4000);
  const { data: pulse } = usePoll<PulseResp>("/api/pulse", 2500);
  const { data: pos } = usePoll<PositionsResp>("/api/positions", 5000);
  const activeCoins = useActiveCoins();

  const coins = activeCoins ?? [];
  // shared global band context — the same toggle drives Open Positions + the
  // chart's default timeframe on the Live page (see useBandStore).
  const band = useBandStore((s) => s.activeBand);
  const setBand = useBandStore((s) => s.setActiveBand);
  const gates = live?.gates?.[band];
  const gQ = gates?.min_model_agreement ?? 3;
  const gC = gates?.min_confidence ?? 0.4;
  const bandsEnabled = live?.bands ?? { scalp: true, trend: true };
  const setEnabledBands = useBandStore((s) => s.setEnabledBands);

  // Publish enabled bands to the shared store, and if the active band gets
  // disabled in Controls, fall back to the still-enabled band so the whole Live
  // page (cores, Open Positions, chart timeframe — all driven by useBandStore)
  // reflects the real single-band trading mode.
  useEffect(() => {
    setEnabledBands(bandsEnabled);
    if (!bandsEnabled[band]) {
      const other: Band = band === "scalp" ? "trend" : "scalp";
      if (bandsEnabled[other]) setBand(other);
    }
  }, [bandsEnabled.scalp, bandsEnabled.trend, band, setBand, setEnabledBands]);

  // per-band structural-gate enabled flags (trend band has no structural gates)
  const gcfg = live?.gates?.scalp;
  const gatesEnabled = band === "scalp"
    ? { long: !!(gcfg?.structural_gates_enabled && gcfg?.long_structural_gate_enabled),
        short: !!(gcfg?.structural_gates_enabled && gcfg?.short_structural_gate_enabled) }
    : { long: false, short: false };

  const posByCoin: Record<string, "LONG" | "SHORT"> = {};
  for (const p of pos?.positions ?? []) {
    if (p.szi !== 0) posByCoin[p.coin] = p.szi > 0 ? "LONG" : "SHORT";
  }
  const armed = coins.filter(
    (c) => live?.verdicts?.[c]?.[band]?.would_fire).length;

  // ---- scrolling tape: latest mid deltas + book skew, newest first -----
  const prev = useRef<Record<string, number>>({});
  const feedId = useRef(0);
  const [feed, setFeed] = useState<{ id: number; t: string; line: string; dir: number }[]>([]);
  useEffect(() => {
    if (!pulse?.coins) return;
    const t = new Date().toLocaleTimeString("en-US", { hour12: true });
    const fresh: typeof feed = [];
    for (const [coin, p] of Object.entries(pulse.coins)) {
      if (activeCoins && !activeCoins.includes(coin)) continue;
      const last = prev.current[coin];
      const dd = last == null ? 0 : Math.sign(p.mid - last);
      prev.current[coin] = p.mid;
      fresh.push({
        id: feedId.current++, t, dir: dd,
        line: `${coin} ${dd > 0 ? "▲" : dd < 0 ? "▼" : "—"} ${p.mid.toLocaleString()} skew ${p.imbalance >= 0 ? "+" : ""}${(p.imbalance * 100).toFixed(0)}%`,
      });
    }
    setFeed((f) => [...fresh, ...f].slice(0, 12));
  }, [pulse, activeCoins]);

  return (
    <div className="card relative overflow-hidden">
      <div className="absolute -top-20 -right-20 w-64 h-64 rounded-full bg-glow/5 blur-3xl pointer-events-none" />

      {/* header */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 mb-4">
        <span className="relative flex h-1.5 w-1.5">
          <span className="animate-ping absolute h-full w-full rounded-full bg-glow opacity-60" />
          <span className="relative rounded-full h-1.5 w-1.5 bg-glow" />
        </span>
        <span className="label">Analysis Core</span>
        <span className="text-[10px] mono uppercase tracking-wider text-slate-500">
          {band === "scalp" ? "5m scalp" : "1h trend"} · 6-model ensemble
          {bandsEnabled.scalp !== bandsEnabled.trend && (
            <span className="text-amber-400/80"> · single-band mode</span>
          )}
        </span>
        {/* band selector — only bands enabled in Controls are shown, so a
            disabled band disappears and a single enabled band reads as the mode. */}
        <div className="flex items-center gap-1 rounded-full border border-edge p-0.5">
          {(["scalp", "trend"] as Band[]).filter((b) => bandsEnabled[b]).map((b) => (
            <button key={b} onClick={() => setBand(b)}
              className={clsx(
                "px-2.5 py-0.5 rounded-full text-[10px] font-semibold uppercase transition",
                band === b
                  ? b === "scalp"
                    ? "bg-cyan-500/25 text-cyan-200"
                    : "bg-purple-500/25 text-purple-200"
                  : "text-slate-500 hover:text-slate-300")}>
              {b}
            </button>
          ))}
        </div>
        <div className="md:ml-auto flex flex-wrap items-center gap-2">
          <span className="border border-edge rounded-full px-2.5 py-1 text-[10px] mono text-slate-300">
            {(pulse?.n ?? 0).toLocaleString()} scans
          </span>
          <span className="border border-edge rounded-full px-2.5 py-1 text-[10px] mono text-slate-300">
            {coins.length * 20} levels
          </span>
          <span className="border border-glow/30 rounded-full px-2.5 py-1 text-[10px] mono text-glow/90"
            title="A trade only fires when at least this many models agree AND weighted confidence clears the gate.">
            ≥{gQ}/6 · ≥{gC}
          </span>
          <span className="border border-edge rounded-full px-2.5 py-1 text-[10px] mono text-slate-300">
            {armed} armed
          </span>
        </div>
      </div>

      {/* per-coin 3D cores */}
      <div className="grid gap-3 lg:grid-cols-3">
        {activeCoins === null
          ? [0, 1, 2].map((i) => (
              <div key={i} className="rounded-xl border border-edge h-[224px] animate-pulse bg-black/30" />
            ))
          : coins.map((coin) => (
              <CoinCard3D key={coin} coin={coin}
                mid={pulse?.coins?.[coin]?.mid}
                depth={pulse?.coins?.[coin]}
                verdict={live?.verdicts?.[coin]?.[band]}
                tickets={live?.coins?.[coin]?.[band]?.tickets ?? []}
                position={posByCoin[coin] ?? null}
                gates={gates} gatesEnabled={gatesEnabled} />
            ))}
        {activeCoins !== null && !coins.length && (
          <div className="text-slate-500 text-sm py-10 text-center lg:col-span-3">
            no active coins
          </div>
        )}
      </div>

      {/* tape */}
      <div className="flex items-center gap-2 mt-3 pt-2 border-t border-edge/60 overflow-hidden">
        <span className="text-[9px] uppercase tracking-wider text-slate-500 shrink-0">tape</span>
        <div className="relative flex-1 min-w-0 overflow-hidden">
          <div className="absolute inset-y-0 right-0 w-16 bg-gradient-to-l from-panel to-transparent z-[1]" />
          <div className="flex gap-4 whitespace-nowrap mono text-[10px]">
            {feed.map((f) => (
              <span key={f.id} className={clsx("tape-in shrink-0",
                f.dir > 0 ? "text-emerald-300/90" : f.dir < 0 ? "text-red-300/90" : "text-slate-400")}>
                <span className="text-slate-600">{f.t}</span> {f.line}
              </span>
            ))}
            {!feed.length && <span className="text-slate-600">waiting for first book scan…</span>}
          </div>
        </div>
      </div>
    </div>
  );
}
