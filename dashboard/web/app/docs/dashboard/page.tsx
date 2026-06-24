import {
  PageHeader,
  Section,
  Card,
  Pill,
  DirPill,
  ACCENT_BRIGHT,
  CYAN,
} from "../_components/ui";
import OrbCard from "../_components/OrbCard";

export const metadata = { title: "Dashboard Guide · Docs" };

const ORB_LEGEND: { n: number; title: string; body: React.ReactNode }[] = [
  { n: 1, title: "Coin · price · change", body: "The coin, its current mark price, and its move over the last 60 five-minute candles (here +1.53%)." },
  { n: 2, title: "Verdict pill", body: "When the coin is idle this rotates through live status lines — lean forming, confidence vs gate, models agreeing, funding veto, or the structural block reason. When a trade is imminent it locks to “▲ LONG ARMED” (or “IN TRADE”). The line shown means confidence 0.69 cleared the 0.49 gate." },
  { n: 3, title: "Price ribbon", body: "The 5m price history. Green for an uptrend, red for a downtrend, grey when flat — and it switches to the verdict colour once the coin is armed or in a position." },
  { n: 4, title: "Order book depth", body: "Live resting bid vs ask size, the spread in basis points, and which side is heavier. Here the book is ASK-HEAVY (more sell depth) with $30.4k on the ask vs $4.0k on the bid." },
  { n: 5, title: "Consensus dots + tally", body: "One dot per active model, lit green/red by its vote and unlit when FLAT. “1L · 0S · 4F · 1/5” = one long vote, zero short, four abstaining, agreement 1 of 5." },
  { n: 6, title: "Confidence bar", body: "The aggregated confidence as a fill (0.69). Compare it to the gate to see how close the signal is to firing." },
  { n: 7, title: "Consensus headline", body: "The resolved verdict — “CONSENSUS 1/5 LONG” — and the big confidence number against the band's gate (0.69 CONF · GATE 0.49)." },
  { n: 8, title: "The orb (core + model nodes)", body: "The wireframe core fills toward the bias colour as confidence builds, and pulses gold when armed. The five nodes (TA, MR, FR, OB, VP) orbit it, each glowing its own vote colour. Drag to spin it like a fidget spinner — fling and it coasts down. It's a visualization, not a control." },
  { n: 9, title: "SHORT / LONG gate poles", body: "Each side's structural gate. The ring fills as its four signals close (LONG: spot lead, OI↑, bid-heavy, no pump · SHORT: spot lag, OI↑ with falling price, ask-heavy, no dump). A beam flows toward the leaning side and a shockwave fires on arming. A gate switched off in Controls shows hazard stripes instead." },
];

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Card>
      <div className="font-semibold text-sm mb-1.5" style={{ color: ACCENT_BRIGHT }}>
        {title}
      </div>
      <div className="text-sm text-slate-400 leading-relaxed">{children}</div>
    </Card>
  );
}

