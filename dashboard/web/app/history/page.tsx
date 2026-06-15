"use client";
import { useEffect, useMemo, useState } from "react";
import clsx from "clsx";
import { api, usePoll, fmtUsd, fmtPct, fmtTs } from "@/lib/api";
import EquityChart from "@/components/EquityChart";

type Summary = {
  n_trades: number; net_pnl?: number; gross_pnl?: number; fees?: number;
  win_rate?: number; wins?: number; losses?: number; profit_factor?: number | null;
  avg_pnl?: number; avg_hold_min?: number; first_ts?: number; last_ts?: number;
  per_coin?: Record<string, { n: number; net: number; fees: number; wins: number; win_rate: number | null }>;
  best?: { coin: string; pnl: number; ts: number };
  worst?: { coin: string; pnl: number; ts: number };
};
type Daily = { date: string; net: number; gross: number; fees: number; n: number; wins: number; win_rate: number | null; cumulative: number };
type Trade = {
  coin: string; direction: string; entry_ts: number; exit_ts: number;
  hold_minutes: number; entry_px: number; exit_px: number; qty: number;
  n_fills: number; gross_pnl: number; fees: number; realized_pnl: number;
};

const COINS = ["BTC", "ETH", "SOL", "ARB", "AVAX", "DOGE", "WIF"];
const pnlColor = (v: number) => (v > 0 ? "text-emerald-400" : v < 0 ? "text-red-400" : "text-slate-400");

function Stat({ label, value, sub, color }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div className="card p-3">
      <div className="label text-[11px]">{label}</div>
      <div className={clsx("text-xl md:text-2xl font-semibold mono mt-0.5", color)}>{value}</div>
      {sub && <div className="text-[11px] text-slate-500 mt-0.5">{sub}</div>}
    </div>
  );
}

