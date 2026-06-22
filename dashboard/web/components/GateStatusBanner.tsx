"use client";
/**
 * GateStatusBanner — a prominent, always-visible read on whether the structural
 * gating system is actually filtering entries, or being bypassed (e.g. the
 * BASELINE preset turns the gates off → the bot trades on raw signal: higher
 * frequency, far less filtering). Reads the live gate-enabled flags the bridge
 * publishes in /api/tickets (exactly what the bot enforces this loop) and the
 * active preset name. Drop it at the top of any page.
 */
import clsx from "clsx";
import { usePoll } from "@/lib/api";

type Gates = {
  long_structural_gate_enabled?: boolean;
  short_structural_gate_enabled?: boolean;
  funding_hard_block_enabled?: boolean;
};

export default function GateStatusBanner() {
  const { data: live } = usePoll<{ gates?: Gates }>("/api/tickets", 8000);
  const { data: active } = usePoll<{ display_name?: string; active?: string }>(
    "/api/presets/active", 15000);
  const g = live?.gates;
  if (!g) return null;

  const longOff = g.long_structural_gate_enabled === false;
  const shortOff = g.short_structural_gate_enabled === false;
  const fundingOff = g.funding_hard_block_enabled === false;
  const bypassed = longOff || shortOff;
  const preset = active?.display_name && active.active !== "CUSTOM"
    ? active.display_name : null;

  if (!bypassed) {
    // fully gated — quiet green confirmation so the state is always legible
    return (
      <div className="card border-emerald-500/30 bg-emerald-500/[0.04] flex flex-wrap items-center gap-x-3 gap-y-1 py-2.5">
        <span className="inline-flex items-center gap-1.5 text-emerald-300 text-sm font-semibold">
          <span className="relative flex h-2 w-2">
            <span className="absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-60 animate-ping" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-400" />
          </span>
          STRUCTURAL GATES ACTIVE
        </span>
        <span className="text-xs text-slate-400">
          Entries filtered by spot/OI/book structure{fundingOff ? "" : " + funding block"}.
        </span>
        {preset && <span className="ml-auto text-xs mono text-slate-500">{preset}</span>}
      </div>
    );
  }

  const which =
    longOff && shortOff ? "LONG + SHORT gates OFF"
      : longOff ? "LONG gate OFF" : "SHORT gate OFF";
  return (
    <div className="card border-amber-500/60 bg-amber-500/[0.08] flex flex-wrap items-center gap-x-3 gap-y-1 py-2.5">
      <span className="inline-flex items-center gap-2 text-amber-200 text-sm font-bold uppercase tracking-wide">
        <span className="relative flex h-2.5 w-2.5">
          <span className="absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-70 animate-ping" />
          <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-amber-400" />
        </span>
        ⚠ Gates Bypassed
      </span>
      <span className="text-xs text-amber-100/80 mono">{which}</span>
      <span className="text-xs text-slate-300">
        Open for trades on raw signal — higher frequency, less filtering.
      </span>
      {preset && (
        <span className={clsx("ml-auto text-xs mono px-2 py-0.5 rounded-full border",
          "border-amber-500/50 text-amber-200")}>
          {preset}
        </span>
      )}
    </div>
  );
}
