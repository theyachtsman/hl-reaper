"use client";
import { usePoll, fmtUsd, fmtPct } from "@/lib/api";
import StateBadge from "@/components/StateBadge";

function DrawdownBar({ label, value, limit }: { label: string; value: number | null; limit: number }) {
  const v = Math.max(0, value ?? 0);
  const pct = Math.min(100, (v / limit) * 100);
  const color = pct < 50 ? "bg-emerald-500" : pct < 85 ? "bg-amber-500" : "bg-red-500";
  return (
    <div>
      <div className="flex justify-between text-xs mb-1">
        <span className="label">{label}</span>
        <span className="mono">{fmtPct(v)} / {fmtPct(limit, 0)} limit</span>
      </div>
      <div className="h-2.5 bg-edge rounded-full overflow-hidden">
        <div className={`h-full ${color} transition-all`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

const PARAM_LABELS: Record<string, string> = {
  daily_drawdown_limit: "Daily drawdown → MANAGING",
  severe_drawdown_limit: "Severe drawdown → HALT + close all",
  max_concurrent_positions: "Max concurrent positions",
  max_per_symbol: "Max positions per symbol",
  max_leverage: "Leverage ceiling",
  min_confidence: "Min aggregate confidence",
  min_model_agreement: "Model quorum",
  max_spread_pct: "Max spread",
  atr_sl_multiplier: "Stop distance (× ATR)",
  trail_activation_r: "Trailing activates at (R)",
  max_loss_per_trade_pct: "Per-trade loss floor",
  emergency_loss_pct: "Emergency close at",
  cascade_oi_drop_pct: "Cascade: OI drop",
  cascade_price_move_pct: "Cascade: price move",
  flash_crash_candle_pct: "Flash-crash candle",
  weekly_drawdown_limit: "Weekly drawdown → COOLDOWN",
};

export default function RiskPage() {
  const { data: risk } = usePoll("/api/risk", 10000);
  const { data: fills } = usePoll("/api/fills", 30000);
  const params = risk?.params ?? {};
  const mode = risk?.mode ?? "conservative";
  const aggressive = mode === "paper_aggressive";
  // params the trading.mode override changes vs the conservative base
  const OVERRIDDEN = aggressive
    ? new Set(["min_confidence", "min_model_agreement", "max_concurrent_positions"])
    : new Set<string>();

  return (
    <div className="grid gap-4">
      <div className="grid md:grid-cols-3 gap-4">
        <div className="card">
          <div className="label">Risk State</div>
          <div className="mt-2"><StateBadge state={risk?.state ?? "UNKNOWN"} large /></div>
          {risk?.reason && <div className="text-xs text-slate-400 mt-2 break-words">{risk.reason}</div>}
          {risk?.halted_until > Date.now() / 1000 && (
            <div className="text-xs text-red-300 mt-1">
              halted until {new Date(risk.halted_until * 1000).toLocaleString()}
            </div>
          )}
        </div>
        <div className="card md:col-span-2 grid gap-4">
          <DrawdownBar label="Daily drawdown" value={risk?.daily_drawdown}
                       limit={params.daily_drawdown_limit ?? 0.05} />
          <DrawdownBar label="Weekly drawdown" value={risk?.weekly_drawdown}
                       limit={params.weekly_drawdown_limit ?? 0.10} />
        </div>
      </div>

      <div className="card">
        <div className="label mb-3">Per-Coin Realized PnL (round-trip trades, net of fees)</div>
        {!fills?.per_coin || !Object.keys(fills.per_coin).length ? (
          <div className="text-slate-500 text-sm py-4 text-center">no fills yet</div>
        ) : (
          <div className="overflow-x-auto">
          <table className="w-full text-sm min-w-[480px]">
            <thead className="text-left text-slate-400">
              <tr><th className="py-1">Coin</th><th>Realized PnL</th><th>Fees Paid</th>
                  <th>Closes</th><th>Win Rate</th></tr>
            </thead>
            <tbody className="mono">
              {Object.entries(fills.per_coin).map(([coin, s]: [string, any]) => (
                <tr key={coin} className="border-t border-edge">
                  <td className="py-2 font-semibold">{coin}</td>
                  <td className={s.realized_pnl >= 0 ? "text-emerald-400" : "text-red-400"}>
                    {fmtUsd(s.realized_pnl, 4)}
                  </td>
                  <td className="text-slate-400">{fmtUsd(s.fees, 4)}</td>
                  <td>{s.closes}</td>
                  <td>{s.win_rate == null ? "—" : fmtPct(s.win_rate, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </div>

      <div className="card">
        <div className="flex flex-wrap items-center gap-2 mb-3">
          <span className="label">Guard Parameters — effective (live)</span>
          {aggressive ? (
            <span className="text-[10px] mono uppercase tracking-wider px-2 py-0.5 rounded-full border border-orange-500/60 bg-orange-500/15 text-orange-300">
              ⚠ paper_aggressive — overrides applied
            </span>
          ) : (
            <span className="text-[10px] mono uppercase tracking-wider px-2 py-0.5 rounded-full border border-slate-500/40 text-slate-400">
              conservative
            </span>
          )}
        </div>
        <div className="grid md:grid-cols-2 gap-x-8 gap-y-1.5 text-sm">
          {Object.entries(PARAM_LABELS).map(([key, label]) => {
            const ov = OVERRIDDEN.has(key);
            return (
              <div key={key} className="flex justify-between border-b border-edge/50 py-1">
                <span className="text-slate-400">
                  {label}
                  {ov && <span className="ml-1.5 text-[9px] text-orange-400/80 uppercase">override</span>}
                </span>
                <span className={ov ? "mono text-orange-300 font-semibold" : "mono"}>
                  {params[key] == null ? "—"
                    : key.includes("pct") || key.includes("limit") || key.includes("confidence")
                      ? (params[key] < 1 ? fmtPct(params[key]) : params[key])
                      : params[key]}
                </span>
              </div>
            );
          })}
        </div>
        <div className="text-[10px] text-slate-500 mt-3">
          {aggressive
            ? "Highlighted rows are loosened from config.yaml for testnet data collection. NOT mainnet-safe."
            : "Showing mainnet-safe config.yaml defaults."}
        </div>
      </div>
    </div>
  );
}
