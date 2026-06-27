"use client";
/**
 * SIGNALS — the public face of the engine. A hero "spotlight" panel locks onto
 * the most active (or armed) coin and shows, in plain English, how the 6-model
 * ensemble forms a verdict and clears the entry gates; a compact verdict grid
 * below shows every other coin at a glance. Built for screen capture: refined,
 * legible motion, honest process metrics (no P&L claims), and click-to-pin so a
 * single coin can be held still for a screenshot.
 *
 * All data is real: /api/pulse (live L2 mids + book skew, 2.5s), /api/tickets
 * (the bot's own per-band model tickets + the verdict its SignalAggregator
 * computes), and /api/signal-history (every evaluation logged, traded or not).
 */
import { useEffect, useMemo, useRef, useState } from "react";
import clsx from "clsx";
import { usePoll, useActiveCoins, fmtTs } from "@/lib/api";

/* ----------------------------------------------------------------- types -- */
type Band = "scalp" | "trend";
type Ticket = { model: string; direction: string; confidence: number; meta: any };
type Verdict = {
  direction: string; confidence: number; agreement: number;
  long_votes: number; short_votes: number; flat_votes: number;
  regime: string; veto: boolean; would_fire: boolean;
  long_gate?: any; short_gate?: any;
};
type BandGate = { min_confidence: number; min_model_agreement: number };
type Pulse = {
  mid: number; spread_bps: number; imbalance: number;
  bid_szs: number[]; ask_szs: number[];
  bid_notional: number; ask_notional: number; ts: number;
};
type TicketsResp = {
  ts: number | null;
  coins: Record<string, { scalp: { tickets: Ticket[] }; trend: { tickets: Ticket[] } }>;
  verdicts?: Record<string, { scalp: Verdict; trend: Verdict }>;
  gates?: { scalp: BandGate; trend: BandGate };
  bands?: { scalp: boolean; trend: boolean };
};
type PulseResp = { coins: Record<string, Pulse>; hist: Record<string, number[]>; n: number };
type Position = {
  coin: string; band?: string; szi: number; entry_px: number;
  position_value: number; unrealized_pnl: number;
  leverage?: number | null; liq_px?: number | string | null;
};
type PositionsResp = { positions?: Position[] };

/* --------------------------------------------------------------- models -- */
/* constellation spokes — only the active directional ensemble (6). REGIME routes
 * weights but doesn't vote; the parked ML / LiqHeatmap slots aren't shown. */
const MODELS: [string, string][] = [
  ["TAModel", "TA"],
  ["MeanReversionModel", "REV"],
  ["FundingRateModel", "FUND"],
  ["OrderbookImbalanceModel", "BOOK"],
  ["VWAPModel", "VWAP"],
  ["MomentumModel", "MOM"],
];
const ACTIVE_N = MODELS.length; // 6

const MODEL_NAME: Record<string, string> = {
  TAModel: "Technical Analysis", MeanReversionModel: "Mean Reversion",
  FundingRateModel: "Funding Rate", OrderbookImbalanceModel: "Order-book Pressure",
  VWAPModel: "VWAP", MomentumModel: "Momentum", RegimeDetectorModel: "Regime",
};

const REGIME_LABEL: Record<string, string> = {
  TRENDING_UP: "TREND ▲", TRENDING_DOWN: "TREND ▼",
  RANGING: "RANGING", HIGH_VOL: "HIGH VOL", UNKNOWN: "—",
};

/* --------------------------------------------------------------- colours -- */
const dirHex = (d?: string) =>
  d === "LONG" ? "#34d399" : d === "SHORT" ? "#f87171" : "#64748b";
function dirText(d?: string) {
  if (d === "LONG") return "text-emerald-400";
  if (d === "SHORT") return "text-red-400";
  return "text-slate-400";
}
const arrow = (d?: string) => (d === "LONG" ? "▲" : d === "SHORT" ? "▼" : "·");
const fmtK = (v: number) =>
  v >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `$${(v / 1e3).toFixed(1)}k` : `$${v.toFixed(0)}`;
const fmtN = (v: number) => v.toLocaleString("en-US");

