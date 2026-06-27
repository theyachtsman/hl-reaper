import {
  PageHeader,
  Section,
  Card,
  Pill,
  Callout,
  ACCENT_BRIGHT,
  CYAN,
} from "../_components/ui";
import { PRESETS } from "../_components/data";

export const metadata = { title: "Presets · Docs" };

// Compact comparison matrix — key knobs each preset changes vs the others.
const COLS = PRESETS.map((p) => p.name);
const ROWS: { label: string; values: string[] }[] = [
  { label: "Scalp band", values: ["✓", "✓", "✗", "✓", "✓", "✓"] },
  { label: "Trend band", values: ["✓", "✗", "✓", "✓", "✓", "✓"] },
  { label: "Longs", values: ["✓", "✓", "✓", "✗", "✓", "✓"] },
  { label: "Shorts", values: ["✓", "✓", "✓", "✓", "✓", "✓"] },
  { label: "Scalp min conf", values: ["0.40", "0.40", "—", "0.40", "0.55", "0.35"] },
  { label: "Trend min conf", values: ["0.55", "—", "0.49", "0.55", "0.62", "0.55"] },
  { label: "Structural gates", values: ["on", "on", "n/a", "on", "on", "off"] },
  { label: "Counter-trend penalty", values: ["0.70", "—", "1.00", "0.70", "0.60", "0.85"] },
];

function cellColor(v: string) {
  if (v === "✓") return ACCENT_BRIGHT;
  if (v === "✗") return "#f87171";
  return "#cbd5e1";
}

export default function Presets() {
  return (
    <>
      <PageHeader
        kicker="Presets"
        title="One-click configurations"
        intro="A preset is a named bundle of settings applied in a single click from the Controls page. It takes effect within one bot loop (~10s) — no restart. Changing any setting by hand afterward flips the active preset to CUSTOM."
      />

      <Callout tone="warn" title="Trend-only presets — 2026-06-26">
        The SCALPER and DUAL BAND presets were removed when the scalp band was
        retired. The remaining presets (TREND RIDER, SHORT HUNTER, CONSERVATIVE,
        BASELINE) are trend-only and never write scalp or structural-gate settings.
        Any scalp/structural rows in the matrix below are historical.
      </Callout>

      <Section title="Comparison matrix">
        <Card className="overflow-x-auto">
          <div className="min-w-[640px]">
            <div
              className="grid gap-x-2 gap-y-0"
              style={{ gridTemplateColumns: `1.4fr repeat(${COLS.length}, 1fr)` }}
            >
              <div className="label py-2 border-b border-edge">Setting</div>
              {COLS.map((c) => (
                <div key={c} className="label py-2 border-b border-edge text-center text-[10px]">
                  {c}
                </div>
              ))}
              {ROWS.map((r) => (
                <div key={r.label} className="contents">
                  <div className="py-2 border-b border-edge/40 text-xs text-slate-300">
                    {r.label}
                  </div>
                  {r.values.map((v, i) => (
                    <div
                      key={i}
                      className="py-2 border-b border-edge/40 text-center mono text-xs"
                      style={{ color: cellColor(v) }}
                    >
                      {v}
                    </div>
                  ))}
                </div>
              ))}
            </div>
          </div>
        </Card>
        <p className="text-[11px] text-slate-500 mt-2">
          Every preset keeps the funding hard-block on. Values shown as “—” mean that band is
          disabled in that preset.
        </p>
      </Section>

      <Section title="When to use each">
        <div className="space-y-3">
          {PRESETS.map((p) => (
            <Card key={p.id} accent={p.id === "DUAL_BAND"}>
              <div className="flex flex-wrap items-center gap-2 mb-2">
                <span className="font-bold tracking-tight" style={{ color: ACCENT_BRIGHT }}>
                  {p.name}
                </span>
                <Pill tone="cyan">{p.bands}</Pill>
                {p.id === "DUAL_BAND" && <Pill tone="accent">flagship</Pill>}
              </div>
              <p className="text-sm text-slate-300 mb-3">{p.tagline}</p>

              <div className="grid sm:grid-cols-2 gap-3">
                <div>
                  <div className="label mb-1">Changes vs other presets</div>
                  <ul className="text-xs text-slate-400 space-y-1">
                    {p.changes.map((c) => (
                      <li key={c} className="flex gap-1.5">
                        <span style={{ color: CYAN }}>•</span>
                        {c}
                      </li>
                    ))}
                  </ul>
                </div>
                <div>
                  <div className="label mb-1">Best when</div>
                  <p className="text-xs text-slate-400 leading-relaxed">{p.when}</p>
                </div>
              </div>

              {p.warning && (
                <Callout tone="warn" title="Warning">
                  {p.warning}
                </Callout>
              )}
            </Card>
          ))}
        </div>
      </Section>

      <Callout tone="muted" title="CUSTOM mode">
        Once you change any tunable by hand, the active preset becomes CUSTOM and the dashboard
        remembers which preset you diverged from (shown as “modified from …”). Re-applying a preset
        snaps everything back to its defined values.
      </Callout>
    </>
  );
}
