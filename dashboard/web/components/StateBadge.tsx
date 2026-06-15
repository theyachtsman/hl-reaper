"use client";
import clsx from "clsx";

const COLORS: Record<string, string> = {
  ACTIVE: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
  MANAGING: "bg-amber-500/15 text-amber-300 border-amber-500/40",
  HALTED: "bg-red-500/15 text-red-300 border-red-500/40",
  RECONNECTING: "bg-sky-500/15 text-sky-300 border-sky-500/40",
  COOLDOWN: "bg-violet-500/15 text-violet-300 border-violet-500/40",
  CASCADE_BOUNCE_ACTIVE: "bg-fuchsia-500/15 text-fuchsia-300 border-fuchsia-500/40",
  DATA_ONLY: "bg-cyan-500/10 text-cyan-300 border-cyan-500/40",
  OFFLINE: "bg-red-500/10 text-red-400 border-red-500/60",
  UNKNOWN: "bg-slate-500/15 text-slate-300 border-slate-500/40",
};

export default function StateBadge({ state, large }: { state: string; large?: boolean }) {
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-2 border rounded-full font-semibold",
        large ? "px-4 py-1.5 text-base" : "px-3 py-0.5 text-xs",
        COLORS[state] ?? COLORS.UNKNOWN
      )}
    >
      <span className="relative flex h-2 w-2">
        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-current opacity-50" />
        <span className="relative inline-flex rounded-full h-2 w-2 bg-current" />
      </span>
      {state}
    </span>
  );
}
