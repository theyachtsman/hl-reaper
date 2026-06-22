"use client";
import { useCallback, useEffect, useState } from "react";
import clsx from "clsx";
import { api, post, usePoll, getPin, setPin, fmtUsd } from "@/lib/api";
import { useStatusStore } from "@/lib/store";

// ---------------------------------------------------------------------------
// Live control plane. Every tunable maps to one dotted config key; Apply POSTs
// it to /api/config (validated server-side) and the bot merges it onto
// config.yaml at the top of its next loop (<=10s) — no restart, no SSH.
// ---------------------------------------------------------------------------
type Cfg = {
  effective: Record<string, any>;
  defaults: Record<string, any>;
  overrides: Record<string, any>;
  schema: Record<string, { type: string; min?: number; max?: number }>;
  coins: string[];
};

function reqDelete(path: string) {
  return api(path, { method: "DELETE", headers: { "X-Dash-Token": getPin() } });
}

type PresetInfo = {
  id: string;
  display_name: string;
  description: string;
  warning: string | null;
  settings: Record<string, any>;
};
type ActivePreset = {
  active: string;
  display_name: string;
  last_applied: string | null;
  last_display_name: string | null;
};

// preset id → accent classes for the pill (active = solid, idle = outlined)
const PRESET_ACCENT: Record<string, { on: string; off: string }> = {
  DUAL_BAND: {
    on: "bg-teal-500/25 border-teal-500/70 text-teal-200",
    off: "border-teal-500/40 text-teal-300 hover:bg-teal-500/10",
  },
  BASELINE: {
    on: "bg-amber-500/25 border-amber-500/70 text-amber-200",
    off: "border-amber-500/40 text-amber-300 hover:bg-amber-500/10",
  },
  SCALPER: {
    on: "bg-cyan-500/25 border-cyan-500/70 text-cyan-200",
    off: "border-cyan-500/40 text-cyan-300 hover:bg-cyan-500/10",
  },
  SHORT_HUNTER: {
    on: "bg-red-500/25 border-red-500/70 text-red-200",
    off: "border-red-500/40 text-red-300 hover:bg-red-500/10",
  },
  TREND_RIDER: {
    on: "bg-purple-500/25 border-purple-500/70 text-purple-200",
    off: "border-purple-500/40 text-purple-300 hover:bg-purple-500/10",
  },
  CONSERVATIVE: {
    on: "bg-blue-500/25 border-blue-500/70 text-blue-200",
    off: "border-blue-500/40 text-blue-300 hover:bg-blue-500/10",
  },
};
const ACCENT_FALLBACK = {
  on: "bg-edge border-glow/60 text-white",
  off: "border-edge text-slate-300 hover:bg-edge",
};

