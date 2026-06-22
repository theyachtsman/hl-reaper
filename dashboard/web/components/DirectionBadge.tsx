"use client";
import clsx from "clsx";

/* Compact indicator of which trade directions are live (toggled from Controls).
 * Both enabled is the normal case (subtle); a single side or none is flagged
 * loudly so a disabled direction is obvious without opening Controls. */
export default function DirectionBadge({
  directions,
}: {
  directions?: { longs: boolean; shorts: boolean };
}) {
  if (!directions) return null;
  const { longs, shorts } = directions;
  const both = longs && shorts;
  const none = !longs && !shorts;
  const label = none
    ? "NO ENTRIES"
    : both
      ? "L+S"
      : longs
        ? "LONG ONLY"
        : "SHORT ONLY";
  return (
    <span
      title={`new entries — LONG: ${longs ? "on" : "off"} · SHORT: ${shorts ? "on" : "off"}`}
      className={clsx(
        "inline-flex items-center gap-1 border rounded-full px-2 py-0.5 text-xs font-semibold mono",
        both
          ? "border-edge text-slate-400"
          : none
            ? "border-red-500/60 text-red-300 bg-red-500/15"
            : "border-amber-500/50 text-amber-300 bg-amber-500/10"
      )}
    >
      {label}
    </span>
  );
}