export default function DashboardGuide() {
  return (
    <>
      <PageHeader
        kicker="Dashboard"
        title="Reading the screens"
        intro="A tour of every page and panel — what each number means and how to interpret it. The header is always visible: network, a heartbeat indicator, the active preset, the enabled directions, and the current risk state."
      />

      <Section title="Live page">
        <div className="grid sm:grid-cols-2 gap-3">
          <Panel title="Open positions">
            Each open trade with its side, size, entry, and live UPNL (unrealized profit/loss). The
            chart overlays the entry, stop-loss and take-profit levels, plus a breakeven marker once
            the lock fires.
          </Panel>
          <Panel title="The Analysis Core card">
            Each coin gets a live 3D card — the price ribbon, order-book depth, the consensus
            read-out and the spinnable orb. It is the densest panel on the page; the full annotated
            breakdown is just below.
          </Panel>
          <Panel title="Per-coin cards">
            Each coin's current band verdict, confidence, and whether it is armed. An armed card
            glows when every entry gate is clear and an order is imminent.
          </Panel>
          <Panel title="24H PnL chart">
            Realized profit and loss over the last day. Read it for session trajectory — a steady
            climb vs a sawtooth tells you whether trades are sticking.
          </Panel>
        </div>
      </Section>

      <Section title="The Analysis Core card, region by region">
        <p className="text-slate-400 text-sm mb-4 leading-relaxed">
          This is the card you'll spend the most time reading. Below is a faithful replica with every
          region numbered — match each number to the list underneath.
        </p>
        <div className="md:flex md:gap-6 md:items-start">
          <div className="md:w-[340px] md:shrink-0 mb-5 md:mb-0">
            <OrbCard />
          </div>
          <div className="flex-1 min-w-0">
            <Card>
              <div className="space-y-3">
                {ORB_LEGEND.map((item) => (
                  <div key={item.n} className="flex gap-3">
                    <span
                      className="flex items-center justify-center w-5 h-5 shrink-0 rounded-full text-[11px] font-bold mono"
                      style={{
                        background: "rgba(7,10,14,0.9)",
                        color: "#2de8b0",
                        border: "1px solid #1D9E75",
                        boxShadow: "0 0 8px #1D9E7588",
                      }}
                    >
                      {item.n}
                    </span>
                    <div>
                      <div className="text-sm font-semibold" style={{ color: ACCENT_BRIGHT }}>
                        {item.title}
                      </div>
                      <p className="text-xs text-slate-400 leading-relaxed">{item.body}</p>
                    </div>
                  </div>
                ))}
              </div>
            </Card>
          </div>
        </div>
        <p className="text-[11px] text-slate-500 mt-3">
          Colour key: green = LONG / passing, red = SHORT / blocking, grey = FLAT or inactive. The
          same palette is used across every card on the dashboard.
        </p>
      </Section>

      <Section title="Signals page">
        <p className="text-slate-400 text-sm mb-3 leading-relaxed">
          The model tickets, per coin, per band. This is where you see <em>why</em> the bot does or
          does not want a trade.
        </p>
        <Card>
          <div className="space-y-2 text-sm">
            <div className="flex items-center gap-2">
              <DirPill dir="LONG" /> <DirPill dir="SHORT" /> <DirPill dir="FLAT" />
              <span className="text-slate-400">
                — each model's vote. L = long, S = short, F = abstaining (does not dilute the score).
              </span>
            </div>
            <div className="flex items-center gap-2">
              <Pill tone="warn">funding dampened</Pill>
              <span className="text-slate-400">
                — funding voted counter-trend and had its weight cut to 0.40×.
              </span>
            </div>
            <div className="flex items-center gap-2">
              <Pill tone="accent">conf 0.52</Pill>
              <span className="text-slate-400">
                — the aggregated confidence. Compare it to the band's gate to see if it clears.
              </span>
            </div>
          </div>
        </Card>
      </Section>

      <Section title="Analysis Core bar">
        <Panel title="Scan · armed · threshold">
          A live readout across the top of the analysis views: how many coins were scanned this
          cycle, how many are currently armed (every gate clear), and the active confidence
          threshold being applied. When armed count rises, an entry is close.
        </Panel>
      </Section>

      <Section title="Risk page">
        <Panel title="State & drawdown bars">
          The current state badge, plus drawdown bars showing how close daily and weekly equity are
          to their limits (green → amber → red as they approach the threshold). It also lists the
          effective risk parameters currently in force.
        </Panel>
      </Section>

      <Section title="History page">
        <div className="grid sm:grid-cols-2 gap-3">
          <Panel title="Round-trip trade log">
            Completed trades paired open-to-close, with the realized result, the band, and the
            preset that was active at the time. Daily summaries roll these up.
          </Panel>
          <Panel title="Audit log & CSV export">
            The decision trail — skips, blocks, fallbacks and state changes — plus a CSV export of
            trade history for offline analysis.
          </Panel>
        </div>
      </Section>

      <Section title="Header indicators">
        <Card>
          <div className="flex flex-wrap gap-2 mb-3">
            <Pill tone="accent">♥ 12s</Pill>
            <Pill tone="cyan">testnet</Pill>
            <Pill tone="muted">DUAL BAND</Pill>
            <DirPill dir="LONG" />
            <Pill tone="warn">MANAGING</Pill>
          </div>
          <p className="text-sm text-slate-400 leading-relaxed">
            The <span style={{ color: ACCENT_BRIGHT }}>heartbeat</span> shows seconds since the
            trading loop last wrote its liveness file — green under 90s, red if it goes stale (the
            bot may be down or the bridge unreachable). The rest mirror the active network, preset,
            enabled directions, and risk state.
          </p>
        </Card>
        <p className="text-xs text-slate-500 mt-2">
          If the dashboard shows UNKNOWN/DOWN it usually means the API bridge is unreachable, not
          necessarily that the bot itself has stopped.
        </p>
      </Section>
    </>
  );
}
