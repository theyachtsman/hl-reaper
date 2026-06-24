import {
  PageHeader,
  Section,
  Card,
  Pill,
  Callout,
  CheckRow,
  KeyVal,
  ACCENT_BRIGHT,
  CYAN,
} from "../_components/ui";
import { StateMachineDiagram } from "../_components/diagrams";
import { STATES } from "../_components/data";

export const metadata = { title: "Risk Engine · Docs" };

const STATE_TONE: Record<string, "accent" | "warn" | "short" | "cyan" | "muted"> = {
  ACTIVE: "accent",
  MANAGING: "warn",
  HALTED: "short",
  RECONNECTING: "cyan",
  COOLDOWN: "warn",
  CASCADE_BOUNCE_ACTIVE: "muted",
};

export default function Risk() {
  return (
    <>
      <PageHeader
        kicker="Risk Engine"
        title="Four layers between a signal and an order"
        intro="Nothing opens a position without passing through the risk manager. It owns a six-state machine, a stack of pre-trade and in-trade guards, and a set of market kill-switches. Every threshold below is the live default."
      />

      <Section title="The state machine">
        <StateMachineDiagram />
        <div className="grid sm:grid-cols-2 gap-2 mt-4">
          {STATES.map((s) => (
            <Card key={s.name}>
              <Pill tone={STATE_TONE[s.name]}>{s.name}</Pill>
              <p className="text-xs text-slate-400 mt-2 leading-relaxed">{s.desc}</p>
            </Card>
          ))}
        </div>
      </Section>

      <Section title="Account circuit breakers">
        <p className="text-slate-400 text-sm mb-3">
          These watch account equity against a daily and weekly baseline (re-anchored at UTC
          midnight / ISO-week rollover).
        </p>
        <Card>
          <KeyVal k="Daily drawdown → MANAGING (no new entries)" v="5%" />
          <KeyVal k="Severe daily drawdown → close all + HALT to midnight" v="10%" />
          <KeyVal k="Weekly drawdown → COOLDOWN (48h lockout)" v="10%" />
        </Card>
      </Section>

      <Section title="Market kill-switches">
        <div className="grid sm:grid-cols-2 gap-3">
          <Card>
            <div className="font-semibold text-sm mb-2" style={{ color: "#f87171" }}>
              Liquidation cascade
            </div>
            <p className="text-xs text-slate-400 leading-relaxed mb-2">
              Detects a deleveraging cascade and closes everything, then halts for 2 hours. Both
              conditions must hit inside a 5-minute window:
            </p>
            <KeyVal k="Open-interest drop" v="> 15%" />
            <KeyVal k="Price move" v="> 3%" />
            <KeyVal k="Halt duration" v="2 h" />
          </Card>
          <Card>
            <div className="font-semibold text-sm mb-2" style={{ color: "#fbbf24" }}>
              Flash-crash pause
            </div>
            <p className="text-xs text-slate-400 leading-relaxed mb-2">
              A single 1-minute candle moving more than 5% pauses new entries on that coin for two
              more candles — long enough for the dust to settle.
            </p>
            <KeyVal k="Flash candle threshold" v="5%" />
            <KeyVal k="Pause length" v="2 candles" />
          </Card>
        </div>
        <Callout tone="warn" title="Extreme-funding halts">
          Per coin, funding beyond ±0.001/8h halts the crowded side: extreme positive funding halts
          new LONGs, extreme negative halts new SHORTs, until funding normalizes.
        </Callout>
      </Section>

      <Section title="Pre-trade guards">
        <p className="text-slate-400 text-sm mb-3">
          Every one of these must pass in <span className="mono">can_open()</span> before an order
          is placed:
        </p>
        <Card>
          <CheckRow ok>State is ACTIVE (not paused, halted, reconnecting, or in a bounce)</CheckRow>
          <CheckRow ok>Confidence ≥ the band floor (scalp 0.40 / trend 0.55)</CheckRow>
          <CheckRow ok>Model agreement ≥ the band quorum (scalp 2 / trend 3)</CheckRow>
          <CheckRow ok>Direction not halted by extreme funding on this coin</CheckRow>
          <CheckRow ok>No flash-crash pause active on this coin</CheckRow>
          <CheckRow ok>Band concurrency limit not reached (scalp 3 / trend 2)</CheckRow>
          <CheckRow ok>Coin not already held (one position per coin, across both bands)</CheckRow>
          <CheckRow ok>Order book present and spread ≤ 0.15%</CheckRow>
          <CheckRow ok>Leverage is positive (and clamped to the 5× ceiling)</CheckRow>
        </Card>
      </Section>

      <Section title="In-trade guards">
        <p className="text-slate-400 text-sm mb-3">
          Once a position is open it is re-checked every cycle against these, in order:
        </p>
        <div className="grid sm:grid-cols-2 gap-3">
          <Card>
            <div className="label mb-2" style={{ color: "#f87171" }}>
              Hard loss floors
            </div>
            <KeyVal k="Emergency close" v="−3% equity" />
            <KeyVal k="Max per-trade loss" v="−2% equity" />
          </Card>
          <Card>
            <div className="label mb-2" style={{ color: ACCENT_BRIGHT }}>
              Exits & protection
            </div>
            <KeyVal k="Stop loss / take profit" v="band geometry" />
            <KeyVal k="Max hold time" v="scalp 30m / trend 48h" />
          </Card>
        </div>

        <Callout tone="accent" title="Breakeven lock">
          The first protective move. Once a trade reaches its breakeven R (scalp{" "}
          <span className="mono">0.4R</span> / trend <span className="mono">0.8R</span> of
          unrealized profit), the stop snaps to entry plus a small fee-covering buffer (0.05%) and
          the take-profit is recalculated to hold the original reward:risk from the new, tighter
          stop. It fires once — a winning trade can no longer give back into a loss.
        </Callout>
        <Callout tone="cyan" title="Trailing stop">
          Activates later, at the band's trail-activation R (scalp{" "}
          <span className="mono">1.0R</span> / trend <span className="mono">2.0R</span>), and then
          trails the price by 1.0× ATR, only ever tightening in the favorable direction.
        </Callout>
      </Section>

      <Section title="The funding guards (three of them)">
        <Card>
          <div className="space-y-3 text-sm">
            <div>
              <Pill tone="muted">1 · Veto</Pill>
              <p className="text-slate-400 mt-1">
                A funding vote opposing the net signal multiplies the final confidence by{" "}
                <span className="mono">0.6</span> — a dampener.
              </p>
            </div>
            <div>
              <Pill tone="warn">2 · Hard-block</Pill>
              <p className="text-slate-400 mt-1">
                When funding is an extreme-crowded SHORT read (confidence ≥{" "}
                <span className="mono">0.75</span>), any LONG verdict is forced to FLAT outright —
                crowded longs get squeezed. The SHORT mirror exists but ships off by default.
              </p>
            </div>
            <div>
              <Pill tone="accent">3 · Counter-trend weight dampen</Pill>
              <p className="text-slate-400 mt-1">
                In a sustained 1h trend, a funding vote against that trend has its aggregator weight
                cut to <span className="mono">0.40×</span> so it cannot quietly cancel the
                trend-aligned voters.
              </p>
            </div>
          </div>
        </Card>
      </Section>

      <Section title="Structural gates">
        <p className="text-slate-400 text-sm mb-3 leading-relaxed">
          On top of the model vote, the <strong>SCALP</strong> band requires confirming
          microstructure before it enters (the TREND band's own 1h signal is its gate, so it skips
          these). All listed signals must pass; if any of signals 1–3 cannot be computed, the entry
          is blocked (fail-safe).
        </p>
        <div className="grid sm:grid-cols-2 gap-3">
          <Card>
            <div className="font-semibold text-sm mb-2" style={{ color: ACCENT_BRIGHT }}>
              LONG gate
            </div>
            <CheckRow ok>Spot leading perp — real demand, not leverage (&gt; 0.02%)</CheckRow>
            <CheckRow ok>Open interest rising — fresh longs entering (&gt; 0.1%)</CheckRow>
            <CheckRow ok>Book bid-heavy — imbalance ≥ 0.20</CheckRow>
            <CheckRow ok>Pump cooldown — no sharp recent run-up (0.5/0.8/1.2% over 5/10/15m)</CheckRow>
          </Card>
          <Card>
            <div className="font-semibold text-sm mb-2" style={{ color: "#f87171" }}>
              SHORT gate
            </div>
            <CheckRow ok>Spot lagging perp — real selling, not a leverage bounce (&lt; −0.02%)</CheckRow>
            <CheckRow ok>OI rising with falling price — fresh shorts entering</CheckRow>
            <CheckRow ok>Book ask-heavy — imbalance ≤ −0.20</CheckRow>
            <CheckRow ok>Dump cooldown — no sharp recent drop (0.5/0.8/1.2% over 5/10/15m)</CheckRow>
          </Card>
        </div>
      </Section>

      <Section title="Infrastructure guards">
        <Card>
          <KeyVal k="Market-data feed stale → RECONNECTING" v="> 30s" />
          <KeyVal k="Heartbeat file age warning" v="3 × interval" />
          <KeyVal k="API calls" v="retry w/ exponential backoff" />
          <KeyVal k="Duplicate-close suppression after a close" v="30s" />
        </Card>
      </Section>
    </>
  );
}
