"use client";
import { useEffect, useState } from "react";
import clsx from "clsx";
import { api, post, usePoll, getPin, setPin } from "@/lib/api";
import { useStatusStore } from "@/lib/store";
import EquityChart from "@/components/EquityChart";

export default function ControlsPage() {
  const status = useStatusStore((s) => s.status);
  const coins = status?.coins ?? [];
  const [msg, setMsg] = useState<string>("");
  const [pin, setPinState] = useState<string>("");
  useEffect(() => setPinState(getPin()), []);
  const [confirmHalt, setConfirmHalt] = useState(false);
  const [bt, setBt] = useState<any>(null);
  const [btName, setBtName] = useState<string>("");
  const { data: backtests } = usePoll<any[]>("/api/backtests", 60000);

  const act = async (fn: () => Promise<any>, label: string) => {
    try {
      const r = await fn();
      setMsg(`${label}: ${r.note ?? "ok"}`);
    } catch (e) {
      setMsg(`${label} FAILED: ${e}`);
    }
  };

  const toggleCoin = (coin: string) => {
    const cur = new Set(status?.coins_disabled ?? []);
    cur.has(coin) ? cur.delete(coin) : cur.add(coin);
    act(() => post("/api/control/coins", { disabled: [...cur] }), `toggle ${coin}`);
  };

  const loadBacktest = async (name: string) => {
    setBtName(name);
    setBt(await api(`/api/backtests/${name}`));
  };

  const metricsOf = (d: any) =>
    d?.results ?? d?.splits?.test ?? d?.splits?.train ?? null;

  return (
    <div className="grid gap-4">
      {msg && (
        <div className="card border-glow/40 text-sm mono">{msg}</div>
      )}

      <div className="card flex flex-wrap items-center gap-3">
        <span className="label">Control PIN</span>
        <input
          type="password"
          value={pin}
          onChange={(e) => { setPinState(e.target.value); setPin(e.target.value); }}
          placeholder="HL_REAPER_DASH_TOKEN from .env"
          className="bg-ink border border-edge rounded-lg px-3 py-2 text-sm mono flex-1 min-w-[200px]"
        />
        <span className="text-xs text-slate-500">
          required for halt/resume/toggles/close — stored only in this browser
        </span>
      </div>

      <div className="grid md:grid-cols-2 gap-4">
        <div className="card border-red-500/30">
          <div className="label text-red-300">Emergency</div>
          <p className="text-xs text-slate-400 mt-1 mb-3">
            Halt closes ALL positions and freezes the bot until manually resumed.
            The bot applies it on its next loop (≤10s).
          </p>
          <div className="flex gap-3">
            {!confirmHalt ? (
              <button
                onClick={() => setConfirmHalt(true)}
                className="px-4 py-2 rounded-lg bg-red-500/20 border border-red-500/50 text-red-300 font-semibold hover:bg-red-500/30"
              >
                EMERGENCY HALT
              </button>
            ) : (
              <>
                <button
                  onClick={() => { setConfirmHalt(false); act(() => post("/api/control/halt"), "HALT"); }}
                  className="px-4 py-2 rounded-lg bg-red-600 text-white font-bold"
                >
                  CONFIRM — CLOSE ALL & HALT
                </button>
                <button onClick={() => setConfirmHalt(false)}
                        className="px-4 py-2 rounded-lg border border-edge text-slate-400">
                  cancel
                </button>
              </>
            )}
            <button
              onClick={() => act(() => post("/api/control/resume"), "RESUME")}
              className="px-4 py-2 rounded-lg bg-emerald-500/20 border border-emerald-500/50 text-emerald-300 font-semibold hover:bg-emerald-500/30"
            >
              RESUME
            </button>
          </div>
          {status?.control_request && (
            <div className="text-xs text-amber-300 mt-2">
              pending request: {status.control_request} (applies next bot loop)
            </div>
          )}
        </div>

        <div className="card">
          <div className="label">Per-Coin Trading</div>
          <p className="text-xs text-slate-400 mt-1 mb-3">
            Disabled coins are skipped for new entries; open positions still managed.
          </p>
          <div className="flex gap-2 flex-wrap">
            {coins.map((c) => {
              const off = (status?.coins_disabled ?? []).includes(c);
              return (
                <button
                  key={c}
                  onClick={() => toggleCoin(c)}
                  className={clsx(
                    "px-4 py-2 rounded-lg border font-semibold",
                    off
                      ? "border-red-500/40 text-red-300 bg-red-500/10"
                      : "border-emerald-500/40 text-emerald-300 bg-emerald-500/10"
                  )}
                >
                  {c} {off ? "OFF" : "ON"}
                </button>
              );
            })}
          </div>
          <div className="label mt-4 mb-2">Manual Close</div>
          <div className="flex gap-2 flex-wrap">
            {coins.map((c) => (
              <button
                key={c}
                onClick={() => act(() => post("/api/control/close", { coin: c }), `close ${c}`)}
                className="px-3 py-1.5 rounded-lg border border-edge text-slate-300 text-sm hover:bg-edge"
              >
                close {c}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="card">
        <div className="label mb-2">Backtest Results Viewer</div>
        <div className="flex gap-2 flex-wrap mb-3">
          {(backtests ?? []).slice(0, 12).map((b) => (
            <button
              key={b.name}
              onClick={() => loadBacktest(b.name)}
              className={clsx(
                "px-3 py-1 rounded-lg text-xs border mono",
                btName === b.name ? "bg-edge border-glow/50" : "border-edge text-slate-400"
              )}
            >
              {b.name.replace(".json", "")}
            </button>
          ))}
        </div>
        {bt && (
          <div className="grid gap-3">
            {bt.splits ? (
              <div className="grid md:grid-cols-3 gap-3">
                {["train", "validation", "test"].map((k) => {
                  const m = bt.splits[k];
                  if (!m) return null;
                  return (
                    <div key={k} className="border border-edge rounded-lg p-3">
                      <div className="label">{k}</div>
                      <div className="text-sm mono mt-1 grid gap-0.5">
                        <span>return {m.total_return_pct?.toFixed(2)}%</span>
                        <span>PF {m.profit_factor?.toFixed?.(2) ?? "∞"}</span>
                        <span>win {(m.win_rate * 100)?.toFixed(0)}% · {m.total_trades} trades</span>
                        <span>maxDD {m.max_drawdown_pct?.toFixed(2)}%</span>
                      </div>
                      {m.equity_curve && <EquityChart points={m.equity_curve} height={100} />}
                    </div>
                  );
                })}
              </div>
            ) : metricsOf(bt) ? (
              <div className="text-sm mono">{JSON.stringify(metricsOf(bt)).slice(0, 600)}</div>
            ) : (
              <pre className="text-xs text-slate-400 overflow-auto max-h-64">
                {JSON.stringify(bt, null, 1).slice(0, 3000)}
              </pre>
            )}
            {bt.oos_degraded != null && (
              <div className={bt.oos_degraded ? "text-red-300 text-sm" : "text-emerald-300 text-sm"}>
                {bt.oos_degraded
                  ? "⚠ OOS degrades >30% vs training — overfit signal"
                  : "OOS degradation check passed"}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
