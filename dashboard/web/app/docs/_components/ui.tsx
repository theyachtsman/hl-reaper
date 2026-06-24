// Presentational primitives for the docs site, styled to match the existing
// dashboard exactly: green #1D9E75 accent on near-black panels, slate text,
// the shared .card / .label / .mono classes from styles/globals.css.
import React from "react";

export const ACCENT = "#1D9E75";
export const ACCENT_BRIGHT = "#22c98e";
export const CYAN = "#22d3ee";
export const RED = "#e24b4a";

export function PageHeader({
  kicker,
  title,
  intro,
}: {
  kicker: string;
  title: string;
  intro: React.ReactNode;
}) {
  return (
    <header className="mb-8">
      <div className="label mb-2" style={{ color: ACCENT_BRIGHT }}>
        {kicker}
      </div>
      <h1 className="text-2xl md:text-3xl font-bold tracking-tight mb-3">{title}</h1>
      <p className="text-slate-400 leading-relaxed max-w-2xl">{intro}</p>
    </header>
  );
}

export function Section({
  title,
  id,
  children,
}: {
  title?: string;
  id?: string;
  children: React.ReactNode;
}) {
  return (
    <section id={id} className="mb-10 scroll-mt-24">
      {title && (
        <h2 className="text-lg md:text-xl font-semibold mb-4 flex items-center gap-2">
          <span
            className="inline-block w-1.5 h-5 rounded-full"
            style={{ background: ACCENT }}
          />
          {title}
        </h2>
      )}
      {children}
    </section>
  );
}

export function Card({
  children,
  className = "",
  accent = false,
}: {
  children: React.ReactNode;
  className?: string;
  accent?: boolean;
}) {
  return (
    <div className={`card relative ${className}`} style={accent ? { borderColor: "rgba(29,158,117,0.4)" } : undefined}>
      {children}
    </div>
  );
}

type Tone = "long" | "short" | "flat" | "accent" | "cyan" | "warn" | "muted";

const TONES: Record<Tone, { bg: string; fg: string; border: string }> = {
  long: { bg: "rgba(29,158,117,0.15)", fg: ACCENT_BRIGHT, border: "rgba(29,158,117,0.45)" },
  short: { bg: "rgba(226,75,74,0.15)", fg: "#f87171", border: "rgba(226,75,74,0.45)" },
  flat: { bg: "rgba(100,116,139,0.15)", fg: "#94a3b8", border: "rgba(100,116,139,0.4)" },
  accent: { bg: "rgba(29,158,117,0.15)", fg: ACCENT_BRIGHT, border: "rgba(29,158,117,0.45)" },
  cyan: { bg: "rgba(34,211,238,0.12)", fg: CYAN, border: "rgba(34,211,238,0.4)" },
  warn: { bg: "rgba(245,158,11,0.14)", fg: "#fbbf24", border: "rgba(245,158,11,0.4)" },
  muted: { bg: "rgba(148,163,184,0.1)", fg: "#cbd5e1", border: "rgba(148,163,184,0.25)" },
};

export function Pill({ tone = "muted", children }: { tone?: Tone; children: React.ReactNode }) {
  const t = TONES[tone];
  return (
    <span
      className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-xs font-medium mono"
      style={{ background: t.bg, color: t.fg, border: `1px solid ${t.border}` }}
    >
      {children}
    </span>
  );
}

export function DirPill({ dir }: { dir: string }) {
  const tone: Tone = dir === "LONG" ? "long" : dir === "SHORT" ? "short" : "flat";
  return <Pill tone={tone}>{dir}</Pill>;
}

export function KeyVal({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1.5 border-b border-edge/60 last:border-0">
      <span className="text-sm text-slate-400">{k}</span>
      <span className="mono text-sm text-slate-200 text-right">{v}</span>
    </div>
  );
}

export function Callout({
  tone = "accent",
  title,
  children,
}: {
  tone?: Tone;
  title?: string;
  children: React.ReactNode;
}) {
  const t = TONES[tone];
  return (
    <div
      className="rounded-lg p-4 my-4 text-sm leading-relaxed"
      style={{ background: t.bg, border: `1px solid ${t.border}` }}
    >
      {title && (
        <div className="font-semibold mb-1" style={{ color: t.fg }}>
          {title}
        </div>
      )}
      <div className="text-slate-300">{children}</div>
    </div>
  );
}

// A horizontal weight bar (data-driven, themeable, no chart lib needed).
export function WeightBar({ value, max = 0.5, tone = ACCENT }: { value: number; max?: number; tone?: string }) {
  const pct = Math.min(100, (value / max) * 100);
  return (
    <div className="flex items-center gap-2">
      <div className="h-2 flex-1 rounded-full overflow-hidden bg-edge">
        <div
          className="h-full rounded-full transition-all"
          style={{ width: `${pct}%`, background: tone, boxShadow: `0 0 8px ${tone}` }}
        />
      </div>
      <span className="mono text-xs text-slate-400 w-10 text-right">
        {value === 0 ? "—" : value.toFixed(2)}
      </span>
    </div>
  );
}

export function CheckRow({ ok, children }: { ok: boolean; children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-2 py-1 text-sm">
      <span style={{ color: ok ? ACCENT_BRIGHT : "#f87171" }} className="mono mt-0.5">
        {ok ? "✓" : "✗"}
      </span>
      <span className="text-slate-300">{children}</span>
    </div>
  );
}
