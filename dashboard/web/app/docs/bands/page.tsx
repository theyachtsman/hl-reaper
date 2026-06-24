import {
  PageHeader,
  Section,
  Card,
  Pill,
  Callout,
  WeightBar,
  ACCENT,
  ACCENT_BRIGHT,
  CYAN,
} from "../_components/ui";
import { BAND_PARAMS, MODELS } from "../_components/data";

export const metadata = { title: "Bands · Docs" };

export default function Bands() {
  return (
    <>
      <PageHeader
        kicker="Bands"
        title="SCALP vs TREND"
        intro="The bot runs two strategies at once on every coin. SCALP is fast, tight and frequent; TREND is patient, wide and rare. They use different model weights, different risk geometry, and different entry bars — all pulled below from the live configuration."
      />

      <Section title="Side by side">
        <div className="grid md:grid-cols-2 gap-3">
          <Card accent>
            <div className="flex items-center justify-between mb-3">
              <Pill tone="accent">SCALP</Pill>
              <span className="mono text-xs text-slate-500">5m · tight · fast</span>
            </div>
            <p className="text-sm text-slate-400 mb-4">
              Optimized to fade short-term extremes and capture small, frequent moves. Mean
              reversion is its dominant signal. Holds for minutes, not hours.
            </p>
          </Card>
          <Card>
            <div className="flex items-center justify-between mb-3">
              <Pill tone="cyan">TREND</Pill>
              <span className="mono text-xs text-slate-500">1h · wide · patient</span>
            </div>
            <p className="text-sm text-slate-400 mb-4">
              Optimized to ride sustained structural moves. No mean reversion at all; leans on TA,
              orderbook and funding. Wide stops let winners run for up to two days.
            </p>
          </Card>
        </div>
      </Section>

      <Section title="Risk geometry & gates">
        <Card>
          <div className="grid grid-cols-[1.3fr_1fr_1fr] gap-x-3 gap-y-0">
            <div className="label py-2 border-b border-edge">Parameter</div>
            <div className="label py-2 border-b border-edge" style={{ color: ACCENT_BRIGHT }}>
              Scalp
            </div>
            <div className="label py-2 border-b border-edge" style={{ color: CYAN }}>
              Trend
            </div>
            {BAND_PARAMS.map((p) => (
              <div key={p.key} className="contents">
                <div className="py-2 border-b border-edge/50 text-sm text-slate-300">
                  {p.label}
                  {p.note && <div className="text-[11px] text-slate-500">{p.note}</div>}
                </div>
                <div className="py-2 border-b border-edge/50 mono text-sm" style={{ color: ACCENT_BRIGHT }}>
                  {p.scalp}
                </div>
                <div className="py-2 border-b border-edge/50 mono text-sm" style={{ color: CYAN }}>
                  {p.trend}
                </div>
              </div>
            ))}
          </div>
        </Card>
      </Section>

      <Section title="Different weight profiles">
        <p className="text-slate-400 text-sm mb-4 leading-relaxed">
          The same five models carry very different weight in each band. SCALP promotes mean
          reversion to nearly half its weight; TREND zeroes it entirely and leans on TA, orderbook
          and funding to confirm structural moves.
        </p>
        <Card>
          {MODELS.map((m) => (
            <div key={m.key} className="grid grid-cols-[1.4fr_1fr_1fr] gap-3 items-center py-2 border-b border-edge/50 last:border-0">
              <span className="mono text-xs sm:text-sm text-slate-200">{m.name}</span>
              <WeightBar value={m.scalp} />
              <WeightBar value={m.trend} tone={CYAN} />
            </div>
          ))}
          <div className="flex gap-4 mt-3 text-xs text-slate-500">
            <span className="flex items-center gap-1">
              <span className="w-3 h-2 rounded-full inline-block" style={{ background: ACCENT }} /> Scalp
            </span>
            <span className="flex items-center gap-1">
              <span className="w-3 h-2 rounded-full inline-block" style={{ background: CYAN }} /> Trend
            </span>
          </div>
        </Card>
      </Section>

      <Section title="Band priority — who claims the coin">
        <Callout tone="cyan" title="Trend goes first">
          Each cycle the TREND band is evaluated before SCALP. If the 1h signal fires, it opens the
          position and the coin is locked. SCALP is only offered the coin if trend left it free.
          Because the exchange nets to one position per coin, the two bands can never both hold the
          same coin at once.
        </Callout>
        <Callout tone="accent" title="The 1h regime dampens counter-trend scalps">
          A scalp that fades <em>into</em> the prevailing 1h trend (shorting inside an uptrend) has
          its confidence multiplied by the counter-trend penalty (default{" "}
          <span className="mono">0.7</span>) so it needs more conviction to fire. It is dampened,
          never blocked — and the trend band's own signal is never touched by this.
        </Callout>
      </Section>

      <Section title="Turning bands on and off">
        <p className="text-sm text-slate-400 leading-relaxed">
          Each band has its own master switch and its own per-direction toggles on the Controls
          page. Several presets are just band switches — <span className="mono">SCALPER</span> runs
          scalp alone, <span className="mono">TREND RIDER</span> runs trend alone. At least one band
          must stay enabled; disabling both halts all new entries (use Pause for that instead).
        </p>
      </Section>
    </>
  );
}
