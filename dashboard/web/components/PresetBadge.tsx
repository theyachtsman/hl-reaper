"use client";
import { useEffect, useState } from "react";
import clsx from "clsx";
import { api } from "@/lib/api";

type Active = {
  active: string;
  display_name: string;
  last_applied: string | null;
  last_display_name: string | null;
};

// preset → badge color. BASELINE is amber (warning: structural gates disabled);
// CUSTOM is gray (user has diverged from any named preset).
const STYLE: Record<string, string> = {
  BASELINE: "bg-amber-500/20 text-amber-300 border-amber-500/60",
  SCALPER: "bg-cyan-500/15 text-cyan-300 border-cyan-500/50",
  SHORT_HUNTER: "bg-red-500/15 text-red-300 border-red-500/50",
  TREND_RIDER: "bg-purple-500/15 text-purple-300 border-purple-500/50",
  CONSERVATIVE: "bg-blue-500/15 text-blue-300 border-blue-500/50",
  CUSTOM: "bg-slate-500/10 text-slate-300 border-slate-500/40",
};

/** Active strategy preset badge — polls /api/presets/active every 15s so it
 *  stays current whether a preset is applied or settings are tweaked by hand. */
export default function PresetBadge({ large }: { large?: boolean }) {
  const [a, setA] = useState<Active | null>(null);
  useEffect(() => {
    const tick = () => api<Active>("/api/presets/active").then(setA).catch(() => {});
    tick();
    const id = setInterval(tick, 15000);
    return () => clearInterval(id);
  }, []);
  if (!a) return null;
  const style = STYLE[a.active] ?? STYLE.CUSTOM;
  const title =
    a.active === "CUSTOM"
      ? a.last_display_name
        ? `Custom config (modified from ${a.last_display_name})`
        : "Custom config (manually modified)"
      : `Strategy preset: ${a.display_name}`;
  return (
    <span
      title={title}
      className={clsx(
        "inline-flex items-center gap-1 border rounded-full font-bold uppercase tracking-wide",
        large ? "px-4 py-1.5 text-sm" : "px-3 py-0.5 text-[10px]",
        style
      )}
    >
      {a.active === "BASELINE" && "⚠ "}
      {a.display_name}
    </span>
  );
}
