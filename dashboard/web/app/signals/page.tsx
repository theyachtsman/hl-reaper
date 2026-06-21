"use client";
import { useEffect, useState } from "react";
import clsx from "clsx";
import { usePoll, useActiveCoins, fmtTs } from "@/lib/api";

const MODEL_ABBR: Record<string, string> = {
  RegimeDetectorModel: "REGIME",
  TAModel: "TA",
  MeanReversionModel: "MEANREV",
  FundingRateModel: "FUNDING",
  OrderbookImbalanceModel: "BOOK",
  VWAPModel: "VWAP",
  LiquidationHeatmapModel: "LIQMAP",
  MLForecastModel: "ML",
};

/* plain-English: what each model is and what it's looking for, shown on the
 * per-coin model cards so the signal breakdown is legible without docs */
const MODEL_DESC: Record<string, string> = {
  RegimeDetectorModel:
    "Classifies the market regime (trending / ranging / high-vol) from ADX + ATR. Doesn't vote — it routes how much the other models count.",
  TAModel:
    "Technical analysis. Fades RSI / Bollinger-band extremes and reads trend structure to call LONG or SHORT.",
  MeanReversionModel:
    "Mean reversion. Measures how stretched price is from its average (z-score) and fades the overshoot back toward the mean.",
  FundingRateModel:
    "Funding-rate signal. When one side is crowded (extreme funding), it leans against it — that side is paying to hold and tends to get squeezed.",
  OrderbookImbalanceModel:
    "Order-book pressure. Compares resting bid vs ask size at the top of book for near-term directional lean. Highest-tilt model in the ensemble.",
  VWAPModel:
    "Volume-weighted average price. Flags whether price is trading above or below its structural equilibrium and which way it's reverting.",
  LiquidationHeatmapModel:
    "Estimates where leveraged positions are clustered and likely to liquidate. Inactive — stays FLAT on normal tape.",
  MLForecastModel:
    "XGBoost next-bar direction forecast. Inactive — no model cleared the honesty gate, so direction classification isn't used.",
};

/* permanently non-voting slots — kept visible but clearly marked inactive,
 * distinct from a live model that happens to be FLAT this cycle */
const INACTIVE_MODELS = new Set(["MLForecastModel", "LiquidationHeatmapModel"]);
const INACTIVE_LABEL: Record<string, string> = {
  MLForecastModel: "no model · direction classification not viable",
  LiquidationHeatmapModel: "inactive · 100% FLAT on live data",
};

function dirColor(d: string) {
  if (d === "LONG") return "text-emerald-400";
  if (d === "SHORT") return "text-red-400";
  if (["TRENDING_UP", "TRENDING_DOWN", "RANGING", "HIGH_VOL"].includes(d)) return "text-sky-300";
  return "text-slate-500";
}

function dirArrow(d: string) {
  if (d === "LONG") return "▲";
  if (d === "SHORT") return "▼";
  return "·";
}

const REGIMES = ["TRENDING_UP", "TRENDING_DOWN", "RANGING", "HIGH_VOL", "UNKNOWN"];

/** compact, human reason for why a model is abstaining */
function flatReason(meta: any): string {
  const r = meta?.reason ?? meta?.zone ?? meta?.band ?? "";
  return String(r).replace(/_/g, " ").slice(0, 18);
}

function TicketChip({ t }: { t: any }) {
  const isRegime = REGIMES.includes(t.direction);
  const dead = INACTIVE_MODELS.has(t.model);
  if (dead) {
    return (
      <span
        title={INACTIVE_LABEL[t.model]}
        className="inline-flex items-center gap-1.5 border border-edge/50 rounded-full px-2.5 py-1 text-xs mono opacity-40">
        <span className="text-slate-500 line-through">{MODEL_ABBR[t.model] ?? t.model}</span>
        <span className="text-slate-600 text-[10px]">inactive</span>
      </span>
    );
  }
  return (
    <span
      title={MODEL_DESC[t.model] ?? (t.meta ? JSON.stringify(t.meta) : "")}
      className="inline-flex items-center gap-1.5 border border-edge rounded-full px-2.5 py-1 text-xs mono">
      <span className="text-slate-400">{MODEL_ABBR[t.model] ?? t.model}</span>
      {isRegime ? (
        <span className="text-sky-300">{t.direction}</span>
      ) : t.direction === "FLAT" ? (
        <>
          <span className="text-slate-500">FLAT</span>
          {flatReason(t.meta) && (
            <span className="text-slate-600 text-[10px]">{flatReason(t.meta)}</span>
          )}
        </>
      ) : (
        <>
          <span className={dirColor(t.direction)}>
            {dirArrow(t.direction)} {t.direction}
          </span>
          <span className="text-slate-500">{Number(t.confidence).toFixed(2)}</span>
        </>
      )}
    </span>
  );
}

