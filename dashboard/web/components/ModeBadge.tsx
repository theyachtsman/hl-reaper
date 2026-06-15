"use client";
import clsx from "clsx";

/** Trading-mode badge: paper_aggressive must be unmissable — it means the
 * entry gates are lowered for testnet data collection (NOT mainnet-safe). */
export default function ModeBadge({ mode, large }: { mode?: string; large?: boolean }) {
  if (!mode) return null;
  const aggressive = mode.startsWith("paper_aggressive");
  const stale = mode.endsWith("(config)"); // bot hasn't reported in yet
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1.5 border rounded-full font-bold uppercase tracking-wide",
        large ? "px-4 py-1.5 text-sm" : "px-3 py-0.5 text-[10px]",
        aggressive
          ? "bg-orange-500/20 text-orange-300 border-orange-500/60 animate-pulse"
          : "bg-slate-500/10 text-slate-300 border-slate-500/40"
      )}
      title={
        aggressive
          ? "PAPER AGGRESSIVE: gates lowered (conf 0.35, quorum 3, 5 positions) — testnet data collection, NOT mainnet-safe"
          : "Conservative gates (conf 0.62, quorum 5, 3 positions) — mainnet-safe defaults"
      }
    >
      {aggressive ? "⚠ PAPER AGGRESSIVE" : "CONSERVATIVE"}
      {stale && <span className="font-normal normal-case opacity-60">(config)</span>}
    </span>
  );
}