/* ======================================================================== */
/* growing mid-price sparkline — glow line + gradient fill                   */
function Spark({ pts, id, h = "h-16" }: { pts: number[]; id: string; h?: string }) {
  if (!pts || pts.length < 2) return <div className={h} />;
  const min = Math.min(...pts), max = Math.max(...pts);
  const r = max - min || max * 0.0001 || 1;
  const PAD = 3;
  const xy = (v: number, i: number): [number, number] =>
    [PAD + (i / (pts.length - 1)) * (100 - 2 * PAD), 30 - ((v - min) / r) * 24 - 3];
  const path = pts.map((v, i) => xy(v, i).join(",")).join(" ");
  const [lx, ly] = xy(pts[pts.length - 1], pts.length - 1);
  const up = pts[pts.length - 1] >= pts[0];
  const c = up ? "#34d399" : "#f87171";
  return (
    <svg viewBox="0 0 100 32" preserveAspectRatio="none" className={clsx("w-full", h)}>
      <defs>
        <linearGradient id={`sa-${id}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={c} stopOpacity="0.28" />
          <stop offset="100%" stopColor={c} stopOpacity="0" />
        </linearGradient>
      </defs>
      <polygon points={`${PAD},32 ${path} ${100 - PAD},32`} fill={`url(#sa-${id})`} />
      <polyline points={path} fill="none" stroke={c} strokeWidth="1.3"
        vectorEffect="non-scaling-stroke" opacity="0.9" />
      <polyline points={path} fill="none" stroke={c} strokeWidth="4"
        vectorEffect="non-scaling-stroke" opacity="0.12" />
      <circle cx={lx} cy={ly} r="2.2" fill={c}>
        <animate attributeName="opacity" values="1;0.2;1" dur="1.2s" repeatCount="indefinite" />
      </circle>
    </svg>
  );
}

/* ======================================================================== */
/* model constellation — the 6 active models beam votes into a verdict core   */
function Constellation({ tickets, verdict, size = 200 }: {
  tickets: Ticket[]; verdict?: Verdict; size?: number;
}) {
  const by: Record<string, Ticket> = {};
  for (const t of tickets) by[t.model] = t;
  const cx = 80, cy = 60, R = 42;
  const dir = verdict?.direction ?? "FLAT";
  const vc = dirHex(dir);
  return (
    <svg viewBox="0 0 160 120" width={size} height={size * 0.75} className="shrink-0">
      {MODELS.map(([name, abbr], i) => {
        const a = (i / MODELS.length) * Math.PI * 2 - Math.PI / 2;
        const x = cx + Math.cos(a) * R, y = cy + Math.sin(a) * R;
        const lx = cx + Math.cos(a) * (R + 12), ly = cy + Math.sin(a) * (R + 12);
        const t = by[name];
        const active = t?.direction === "LONG" || t?.direction === "SHORT";
        const c = active ? dirHex(t.direction) : "#334155";
        const conf = t ? Number(t.confidence) : 0;
        const why = t?.meta?.reason ?? t?.meta?.zone ?? t?.meta?.band ?? "";
        return (
          <g key={name}>
            <line x1={x} y1={y} x2={cx} y2={cy} stroke={c}
              strokeWidth={active ? 1.2 : 0.6}
              strokeDasharray={active ? "3 5" : undefined}
              className={active ? "beam" : undefined}
              opacity={active ? 0.35 + conf * 0.55 : 0.32} />
            <circle cx={x} cy={y} r={active ? 4.2 : 2.8} fill={c}>
              <title>{t
                ? `${MODEL_NAME[name] ?? name}: ${t.direction} conf ${conf.toFixed(2)}${why ? ` (${String(why).replace(/_/g, " ")})` : ""}`
                : `${MODEL_NAME[name] ?? name}: no ticket`}</title>
            </circle>
            <text x={lx} y={ly + 2.5} textAnchor="middle" fontSize="7"
              fill={active ? "#94a3b8" : "#475569"}
              style={{ fontFamily: "ui-monospace, monospace" }}>
              {abbr}
            </text>
          </g>
        );
      })}
      <circle cx={cx} cy={cy} r={13} fill="none" stroke={vc} strokeWidth="1">
        <animate attributeName="r" values="11;15;11" dur="2.4s" repeatCount="indefinite" />
        <animate attributeName="opacity" values="0.7;0.1;0.7" dur="2.4s" repeatCount="indefinite" />
      </circle>
      <circle cx={cx} cy={cy} r={10} fill="#0b0f14" stroke={vc} strokeWidth="1.4" />
      <text x={cx} y={cy + 3.6} textAnchor="middle" fontSize="11" fontWeight="bold" fill={vc}>
        {arrow(dir)}
      </text>
    </svg>
  );
}

/* ======================================================================== */
/* confidence arc gauge — fills toward 1.0, tick marks the entry gate         */
function ConfArc({ conf, gate, dir }: { conf: number; gate: number; dir?: string }) {
  const c = dirHex(dir);
  const v = Math.max(0, Math.min(1, conf));
  const cx = 60, cy = 56, r = 46;
  // semicircle path (left → over the top → right), pathLength normalised to 100
  const arc = `M ${cx - r},${cy} A ${r},${r} 0 0 1 ${cx + r},${cy}`;
  const gt = Math.PI * (1 - Math.min(1, gate)); // gate angle (rad)
  const gx1 = cx + Math.cos(gt) * (r - 6), gy1 = cy - Math.sin(gt) * (r - 6);
  const gx2 = cx + Math.cos(gt) * (r + 6), gy2 = cy - Math.sin(gt) * (r + 6);
  const cleared = conf >= gate && (dir === "LONG" || dir === "SHORT");
  return (
    <div className="relative shrink-0" style={{ width: 120, height: 78 }}>
      <svg viewBox="0 0 120 70" className="w-full h-full">
        <path d={arc} fill="none" stroke="#1e2a3a" strokeWidth="7" strokeLinecap="round" />
        <path d={arc} fill="none" stroke={c} strokeWidth="7" strokeLinecap="round"
          pathLength={100} strokeDasharray={`${v * 100} 100`}
          style={{ transition: "stroke-dasharray 0.7s ease, stroke 0.4s ease",
                   filter: cleared ? `drop-shadow(0 0 4px ${c})` : "none" }} />
        <line x1={gx1} y1={gy1} x2={gx2} y2={gy2} stroke="#e2e8f0" strokeWidth="1.6" opacity="0.85" />
      </svg>
      <div className="absolute inset-x-0 bottom-0 flex flex-col items-center">
        <span className={clsx("mono font-bold leading-none text-2xl", dirText(dir))}>
          {conf.toFixed(2)}
        </span>
        <span className="text-[9px] text-slate-500 mono mt-0.5">gate {gate.toFixed(2)}</span>
      </div>
    </div>
  );
}

/* quorum dots — N models agreeing vs the required quorum */
function Quorum({ agree, need, dir }: { agree: number; need: number; dir?: string }) {
  const fill = dir === "LONG" ? "bg-emerald-400" : dir === "SHORT" ? "bg-red-400" : "bg-slate-500";
  return (
    <div className="flex items-center gap-1.5">
      {Array.from({ length: Math.max(need, agree) }).map((_, i) => (
        <span key={i} className={clsx("w-2 h-2 rounded-full transition-colors duration-500",
          i < agree ? fill : "bg-edge")}
          style={i < agree && (dir === "LONG" || dir === "SHORT")
            ? { boxShadow: `0 0 5px ${dirHex(dir)}` } : undefined} />
      ))}
      <span className="mono text-[11px] text-slate-400 ml-1">{agree}/{need} agree</span>
    </div>
  );
}

function Votes({ v }: { v?: Verdict }) {
  return (
    <span className="mono text-xs">
      <span className="text-emerald-400">{v?.long_votes ?? 0}L</span>
      <span className="text-slate-600"> · </span>
      <span className="text-red-400">{v?.short_votes ?? 0}S</span>
      <span className="text-slate-600"> · </span>
      <span className="text-slate-400">{v?.flat_votes ?? 0}F</span>
    </span>
  );
}

/* ------------------------------------------------- plain-English verdict --- */
function statusLabel(v?: Verdict): { text: string; cls: string; armed: boolean } {
  if (!v) return { text: "ENGINE IDLE", cls: "text-slate-500 border-edge", armed: false };
  if (v.direction === "FLAT") return { text: "STANDING DOWN", cls: "text-slate-400 border-edge", armed: false };
  const side = v.direction === "LONG"
    ? "text-emerald-300 border-emerald-400/40" : "text-red-300 border-red-400/40";
  if (v.would_fire) return { text: `${v.direction} ARMED`, cls: side, armed: true };
  return { text: `${v.direction} LEAN`, cls: side, armed: false };
}

function narrative(coin: string, v: Verdict | undefined, g: BandGate): string {
  if (!v) return "Decision engine idle — the trading loop isn’t active right now.";
  const gQ = g.min_model_agreement, gC = g.min_confidence;
  if (v.direction === "FLAT")
    return `No directional consensus on ${coin}. The models disagree, so the engine is standing down rather than forcing a trade.`;
  if (v.would_fire)
    return `${v.agreement} of ${ACTIVE_N} models agree ${v.direction} on ${coin}. Weighted confidence ${v.confidence.toFixed(2)} clears the ${gC.toFixed(2)} gate — signal ARMED, sizing a trade.`;
  const blocks: string[] = [];
  if (v.agreement < gQ) {
    const n = gQ - v.agreement;
    blocks.push(`needs ${n} more model${n > 1 ? "s" : ""} to agree`);
  }
  if (v.confidence < gC) blocks.push(`confidence ${v.confidence.toFixed(2)} is under the ${gC.toFixed(2)} gate`);
  if (v.veto) blocks.push("funding veto");
  return `${v.direction} lean on ${coin}, but held back — ${blocks.join("; ") || "a risk gate is open"}.`;
}

/* ======================================================================== */
/* HERO SPOTLIGHT — the featured coin, big and legible                       */
function Hero({ coin, pulse, hist, tickets, verdict, gate, regime, position }: {
  coin: string; pulse?: Pulse; hist: number[]; tickets: Ticket[];
  verdict?: Verdict; gate: BandGate; regime?: string; position?: Position;
}) {
  const d = hist.length > 1 ? Math.sign(hist[hist.length - 1] - hist[hist.length - 2]) : 0;
  const chg = hist.length > 1 ? ((hist[hist.length - 1] - hist[0]) / hist[0]) * 100 : 0;
  const mins = Math.max(1, Math.round((hist.length * 2.5) / 60));
  const st = statusLabel(verdict);
  const dir = verdict?.direction ?? "FLAT";
  const skew = (pulse?.imbalance ?? 0) * 100;
  return (
    <div className={clsx("relative rounded-2xl p-5 md:p-6 overflow-hidden border bg-ink/40",
      st.armed ? "border-glow/40" : "border-edge/70")}>
      {/* ambient glow keyed to the verdict colour */}
      <div className="absolute -top-24 -right-16 w-80 h-80 rounded-full blur-3xl pointer-events-none opacity-20"
        style={{ background: dirHex(dir) }} />

      <div className="relative grid lg:grid-cols-[1.05fr_0.95fr] gap-5">
        {/* left: identity + price + spark + status */}
        <div className="min-w-0 flex flex-col">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-[10px] uppercase tracking-[0.2em] text-slate-500">Featured signal</span>
            {REGIME_LABEL[regime ?? ""] && (
              <span className="text-[9px] mono text-sky-300 border border-sky-300/30 rounded-full px-2 py-0.5">
                {REGIME_LABEL[regime ?? ""]}
              </span>
            )}
          </div>
          <div className="flex items-end gap-3 flex-wrap">
            <span className="text-4xl md:text-5xl font-bold tracking-tight">{coin}</span>
            <span key={`${coin}-${pulse?.mid}`}
              className={clsx("mono text-2xl md:text-3xl tabular-nums leading-none pb-1",
                d > 0 ? "flash-up" : d < 0 ? "flash-down" : "text-slate-200")}>
              {pulse?.mid != null ? pulse.mid.toLocaleString() : "—"}
            </span>
            <span className={clsx("mono text-sm tabular-nums pb-1.5",
              chg >= 0 ? "text-emerald-400/80" : "text-red-400/80")}>
              {chg >= 0 ? "+" : ""}{chg.toFixed(2)}% · {mins}m
            </span>
          </div>

          <div className="relative mt-3">
            <Spark pts={hist} id={`hero-${coin}`} h="h-20" />
            <div className="sweep" />
          </div>

          {/* book pressure one-liner */}
          {pulse && (
            <div className="flex items-center justify-between mono text-[11px] text-slate-500 mt-2">
              <span className="text-emerald-300/70">bids {fmtK(pulse.bid_notional)}</span>
              <span className="text-slate-300">
                {Math.abs(skew) < 5 ? "book balanced"
                  : skew > 0 ? `bid-heavy +${skew.toFixed(0)}%` : `ask-heavy ${skew.toFixed(0)}%`}
                <span className="text-slate-600"> · spread {pulse.spread_bps.toFixed(1)}bp</span>
              </span>
              <span className="text-red-300/70">asks {fmtK(pulse.ask_notional)}</span>
            </div>
          )}

          {/* big status pill */}
          <div className={clsx("mt-4 self-start inline-flex items-center gap-2 border rounded-xl px-4 py-2",
            st.cls, st.armed && "armed")}>
            <span className="text-xl font-bold">{arrow(dir)}</span>
            <span className="text-lg font-bold tracking-wide">{st.text}</span>
          </div>
        </div>

        {/* right: the decision — constellation + gauge + quorum + narrative */}
        <div className="min-w-0 flex flex-col gap-3 lg:border-l lg:border-edge/50 lg:pl-5">
          <div className="text-[10px] uppercase tracking-[0.2em] text-slate-500">
            {ACTIVE_N}-model ensemble → verdict
          </div>
          <div className="flex items-center justify-between gap-2">
            <Constellation tickets={tickets} verdict={verdict} size={210} />
            <ConfArc conf={verdict?.confidence ?? 0} gate={gate.min_confidence} dir={dir} />
          </div>
          <div className="flex items-center justify-between gap-2 border-t border-edge/50 pt-2.5">
            <Quorum agree={verdict?.agreement ?? 0} need={gate.min_model_agreement} dir={dir} />
            <Votes v={verdict} />
          </div>
          <p className="text-sm leading-relaxed text-slate-300">
            {narrative(coin, verdict, gate)}
          </p>
        </div>
      </div>

      {/* open position — rendered ONLY when a live position exists on this coin */}
      {position && <OpenPosition pos={position} mark={pulse?.mid} />}
    </div>
  );
}

/* live position strip — side, size, entry/mark, unrealized PnL, lev, liq */
function OpenPosition({ pos, mark }: { pos: Position; mark?: number }) {
  const side = pos.szi > 0 ? "LONG" : "SHORT";
  const sz = Math.abs(pos.szi);
  const c = dirHex(side);
  const pnl = pos.unrealized_pnl;
  const up = pnl >= 0;
  const roe = pos.position_value
    ? (pnl / (pos.position_value / (Number(pos.leverage) || 1))) * 100 : null;
  const liq = pos.liq_px != null && pos.liq_px !== "" ? Number(pos.liq_px) : null;
  const Cell = ({ label, children }: { label: string; children: React.ReactNode }) => (
    <div className="min-w-0">
      <div className="text-[9px] uppercase tracking-wider text-slate-500">{label}</div>
      <div className="mono text-sm text-slate-200 tabular-nums truncate">{children}</div>
    </div>
  );
  return (
    <div className="relative mt-4 border-t border-edge/50 pt-3">
      <div className="flex items-center justify-between mb-2.5">
        <span className="text-[10px] uppercase tracking-[0.2em] text-slate-500">Open position</span>
        <span className="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-bold tracking-wide"
          style={{ color: c, borderColor: `${c}66` }}>
          {arrow(side)} {side}
          {pos.leverage ? <span className="text-slate-500 font-normal">{Number(pos.leverage)}×</span> : null}
        </span>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-2.5 items-end">
        <div className="min-w-0">
          <div className="text-[9px] uppercase tracking-wider text-slate-500">Unrealized PnL</div>
          <div className={clsx("mono text-lg font-bold tabular-nums leading-none",
            up ? "text-emerald-400" : "text-red-400")}>
            {up ? "+" : "−"}${Math.abs(pnl).toLocaleString("en-US", { maximumFractionDigits: 2 })}
            {roe != null && (
              <span className="text-xs font-normal ml-1.5">{up ? "+" : ""}{roe.toFixed(1)}%</span>
            )}
          </div>
        </div>
        <Cell label="Size">{sz.toLocaleString("en-US", { maximumFractionDigits: 4 })}
          <span className="text-slate-500"> · {fmtK(pos.position_value)}</span></Cell>
        <Cell label="Entry">{pos.entry_px.toLocaleString()}</Cell>
        <Cell label={liq != null ? "Mark · Liq" : "Mark"}>
          {mark != null ? mark.toLocaleString() : "—"}
          {liq != null && <span className="text-red-400/70"> · {liq.toLocaleString()}</span>}
        </Cell>
      </div>
    </div>
  );
}

/* ======================================================================== */
/* COMPACT VERDICT CARD — one per non-featured coin; click to pin to hero    */
function MiniCard({ coin, pulse, hist, tickets, verdict, gate, onPin, pinned }: {
  coin: string; pulse?: Pulse; hist: number[]; tickets: Ticket[];
  verdict?: Verdict; gate: BandGate; onPin: () => void; pinned: boolean;
}) {
  const d = hist.length > 1 ? Math.sign(hist[hist.length - 1] - hist[hist.length - 2]) : 0;
  const st = statusLabel(verdict);
  const dir = verdict?.direction ?? "FLAT";
  const conf = verdict?.confidence ?? 0;
  const gC = gate.min_confidence;
  return (
    <button onClick={onPin}
      className={clsx(
        "text-left relative rounded-xl border p-3 bg-ink/30 transition-colors w-full overflow-hidden",
        st.armed ? "border-glow/40" : pinned ? "border-glow/30" : "border-edge/70 hover:border-edge")}>
      {pinned && <span className="absolute top-2 right-2 text-[9px] mono text-glow/80">📌 pinned</span>}
      <div className="flex items-baseline justify-between gap-2">
        <div className="flex items-baseline gap-2 min-w-0">
          <span className="font-bold">{coin}</span>
          <span key={`${coin}-${pulse?.mid}`}
            className={clsx("mono text-xs tabular-nums",
              d > 0 ? "flash-up" : d < 0 ? "flash-down" : "text-slate-300")}>
            {pulse?.mid != null ? pulse.mid.toLocaleString() : "—"}
          </span>
        </div>
        <span className={clsx("text-[11px] font-bold tracking-wide", dirText(dir), st.armed && "armed px-1 rounded")}>
          {arrow(dir)} {st.text}
        </span>
      </div>

      <div className="flex items-center gap-3 mt-2">
        <Constellation tickets={tickets} verdict={verdict} size={92} />
        <div className="flex-1 min-w-0 grid gap-1.5">
          {/* mini confidence bar with gate tick */}
          <div>
            <div className="flex justify-between text-[9px] text-slate-500 mono">
              <span>conf</span><span>{conf.toFixed(2)} / {gC.toFixed(2)}</span>
            </div>
            <div className="relative h-1.5 rounded-full bg-edge mt-0.5">
              <div className="absolute inset-y-0 left-0 rounded-full transition-all duration-700"
                style={{ width: `${Math.min(100, conf * 100)}%`, background: dirHex(dir) }} />
              <div className="absolute -top-[3px] h-3 w-px bg-slate-200/80"
                style={{ left: `${Math.min(100, gC * 100)}%` }} />
            </div>
          </div>
          <div className="flex items-center justify-between">
            <Quorum agree={verdict?.agreement ?? 0} need={gate.min_model_agreement} dir={dir} />
          </div>
          <Votes v={verdict} />
        </div>
      </div>
    </button>
  );
}

/* ======================================================================== */
/* honest stat chip                                                          */
function Stat({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <div className={clsx("rounded-xl border px-3.5 py-2.5 bg-ink/30 min-w-0",
      accent ? "border-glow/30" : "border-edge/70")}>
      <div className={clsx("mono text-xl md:text-2xl font-bold leading-none tabular-nums",
        accent ? "text-glow" : "text-slate-100")}>{value}</div>
      <div className="text-[10px] uppercase tracking-wider text-slate-500 mt-1">{label}</div>
    </div>
  );
}

/* ======================================================================== */
export default function SignalsPage() {
  const activeCoins = useActiveCoins();
  const coins = useMemo(() => activeCoins ?? [], [activeCoins]);
  const { data: live } = usePoll<TicketsResp>("/api/tickets", 4000);
  const { data: pulse } = usePoll<PulseResp>("/api/pulse", 2500);
  const { data: posData } = usePoll<PositionsResp>("/api/positions", 5000);
  // live positions keyed by coin — only coins with a non-zero size appear, so a
  // lookup miss means "no open position" and the hero strip stays hidden
  const posByCoin: Record<string, Position> = {};
  for (const p of posData?.positions ?? []) if (p.szi !== 0) posByCoin[p.coin] = p;

  // honest process metrics — every evaluation is logged (traded or not). Fix the
  // "today" window once at mount so the poll path is stable for the session.
  const dayStart = useMemo(() => {
    const d = new Date(); d.setUTCHours(0, 0, 0, 0); return d.toISOString();
  }, []);
  const { data: evalToday } = usePoll<{ count: number }>(
    `/api/signal-history/count?from_ts=${encodeURIComponent(dayStart)}`, 20000);
  const { data: armedToday } = usePoll<{ count: number }>(
    `/api/signal-history/count?cleared_only=true&from_ts=${encodeURIComponent(dayStart)}`, 20000);
  const { data: evalAll } = usePoll<{ count: number }>("/api/signal-history/count", 60000);

  // SCALP BAND RETIRED 2026-06-26 — trend-only. The band selector is gone; the
  // page always shows the 1h trend view.
  const effBand: Band = "trend";
  const gate: BandGate = live?.gates?.[effBand] ??
    { min_confidence: 0.55, min_model_agreement: 3 };

  const verdictOf = (c: string) => live?.verdicts?.[c]?.[effBand];
  const ticketsOf = (c: string) => live?.coins?.[c]?.[effBand]?.tickets ?? [];

  // priority order for the spotlight: armed → strongest directional lean → flat
  const rank = (c: string) => {
    const v = verdictOf(c);
    if (!v) return [3, 0] as const;
    if (v.would_fire) return [0, -v.confidence] as const;
    if (v.direction === "LONG" || v.direction === "SHORT") return [1, -v.confidence] as const;
    return [2, 0] as const;
  };
  const ordered = useMemo(() => [...coins].sort((a, b) => {
    const [ra, sa] = rank(a), [rb, sb] = rank(b);
    return ra !== rb ? ra - rb : sa - sb;
  }), [coins, live, effBand]); // eslint-disable-line react-hooks/exhaustive-deps

  // featured coin: pinned (user click) wins; otherwise auto-rotate the ordered
  // list every ~7s so a clip shows the whole book, but snap to any ARMED coin.
  const [pinned, setPinned] = useState<string | null>(null);
  const [featured, setFeatured] = useState<string>("");
  const orderedRef = useRef(ordered);
  orderedRef.current = ordered;
  const liveRef = useRef(live);
  liveRef.current = live;

  useEffect(() => {
    if (pinned) return;
    setFeatured((f) => (f && coins.includes(f) ? f : ordered[0] ?? ""));
    const id = setInterval(() => {
      const ord = orderedRef.current;
      if (!ord.length) return;
      // snap to an armed coin the moment one appears
      const armed = ord.find((c) => liveRef.current?.verdicts?.[c]?.[effBand]?.would_fire);
      setFeatured((cur) => {
        if (armed && armed !== cur) return armed;
        const i = ord.indexOf(cur);
        return ord[(i + 1) % ord.length];
      });
    }, 7000);
    return () => clearInterval(id);
  }, [pinned, coins, ordered, effBand]);

  // if the pinned/featured coin gets toggled off in Controls, recover
  useEffect(() => {
    if (pinned && coins.length && !coins.includes(pinned)) setPinned(null);
    if (featured && coins.length && !coins.includes(featured)) setFeatured(ordered[0] ?? "");
  }, [coins, pinned, featured, ordered]);

  const heroCoin = pinned ?? featured ?? ordered[0] ?? "";
  const restCoins = ordered.filter((c) => c !== heroCoin);
  const armedCount = coins.filter((c) => verdictOf(c)?.would_fire).length;

  /* ----------------------------------------------------------- loading -- */
  if (activeCoins === null) {
    return (
      <div className="grid gap-4">
        <div className="card animate-pulse h-10" />
        <div className="rounded-2xl border border-edge h-64 animate-pulse bg-black/30" />
        <div className="grid md:grid-cols-3 gap-3">
          {[0, 1, 2].map((i) => <div key={i} className="rounded-xl border border-edge h-28 animate-pulse bg-black/30" />)}
        </div>
      </div>
    );
  }

  return (
    <div className="grid gap-4">
      {/* ---------- masthead --------------------------------------------- */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="flex items-center gap-2">
          <span className="relative flex h-2 w-2">
            <span className="animate-ping absolute h-full w-full rounded-full bg-glow opacity-60" />
            <span className="relative rounded-full h-2 w-2 bg-glow" />
          </span>
          <h1 className="text-lg md:text-xl font-bold tracking-tight">
            HL <span className="text-glow" style={{ textShadow: "0 0 12px rgba(34,211,238,0.6)" }}>REAPER</span>
            <span className="text-slate-400 font-medium"> · Signal Engine</span>
          </h1>
        </div>
        <span className="text-[10px] mono uppercase tracking-wider text-slate-500 hidden sm:inline">
          {ACTIVE_N}-model ensemble · live L2 books · 1h trend
        </span>

        <div className="md:ml-auto flex items-center gap-2">
          {/* SCALP BAND RETIRED 2026-06-26 — trend-only; static band label. */}
          <span className="px-2.5 py-0.5 rounded-full text-[11px] font-semibold uppercase border bg-purple-500/20 text-purple-200 border-purple-500/40">
            trend band
          </span>
          {/* scan / pin state */}
          <button onClick={() => setPinned(null)}
            title={pinned ? "Resume auto-scan" : "Spotlight auto-scanning the book"}
            className={clsx("flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[10px] mono transition",
              pinned ? "border-glow/40 text-glow/90 hover:bg-glow/10" : "border-edge text-slate-400")}>
            <span className={clsx("h-1.5 w-1.5 rounded-full", pinned ? "bg-amber-400" : "bg-glow animate-pulse")} />
            {pinned ? "pinned · resume scan" : "auto-scan"}
          </button>
        </div>
      </div>

      {/* ---------- honest live-stats strip ------------------------------ */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2.5">
        <Stat label="active models" value={`${ACTIVE_N}`} />
        <Stat label="coins watched" value={`${coins.length}`} />
        <Stat label="book scans" value={pulse?.n != null ? fmtN(pulse.n) : "—"} />
        <Stat label="signals today" value={evalToday?.count != null ? fmtN(evalToday.count) : "—"} />
        <Stat label="armed today" value={armedToday?.count != null ? fmtN(armedToday.count) : "—"} accent />
        <Stat label="evaluated all-time" value={evalAll?.count != null ? fmtN(evalAll.count) : "—"} />
      </div>

      {/* ---------- hero spotlight --------------------------------------- */}
      {heroCoin ? (
        <Hero coin={heroCoin}
          pulse={pulse?.coins?.[heroCoin]}
          hist={pulse?.hist?.[heroCoin] ?? []}
          tickets={ticketsOf(heroCoin)}
          verdict={verdictOf(heroCoin)}
          gate={gate}
          regime={verdictOf(heroCoin)?.regime}
          position={posByCoin[heroCoin]} />
      ) : (
        <div className="rounded-2xl border border-edge p-10 text-center text-slate-500">
          no active coins — spinning up the book sampler…
        </div>
      )}

      {/* ---------- verdict grid ----------------------------------------- */}
      {restCoins.length > 0 && (
        <>
          <div className="flex items-center gap-2">
            <span className="label">The book</span>
            <span className="text-[10px] text-slate-600 mono">click a card to spotlight it</span>
            {live?.ts && (
              <span className="ml-auto text-[10px] text-slate-500 mono">
                {armedCount} armed · {fmtTs(live.ts)}
              </span>
            )}
          </div>
          <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
            {restCoins.map((c) => (
              <MiniCard key={c} coin={c}
                pulse={pulse?.coins?.[c]}
                hist={pulse?.hist?.[c] ?? []}
                tickets={ticketsOf(c)}
                verdict={verdictOf(c)}
                gate={gate}
                pinned={false}
                onPin={() => { setPinned(c); setFeatured(c); }} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