function pct(x: any, dp = 3) {
  return x === null || x === undefined ? "—" : `${(Number(x) * 100).toFixed(dp)}%`;
}

/* LONG structural gate (2026-06-17): spot-leading / OI-rising / book-bid-heavy
 * plus the momentum cooldown (no recent pump, 2026-06-18) must ALL pass for a
 * LONG to fire. Published by the bot each loop. */
function LongGate({ gate }: { gate: any }) {
  if (!gate) return null;
  const enabled = gate.enabled !== false;
  const Row = ({ ok, label, detail }: { ok: boolean; label: string; detail: string }) => (
    <div className="flex items-center gap-2 text-xs mono">
      <span className={ok ? "text-emerald-400" : "text-red-400"}>{ok ? "✓" : "✗"}</span>
      <span className="text-slate-400 w-28">{label}</span>
      <span className="text-slate-500">{detail}</span>
    </div>
  );
  return (
    <div className="mt-3 border-t border-edge/40 pt-2">
      <div className="label mb-1">
        LONG structural gate {!enabled && <span className="text-slate-600">· disabled</span>}
      </div>
      <Row ok={!!gate.spot_leading} label="Spot leading"
        detail={`${pct(gate.spot_ret)} vs perp ${pct(gate.perp_ret)}`} />
      <Row ok={!!gate.oi_rising} label="OI rising"
        detail={`${pct(gate.oi_change)} in window`} />
      <Row ok={!!gate.ob_bid_heavy} label="Book bid-heavy"
        detail={`imbalance ${gate.imbalance === null || gate.imbalance === undefined
          ? "—" : Number(gate.imbalance).toFixed(2)}`} />
      <Row ok={!!gate.momentum_ok} label="No recent pump"
        detail={`5m ${pct(gate.move_5m)} 10m ${pct(gate.move_10m)} 15m ${pct(gate.move_15m)}`} />
      <div className={clsx("text-xs mono font-bold mt-1",
        gate.allowed ? "text-emerald-400" : "text-red-400")}>
        → LONG {gate.allowed ? "ALLOWED" : `BLOCKED (${gate.block_reason || "—"})`}
      </div>
    </div>
  );
}

/* SHORT structural gate (2026-06-19): mirror of the LONG gate. spot-lagging /
 * OI-rising-with-falling-price / book-ask-heavy plus the dump cooldown (no
 * recent drop) must ALL pass for a SHORT to fire. Published by the bot each
 * loop. Reads live microstructure, overriding a regime detector that lags into
 * TRENDING_UP. */
function ShortGate({ gate }: { gate: any }) {
  if (!gate) return null;
  const enabled = gate.enabled !== false;
  const Row = ({ ok, label, detail }: { ok: boolean; label: string; detail: string }) => (
    <div className="flex items-center gap-2 text-xs mono">
      <span className={ok ? "text-emerald-400" : "text-red-400"}>{ok ? "✓" : "✗"}</span>
      <span className="text-slate-400 w-28">{label}</span>
      <span className="text-slate-500">{detail}</span>
    </div>
  );
  return (
    <div className="mt-3 border-t border-edge/40 pt-2">
      <div className="label mb-1">
        SHORT structural gate {!enabled && <span className="text-slate-600">· disabled</span>}
      </div>
      <Row ok={!!gate.spot_lagging} label="Spot lagging"
        detail={`${pct(gate.spot_ret)} vs perp ${pct(gate.perp_ret)}`} />
      <Row ok={!!gate.oi_rising} label="OI rising+fall"
        detail={`${pct(gate.oi_change)} OI, price ${pct(gate.perp_ret)}`} />
      <Row ok={!!gate.ob_ask_heavy} label="Book ask-heavy"
        detail={`imbalance ${gate.imbalance === null || gate.imbalance === undefined
          ? "—" : Number(gate.imbalance).toFixed(2)}`} />
      <Row ok={!!gate.momentum_ok} label="No recent dump"
        detail={`5m ${pct(gate.move_5m)} 10m ${pct(gate.move_10m)} 15m ${pct(gate.move_15m)}`} />
      <div className={clsx("text-xs mono font-bold mt-1",
        gate.allowed ? "text-emerald-400" : "text-red-400")}>
        → SHORT {gate.allowed ? "ALLOWED" : `BLOCKED (${gate.block_reason || "—"})`}
      </div>
    </div>
  );
}

