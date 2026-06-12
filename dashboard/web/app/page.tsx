"use client";
import { usePoll, fmtUsd, fmtPct, fmtTs } from "@/lib/api";
import { useStatusStore } from "@/lib/store";
import AnalysisCore from "@/components/AnalysisCore";
import EquityChart from "@/components/EquityChart";
import StateBadge from "@/components/StateBadge";

export default function LivePage() {
  const status = useStatusStore((s) => s.status);
  const { data: prices } = usePoll("/api/prices", 5000);
  const { data: pos } = usePoll("/api/positions", 5000);
  const { data: equity } = usePoll<{ ts: number; account_value: number }[]>(
    "/api/equity?hours=168", 30000);

  const curve = (equity ?? []).map((e) => e.account_value);
  const labels = (equity ?? []).map((e) =>
    new Date(e.ts).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", hour12: false }));
  const dayPnl = status?.day_open_equity && pos?.account_value
    ? pos.account_value - status.day_open_equity : null;

  return (
    <div className="grid gap-4">
      <div className="grid md:grid-cols-4 gap-4">
        <div className="card">
          <div className="label">Bot State</div>
          <div className="mt-2"><StateBadge state={status?.risk_state ?? "UNKNOWN"} large /></div>
          {status?.risk_reason && (
            <div className="text-xs text-slate-400 mt-2">{status.risk_reason}</div>
          )}
        </div>
        <div className="card">
          <div className="label">Account Value</div>
          <div className="text-2xl font-semibold mt-1 mono">{fmtUsd(pos?.account_value)}</div>
          <div className="text-xs text-slate-400 mt-1">
            margin used {fmtUsd(pos?.margin_used)}
          </div>
        </div>
        <div className="card">
          <div className="label">Day PnL</div>
          <div className={`text-2xl font-semibold mt-1 mono ${dayPnl == null ? "" : dayPnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
            {dayPnl == null ? "—" : `${dayPnl >= 0 ? "+" : ""}${fmtUsd(dayPnl)}`}
          </div>
          <div className="text-xs text-slate-400 mt-1">
            day open {fmtUsd(status?.day_open_equity || null)}
          </div>
        </div>
        <div className="card">
          <div className="label">Recorder</div>
          <div className={`text-2xl font-semibold mt-1 ${status?.recorder_heartbeat_age_s != null && status.recorder_heartbeat_age_s < 60 ? "text-emerald-400" : "text-red-400"}`}>
            {status?.recorder_heartbeat_age_s != null && status.recorder_heartbeat_age_s < 60 ? "RECORDING" : "DOWN"}
          </div>
          <div className="text-xs text-slate-400 mt-1">
            L2/OI capture for book models
          </div>
        </div>
      </div>

      <AnalysisCore />

      <div className="card">
        <div className="label mb-2">Equity — 7 days</div>
        <EquityChart points={curve} labels={labels} />
      </div>

      <div className="grid md:grid-cols-3 gap-4">
        {(status?.coins ?? []).map((coin: string) => {
          const mid = prices?.mids?.[coin];
          const ctx = prices?.ctx?.[coin];
          const f8 = ctx ? ctx.funding * 8 : null;
          return (
            <div key={coin} className="card">
              <div className="flex items-baseline justify-between">
                <span className="font-bold">{coin}</span>
                <span className="text-xl mono">{mid ? mid.toLocaleString() : "—"}</span>
              </div>
              <div className="grid grid-cols-2 gap-2 mt-3 text-sm">
                <div>
                  <div className="label">Funding /8h</div>
                  <div className={`mono ${f8 == null ? "" : f8 > 0.0005 ? "text-red-400" : f8 < -0.0005 ? "text-emerald-400" : ""}`}>
                    {f8 == null ? "—" : fmtPct(f8, 4)}
                  </div>
                </div>
                <div>
                  <div className="label">Open Interest</div>
                  <div className="mono">{ctx ? ctx.oi.toLocaleString() : "—"}</div>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      <div className="card">
        <div className="label mb-2">Open Positions</div>
        {!pos?.positions?.length ? (
          <div className="text-slate-500 text-sm py-4 text-center">no open positions</div>
        ) : (
          <div className="overflow-x-auto">
          <table className="w-full text-sm min-w-[560px]">
            <thead className="text-left text-slate-400">
              <tr>
                <th className="py-1">Coin</th><th>Side</th><th>Size</th>
                <th>Entry</th><th>Value</th><th>uPnL</th><th>Lev</th><th>Liq Px</th>
              </tr>
            </thead>
            <tbody className="mono">
              {pos.positions.map((p: any) => (
                <tr key={p.coin} className="border-t border-edge">
                  <td className="py-2 font-semibold">{p.coin}</td>
                  <td className={p.szi > 0 ? "text-emerald-400" : "text-red-400"}>
                    {p.szi > 0 ? "LONG" : "SHORT"}
                  </td>
                  <td>{Math.abs(p.szi)}</td>
                  <td>{p.entry_px}</td>
                  <td>{fmtUsd(p.position_value)}</td>
                  <td className={p.unrealized_pnl >= 0 ? "text-emerald-400" : "text-red-400"}>
                    {fmtUsd(p.unrealized_pnl)}
                  </td>
                  <td>{p.leverage ?? "—"}x</td>
                  <td>{p.liq_px ?? "—"}</td>
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
