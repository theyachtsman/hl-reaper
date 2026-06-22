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
  per_band?: Record<string, { n: number; net: number; fees: number; wins: number; win_rate: number | null }>;
  best?: { coin: string; pnl: number; ts: number };
  worst?: { coin: string; pnl: number; ts: number };
};
type Daily = { date: string; net: number; gross: number; fees: number; n: number; wins: number; win_rate: number | null; cumulative: number };
type Trade = {
  coin: string; direction: string; band?: string | null; entry_ts: number; exit_ts: number;
  hold_minutes: number; entry_px: number; exit_px: number; qty: number;
  n_fills: number; gross_pnl: number; fees: number; realized_pnl: number;
};
type AuditRow = {
  id: number; ts: number; coin: string; side: string; action: string;
  size: number | null; price: number | null; status: string | null;
  note: string | null; band: string | null;
};

const BAND_COLOR = (b?: string | null) =>
  b === "scalp" ? "text-cyan-300" : b === "trend" ? "text-purple-300" : "text-slate-600";

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

// viewer's UTC offset (minutes, JS convention: UTC minus local) so the server
// can bucket Daily PnL on the local calendar day instead of UTC. Stable per
// session — computed once.
const TZ_OFFSET = new Date().getTimezoneOffset();

export default function HistoryPage() {
  const { data: summary } = usePoll<Summary>("/api/history/summary", 30000);
  const { data: daily } = usePoll<Daily[]>(
    `/api/history/daily?tz_offset=${TZ_OFFSET}`, 30000);

  // trade table filters
  const [coin, setCoin] = useState("");
  const [direction, setDirection] = useState("");
  const [band, setBand] = useState("");
  const [result, setResult] = useState("");
  const [sort, setSort] = useState("exit_ts");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [limit, setLimit] = useState(50);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);

  const qs = useMemo(() => {
    const p = new URLSearchParams();
    if (coin) p.set("coin", coin);
    if (direction) p.set("direction", direction);
    if (band) p.set("band", band);
    if (result) p.set("result", result);
    p.set("sort", sort);
    p.set("order", order);
    return p.toString();
  }, [coin, direction, band, result, sort, order]);

  useEffect(() => {
    setLoading(true);
    api<{ total: number; trades: Trade[] }>(`/api/history/trades?${qs}&limit=${limit}`)
      .then((d) => { setTrades(d.trades); setTotal(d.total); })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [qs, limit]);

  const toggleSort = (col: string) => {
    if (sort === col) setOrder(order === "asc" ? "desc" : "asc");
    else { setSort(col); setOrder("desc"); }
  };
  const arrow = (col: string) => (sort === col ? (order === "asc" ? " ▲" : " ▼") : "");

  // trade audit log (raw OPEN/CLOSE actions the bot logged) — moved here from
  // the Signals page; filterable, skips hidden unless explicitly included
  const [aCoin, setACoin] = useState("");
  const [aAction, setAAction] = useState("");
  const [aBand, setABand] = useState("");
  const [aSkips, setASkips] = useState(false);
  const [aLimit, setALimit] = useState(200);
  const auditQs = useMemo(() => {
    const p = new URLSearchParams();
    if (aCoin) p.set("coin", aCoin);
    if (aAction) p.set("action", aAction);
    if (aBand) p.set("band", aBand);
    if (aSkips) p.set("include_skips", "true");
    p.set("limit", String(aLimit));
    return p.toString();
  }, [aCoin, aAction, aBand, aSkips, aLimit]);
  // CSV export uses the same filters but no row limit (exports everything that
  // matches, not just the page currently shown).
  const auditCsvQs = useMemo(() => {
    const p = new URLSearchParams();
    if (aCoin) p.set("coin", aCoin);
    if (aAction) p.set("action", aAction);
    if (aBand) p.set("band", aBand);
    if (aSkips) p.set("include_skips", "true");
    return p.toString();
  }, [aCoin, aAction, aBand, aSkips]);
  const { data: audit } = usePoll<{ total: number; trades: AuditRow[] }>(
    `/api/trades?${auditQs}`, 15000);

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
          ⬇ Export CSV{coin || direction || band || result ? " (filtered)" : " (all)"}
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

      {/* by band */}
      <div className="card p-3">
        <div className="label mb-2">By band</div>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-[11px] text-slate-500 text-left">
              <th className="py-1">Band</th><th className="text-right">Trades</th>
              <th className="text-right">Win%</th><th className="text-right">Net PnL</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(s?.per_band ?? {})
              .sort((a, b) => b[1].net - a[1].net)
              .map(([b, v]) => (
                <tr key={b} className="border-t border-edge/50">
                  <td className={clsx("py-1.5 font-medium uppercase", BAND_COLOR(b))}>
                    {b === "unattributed" ? "—" : b}
                  </td>
                  <td className="text-right mono">{v.n}</td>
                  <td className="text-right mono text-slate-400">{fmtPct(v.win_rate)}</td>
                  <td className={clsx("text-right mono", pnlColor(v.net))}>{fmtUsd(v.net)}</td>
                </tr>
              ))}
            {!Object.keys(s?.per_band ?? {}).length && (
              <tr><td colSpan={4} className="py-3 text-center text-slate-500">no banded trades yet</td></tr>
            )}
          </tbody>
        </table>
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
            Trades{" "}
            {loading ? "…" : (
              <span className="text-slate-500">
                (showing {Math.min(limit, total)} of {total}
                {coin || direction || result ? " filtered" : ""})
              </span>
            )}
          </div>
          <div className="flex gap-2 text-sm flex-wrap">
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
            <select value={band} onChange={(e) => setBand(e.target.value)}
              className="bg-edge/50 border border-edge rounded px-2 py-1">
              <option value="">All bands</option>
              <option value="scalp">Scalp</option>
              <option value="trend">Trend</option>
            </select>
            <select value={result} onChange={(e) => setResult(e.target.value)}
              className="bg-edge/50 border border-edge rounded px-2 py-1">
              <option value="">Wins & Losses</option>
              <option value="win">Wins</option>
              <option value="loss">Losses</option>
            </select>
            <select value={limit} onChange={(e) => setLimit(Number(e.target.value))}
              className="bg-edge/50 border border-edge rounded px-2 py-1">
              {[50, 100, 200, 500].map((n) => (
                <option key={n} value={n}>{n} rows</option>
              ))}
            </select>
          </div>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm whitespace-nowrap">
            <thead>
              <tr className="text-[11px] text-slate-500 text-left select-none">
                {[
                  ["exit_ts", "Closed"], ["coin", "Coin"], ["direction", "Dir"],
                ].map(([k, lbl]) => (
                  <th key={k} onClick={() => toggleSort(k)}
                    className="py-1 cursor-pointer hover:text-slate-300">
                    {lbl}{arrow(k)}
                  </th>
                ))}
                <th className="py-1">Band</th>
                {[
                  ["hold_minutes", "Hold"], ["n_fills", "Fills"],
                  ["gross_pnl", "Gross"], ["fees", "Fees"], ["realized_pnl", "Net PnL"],
                ].map(([k, lbl]) => (
                  <th key={k} onClick={() => toggleSort(k)}
                    className="py-1 cursor-pointer hover:text-slate-300 text-right">
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
                  <td className={clsx("uppercase text-xs", BAND_COLOR(t.band))}>
                    {t.band ?? "—"}
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
                <tr><td colSpan={9} className="py-6 text-center text-slate-500">No trades match.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* trade audit log — raw bot actions (OPEN/CLOSE), filterable */}
      <div className="card p-3">
        <div className="flex items-center justify-between flex-wrap gap-2 mb-1">
          <div className="label">
            Trade Audit Log{" "}
            {audit ? (
              <span className="text-slate-500">
                (showing {audit.trades.length} of {audit.total}
                {aCoin || aAction || aBand || aSkips ? " filtered" : ""})
              </span>
            ) : "…"}
          </div>
          <div className="flex gap-2 text-sm flex-wrap items-center">
            <select value={aCoin} onChange={(e) => setACoin(e.target.value)}
              className="bg-edge/50 border border-edge rounded px-2 py-1">
              <option value="">All coins</option>
              {COINS.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
            <select value={aAction} onChange={(e) => setAAction(e.target.value)}
              className="bg-edge/50 border border-edge rounded px-2 py-1">
              <option value="">All actions</option>
              <option value="OPEN">Open</option>
              <option value="CLOSE">Close</option>
              <option value="TEST">Test</option>
            </select>
            <select value={aBand} onChange={(e) => setABand(e.target.value)}
              className="bg-edge/50 border border-edge rounded px-2 py-1">
              <option value="">All bands</option>
              <option value="scalp">Scalp</option>
              <option value="trend">Trend</option>
            </select>
            <select value={aLimit} onChange={(e) => setALimit(Number(e.target.value))}
              className="bg-edge/50 border border-edge rounded px-2 py-1">
              {[100, 200, 500, 1000, 2000].map((n) => (
                <option key={n} value={n}>{n} rows</option>
              ))}
            </select>
            <label className="flex items-center gap-1.5 text-xs text-slate-400 cursor-pointer select-none">
              <input type="checkbox" checked={aSkips}
                onChange={(e) => setASkips(e.target.checked)} />
              show skips
            </label>
            <a
              href={`/api/trades/export.csv?${auditCsvQs}`}
              className="px-3 py-1.5 rounded-lg text-sm bg-edge hover:bg-edge/70 text-white"
            >
              ⬇ Export CSV{aCoin || aAction || aBand || aSkips ? " (filtered)" : " (all)"}
            </a>
          </div>
        </div>
        <p className="text-[11px] text-slate-500 mb-2">
          Raw bot actions as logged. Filter skips (no-confirmation / maker-timeout) are
          hidden unless “show skips” is on.
        </p>
        <div className="overflow-x-auto max-h-[600px] overflow-y-auto">
          <table className="w-full text-xs whitespace-nowrap">
            <thead className="text-left text-slate-500 sticky top-0 bg-panel">
              <tr className="text-[11px]">
                <th className="py-1">Time</th><th>Coin</th><th>Band</th><th>Side</th><th>Action</th>
                <th className="text-right">Size</th><th className="text-right">Price</th>
                <th>Status</th><th>Note</th>
              </tr>
            </thead>
            <tbody className="mono">
              {(audit?.trades ?? []).map((t) => (
                <tr key={t.id} className="border-t border-edge/50 hover:bg-edge/30">
                  <td className="py-1.5 text-slate-400">{fmtTs(t.ts)}</td>
                  <td className="font-semibold">{t.coin}</td>
                  <td className={clsx("uppercase", BAND_COLOR(t.band))}>{t.band ?? "—"}</td>
                  <td className={t.side === "LONG" ? "text-emerald-400" : t.side === "SHORT" ? "text-red-400" : "text-slate-500"}>{t.side}</td>
                  <td>{t.action}</td>
                  <td className="text-right text-slate-400">{t.size != null ? Number(t.size).toFixed(4) : "—"}</td>
                  <td className="text-right text-slate-400">{t.price ?? "—"}</td>
                  <td className="text-slate-400">{t.status ?? "—"}</td>
                  <td className="text-slate-500 max-w-[320px] truncate" title={t.note ?? ""}>{t.note ?? ""}</td>
                </tr>
              ))}
              {audit && audit.trades.length === 0 && (
                <tr><td colSpan={9} className="py-6 text-center text-slate-500">No actions match.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