export default function ControlsPage() {
  const status = useStatusStore((s) => s.status);
  const coins = status?.coins ?? [];
  const [cfg, setCfg] = useState<Cfg | null>(null);
  const [msg, setMsg] = useState<string>("");
  const [pin, setPinState] = useState<string>("");
  const [confirmHalt, setConfirmHalt] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const [presets, setPresets] = useState<PresetInfo[]>([]);
  const [active, setActive] = useState<ActivePreset | null>(null);
  const [pendingBaseline, setPendingBaseline] = useState(false);
  const { data: pos } = usePoll<any>("/api/positions", 5000);

  useEffect(() => setPinState(getPin()), []);

  const reload = useCallback(async () => {
    try {
      setCfg(await api<Cfg>("/api/config"));
    } catch (e) {
      setMsg(`load config FAILED: ${e}`);
    }
  }, []);
  const loadActive = useCallback(() => {
    api<ActivePreset>("/api/presets/active").then(setActive).catch(() => {});
  }, []);
  useEffect(() => { reload(); }, [reload]);
  useEffect(() => {
    api<{ presets: PresetInfo[] }>("/api/presets")
      .then((d) => setPresets(d.presets)).catch(() => {});
  }, []);
  useEffect(() => {
    loadActive();
    const id = setInterval(loadActive, 15000);
    return () => clearInterval(id);
  }, [loadActive]);

  const act = async (fn: () => Promise<any>, label: string) => {
    try {
      const r = await fn();
      setMsg(`${label}: ${r?.note ?? "ok"}`);
      await reload();
      loadActive();
    } catch (e) {
      setMsg(`${label} FAILED: ${e}`);
    }
  };

  const applyPreset = (id: string) =>
    act(() => post("/api/presets/apply", { preset_id: id }), `preset ${id}`);

  const setKey = (key: string, value: any) =>
    act(() => post("/api/config", { key, value }), `set ${key}`);
  const clearKey = (key: string) =>
    act(() => reqDelete(`/api/config/${key}`), `reset ${key}`);
  const sendCmd = (command: string) =>
    act(() => post("/api/bot/command", { command }), command);

  if (!cfg) {
    return <div className="card text-sm mono text-slate-400">loading config…</div>;
  }

  const state = status?.risk_state ?? "—";
  const stateColor =
    state === "ACTIVE" ? "text-emerald-300"
      : state === "MANAGING" ? "text-amber-300"
        : state === "HALTED" || state === "COOLDOWN" ? "text-red-300"
          : "text-slate-300";

  return (
    <div className="grid gap-4">
      {msg && <div className="card border-glow/40 text-sm mono">{msg}</div>}

      {/* ---- Strategy Presets (top of page) ---- */}
      <div className="card grid gap-3">
        <div className="label text-glow/80">Strategy Presets</div>
        <div className="flex flex-wrap gap-2">
          {presets.map((p) => {
            const isActive = active?.active === p.id;
            const accent = PRESET_ACCENT[p.id] ?? ACCENT_FALLBACK;
            return (
              <button
                key={p.id}
                title={p.description}
                onClick={() =>
                  p.id === "BASELINE" ? setPendingBaseline(true) : applyPreset(p.id)
                }
                className={clsx(
                  "px-4 py-1.5 rounded-full border text-sm font-semibold whitespace-nowrap transition",
                  isActive ? accent.on : accent.off
                )}
              >
                {p.id === "BASELINE" && <span title="structural gates disabled">⚠ </span>}
                {p.display_name}
                {isActive && " ✓"}
              </button>
            );
          })}
        </div>

        {pendingBaseline && (
          <div className="border border-amber-500/50 bg-amber-500/10 rounded-lg p-3 grid gap-2">
            <div className="text-sm text-amber-200">
              This disables structural gates and increases trade frequency.
              Use only in trending markets. Continue?
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => { setPendingBaseline(false); applyPreset("BASELINE"); }}
                className="px-3 py-1.5 rounded-lg bg-amber-600 text-white text-sm font-bold">
                Apply BASELINE
              </button>
              <button
                onClick={() => setPendingBaseline(false)}
                className="px-3 py-1.5 rounded-lg border border-edge text-slate-400 text-sm">
                Cancel
              </button>
            </div>
          </div>
        )}

        <div className="text-sm">
          {active?.active === "CUSTOM" ? (
            <span className="text-slate-300">
              Active: <span className="font-semibold text-slate-200">CUSTOM</span>
              {active.last_display_name && (
                <span className="text-slate-500"> (modified from {active.last_display_name})</span>
              )}
            </span>
          ) : active ? (
            <span className="text-slate-300">
              Active: <span className="font-semibold text-glow">{active.display_name}</span>
              {(() => {
                const cur = presets.find((p) => p.id === active.active);
                return cur ? <span className="text-slate-500"> — {cur.description}</span> : null;
              })()}
            </span>
          ) : (
            <span className="text-slate-500">loading preset…</span>
          )}
        </div>

        <div className="text-xs text-amber-300/80 border-t border-edge pt-2">
          ⚠ Modifying any setting below switches to CUSTOM mode. Apply a preset to
          restore a named configuration. Takes effect within ~10s, no restart.
        </div>
      </div>

      {/* ---- PIN ---- */}
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
          required for every write — stored only in this browser
        </span>
      </div>

      {/* ---- Section 1 — status & quick actions ---- */}
      <div className="card">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="label">Bot Status</div>
            <div className="mt-1 flex items-center gap-3">
              <span className={clsx("text-lg font-bold", stateColor)}>● {state}</span>
              <span className="text-xs text-slate-400 mono">
                loop {cfg.effective["trading.loop_interval_seconds"] ?? 10}s ·
                hb {status?.heartbeat_age_s ?? "—"}s ago ·
                open {pos?.positions?.length ?? 0}
              </span>
            </div>
            {status?.risk_reason && (
              <div className="text-xs text-slate-500 mt-1">{status.risk_reason}</div>
            )}
          </div>
          <div className="flex gap-2 flex-wrap">
            <button onClick={() => sendCmd("pause")}
              className="px-3 py-2 rounded-lg bg-amber-500/15 border border-amber-500/40 text-amber-300 text-sm font-semibold hover:bg-amber-500/25">
              ⏸ Pause
            </button>
            <button onClick={() => sendCmd("resume")}
              className="px-3 py-2 rounded-lg bg-emerald-500/15 border border-emerald-500/40 text-emerald-300 text-sm font-semibold hover:bg-emerald-500/25">
              ▶ Resume
            </button>
            <button onClick={() => sendCmd("close_all")}
              className="px-3 py-2 rounded-lg bg-red-500/15 border border-red-500/40 text-red-300 text-sm font-semibold hover:bg-red-500/25">
              ⚠ Close All
            </button>
          </div>
        </div>
        <div className="mt-3 pt-3 border-t border-edge flex flex-wrap items-center gap-3">
          <span className="text-xs text-slate-500">
            Hard emergency stop (HALT closes all + freezes until manual resume):
          </span>
          {!confirmHalt ? (
            <button onClick={() => setConfirmHalt(true)}
              className="px-3 py-1.5 rounded-lg bg-red-500/20 border border-red-500/50 text-red-300 text-sm font-semibold">
              EMERGENCY HALT
            </button>
          ) : (
            <>
              <button onClick={() => { setConfirmHalt(false); act(() => post("/api/control/halt"), "HALT"); }}
                className="px-3 py-1.5 rounded-lg bg-red-600 text-white text-sm font-bold">
                CONFIRM — CLOSE ALL & HALT
              </button>
              <button onClick={() => setConfirmHalt(false)}
                className="px-3 py-1.5 rounded-lg border border-edge text-slate-400 text-sm">cancel</button>
            </>
          )}
          {status?.control_request && (
            <span className="text-xs text-amber-300">pending: {status.control_request}</span>
          )}
        </div>
      </div>

      {/* ═══ GLOBAL ═══ (applies across both bands) */}
      <Section title="Global">
        <Slider cfg={cfg} ck="risk.max_leverage" label="Max Leverage (hard ceiling 10x)"
          min={1} max={10} step={0.5} unit="x" onApply={setKey} onReset={clearKey}
          note="hard ceiling — applies to both bands" />
        <div className="label mt-2 mb-1">Active Trading Pairs</div>
        <div className="flex gap-2 flex-wrap">
          {coins.map((c) => {
            const activeList: string[] = cfg.effective["trading.coins_active"] ?? coins;
            const on = activeList.includes(c);
            return (
              <button key={c}
                onClick={() => {
                  const next = on ? activeList.filter((x) => x !== c) : [...activeList, c];
                  setKey("trading.coins_active", next);
                }}
                className={clsx("px-3 py-1.5 rounded-lg border text-sm font-semibold",
                  on ? "border-emerald-500/40 text-emerald-300 bg-emerald-500/10"
                    : "border-red-500/40 text-red-300 bg-red-500/10")}>
                {c} {on ? "ON" : "OFF"}
              </button>
            );
          })}
        </div>
        <div className="label mt-4 mb-1">Per-coin overrides (blank = use band default)</div>
        <div className="grid gap-2">
          {coins.map((c) => (
            <PerCoin key={c} coin={c} cfg={cfg} onApply={setKey} onReset={clearKey} />
          ))}
        </div>
        <div className="label mt-4 mb-1">Global direction master (both bands)</div>
        <DirectionControls cfg={cfg} onApply={setKey} />
        <div className="label mt-4 text-slate-300/80">Funding (aggregator — both bands)</div>
        <Toggle cfg={cfg} ck="risk.funding_hard_block_enabled"
          label="Funding hard-block enabled (blocks crowded LONGs)" onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="risk.funding_hard_block_conf" label="Funding block threshold"
          min={0} max={1} step={0.05} dp={2} onApply={setKey} onReset={clearKey}
          note="FundingRate SHORT conf ≥ this blocks LONG" />
        <Toggle cfg={cfg} ck="risk.funding_hard_block_short_enabled"
          label="Mirror funding block for SHORTs" onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="risk.funding_hard_block_short_conf" label="SHORT funding block threshold"
          min={0} max={1} step={0.05} dp={2} onApply={setKey} onReset={clearKey} />
        <Toggle cfg={cfg} ck="risk.funding_smooth_mapping_enabled"
          label="Smooth funding mapping (ON: mild-positive funding leans SHORT; OFF: binary)"
          onApply={setKey} onReset={clearKey} />
        <div className="label mt-4 text-slate-300/80">Taker Fallback (both bands' entries)</div>
        <Toggle cfg={cfg} ck="trading.maker_timeout_fallback_enabled"
          label="Maker-timeout fallback enabled" onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="trading.maker_timeout_fallback_n" label="Consecutive timeouts before fallback"
          min={1} max={10} step={1} onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="trading.maker_timeout_fallback_window_s" label="Fallback window (seconds)"
          min={30} max={600} step={10} unit="s" onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="trading.maker_timeout_exhaustion_atr_mult" label="Exhaustion threshold (ATR mult)"
          min={0.5} max={3.0} step={0.1} unit="x" dp={1} onApply={setKey} onReset={clearKey} />
        <div className="label mt-4 text-slate-300/80">Breakeven Lock (mechanism — R is per band)</div>
        <Toggle cfg={cfg} ck="risk.breakeven_lock_enabled"
          label="Breakeven profit lock enabled" onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="risk.breakeven_lock_buffer_pct" label="Breakeven buffer (% above/below entry)"
          min={0} max={0.5} step={0.01} unit="%" dp={2} onApply={setKey} onReset={clearKey} />
        <div className="label mt-4 text-slate-300/80">Circuit Breakers (global kill switches)</div>
        <Slider cfg={cfg} ck="risk.daily_drawdown_limit" label="Daily Drawdown Limit"
          min={1} max={20} step={1} pct unit="%" onApply={setKey} onReset={clearKey}
          note="below 5% not recommended for mainnet" />
        <Slider cfg={cfg} ck="risk.weekly_drawdown_limit" label="Weekly Drawdown Limit"
          min={1} max={50} step={1} pct unit="%" onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="risk.max_loss_per_trade_pct" label="Max Loss Per Trade"
          min={0.5} max={20} step={0.5} pct unit="%" dp={1} onApply={setKey} onReset={clearKey} />
        <Toggle cfg={cfg} ck="risk.cascade_detection_enabled"
          label="Cascade detection enabled" onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="risk.cascade_oi_drop_pct" label="Cascade OI drop trigger"
          min={5} max={50} step={1} pct unit="%" onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="risk.cascade_window_minutes" label="Cascade window"
          min={1} max={60} step={1} unit="min" onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="risk.cascade_price_move_pct" label="Cascade price move trigger"
          min={1} max={20} step={1} pct unit="%" onApply={setKey} onReset={clearKey} />
        <div className="pt-3 mt-2 border-t border-edge flex items-center gap-3 flex-wrap">
          {!confirmReset ? (
            <button onClick={() => setConfirmReset(true)}
              className="px-4 py-2 rounded-lg bg-red-500/15 border border-red-500/50 text-red-300 font-semibold">
              Reset All to Defaults
            </button>
          ) : (
            <>
              <span className="text-xs text-amber-300">
                Clear ALL {Object.keys(cfg.overrides).length} overrides → config.yaml floor?
              </span>
              <button onClick={() => { setConfirmReset(false); act(() => reqDelete("/api/config"), "reset all"); }}
                className="px-4 py-2 rounded-lg bg-red-600 text-white font-bold">CONFIRM RESET</button>
              <button onClick={() => setConfirmReset(false)}
                className="px-4 py-2 rounded-lg border border-edge text-slate-400">cancel</button>
            </>
          )}
          <span className="text-xs text-slate-500">
            {Object.keys(cfg.overrides).length} active override(s)
          </span>
        </div>
      </Section>

      {/* ═══ SCALP BAND (5m) ═══ */}
      <BandSection title="Scalp Band" subtitle="5m · tight & fast"
        accent="cyan" cfg={cfg} enableKey="trading.scalp_band_enabled"
        otherEnableKey="trading.trend_band_enabled" onApply={setKey}>
        <Slider cfg={cfg} ck="risk.scalp_position_size_usd" label="Position Size (USD)"
          min={10} max={500} step={5} prefix="$" onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="risk.scalp_max_concurrent_positions" label="Max Concurrent Positions"
          min={1} max={7} step={1} onApply={setKey} onReset={clearKey} />
        <div className="label pt-1">Signal Gate</div>
        <Slider cfg={cfg} ck="risk.scalp_min_confidence" label="Minimum Confidence"
          min={0.30} max={0.80} step={0.01} dp={2} onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="risk.scalp_min_model_agreement" label="Minimum Model Agreement"
          min={1} max={6} step={1} onApply={setKey} onReset={clearKey} />
        <div className="label pt-1">Directions</div>
        <BandDirection band="scalp" cfg={cfg} onApply={setKey} />
        <div className="label pt-1">Entry Filters (structural gates — scalp only)</div>
        <Toggle cfg={cfg} ck="risk.scalp_structural_gates_enabled"
          label="Structural gates master (LONG/SHORT)" onApply={setKey} onReset={clearKey} />
        <div className="label text-emerald-300/80 pt-1">LONG structural</div>
        <Toggle cfg={cfg} ck="trading.long_structural_gate_enabled"
          label="LONG structural gate enabled" onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="trading.long_spot_lead_threshold" label="Spot lead threshold"
          min={0} max={1.0} step={0.01} pct unit="%" dp={2} onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="trading.long_oi_rise_threshold" label="OI rise threshold"
          min={0} max={5.0} step={0.1} pct unit="%" dp={2} onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="trading.long_ob_bid_threshold" label="Book bid threshold"
          min={0} max={0.9} step={0.05} dp={2} onApply={setKey} onReset={clearKey}
          note="bid imbalance ≥ this (60/40 = 0.20)" />
        <Toggle cfg={cfg} ck="trading.long_pump_cooldown_enabled"
          label="Pump cooldown (block LONG after sharp pump)" onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="trading.long_pump_threshold_1" label="5m pump threshold"
          min={0.1} max={2.0} step={0.1} pct unit="%" dp={1} onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="trading.long_pump_threshold_2" label="10m pump threshold"
          min={0.1} max={3.0} step={0.1} pct unit="%" dp={1} onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="trading.long_pump_threshold_3" label="15m pump threshold"
          min={0.1} max={4.0} step={0.1} pct unit="%" dp={1} onApply={setKey} onReset={clearKey} />
        <div className="label text-red-300/80 pt-1">SHORT structural</div>
        <Toggle cfg={cfg} ck="trading.short_structural_gate_enabled"
          label="SHORT structural gate enabled" onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="trading.short_spot_lag_threshold" label="Spot lag threshold"
          min={0} max={1.0} step={0.01} pct unit="%" dp={2} onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="trading.short_oi_rise_threshold" label="OI rise threshold"
          min={0} max={5.0} step={0.1} pct unit="%" dp={2} onApply={setKey} onReset={clearKey}
          note="0 = testnet (OI barely moves)" />
        <Slider cfg={cfg} ck="trading.short_ob_ask_threshold" label="Book ask threshold"
          min={0} max={0.9} step={0.05} dp={2} onApply={setKey} onReset={clearKey} />
        <Toggle cfg={cfg} ck="trading.short_dump_cooldown_enabled"
          label="Dump cooldown (block SHORT after sharp drop)" onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="trading.short_dump_threshold_1" label="5m dump threshold"
          min={0.1} max={2.0} step={0.1} pct unit="%" dp={1} onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="trading.short_dump_threshold_2" label="10m dump threshold"
          min={0.1} max={3.0} step={0.1} pct unit="%" dp={1} onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="trading.short_dump_threshold_3" label="15m dump threshold"
          min={0.1} max={4.0} step={0.1} pct unit="%" dp={1} onApply={setKey} onReset={clearKey} />
        <div className="label pt-1">Risk / Stop Loss</div>
        <Slider cfg={cfg} ck="risk.scalp_atr_sl_multiplier" label="ATR Stop Loss Multiplier"
          min={0.5} max={3.0} step={0.1} unit="x" dp={1} onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="risk.scalp_take_profit_r" label="Take Profit (R)"
          min={1.0} max={4.0} step={0.1} unit="R" dp={1} onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="risk.scalp_trail_activation_r" label="Trailing Stop Activation (R)"
          min={0.5} max={3.0} step={0.1} unit="R" dp={1} onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="risk.scalp_max_hold_hours" label="Max Hold Time (hours)"
          min={0.1} max={12} step={0.1} unit="h" dp={1} onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="risk.scalp_breakeven_lock_r" label="Breakeven Lock (R)"
          min={0} max={2.0} step={0.1} unit="R" dp={1} onApply={setKey} onReset={clearKey} />
      </BandSection>

      {/* ═══ TREND BAND (1h) ═══ */}
      <BandSection title="Trend Band" subtitle="1h · wide & patient"
        accent="purple" cfg={cfg} enableKey="trading.trend_band_enabled"
        otherEnableKey="trading.scalp_band_enabled" onApply={setKey}>
        <Slider cfg={cfg} ck="risk.trend_position_size_usd" label="Position Size (USD)"
          min={10} max={1000} step={5} prefix="$" onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="risk.trend_max_concurrent_positions" label="Max Concurrent Positions"
          min={1} max={7} step={1} onApply={setKey} onReset={clearKey} />
        <div className="label pt-1">Signal Gate</div>
        <Slider cfg={cfg} ck="risk.trend_min_confidence" label="Minimum Confidence"
          min={0.30} max={0.80} step={0.01} dp={2} onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="risk.trend_min_model_agreement" label="Minimum Model Agreement"
          min={1} max={6} step={1} onApply={setKey} onReset={clearKey} />
        <div className="label pt-1">Directions</div>
        <BandDirection band="trend" cfg={cfg} onApply={setKey} />
        <div className="text-xs text-slate-500">
          No structural gates — the trend band&apos;s own 1h signal is its gate.
        </div>
        <div className="label pt-1">Risk / Stop Loss</div>
        <Slider cfg={cfg} ck="risk.trend_atr_sl_multiplier" label="ATR Stop Loss Multiplier"
          min={0.5} max={5.0} step={0.1} unit="x" dp={1} onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="risk.trend_take_profit_r" label="Take Profit (R)"
          min={1.0} max={8.0} step={0.1} unit="R" dp={1} onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="risk.trend_trail_activation_r" label="Trailing Stop Activation (R)"
          min={0.5} max={5.0} step={0.1} unit="R" dp={1} onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="risk.trend_max_hold_hours" label="Max Hold Time (hours)"
          min={1} max={168} step={1} unit="h" dp={0} onApply={setKey} onReset={clearKey} />
        <Slider cfg={cfg} ck="risk.trend_breakeven_lock_r" label="Breakeven Lock (R)"
          min={0} max={3.0} step={0.1} unit="R" dp={1} onApply={setKey} onReset={clearKey} />
      </BandSection>

      {/* ═══ REGIME BIAS (connects the bands) ═══ */}
      <Section title="Regime Bias">
        <Slider cfg={cfg} ck="risk.regime_counter_trend_penalty"
          label="Counter-trend confidence penalty"
          min={0.3} max={1.0} step={0.05} unit="x" dp={2} onApply={setKey} onReset={clearKey}
          note="1h regime dampens scalps fading the trend (1.0 = no penalty)" />
        <div className="text-xs text-slate-500">
          The 1h trend regime DAMPENS (never blocks) scalps that fade it — a scalp
          short within a 1h uptrend (or long within a downtrend) needs higher
          conviction to fire. Opposing positions can&apos;t coexist on one coin
          (one-way exchange nets per coin), so a coin is owned by one band at a time.
        </div>
      </Section>

      {/* ═══ OPEN POSITIONS ═══ */}
      <Section title="Open Positions">
        {(pos?.positions ?? []).length === 0 ? (
          <div className="text-sm text-slate-500">no open positions</div>
        ) : (
          <div className="grid gap-2">
            {(pos?.positions ?? []).map((p: any) => (
              <div key={p.coin} className="flex items-center justify-between gap-3 border border-edge rounded-lg px-3 py-2">
                <span className="mono text-sm flex items-center gap-2">
                  {p.band && (
                    <span className={clsx("px-1.5 py-0.5 rounded text-[10px] font-bold uppercase border",
                      p.band === "scalp" ? "border-cyan-500/50 text-cyan-300 bg-cyan-500/10"
                        : "border-purple-500/50 text-purple-300 bg-purple-500/10")}>
                      {p.band}
                    </span>
                  )}
                  {p.coin} {p.szi > 0 ? "LONG" : "SHORT"} · entry {p.entry_px} ·{" "}
                  <span className={p.unrealized_pnl >= 0 ? "text-emerald-300" : "text-red-300"}>
                    {fmtUsd(p.unrealized_pnl)}
                  </span>
                </span>
                <button onClick={() => act(() => post("/api/control/close", { coin: p.coin }), `close ${p.coin}`)}
                  className="px-3 py-1.5 rounded-lg border border-red-500/40 text-red-300 text-sm hover:bg-red-500/10">
                  Force Close
                </button>
              </div>
            ))}
          </div>
        )}
      </Section>
    </div>
  );
}