/* normalize a live /api/tickets verdict into the AggCard shape so the Signals
 * top card shows the same real-time verdict as the Live page (instead of the
 * last NON-FLAT AGGREGATOR row from the DB, which goes stale when a coin flips
 * to FLAT — only non-FLAT verdicts get logged). */
function verdictToAgg(coin: string, v: any, ts: number | null) {
  return {
    coin,
    direction: v.direction,
    confidence: v.confidence,
    meta: { regime: v.regime, long: v.long_votes, short: v.short_votes, flat: v.flat_votes },
    ts,
    live: true,
  };
}

function AggCard({ agg }: { agg: any }) {
  return (
    <div className="card border-glow/30">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="font-bold">{agg.coin}</span>
        <span className={`text-xl font-bold ${dirColor(agg.direction)}`}>{agg.direction}</span>
        <span className="mono text-sm">conf {Number(agg.confidence).toFixed(3)}</span>
        {agg.meta?.regime && <span className="text-sky-300 text-sm">{agg.meta.regime}</span>}
      </div>
      <div className="flex flex-wrap gap-x-3 text-xs text-slate-400 mt-1">
        {agg.meta && <span>votes L/S/F {agg.meta.long}/{agg.meta.short}/{agg.meta.flat}</span>}
        <span className="text-slate-500">{fmtTs(agg.ts)}</span>
        <span className={agg.live ? "text-emerald-500/70" : "text-amber-500/70"}>
          {agg.live ? "· live" : "· last logged"}
        </span>
      </div>
    </div>
  );
}

type Band = "scalp" | "trend";

