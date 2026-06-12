"use client";
import { useState } from "react";
import clsx from "clsx";
import { usePoll, fmtTs } from "@/lib/api";
import { useStatusStore } from "@/lib/store";

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
  return (
    <span
      title={t.meta ? JSON.stringify(t.meta) : ""}
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
      </div>
    </div>
  );
}

export default function SignalsPage() {
  const status = useStatusStore((s) => s.status);
  const coins = status?.coins ?? ["BTC", "ETH", "SOL"];
  const [coin, setCoin] = useState<string>("");
  const { data: signals } = usePoll<any[]>(
    `/api/signals?limit=300${coin ? `&coin=${coin}` : ""}`, 10000);
  const { data: live } = usePoll<{ ts: number | null; coins: Record<string, any[]> }>(
    "/api/tickets", 5000);
  const { data: trades } = usePoll<any[]>("/api/trades?limit=50", 15000);

  // latest aggregated signal per coin (signals table holds AGGREGATOR rows)
  const aggByCoin: Record<string, any> = {};
  for (const s of signals ?? []) {
    if (s.model === "AGGREGATOR" && !aggByCoin[s.coin]) aggByCoin[s.coin] = s;
  }
  const liveCoins = live?.coins ?? {};
  const showCoins = coin ? [coin] : coins;

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
        {live?.ts && (
          <span className="md:ml-auto text-xs text-slate-500">
            model tickets live · {fmtTs(live.ts)}
          </span>
        )}
      </div>

      {/* latest aggregated signal — one card per coin */}
      <div className={clsx("grid gap-3", !coin && "md:grid-cols-3")}>
        {showCoins.map((c) =>
          aggByCoin[c] ? (
            <AggCard key={c} agg={aggByCoin[c]} />
          ) : (
            <div key={c} className="card text-sm text-slate-500">
              {c}: no aggregated signal logged yet
            </div>
          )
        )}
      </div>

      {/* live model tickets straight from the bot loop */}
      {showCoins.map((c) => {
        const tickets: any[] = liveCoins[c] ?? [];
        return (
          <div key={c} className="card">
            <div className="label mb-2">{c} — live model tickets</div>
            {!tickets.length ? (
              <div className="text-slate-500 text-sm py-2">
                no live tickets — bot idle, not ACTIVE, or coin disabled
              </div>
            ) : coin ? (
              /* single-coin view: full cards */
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                {tickets.map((t) => (
                  <div key={t.model} className="border border-edge rounded-lg p-3 min-w-0">
                    <div className="label truncate">{MODEL_ABBR[t.model] ?? t.model}</div>
                    <div className={`text-lg font-bold mt-1 ${dirColor(t.direction)}`}>
                      {t.direction}
                    </div>
                    <div className="text-xs mono text-slate-400">
                      conf {Number(t.confidence).toFixed(2)}
                    </div>
                    <div className="text-[10px] text-slate-500 mt-1 break-all line-clamp-2">
                      {t.meta ? JSON.stringify(t.meta).slice(0, 80) : ""}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              /* ALL view: compact chips */
              <div className="flex flex-wrap gap-2">
                {tickets.map((t) => (
                  <span key={t.model}
                    className="inline-flex items-center gap-1.5 border border-edge rounded-full px-2.5 py-1 text-xs mono">
                    <span className="text-slate-400">{MODEL_ABBR[t.model] ?? t.model}</span>
                    <span className={dirColor(t.direction)}>
                      {dirArrow(t.direction)} {t.direction === "FLAT" ? "" : t.direction}
                    </span>
                    {t.confidence > 0 && (
                      <span className="text-slate-500">{Number(t.confidence).toFixed(2)}</span>
                    )}
                  </span>
                ))}
              </div>
            )}
          </div>
        );
      })}

      <div className="card">
        <div className="label mb-2">Trade Audit Log</div>
        {!trades?.length ? (
          <div className="text-slate-500 text-sm py-4 text-center">no trades yet</div>
        ) : (
          <div className="overflow-x-auto">
          <table className="w-full text-xs min-w-[640px]">
            <thead className="text-left text-slate-400">
              <tr><th className="py-1">Time</th><th>Coin</th><th>Side</th><th>Action</th>
                  <th>Size</th><th>Price</th><th>Status</th><th>Note</th></tr>
            </thead>
            <tbody className="mono">
              {trades.map((t: any) => (
                <tr key={t.id} className="border-t border-edge">
                  <td className="py-1.5">{fmtTs(t.ts)}</td>
                  <td className="font-semibold">{t.coin}</td>
                  <td className={t.side === "LONG" ? "text-emerald-400" : t.side === "SHORT" ? "text-red-400" : ""}>{t.side}</td>
                  <td>{t.action}</td>
                  <td>{t.size ?? "—"}</td>
                  <td>{t.price ?? "—"}</td>
                  <td>{t.status ?? "—"}</td>
                  <td className="text-slate-400 max-w-[260px] truncate">{t.note ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </div>
    </div>
  );
}