// ---------------------------------------------------------------------------
function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="card grid gap-3">
      <div className="label text-glow/80">{title}</div>
      {children}
    </div>
  );
}

// Band container: colored left-border accent (cyan=scalp, purple=trend),
// collapsible body, and an enable/disable toggle in the header. Keeps the
// existing card/theme — only adds the band accent + collapse affordance.
const BAND_ACCENT: Record<string, { border: string; text: string; dot: string }> = {
  cyan: { border: "border-l-cyan-500/70", text: "text-cyan-300", dot: "bg-cyan-400" },
  purple: { border: "border-l-purple-500/70", text: "text-purple-300", dot: "bg-purple-400" },
};

function BandSection({
  title, subtitle, accent, cfg, enableKey, otherEnableKey, onApply, children,
}: {
  title: string; subtitle: string; accent: "cyan" | "purple"; cfg: Cfg;
  enableKey: string; otherEnableKey: string;
  onApply: (k: string, v: any) => void; children: React.ReactNode;
}) {
  const a = BAND_ACCENT[accent];
  const enabled = Boolean(cfg.effective[enableKey] ?? cfg.defaults[enableKey] ?? true);
  const otherEnabled = Boolean(
    cfg.effective[otherEnableKey] ?? cfg.defaults[otherEnableKey] ?? true);
  const wouldDisableBoth = enabled && !otherEnabled;
  const [open, setOpen] = useState(true);
  return (
    <div className={clsx("card grid gap-3 border-l-4", a.border, !enabled && "opacity-60")}>
      <div className="flex items-center justify-between gap-3">
        <button onClick={() => setOpen((o) => !o)}
          className="flex items-center gap-2 text-left">
          <span className={clsx("text-xs", a.text)}>{open ? "▾" : "▸"}</span>
          <span className={clsx("h-2 w-2 rounded-full", enabled ? a.dot : "bg-slate-600")} />
          <span className={clsx("label", a.text)}>{title}</span>
          <span className="text-xs text-slate-500">{subtitle}</span>
        </button>
        <button
          onClick={() => { if (!wouldDisableBoth) onApply(enableKey, !enabled); }}
          disabled={wouldDisableBoth}
          title={wouldDisableBoth ? "at least one band must stay enabled" : ""}
          className={clsx("px-3 py-1 rounded-lg text-xs font-semibold border whitespace-nowrap",
            wouldDisableBoth ? "border-edge text-slate-600 cursor-not-allowed"
              : enabled ? "border-emerald-500/40 text-emerald-300 bg-emerald-500/10 hover:bg-emerald-500/20"
                : "border-red-500/40 text-red-300 bg-red-500/10 hover:bg-red-500/20")}>
          {enabled ? "ENABLED" : "DISABLED"}
        </button>
      </div>
      {open && <div className="grid gap-3">{children}</div>}
    </div>
  );
}