export default function SignalsPage() {
  const activeCoins = useActiveCoins();
  const coins = activeCoins ?? [];
  const [coin, setCoin] = useState<string>("");
  const [band, setBand] = useState<Band>("scalp");
  const { data: signals } = usePoll<any[]>(
    `/api/signals?limit=300${coin ? `&coin=${coin}` : ""}`, 10000);
  const { data: live } = usePoll<{
    ts: number | null;
    coins: Record<string, { scalp: { tickets: any[] }; trend: { tickets: any[] } }>;
    verdicts?: Record<string, { scalp: any; trend: any }>;
    bands?: { scalp: boolean; trend: boolean };
  }>("/api/tickets", 5000);

  // if the selected coin gets toggled off in Controls, fall back to ALL
  useEffect(() => {
    if (coin && coins.length && !coins.includes(coin)) setCoin("");
  }, [coin, coins]);

  // latest aggregated signal per coin for the selected band (signals table
  // holds AGGREGATOR_SCALP / AGGREGATOR_TREND rows since the dual-band split)
  const aggModel = `AGGREGATOR_${band.toUpperCase()}`;
  const aggByCoin: Record<string, any> = {};
  for (const s of signals ?? []) {
    if (s.model === aggModel && !aggByCoin[s.coin]) aggByCoin[s.coin] = s;
  }
  const bandsEnabled = live?.bands ?? { scalp: true, trend: true };
  const showCoins = coin ? [coin] : coins;

  if (activeCoins === null) {
    return (
      <div className="grid gap-4">
        <div className="card animate-pulse h-12" />
        <div className="grid md:grid-cols-3 gap-3">
          {[0, 1, 2].map((i) => <div key={i} className="card animate-pulse h-20" />)}
        </div>
      </div>
    );
  }

  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="label">Coin</span>
        {["", ...coins].map((c) => (
          <button
            key={c || "all"}
            onClick={() => setCoin(c)}
            className={clsx(
              "px-3 py-1 rounded-lg text-sm border",
              coin === c ? "bg-edge border-glow/50 text-white" : "border-edge text-slate-400"
            )}
          >
            {c || "ALL"}
          </button>
        ))}
        <div className="flex items-center gap-1 rounded-full border border-edge p-0.5 md:ml-auto">
          {(["scalp", "trend"] as Band[]).map((b) => (
            <button key={b} onClick={() => setBand(b)}
              className={clsx(
                "px-2.5 py-0.5 rounded-full text-[11px] font-semibold uppercase transition",
                band === b
                  ? b === "scalp" ? "bg-cyan-500/25 text-cyan-200" : "bg-purple-500/25 text-purple-200"
                  : "text-slate-500 hover:text-slate-300",
                !bandsEnabled[b] && "opacity-50")}
              title={bandsEnabled[b] ? "" : `${b} band disabled`}>
              {b}{!bandsEnabled[b] && " ⏻"}
            </button>
          ))}
        </div>
        {live?.ts && (
          <span className="text-xs text-slate-500">
            {band} tickets live · {fmtTs(live.ts)}
          </span>
        )}
      </div>

      {/* live verdict — one card per coin. Prefer the real-time /api/tickets
          verdict (matches the Live page); fall back to the last logged
          AGGREGATOR row only when the bot isn't publishing tickets. */}
      <div className={clsx("grid gap-3", !coin && "md:grid-cols-3")}>
        {showCoins.map((c) => {
          const v = live?.verdicts?.[c]?.[band];
          const agg = v ? verdictToAgg(c, v, live?.ts ?? null) : aggByCoin[c];
          return agg ? (
            <AggCard key={c} agg={agg} />
          ) : (
            <div key={c} className="card text-sm text-slate-500">
              {c}: no live verdict yet
            </div>
          );
        })}
      </div>

      {/* live model tickets straight from the bot loop */}
      {showCoins.map((c) => {
        // hide the parked non-voters (ML / LiqHeatmap) entirely — they're not
        // part of the active ensemble and shouldn't show as model cards/chips
        const tickets: any[] = (live?.coins?.[c]?.[band]?.tickets ?? [])
          .filter((t: any) => !INACTIVE_MODELS.has(t.model));
        const gate = live?.verdicts?.[c]?.[band]?.long_gate;
        const shortGate = live?.verdicts?.[c]?.[band]?.short_gate;
        return (
          <div key={c} className="card">
            <div className="label mb-2">
              {c} — live model tickets
              <span className={clsx("ml-2 text-[10px] uppercase",
                band === "scalp" ? "text-cyan-300" : "text-purple-300")}>{band}</span>
            </div>
            {!tickets.length ? (
              <div className="text-slate-500 text-sm py-2">
                no live tickets — bot idle, not ACTIVE, or coin disabled
              </div>
            ) : coin ? (
              /* single-coin view: full cards */
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                {tickets.map((t) => {
                  const dead = INACTIVE_MODELS.has(t.model);
                  return (
                  <div key={t.model} title={t.meta ? JSON.stringify(t.meta) : ""}
                    className={clsx(
                    "border rounded-lg p-3 min-w-0 flex flex-col",
                    dead ? "border-edge/40 opacity-50" : "border-edge")}>
                    <div className={clsx("label truncate", dead && "line-through")}>
                      {MODEL_ABBR[t.model] ?? t.model}
                    </div>
                    {dead ? (
                      <div className="text-lg font-bold mt-1 text-slate-600">INACTIVE</div>
                    ) : (
                      <>
                        <div className={`text-lg font-bold mt-1 ${dirColor(t.direction)}`}>
                          {t.direction}
                        </div>
                        <div className="text-xs mono text-slate-400">
                          conf {Number(t.confidence).toFixed(2)}
                        </div>
                      </>
                    )}
                    <div className="text-[11px] leading-snug text-slate-500 mt-2 border-t border-edge/40 pt-1.5">
                      {MODEL_DESC[t.model] ?? ""}
                    </div>
                  </div>
                  );
                })}
              </div>
            ) : (
              /* ALL view: compact chips */
              <div className="flex flex-wrap gap-2">
                {tickets.map((t) => <TicketChip key={t.model} t={t} />)}
              </div>
            )}
            {gate && <LongGate gate={gate} />}
            {shortGate && <ShortGate gate={shortGate} />}
          </div>
        );
      })}
    </div>
  );
}
