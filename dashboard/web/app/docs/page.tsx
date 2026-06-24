import Link from "next/link";
import { PageHeader, Section, Card, Pill, ACCENT_BRIGHT } from "./_components/ui";
import { FlowDiagram } from "./_components/diagrams";
import { NAV } from "./_components/data";

export const metadata = { title: "Docs · HL Reaper" };

const CONCEPTS = [
  {
    title: "Ensemble voting",
    body: "Five independent models each cast a directional vote. A weighted aggregator turns those votes into one confidence score per coin.",
  },
  {
    title: "Dual bands",
    body: "Every coin is evaluated twice each cycle — a fast 5m SCALP band and a patient 1h TREND band — but holds at most one position at a time.",
  },
  {
    title: "Confidence gate",
    body: "A signal only becomes a trade if it clears the band's minimum confidence and model-agreement thresholds.",
  },
  {
    title: "Layered risk",
    body: "A four-layer guard system and a six-state machine sit between every signal and the exchange. Nothing reaches an order without passing through it.",
  },
];

export default function DocsHome() {
  return (
    <>
      <PageHeader
        kicker="HL Reaper"
        title="How the bot thinks"
        intro={
          <>
            HL Reaper is an automated perpetual-futures trader for Hyperliquid. It reads live market
            data, runs it through a small ensemble of models, scores a directional conviction, and
            only trades when that conviction clears a series of risk gates. These docs explain
            exactly how each step works — every number here is the real default from the running
            configuration.
          </>
        }
      />

      <Section title="The signal pipeline">
        <p className="text-slate-400 text-sm mb-4 leading-relaxed">
          Raw market data flows left-to-right through the models, the aggregator, the confidence
          gate, and the risk engine before any order is placed. If a stage rejects the signal, it
          never advances.
        </p>
        <FlowDiagram />
      </Section>

      <Section title="Key concepts at a glance">
        <div className="grid sm:grid-cols-2 gap-3">
          {CONCEPTS.map((c) => (
            <Card key={c.title}>
              <div className="font-semibold mb-1" style={{ color: ACCENT_BRIGHT }}>
                {c.title}
              </div>
              <p className="text-sm text-slate-400 leading-relaxed">{c.body}</p>
            </Card>
          ))}
        </div>
      </Section>

      <Section title="Who this is for">
        <Card>
          <p className="text-sm text-slate-300 leading-relaxed">
            If you operate this bot from the dashboard, these docs let you understand what every
            panel, badge, and control means — and why the bot took (or skipped) a trade. You do not
            need to read the code. New operators should start with{" "}
            <DocLink href="/docs/how-it-works">How It Works</DocLink>; returning operators can jump
            straight to any section to look up a specific behavior.
          </p>
        </Card>
      </Section>

      <Section title="Browse the docs">
        <div className="grid sm:grid-cols-2 gap-2">
          {NAV.filter((n) => n.href !== "/docs").map((n) => (
            <Link
              key={n.href}
              href={n.href}
              className="card flex items-center justify-between hover:border-[#1D9E75]/40 transition-colors"
            >
              <span className="text-slate-200 text-sm font-medium">{n.label}</span>
              <span className="mono text-slate-500">→</span>
            </Link>
          ))}
        </div>
      </Section>

      <div className="mt-8 flex flex-wrap gap-2">
        <Pill tone="accent">5 active models</Pill>
        <Pill tone="cyan">2 bands</Pill>
        <Pill tone="warn">6 presets</Pill>
        <Pill tone="short">6 risk states</Pill>
      </div>
    </>
  );
}

function DocLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <Link href={href} className="underline" style={{ color: ACCENT_BRIGHT }}>
      {children}
    </Link>
  );
}