// Per-band LONG/SHORT toggles. Effective direction = global master AND this.
function BandDirection({
  band, cfg, onApply,
}: { band: "scalp" | "trend"; cfg: Cfg; onApply: (k: string, v: any) => void }) {
  const lk = `trading.${band}_longs_enabled`;
  const sk = `trading.${band}_shorts_enabled`;
  const longs = Boolean(cfg.effective[lk] ?? cfg.defaults[lk] ?? true);
  const shorts = Boolean(cfg.effective[sk] ?? cfg.defaults[sk] ?? true);
  const Btn = ({ on, label, k }: { on: boolean; label: string; k: string }) => (
    <button onClick={() => onApply(k, !on)}
      className={clsx("px-3 py-1 rounded-lg text-xs font-semibold border",
        on ? "border-emerald-500/40 text-emerald-300 bg-emerald-500/10"
          : "border-red-500/40 text-red-300 bg-red-500/10")}>
      {label} {on ? "ON" : "OFF"}
    </button>
  );
  return (
    <div className="flex items-center gap-2 flex-wrap">
      <Btn on={longs} label="LONG" k={lk} />
      <Btn on={shorts} label="SHORT" k={sk} />
      <span className="text-xs text-slate-500">effective = global master AND band</span>
    </div>
  );
}

