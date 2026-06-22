"use client";
import { usePoll, fmtUsd } from "@/lib/api";
import { useStatusStore } from "@/lib/store";
import AnalysisCoreSection from "@/components/AnalysisCoreSection";
import ProfitDeck from "@/components/ProfitDeck";
import CandleChart from "@/components/CandleChart";
import EquityChart from "@/components/EquityChart";
import StateBadge from "@/components/StateBadge";

export default function LivePage() {
  const status = useStatusStore((s) => s.status);
  const { data: pos } = usePoll<any>("/api/positions", 5000);
  const { data: equity } = usePoll<{ ts: number; account_value: number }[]>(
    "/api/equity?hours=168", 30000);

  const curve = (equity ?? []).map((e) => e.account_value);
  const labels = (equity ?? []).map((e) =>
    new Date(e.ts).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", hour12: true }));

  return (
    <div className="grid gap-4">
      <div className="grid md:grid-cols-3 gap-4">
        <div className="card">
          <div className="label">Bot State</div>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <StateBadge state={status?.risk_state ?? "UNKNOWN"} large />
          </div>
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
          <div className="label">Recorder</div>
          <div className={`text-2xl font-semibold mt-1 ${status?.recorder_heartbeat_age_s != null && status.recorder_heartbeat_age_s < 60 ? "text-emerald-400" : "text-red-400"}`}>
            {status?.recorder_heartbeat_age_s != null && status.recorder_heartbeat_age_s < 60 ? "RECORDING" : "DOWN"}
          </div>
          <div className="text-xs text-slate-400 mt-1">
            L2/OI capture for book models
          </div>
        </div>
      </div>

      <ProfitDeck />

      <CandleChart />

      <AnalysisCoreSection />

      <div className="card">
        <div className="label mb-2">Equity — 7 days</div>
        <EquityChart points={curve} labels={labels} />
      </div>
    </div>
  );
}
