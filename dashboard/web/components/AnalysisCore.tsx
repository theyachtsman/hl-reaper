"use client";
/**
 * ANALYSIS CORE — the Live page's heartbeat, rebuilt as a decision console.
 *
 * Everything moving here is real: the bridge samples actual L2 order books
 * every 2.5s, and the bot publishes its live model tickets each loop. The
 * bridge re-runs the bot's own SignalAggregator over those tickets, so the
 * verdict, votes and gate distances shown here are exactly what the trading
 * loop computes — per coin: price + regime, order-book pressure, a model
 * constellation beaming votes into a verdict core, and the two entry gates
 * (quorum + weighted confidence) the signal must clear to become a trade.
 */
import { useEffect, useRef, useState } from "react";
import clsx from "clsx";
import { usePoll, useActiveCoins } from "@/lib/api";

type CoinPulse = {
  mid: number; spread_bps: number; imbalance: number;
  bid_szs: number[]; ask_szs: number[];
  bid_notional: number; ask_notional: number; ts: number;
};
type TicketT = { model: string; direction: string; confidence: number; meta: any };
type Verdict = {
  direction: string; confidence: number; agreement: number;
  long_votes: number; short_votes: number; flat_votes: number;
  regime: string; veto: boolean; would_fire: boolean;
};
type GatesT = { min_confidence: number; min_model_agreement: number };
type TicketsResp = {
  ts: number | null;
  coins: Record<string, TicketT[]>;
  verdicts?: Record<string, Verdict>;
  gates?: GatesT;
};

/* constellation slots (REGIME routes weights only, not shown). Two slots are
 * permanently INACTIVE — kept visible so the picture is honest and so a future
 * different-target model has a home, but clearly marked as non-voting. */
const MODELS: [string, string][] = [
  ["TAModel", "TA"],
  ["MLForecastModel", "ML"],
  ["MeanReversionModel", "REV"],
  ["FundingRateModel", "FUND"],
  ["OrderbookImbalanceModel", "BOOK"],
  ["VWAPModel", "VWAP"],
  ["MomentumModel", "MOM"],
  ["LiquidationHeatmapModel", "LIQ"],
];
/* permanently FLAT / zero-weight — see docs/ml_retrain_report.md (ML) and the
 * microstructure backtest (LiqHeatmap). Not counted in the quorum denominator. */
const INACTIVE = new Set(["MLForecastModel", "LiquidationHeatmapModel"]);
const INACTIVE_NOTE: Record<string, string> = {
  MLForecastModel: "ML Forecast — no model (direction classification not viable)",
  LiquidationHeatmapModel: "Liquidation Heatmap — inactive (100% FLAT on live data)",
};
const ACTIVE_DIRECTIONAL = MODELS.filter(([m]) => !INACTIVE.has(m)).length; // 5

const dirHex = (d?: string) =>
  d === "LONG" ? "#34d399" : d === "SHORT" ? "#f87171" : "#475569";

const fmtK = (v: number) =>
  v >= 1e6 ? `$${(v / 1e6).toFixed(1)}M`
  : v >= 1e3 ? `$${(v / 1e3).toFixed(1)}k`
  : `$${v.toFixed(0)}`;

const REGIME_LABEL: Record<string, string> = {
  TRENDING_UP: "TREND ▲", TRENDING_DOWN: "TREND ▼",
  RANGING: "RANGING", HIGH_VOL: "HIGH VOL",
};