type RowProps = {
  cfg: Cfg; ck: string; label: string;
  onApply: (k: string, v: any) => void; onReset: (k: string) => void;
};

function Slider({
  cfg, ck, label, min, max, step, unit, prefix, pct, dp = 0, note, onApply, onReset,
}: RowProps & {
  min: number; max: number; step: number; unit?: string; prefix?: string;
  pct?: boolean; dp?: number; note?: string;
}) {
  const scale = pct ? 100 : 1;
  const eff = Number(cfg.effective[ck] ?? cfg.defaults[ck] ?? min);
  const def = Number(cfg.defaults[ck] ?? min);
  const overridden = ck in cfg.overrides;
  const [v, setV] = useState<number>(eff * scale);
  useEffect(() => { setV(eff * scale); }, [eff, scale]);
  const dirty = Math.abs(v - eff * scale) > 1e-9;
  const fmt = (x: number) => `${prefix ?? ""}${x.toFixed(dp)}${unit ?? ""}`;

  return (
    <div className="grid gap-1">
      <div className="flex items-baseline justify-between">
        <span className="text-sm text-slate-300">{label}</span>
        <span className="mono text-sm text-glow">{fmt(v)}</span>
      </div>
      <div className="flex items-center gap-3">
        <input type="range" min={min} max={max} step={step} value={v}
          onChange={(e) => setV(Number(e.target.value))}
          className="flex-1 accent-glow" />
        <button onClick={() => onApply(ck, pct ? v / 100 : v)} disabled={!dirty}
          className={clsx("px-3 py-1 rounded-lg text-xs border",
            dirty ? "border-glow/50 text-glow hover:bg-edge" : "border-edge text-slate-600")}>
          Apply
        </button>
      </div>
      <div className="text-xs text-slate-500">
        default {fmt(def * scale)}
        {overridden && <span className="text-amber-300"> · overridden
          <button onClick={() => onReset(ck)} className="ml-1 underline">reset</button>
        </span>}
        {note && <span> · {note}</span>}
      </div>
    </div>
  );
}