export default function HistoryPage() {
  const { data: summary } = usePoll<Summary>("/api/history/summary", 30000);
  const { data: daily } = usePoll<Daily[]>("/api/history/daily", 30000);

  // trade table filters
  const [coin, setCoin] = useState("");
  const [direction, setDirection] = useState("");
  const [result, setResult] = useState("");
  const [sort, setSort] = useState("exit_ts");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [trades, setTrades] = useState<Trade[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);

  const qs = useMemo(() => {
    const p = new URLSearchParams();
    if (coin) p.set("coin", coin);
    if (direction) p.set("direction", direction);
    if (result) p.set("result", result);
    p.set("sort", sort);
    p.set("order", order);
    return p.toString();
  }, [coin, direction, result, sort, order]);

  useEffect(() => {
    setLoading(true);
    api<{ total: number; trades: Trade[] }>(`/api/history/trades?${qs}&limit=1000`)
      .then((d) => { setTrades(d.trades); setTotal(d.total); })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [qs]);

  const toggleSort = (col: string) => {
    if (sort === col) setOrder(order === "asc" ? "desc" : "asc");
    else { setSort(col); setOrder("desc"); }
  };
  const arrow = (col: string) => (sort === col ? (order === "asc" ? " ▲" : " ▼") : "");

  const s = summary;
  const span = s?.first_ts && s?.last_ts
    ? `${new Date(s.first_ts).toLocaleDateString()} – ${new Date(s.last_ts).toLocaleDateString()}`
    : "—";

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div>
          <h1 className="text-lg font-semibold">Trade History</h1>
          <p className="text-xs text-slate-500">
            Round-trip trades reconstructed from exchange fills (realized PnL, net of fees).
            Per-trade — not per-fill.
          </p>
        </div>
        <a
          href={`/api/history/export.csv?${qs}`}
          className="px-3 py-1.5 rounded-lg text-sm bg-edge hover:bg-edge/70 text-white"
        >
          ⬇ Export CSV{coin || direction || result ? " (filtered)" : " (all)"}
        </a>
      </div>

      {/* all-time summary */}
      {s && s.n_trades > 0 ? (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
            <Stat label="All-time net PnL" value={fmtUsd(s.net_pnl)} color={pnlColor(s.net_pnl ?? 0)}
              sub={`gross ${fmtUsd(s.gross_pnl)} · fees ${fmtUsd(s.fees)}`} />
            <Stat label="Trades" value={String(s.n_trades)} sub={`${s.wins}W / ${s.losses}L · ${span}`} />
            <Stat label="Win rate" value={fmtPct(s.win_rate)} sub={`profit factor ${s.profit_factor ?? "—"}`} />
            <Stat label="Avg / trade" value={fmtUsd(s.avg_pnl)} sub={`avg hold ${s.avg_hold_min}m`} />
          </div>
          {(s.best || s.worst) && (
            <div className="grid grid-cols-2 gap-2.5">
              <Stat label="Best trade" value={fmtUsd(s.best?.pnl)} color="text-emerald-400"
                sub={`${s.best?.coin} · ${fmtTs(s.best?.ts)}`} />
              <Stat label="Worst trade" value={fmtUsd(s.worst?.pnl)} color="text-red-400"
                sub={`${s.worst?.coin} · ${fmtTs(s.worst?.ts)}`} />
            </div>
          )}
        </>
      ) : (
        <div className="card p-8 text-center text-slate-500 text-sm">
          {s ? "No closed round-trip trades yet." : "Loading…"}
        </div>
      )}

      {/* cumulative PnL + per-coin */}
      <div className="grid md:grid-cols-2 gap-4">
        <div className="card p-3">
          <div className="label mb-2">Cumulative realized PnL</div>
          <EquityChart
            points={(daily ?? []).map((d) => d.cumulative)}
            labels={(daily ?? []).map((d) => d.date.slice(5))}
            height={200}
          />
        </div>
        <div className="card p-3">
          <div className="label mb-2">By coin</div>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[11px] text-slate-500 text-left">
                <th className="py-1">Coin</th><th className="text-right">Trades</th>
                <th className="text-right">Win%</th><th className="text-right">Net PnL</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(s?.per_coin ?? {})
                .sort((a, b) => b[1].net - a[1].net)
                .map(([c, v]) => (
                  <tr key={c} className="border-t border-edge/50">
                    <td className="py-1.5 font-medium">{c}</td>
                    <td className="text-right mono">{v.n}</td>
                    <td className="text-right mono text-slate-400">{fmtPct(v.win_rate)}</td>
                    <td className={clsx("text-right mono", pnlColor(v.net))}>{fmtUsd(v.net)}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* daily breakdown */}
      <div className="card p-3">
        <div className="label mb-2">Daily PnL</div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[11px] text-slate-500 text-left">
                <th className="py-1">Date</th><th className="text-right">Trades</th>
                <th className="text-right">Win%</th><th className="text-right">Gross</th>
                <th className="text-right">Fees</th><th className="text-right">Net</th>
                <th className="text-right">Cumulative</th>
              </tr>
            </thead>
            <tbody>
              {[...(daily ?? [])].reverse().map((d) => (
                <tr key={d.date} className="border-t border-edge/50">
                  <td className="py-1.5 mono">{d.date}</td>
                  <td className="text-right mono">{d.n}</td>
                  <td className="text-right mono text-slate-400">{fmtPct(d.win_rate)}</td>
                  <td className="text-right mono text-slate-400">{fmtUsd(d.gross)}</td>
                  <td className="text-right mono text-slate-500">{fmtUsd(d.fees)}</td>
                  <td className={clsx("text-right mono", pnlColor(d.net))}>{fmtUsd(d.net)}</td>
                  <td className={clsx("text-right mono", pnlColor(d.cumulative))}>{fmtUsd(d.cumulative)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* trades table + filters */}
      <div className="card p-3">
        <div className="flex items-center justify-between flex-wrap gap-2 mb-3">
          <div className="label">
            Trades {loading ? "…" : `(${total}${coin || direction || result ? " filtered" : ""})`}
          </div>
          <div className="flex gap-2 text-sm">
            <select value={coin} onChange={(e) => setCoin(e.target.value)}
              className="bg-edge/50 border border-edge rounded px-2 py-1">
              <option value="">All coins</option>
              {COINS.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
            <select value={direction} onChange={(e) => setDirection(e.target.value)}
              className="bg-edge/50 border border-edge rounded px-2 py-1">
              <option value="">Long & Short</option>
              <option value="LONG">Long</option>
              <option value="SHORT">Short</option>
            </select>
            <select value={result} onChange={(e) => setResult(e.target.value)}
              className="bg-edge/50 border border-edge rounded px-2 py-1">
              <option value="">Wins & Losses</option>
              <option value="win">Wins</option>
              <option value="loss">Losses</option>
            </select>
          </div>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm whitespace-nowrap">
            <thead>
              <tr className="text-[11px] text-slate-500 text-left select-none">
                {[
                  ["exit_ts", "Closed"], ["coin", "Coin"], ["direction", "Dir"],
                  ["hold_minutes", "Hold"], ["n_fills", "Fills"],
                  ["gross_pnl", "Gross"], ["fees", "Fees"], ["realized_pnl", "Net PnL"],
                ].map(([k, lbl], i) => (
                  <th key={k} onClick={() => toggleSort(k)}
                    className={clsx("py-1 cursor-pointer hover:text-slate-300",
                      i >= 3 && "text-right")}>
                    {lbl}{arrow(k)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {trades.map((t, i) => (
                <tr key={i} className="border-t border-edge/50 hover:bg-edge/30">
                  <td className="py-1.5 mono text-slate-400 text-xs">{fmtTs(t.exit_ts)}</td>
                  <td className="font-medium">{t.coin}</td>
                  <td className={t.direction === "LONG" ? "text-emerald-400" : "text-red-400"}>
                    {t.direction === "LONG" ? "▲ LONG" : "▼ SHORT"}
                  </td>
                  <td className="text-right mono text-slate-400">{t.hold_minutes}m</td>
                  <td className="text-right mono text-slate-500">{t.n_fills}</td>
                  <td className="text-right mono text-slate-400">{fmtUsd(t.gross_pnl)}</td>
                  <td className="text-right mono text-slate-500">{fmtUsd(t.fees)}</td>
                  <td className={clsx("text-right mono font-medium", pnlColor(t.realized_pnl))}>
                    {fmtUsd(t.realized_pnl)}
                  </td>
                </tr>
              ))}
              {!loading && trades.length === 0 && (
                <tr><td colSpan={8} className="py-6 text-center text-slate-500">No trades match.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