/* ---------- growing mid-price sparkline (glow line + gradient area) ----- */
function Spark({ pts, id }: { pts: number[]; id: string }) {
  if (!pts || pts.length < 2) return <div className="h-12" />;
  const min = Math.min(...pts), max = Math.max(...pts);
  const r = max - min || max * 0.0001 || 1;
  // slight horizontal inset (viewBox-x units) so the stroke + pulsing end-dot
  // never clip against the SVG edges — stays effectively full width
  const PAD = 3;
  const xy = (v: number, i: number): [number, number] =>
    [PAD + (i / (pts.length - 1)) * (100 - 2 * PAD),
     30 - ((v - min) / r) * 24 - 3];
  const path = pts.map((v, i) => xy(v, i).join(",")).join(" ");
  const [lx, ly] = xy(pts[pts.length - 1], pts.length - 1);
  const up = pts[pts.length - 1] >= pts[0];
  const c = up ? "#34d399" : "#f87171";
  return (
    <svg viewBox="0 0 100 32" preserveAspectRatio="none" className="w-full h-12">
      <defs>
        <linearGradient id={`area-${id}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={c} stopOpacity="0.28" />
          <stop offset="100%" stopColor={c} stopOpacity="0" />
        </linearGradient>
      </defs>
      <polygon points={`${PAD},32 ${path} ${100 - PAD},32`} fill={`url(#area-${id})`} />
      <polyline points={path} fill="none" stroke={c} strokeWidth="1.3"
        vectorEffect="non-scaling-stroke" opacity="0.9" />
      <polyline points={path} fill="none" stroke={c} strokeWidth="4"
        vectorEffect="non-scaling-stroke" opacity="0.12" />
      <circle cx={lx} cy={ly} r="2.2" fill={c}>
        <animate attributeName="opacity" values="1;0.2;1" dur="1.2s"
          repeatCount="indefinite" />
      </circle>
    </svg>
  );
}

/* ---------- order-book pressure: depth mirror + labeled skew gauge ------ */
/* convention everywhere in this block: bids/buyers LEFT, asks/sellers RIGHT */
function Pressure({ p }: { p: CoinPulse }) {
  const mx = Math.max(...p.bid_szs, ...p.ask_szs, 1e-9);
  const skew = p.imbalance * 100; // >0 = more bid size resting
  const needle = ((1 - p.imbalance) / 2) * 100; // +1 → far left (buy side)
  return (
    <div className="grid gap-1.5">
      <div className="flex items-center justify-between text-[9px] uppercase tracking-wider text-slate-500">
        <span className="text-emerald-300/80">buyers</span>
        <span className="normal-case tracking-normal mono text-[10px] text-slate-300">
          {Math.abs(skew) < 5 ? "book balanced"
            : skew > 0 ? `bid-heavy +${skew.toFixed(0)}%`
            : `ask-heavy ${skew.toFixed(0)}%`}
        </span>
        <span className="text-red-300/80">sellers</span>
      </div>
      <div className="flex items-center gap-2">
        <div className="flex items-end gap-[2px] h-7">
          {[...p.bid_szs].reverse().map((s, i) => (
            <div key={`b${i}`}
              className="w-[5px] rounded-sm bg-emerald-400/70 transition-all duration-700 ease-out"
              style={{ height: `${Math.max(8, (s / mx) * 100)}%` }} />
          ))}
          <div className="w-px h-full bg-edge mx-0.5" />
          {p.ask_szs.map((s, i) => (
            <div key={`a${i}`}
              className="w-[5px] rounded-sm bg-red-400/70 transition-all duration-700 ease-out"
              style={{ height: `${Math.max(8, (s / mx) * 100)}%` }} />
          ))}
        </div>
        <div className="relative flex-1 h-1.5 rounded-full bg-gradient-to-r from-emerald-500/40 via-edge to-red-500/40">
          <div
            className="absolute -top-[3px] w-[3px] h-3 rounded bg-white shadow-[0_0_6px_#22d3ee] transition-all duration-700 ease-out"
            style={{ left: `calc(${needle}% - 1px)` }} />
        </div>
      </div>
      <div className="flex justify-between mono text-[9px] text-slate-500">
        <span className="text-emerald-300/70">bids {fmtK(p.bid_notional)}</span>
        <span>spread {p.spread_bps.toFixed(1)}bp</span>
        <span className="text-red-300/70">asks {fmtK(p.ask_notional)}</span>
      </div>
    </div>
  );
}

/* ---------- model constellation: 7 models beam votes into the verdict --- */
function Constellation({ tickets, verdict }: { tickets: TicketT[]; verdict?: Verdict }) {
  const by: Record<string, TicketT> = {};
  for (const t of tickets) by[t.model] = t;
  const cx = 80, cy = 60, R = 40;
  const dir = verdict?.direction ?? "FLAT";
  const vc = dirHex(dir);
  return (
    <svg viewBox="0 0 160 120" className="w-[150px] h-[112px] shrink-0">
      {MODELS.map(([name, abbr], i) => {
        const a = (i / MODELS.length) * Math.PI * 2 - Math.PI / 2;
        const x = cx + Math.cos(a) * R, y = cy + Math.sin(a) * R;
        const lx = cx + Math.cos(a) * (R + 12), ly = cy + Math.sin(a) * (R + 12);
        const t = by[name];
        const dead = INACTIVE.has(name);
        const active = !dead && (t?.direction === "LONG" || t?.direction === "SHORT");
        const c = dead ? "#283244" : active ? dirHex(t.direction) : "#334155";
        const conf = t ? Number(t.confidence) : 0;
        const why = t?.meta?.reason ?? t?.meta?.zone ?? t?.meta?.band ?? "";
        return (
          <g key={name} opacity={dead ? 0.5 : 1}>
            <line x1={x} y1={y} x2={cx} y2={cy} stroke={c}
              strokeWidth={active ? 1.2 : 0.6}
              strokeDasharray={dead ? "1 4" : active ? "3 5" : undefined}
              className={active ? "beam" : undefined}
              opacity={dead ? 0.25 : active ? 0.35 + conf * 0.55 : 0.35} />
            {/* inactive slots: hollow ring + ✕, never a filled vote node */}
            <circle cx={x} cy={y} r={dead ? 3 : active ? 4 : 2.8}
              fill={dead ? "none" : c} stroke={dead ? c : "none"}
              strokeWidth={dead ? 1 : 0}>
              <title>{dead ? INACTIVE_NOTE[name]
                : t ? `${name}: ${t.direction} conf ${conf.toFixed(2)}${why ? ` (${String(why).replace(/_/g, " ")})` : ""}`
                : `${name}: no ticket`}</title>
            </circle>
            {dead && (
              <text x={x} y={y + 2.4} textAnchor="middle" fontSize="5.5"
                fill={c}>✕</text>
            )}
            <text x={lx} y={ly + 2.5} textAnchor="middle" fontSize="7"
              fill={dead ? "#3a4660" : active ? "#94a3b8" : "#475569"}
              style={{ fontFamily: "ui-monospace, monospace",
                       textDecoration: dead ? "line-through" : "none" }}>{abbr}</text>
          </g>
        );
      })}
      <circle cx={cx} cy={cy} r={13} fill="none" stroke={vc} strokeWidth="1">
        <animate attributeName="r" values="11;15;11" dur="2.2s" repeatCount="indefinite" />
        <animate attributeName="opacity" values="0.7;0.1;0.7" dur="2.2s" repeatCount="indefinite" />
      </circle>
      <circle cx={cx} cy={cy} r={10} fill="#0b0f14" stroke={vc} strokeWidth="1.2" />
      <text x={cx} y={cy + 3.5} textAnchor="middle" fontSize="10" fontWeight="bold" fill={vc}>
        {dir === "LONG" ? "▲" : dir === "SHORT" ? "▼" : "·"}
      </text>
    </svg>
  );
}

/* ---------- the two entry gates the verdict must clear ------------------ */
function Gates({ v, gates }: { v?: Verdict; gates?: GatesT }) {
  const gConf = gates?.min_confidence ?? 0.62;
  const gQuorum = gates?.min_model_agreement ?? 5;
  const conf = v?.confidence ?? 0;
  const agree = v?.agreement ?? 0;
  const dir = v?.direction ?? "FLAT";
  const fill = dir === "LONG" ? "bg-emerald-400" : dir === "SHORT" ? "bg-red-400" : "bg-slate-500";
  return (
    <div className="grid gap-2 flex-1 min-w-0">
      <div>
        <div className="flex justify-between text-[9px] text-slate-500">
          <span>weighted confidence</span>
          <span className="mono text-slate-300">{conf.toFixed(2)} <span className="text-slate-500">/ gate {gConf}</span></span>
        </div>
        <div className="relative h-1.5 rounded-full bg-edge mt-1">
          <div className={clsx("absolute inset-y-0 left-0 rounded-full transition-all duration-700", fill)}
            style={{ width: `${Math.min(100, conf * 100)}%` }} />
          <div className="absolute -top-[3px] h-3 w-px bg-slate-200/80"
            style={{ left: `${gConf * 100}%` }} />
        </div>
      </div>
      <div className="flex items-center justify-between">
        <span className="text-[9px] text-slate-500">models agreeing</span>
        <div className="flex items-center gap-1">
          {Array.from({ length: gQuorum }).map((_, i) => (
            <span key={i} className={clsx("w-1.5 h-1.5 rounded-full transition-colors duration-500",
              i < agree ? fill : "bg-edge")} />
          ))}
          <span className="mono text-[9px] text-slate-400 ml-1">{agree}/{gQuorum}</span>
        </div>
      </div>
      <div className="flex items-center justify-between text-[9px] text-slate-500">
        <span>votes L·S·F</span>
        <span className="mono">
          <span className="text-emerald-400">{v?.long_votes ?? 0}</span>
          <span className="text-slate-600"> · </span>
          <span className="text-red-400">{v?.short_votes ?? 0}</span>
          <span className="text-slate-600"> · </span>
          <span className="text-slate-400">{v?.flat_votes ?? 0}</span>
        </span>
      </div>
    </div>
  );
}

/* plain-language verdict line — the "so what" of the whole panel */
function verdictLine(v?: Verdict, gates?: GatesT): { text: string; cls: string; armed: boolean } {
  if (!v) return { text: "DECISION ENGINE IDLE — trading loop not active", cls: "text-slate-500 border-edge", armed: false };
  const gConf = gates?.min_confidence ?? 0.62;
  const gQ = gates?.min_model_agreement ?? 5;
  if (v.direction === "FLAT")
    return { text: "STANDING DOWN — no directional consensus", cls: "text-slate-400 border-edge", armed: false };
  const side = v.direction === "LONG" ? "text-emerald-400 border-emerald-400/40" : "text-red-400 border-red-400/40";
  if (v.would_fire)
    return { text: `${v.direction} ARMED — all gates clear, sizing trade`, cls: side, armed: true };
  const blocks: string[] = [];
  if (v.agreement < gQ) blocks.push(`needs ${gQ - v.agreement} more vote${gQ - v.agreement > 1 ? "s" : ""}`);
  if (v.confidence < gConf) blocks.push(`conf ${v.confidence.toFixed(2)} < ${gConf}`);
  if (v.veto) blocks.push("funding veto");
  return { text: `${v.direction} LEAN HELD — ${blocks.join(" · ") || "risk gate"}`, cls: side, armed: false };
}

/* ---------- one coin's decision panel ----------------------------------- */
function CoinPanel({ coin, p, hist, tickets, verdict, gates }: {
  coin: string; p: CoinPulse; hist: number[];
  tickets: TicketT[]; verdict?: Verdict; gates?: GatesT;
}) {
  const d = hist.length > 1 ? Math.sign(hist[hist.length - 1] - hist[hist.length - 2]) : 0;
  const chg = hist.length > 1 ? ((hist[hist.length - 1] - hist[0]) / hist[0]) * 100 : 0;
  const mins = Math.max(1, Math.round((hist.length * 2.5) / 60));
  const regime = verdict?.regime ?? "";
  const vl = verdictLine(verdict, gates);
  return (
    <div className="relative border border-edge/70 rounded-xl p-3 grid gap-2.5 min-w-0 bg-ink/30 overflow-hidden">
      <div className="flex items-baseline justify-between gap-2">
        <div className="flex items-baseline gap-2 min-w-0">
          <span className="font-bold">{coin}</span>
          <span key={`${coin}-${p.mid}`}
            className={clsx("mono text-sm tabular-nums",
              d > 0 ? "flash-up" : d < 0 ? "flash-down" : "text-slate-200")}>
            {p.mid.toLocaleString()}
          </span>
          <span className={clsx("mono text-[10px] tabular-nums",
            chg >= 0 ? "text-emerald-400/80" : "text-red-400/80")}>
            {chg >= 0 ? "+" : ""}{chg.toFixed(2)}% · {mins}m
          </span>
        </div>
        {REGIME_LABEL[regime] && (
          <span className="text-[9px] mono text-sky-300 border border-sky-300/30 rounded-full px-2 py-0.5 shrink-0">
            {REGIME_LABEL[regime]}
          </span>
        )}
      </div>

      <div className="relative">
        <Spark pts={hist} id={coin} />
        <div className="sweep" />
      </div>

      <Pressure p={p} />

      <div className="border-t border-edge/60 pt-2">
        <div className="text-[9px] uppercase tracking-wider text-slate-500 mb-1">
          model ensemble → verdict
        </div>
        <div className="flex items-center gap-3">
          <Constellation tickets={tickets} verdict={verdict} />
          <Gates v={verdict} gates={gates} />
        </div>
      </div>

      <div className={clsx(
        "border rounded-lg px-2.5 py-1.5 text-[11px] mono tracking-tight text-center",
        vl.cls, vl.armed && "armed")}>
        {vl.text}
      </div>
    </div>
  );
}

/* ======================================================================== */
export default function AnalysisCore() {
  const { data } = usePoll<{ coins: Record<string, CoinPulse>;
    hist: Record<string, number[]>; n: number }>("/api/pulse", 2500);
  const { data: live } = usePoll<TicketsResp>("/api/tickets", 4000);
  const activeCoins = useActiveCoins();
  const prev = useRef<Record<string, number>>({});
  const [feed, setFeed] = useState<{ id: number; t: string; line: string; dir: number }[]>([]);
  const feedId = useRef(0);

  useEffect(() => {
    if (!data?.coins) return;
    const t = new Date().toLocaleTimeString("en-US", { hour12: true });
    const fresh: typeof feed = [];
    for (const [coin, p] of Object.entries(data.coins)) {
      const last = prev.current[coin];
      const dd = last == null ? 0 : Math.sign(p.mid - last);
      prev.current[coin] = p.mid;
      fresh.push({
        id: feedId.current++,
        t,
        dir: dd,
        line: `${coin} ${p.mid.toLocaleString()} ${dd > 0 ? "▲" : dd < 0 ? "▼" : "·"} skew ${p.imbalance >= 0 ? "+" : ""}${(p.imbalance * 100).toFixed(0)}%`,
      });
    }
    setFeed((f) => [...fresh, ...f].slice(0, 9));
  }, [data]);

  // only surface coins that are active in live config (Controls toggles);
  // while config is still loading (null) show whatever the pulse provides
  const coins = Object.entries(data?.coins ?? {}).filter(
    ([c]) => activeCoins === null || activeCoins.includes(c));
  const gates = live?.gates;
  const gQ = gates?.min_model_agreement ?? 5;
  const gC = gates?.min_confidence ?? 0.62;

  return (
    <div className="card relative overflow-hidden">
      <div className="absolute -top-20 -right-20 w-64 h-64 rounded-full bg-glow/5 blur-3xl pointer-events-none" />

      {/* header: what this is + the live counters + the rule a trade must pass */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 mb-4">
        <span className="label">Analysis Core</span>
        <span className="relative flex h-1.5 w-1.5">
          <span className="animate-ping absolute h-full w-full rounded-full bg-glow opacity-60" />
          <span className="relative rounded-full h-1.5 w-1.5 bg-glow" />
        </span>
        <span className="text-[10px] text-slate-500">
          live L2 books + {ACTIVE_DIRECTIONAL}-model ensemble · 2.5s cadence
        </span>
        <div className="md:ml-auto flex flex-wrap items-center gap-2">
          <span className="flex items-center gap-1.5 border border-edge rounded-full px-2.5 py-1 text-[10px] mono text-slate-300">
            <span className="relative inline-block w-3 h-3 rounded-full core-ring" />
            {(data?.n ?? 0).toLocaleString()} book scans
          </span>
          <span className="border border-edge rounded-full px-2.5 py-1 text-[10px] mono text-slate-300">
            {coins.length * 20} levels live
          </span>
          <span className="border border-glow/30 rounded-full px-2.5 py-1 text-[10px] mono text-glow/90"
            title="A trade only fires when at least this many models vote the same direction AND the regime-weighted confidence clears the gate — then risk guards still get the final say.">
            entry gate · ≥{gQ}/{ACTIVE_DIRECTIONAL} models · conf ≥{gC}
          </span>
        </div>
      </div>

      {/* per-coin decision panels */}
      <div className="grid gap-3 lg:grid-cols-3">
        {coins.map(([coin, p]) => (
          <CoinPanel key={coin} coin={coin} p={p}
            hist={data?.hist?.[coin] ?? []}
            tickets={live?.coins?.[coin] ?? []}
            verdict={live?.verdicts?.[coin]}
            gates={gates} />
        ))}
        {!coins.length && (
          <div className="text-slate-500 text-sm py-10 text-center lg:col-span-3">
            spinning up book sampler…
          </div>
        )}
      </div>

      {/* tape: latest tick deltas, newest first */}
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