function Toggle({ cfg, ck, label, onApply, onReset }: RowProps) {
  const eff = Boolean(cfg.effective[ck] ?? cfg.defaults[ck]);
  const overridden = ck in cfg.overrides;
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-sm text-slate-300">{label}</span>
      <div className="flex items-center gap-2">
        {overridden && <button onClick={() => onReset(ck)} className="text-xs text-amber-300 underline">reset</button>}
        <button onClick={() => onApply(ck, !eff)}
          className={clsx("px-3 py-1 rounded-lg text-xs font-semibold border",
            eff ? "border-emerald-500/40 text-emerald-300 bg-emerald-500/10"
              : "border-edge text-slate-400")}>
          {eff ? "ON" : "OFF"}
        </button>
      </div>
    </div>
  );
}

function DirectionControls({
  cfg, onApply,
}: { cfg: Cfg; onApply: (k: string, v: any) => void }) {
  const longs = cfg.effective["trading.longs_enabled"] ?? cfg.defaults["trading.longs_enabled"] ?? true;
  const shorts = cfg.effective["trading.shorts_enabled"] ?? cfg.defaults["trading.shorts_enabled"] ?? true;
  // audit rows the bot logs when a signal is intercepted by a disabled side
  const { data: blocked } = usePoll<{ total: number; trades: any[] }>(
    "/api/trades?status=direction_disabled&include_skips=true&limit=2000", 10000);
  const midnight = new Date(); midnight.setHours(0, 0, 0, 0);
  const since = midnight.getTime();
  const today = (blocked?.trades ?? []).filter((t) => t.ts >= since);
  const longsToday = today.filter((t) => t.side === "LONG").length;
  const shortsToday = today.filter((t) => t.side === "SHORT").length;

  const DirRow = ({
    on, label, side, counter,
  }: { on: boolean; label: string; side: "LONG" | "SHORT"; counter: number }) => {
    const wouldDisableBoth = on && !(side === "LONG" ? shorts : longs);
    return (
      <div className="flex items-center justify-between gap-3 border border-edge rounded-lg px-3 py-2">
        <div className="min-w-0">
          <div className="text-sm text-slate-200">{label}</div>
          <div className={clsx("text-xs mono", on ? "text-emerald-300" : "text-red-300")}>
            {side} entries: {on ? "ENABLED" : "DISABLED"}
            {!on && counter > 0 && (
              <span className="text-slate-500"> · {counter} blocked today</span>
            )}
          </div>
        </div>
        <button
          onClick={() => {
            if (wouldDisableBoth) return;  // server also rejects; guard the UI
            onApply(side === "LONG" ? "trading.longs_enabled" : "trading.shorts_enabled", !on);
          }}
          disabled={wouldDisableBoth}
          title={wouldDisableBoth ? "can't disable both directions" : ""}
          className={clsx("px-4 py-1.5 rounded-lg text-sm font-semibold border whitespace-nowrap",
            wouldDisableBoth ? "border-edge text-slate-600 cursor-not-allowed"
              : on ? "border-emerald-500/40 text-emerald-300 bg-emerald-500/10 hover:bg-emerald-500/20"
                : "border-red-500/40 text-red-300 bg-red-500/10 hover:bg-red-500/20")}>
          {on ? "ENABLED" : "DISABLED"}
        </button>
      </div>
    );
  };

  return (
    <div className="grid gap-2">
      <DirRow on={Boolean(longs)} label="LONG entries" side="LONG" counter={longsToday} />
      <DirRow on={Boolean(shorts)} label="SHORT entries" side="SHORT" counter={shortsToday} />
      <div className={clsx("text-xs mt-1",
        !longs || !shorts ? "text-amber-300" : "text-slate-500")}>
        ⚠ Disabling both directions halts all new entries — not allowed (the
        last enabled side can't be turned off). Takes effect within ~10s, no restart.
      </div>
    </div>
  );
}

function PerCoin({
  coin, cfg, onApply, onReset,
}: { coin: string; cfg: Cfg; onApply: (k: string, v: any) => void; onReset: (k: string) => void }) {
  const levKey = `per_coin.${coin}.leverage`;
  const szKey = `per_coin.${coin}.usd_size`;
  const [lev, setLev] = useState<string>(cfg.overrides[levKey]?.toString() ?? "");
  const [sz, setSz] = useState<string>(cfg.overrides[szKey]?.toString() ?? "");
  useEffect(() => { setLev(cfg.overrides[levKey]?.toString() ?? ""); }, [cfg, levKey]);
  useEffect(() => { setSz(cfg.overrides[szKey]?.toString() ?? ""); }, [cfg, szKey]);
  const cell = "bg-ink border border-edge rounded px-2 py-1 text-xs mono w-20";
  return (
    <div className="flex items-center gap-2 text-sm">
      <span className="mono w-12 text-slate-300">{coin}</span>
      <span className="text-xs text-slate-500">lev</span>
      <input className={cell} placeholder="—" value={lev}
        onChange={(e) => setLev(e.target.value)} />
      <button onClick={() => lev === "" ? onReset(levKey) : onApply(levKey, Number(lev))}
        className="px-2 py-1 rounded text-xs border border-edge text-slate-400 hover:bg-edge">set</button>
      <span className="text-xs text-slate-500 ml-2">size $</span>
      <input className={cell} placeholder="—" value={sz}
        onChange={(e) => setSz(e.target.value)} />
      <button onClick={() => sz === "" ? onReset(szKey) : onApply(szKey, Number(sz))}
        className="px-2 py-1 rounded text-xs border border-edge text-slate-400 hover:bg-edge">set</button>
    </div>
  );
}
