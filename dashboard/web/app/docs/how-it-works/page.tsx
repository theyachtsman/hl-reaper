import {
  PageHeader,
  Section,
  Card,
  Pill,
  DirPill,
  Callout,
  WeightBar,
  ACCENT,
  ACCENT_BRIGHT,
  CYAN,
} from "../_components/ui";
import { FlowDiagram } from "../_components/diagrams";
import { MODELS, VOTE_EXAMPLE } from "../_components/data";

export const metadata = { title: "How It Works · Docs" };

export default function HowItWorks() {
  const ex = VOTE_EXAMPLE;
  return (
    <>
      <PageHeader
        kicker="How It Works"
        title="The signal pipeline, end to end"
        intro="Every 10 seconds the bot pulls fresh data, asks each model for a vote, combines those votes into a single conviction, and checks that conviction against the entry gates. Here is exactly what happens at each step."
      />

      <Callout tone="warn" title="Trend-only since 2026-06-26">
        The 5m scalp band and its structural entry gates (spot lead/lag, OI rise,
        book bid/ask, pump/dump cooldown) were retired. The pipeline below now runs
        once per coin on the 1h trend band only; references to a second band or to
        structural gates are historical.
      </Callout>

      <Section title="The full pipeline">
        <FlowDiagram />
      </Section>

      <Section title="1 · The six-model ensemble">
        <p className="text-slate-400 text-sm mb-4 leading-relaxed">
          Six models vote on direction. Each reads a different slice of the market, so they
          rarely all agree — which is the point. Each model returns a <em>ticket</em>: a direction
          (<DirPill dir="LONG" /> <DirPill dir="SHORT" /> or <DirPill dir="FLAT" />) and a
          confidence from 0 to 1. Weights differ by band; the values below are the per-band defaults.
        </p>
        <Card>
          <div className="grid grid-cols-[1fr_auto] sm:grid-cols-[1.4fr_1fr_1fr] gap-x-4 gap-y-3 items-center">
            <div className="label hidden sm:block">Model</div>
            <div className="label hidden sm:block">Scalp weight</div>
            <div className="label hidden sm:block">Trend weight</div>
            {MODELS.map((m) => (
              <div key={m.key} className="contents">
                <div>
                  <div className="mono text-sm" style={{ color: ACCENT_BRIGHT }}>
                    {m.name}
                  </div>
                  <div className="text-xs text-slate-500 leading-tight">{m.blurb}</div>
                </div>
                <div className="hidden sm:block">
                  <WeightBar value={m.scalp} />
                </div>
                <div className="hidden sm:block">
                  <WeightBar value={m.trend} tone={CYAN} />
                </div>
              </div>
            ))}
          </div>
        </Card>
        <p className="text-xs text-slate-500 mt-2">
          Per band the active weights are renormalized to sum to 1.0. Read each model in depth on
          the Models page.
        </p>
      </Section>

      <Section title="2 · FLAT votes are abstentions, not opposition">
        <p className="text-slate-400 text-sm mb-3 leading-relaxed">
          A model votes <DirPill dir="FLAT" /> when it sees no edge (or a confidence below 0.05).
          A FLAT vote is treated as <strong>sitting out</strong>: it is removed from{" "}
          <em>both</em> the score and the weight total. This matters — it means a model that abstains
          cannot dilute the conviction of the models that did vote.
        </p>
        <Callout tone="accent" title="Why this is not the same as voting against">
          If MeanReversion abstains in a trend, the remaining voters still produce a full-strength
          signal. But a genuine <em>opposing</em> vote (a LONG when the others are SHORT) is kept —
          it counts, and it pulls the net score back toward zero.
        </Callout>
      </Section>

      <Section title="3 · How the aggregator scores a signal">
        <p className="text-slate-400 text-sm mb-4 leading-relaxed">
          The aggregator sums each active vote as a signed{" "}
          <span className="mono">weight × confidence</span> (positive for LONG, negative for SHORT),
          then divides by the total active weight. The sign of the score is the direction; the
          magnitude is the confidence.
        </p>

        <Card accent>
          <div className="flex items-center gap-2 mb-3">
            <Pill tone="cyan">{ex.band} band</Pill>
            <Pill tone="muted">regime: {ex.regime}</Pill>
            <span className="text-xs text-slate-500">worked example</span>
          </div>

          <div className="space-y-2">
            {ex.rows.map((r) => {
              const abstain = r.dir === "FLAT";
              const contrib = (r.dir === "LONG" ? 1 : r.dir === "SHORT" ? -1 : 0) * r.weight * r.conf;
              return (
                <div
                  key={r.model}
                  className="grid grid-cols-[1.5fr_auto_auto_auto] gap-2 items-center text-sm py-1.5 border-b border-edge/50 last:border-0"
                  style={abstain ? { opacity: 0.5 } : undefined}
                >
                  <span className="mono text-xs sm:text-sm text-slate-200">{r.model}</span>
                  <DirPill dir={r.dir} />
                  <span className="mono text-xs text-slate-400 w-20 text-right">
                    w {r.weight.toFixed(2)} · c {r.conf.toFixed(2)}
                  </span>
                  <span
                    className="mono text-xs w-14 text-right"
                    style={{
                      color: abstain ? "#64748b" : contrib < 0 ? "#f87171" : ACCENT_BRIGHT,
                    }}
                  >
                    {abstain ? "abstain" : contrib.toFixed(3)}
                  </span>
                </div>
              );
            })}
          </div>

          <div className="mt-4 rounded-lg p-3 mono text-xs leading-relaxed" style={{ background: "rgba(34,211,238,0.06)", border: "1px solid rgba(34,211,238,0.25)" }}>
            <div className="text-slate-400">active weight (FLAT excluded) = 0.15 + 0.45 + 0.20 + 0.15 = <span className="text-slate-200">{ex.activeWeight}</span></div>
            <div className="text-slate-400">score = −0.093 − 0.351 − 0.132 + 0.083 = <span style={{ color: "#f87171" }}>{ex.score}</span></div>
            <div className="text-slate-400">confidence = |{ex.score}| / {ex.activeWeight} = <span style={{ color: ACCENT_BRIGHT }}>{ex.confidence}</span></div>
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
            <span className="text-slate-400">Result:</span>
            <DirPill dir={ex.direction} />
            <Pill tone="accent">conf {ex.confidence}</Pill>
            <Pill tone="muted">
              votes {ex.longVotes}L / {ex.shortVotes}S / {ex.flatVotes}F
            </Pill>
          </div>
        </Card>
      </Section>

      <Section title="4 · The confidence gate">
        <p className="text-slate-400 text-sm mb-3 leading-relaxed">
          A scored signal still has to clear two thresholds before it can become an order, and both
          are <strong>per band</strong>:
        </p>
        <div className="grid sm:grid-cols-2 gap-3">
          <Card>
            <div className="label mb-1">Minimum confidence</div>
            <p className="text-sm text-slate-400">
              The aggregated confidence must reach the band floor —{" "}
              <span className="mono" style={{ color: ACCENT_BRIGHT }}>0.40</span> for scalp,{" "}
              <span className="mono" style={{ color: CYAN }}>0.55</span> for trend. Our example
              (0.52) clears scalp but not trend.
            </p>
          </Card>
          <Card>
            <div className="label mb-1">Minimum agreement</div>
            <p className="text-sm text-slate-400">
              At least this many models must vote the winning direction —{" "}
              <span className="mono" style={{ color: ACCENT_BRIGHT }}>2</span> for scalp,{" "}
              <span className="mono" style={{ color: CYAN }}>3</span> for trend. Our example has 3
              SHORT votes, so it passes both.
            </p>
          </Card>
        </div>
        <Callout tone="warn" title="Funding can still override">
          Even a clean LONG signal is forced to FLAT if the funding model is in an extreme-crowded
          SHORT read (confidence ≥ 0.75) — the funding hard-block. See the Risk and Models pages.
        </Callout>
      </Section>

      <Section title="5 · Two bands, one position per coin">
        <p className="text-slate-400 text-sm mb-3 leading-relaxed">
          The whole pipeline above runs <strong>twice per coin every cycle</strong> — once on 5m
          candles (the SCALP band) and once on 1h candles (the TREND band). Because Hyperliquid nets
          to a single position per coin (one-way mode), a coin can be owned by only one band at a
          time.
        </p>
        <Card>
          <div className="flex items-center gap-3 text-sm">
            <Pill tone="cyan">TREND</Pill>
            <span className="mono text-slate-500">→</span>
            <span className="text-slate-300">
              evaluated first. A rare 1h signal claims the coin.
            </span>
          </div>
          <div className="flex items-center gap-3 text-sm mt-2">
            <Pill tone="accent">SCALP</Pill>
            <span className="mono text-slate-500">→</span>
            <span className="text-slate-300">
              only fires if the trend band left the coin free.
            </span>
          </div>
        </Card>
        <p className="text-sm text-slate-400 mt-3">
          The two bands differ in far more than resolution — weights, stops, targets, and patience.
          That is the <a href="/docs/bands" className="underline" style={{ color: ACCENT }}>Bands</a>{" "}
          page.
        </p>
      </Section>
    </>
  );
}
