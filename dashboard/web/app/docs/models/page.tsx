import {
  PageHeader,
  Section,
  Card,
  Pill,
  DirPill,
  Callout,
  WeightBar,
  ACCENT_BRIGHT,
  CYAN,
} from "../_components/ui";

export const metadata = { title: "Models · Docs" };

type ModelDoc = {
  name: string;
  scalp: number;
  trend: number;
  reads: string;
  long: string;
  short: string;
  flat: string;
  best: string;
  worst: string;
  extra?: { title: string; body: React.ReactNode };
};

const DOCS: ModelDoc[] = [
  {
    name: "TAModel",
    scalp: 0.13,
    trend: 0.24,
    reads:
      "The last 100 candles on the band's resolution. Blends four classic indicators into one score: RSI(14), MACD histogram, the EMA-9 vs EMA-21 cross, and Bollinger Bands(20, 2σ).",
    long: "the blended score is positive — momentum and trend lean up (e.g. EMA-9 above EMA-21, rising MACD, price near the lower Bollinger band). In a confirmed uptrend it now also goes LONG at moderate RSI (see below).",
    short: "the blended score is negative — momentum and trend lean down. In a confirmed downtrend it goes SHORT at moderate RSI (RSI ≥ 48) rather than waiting for an extreme.",
    flat: "in normal conditions, the blended score is within ±0.15 of zero. In a trending regime, only when RSI sits in the narrow neutral zone between the relaxed thresholds.",
    best: "Trending markets, where momentum and the EMA cross carry real information. It is the trend band's heaviest TA contributor — and the regime-aware mode below keeps it voting there.",
    worst: "Choppy ranges, where the indicators whipsaw and cancel out (RANGING keeps the original conservative blend).",
    extra: {
      title: "Regime-aware trending mode (newest behavior)",
      body: (
        <>
          In a clear 1h trend the blend's mean-reversion parts (RSI, Bollinger)
          fight its trend parts (EMA cross, MACD), the score collapses into the
          ±0.15 dead-band, and TA abstains <DirPill dir="FLAT" /> exactly when the
          regime is trending — starving the aggregator of voters. So in{" "}
          <strong>TRENDING_UP / TRENDING_DOWN</strong> TA switches to a relaxed RSI
          rule: it agrees with the trend at <em>moderate</em> RSI (e.g.{" "}
          <DirPill dir="SHORT" /> once RSI ≥ 48 in a downtrend) and only fades it
          at a genuine extreme (<DirPill dir="LONG" /> at RSI ≤ 38). Confidence
          scales from <span className="mono">0.40</span> at the firing edge up to{" "}
          <span className="mono">0.95</span> as RSI stretches, and a Bollinger-mid
          / price-direction check dampens (×0.85) an unconfirmed trend-aligned
          vote. <strong>RANGING / HIGH_VOL / UNKNOWN keep the original blend.</strong>{" "}
          The thresholds are tunable live on the Controls page (TA Trending Mode).
        </>
      ),
    },
  },
  {
    name: "MeanReversionModel",
    scalp: 0.38,
    trend: 0.0,
    reads:
      "The z-score of the latest close against its rolling 20-period mean and standard deviation.",
    long: "price is stretched far below its mean (z ≤ −2.0) — a snap-back up is likely.",
    short: "price is stretched far above its mean (z ≥ +2.0) — a snap-back down is likely.",
    flat: "price is within ±2σ of the mean, or the regime detector says the market is not RANGING.",
    best: "Range-bound, mean-reverting tape. It is the SCALP band's dominant signal at 0.45 weight — nearly half.",
    worst: "Sustained trends, where 'overbought' keeps getting more overbought. This is exactly why it is weighted to zero in the TREND band and gated to RANGING regimes only.",
  },
  {
    name: "FundingRateModel",
    scalp: 0.04,
    trend: 0.16,
    reads:
      "The current perp funding rate (annualized to an 8h figure), its 24h average, and its 3h trend from the funding history table.",
    long: "funding is negative — shorts are crowded and paying longs, so the contrarian read is up.",
    short: "funding is positive — longs are crowded and paying shorts, so the contrarian read is down.",
    flat: "funding sits inside the neutral band (|rate| ≤ 0.0001/8h) — no crowding to fade.",
    best: "Crowded, one-sided markets and funding squeezes, where the paying side eventually capitulates.",
    worst: "Sustained trends, where funding can stay negative (or positive) for hours while price keeps going — which is what the new dampening below fixes.",
    extra: {
      title: "Counter-trend funding dampening (newest behavior)",
      body: (
        <>
          Funding fades the crowd — great in a squeeze, but in a sustained 1h trend a persistently
          negative funding rate makes it vote <DirPill dir="LONG" /> all the way down a drop,
          cancelling the conviction of the trend-aligned voters. So when funding votes{" "}
          <em>against</em> the confirmed 1h trend, its aggregator weight is cut to{" "}
          <span className="mono" style={{ color: ACCENT_BRIGHT }}>0.40×</span> (numerator and
          denominator both). Trend-aligned funding and ranging/high-vol regimes keep full weight.
          There are also two stronger funding guards — a 0.6× confidence veto and an outright
          hard-block — covered on the Risk page.
        </>
      ),
    },
  },
  {
    name: "OrderbookImbalanceModel",
    scalp: 0.17,
    trend: 0.24,
    reads:
      "Live L2 order book depth — the summed bid size vs ask size over the top 10 levels, expressed as an imbalance from −1 (all ask) to +1 (all bid). Ignores books older than 10s.",
    long: "the book is bid-heavy — imbalance above +0.30 (more resting buy depth).",
    short: "the book is ask-heavy — imbalance below −0.30 (more resting sell depth).",
    flat: "imbalance is within ±0.30, or the book is missing/stale.",
    best: "Confirming live, immediate pressure. It is the most heavily weighted voter in the base set and carries strong weight in both bands.",
    worst: "When large players spoof or absorb — a counter-trend imbalance in a strong 1h trend is usually absorption, not a reversal, so such votes are dampened to 0.40× confidence.",
  },
  {
    name: "VWAPModel",
    scalp: 0.13,
    trend: 0.16,
    reads:
      "Session VWAP (volume-weighted average price) since UTC midnight, with ±1σ bands. Falls back to the last 120 candles early in a session.",
    long: "price tags the −1σ band (strong mean-reversion buy) or holds above a rising VWAP.",
    short: "price tags the +1σ band (strong mean-reversion sell) or holds below a falling VWAP.",
    flat: "price sits at VWAP equilibrium (within ~0.1%) with no band touch or directional alignment.",
    best: "Intraday support/resistance — confirming whether price is rich or cheap versus the day's fair value.",
    worst: "Very young sessions or thin-volume coins, where VWAP is noisy and the bands are unreliable.",
  },
  {
    name: "MomentumModel",
    scalp: 0.15,
    trend: 0.2,
    reads:
      "The weighted rate of change of close over three lookback windows — 3, 6 and 12 candles on the band's resolution — blended 0.50 / 0.30 / 0.20 so the freshest move dominates. It ignores support, funding and book depth entirely; only the velocity of price matters.",
    long: "the composite move is a hard pump — at or above +0.3% it starts voting LONG, ramping to full confidence by +1.0%. It follows the move, never fades it.",
    short: "the composite move is a hard drop — at or below −0.3% it starts voting SHORT, ramping to full confidence by −1.0%.",
    flat: "the composite move sits inside ±0.3% (no decisive velocity), or there are fewer than 15 candles to measure.",
    best: "Fast one-directional moves — the freefall / blow-off where the mean-reversion voters call a bounce and price keeps going. It is the model that answers 'is price moving hard right now?', the question the other five miss.",
    worst: "Choppy ranges, where fast moves are false breakouts that snap back — so in a RANGING regime its weight is cut to 0.70×. Trending and high-vol regimes keep full weight.",
    extra: {
      title: "Why it was added (6/24 drop)",
      body: (
        <>
          On 2026-06-24 BTC fell 4.86% and ETH 5.91% in hours while the ensemble
          called <DirPill dir="LONG" /> at 0.65–0.83 the entire way down —{" "}
          MEANREV and VWAP read the oversold freefall as a bounce setup and no
          model voted <DirPill dir="SHORT" />. MomentumModel is the trend-following
          counterweight: a strong downward velocity now votes{" "}
          <DirPill dir="SHORT" /> with high confidence, pulling the net verdict
          away from a confident dip-buy into a crash. By construction it always
          votes <em>with</em> the move, so the counter-trend scalp penalty never
          applies to it. Thresholds are tunable live on the Controls page.
        </>
      ),
    },
  },
];

export default function Models() {
  return (
    <>
      <PageHeader
        kicker="Models"
        title="The six voters in detail"
        intro="Each model reads a different part of the market and returns a directional ticket. Below is exactly what each one watches, what makes it vote LONG, SHORT or FLAT, how heavily it counts in each band, and when to trust it."
      />

      {DOCS.map((m) => (
        <Section key={m.name} id={m.name} title={m.name}>
          <Card>
            <div className="flex flex-wrap items-center gap-4 mb-4">
              <div className="flex items-center gap-2">
                <span className="label">Scalp</span>
                <div className="w-24">
                  <WeightBar value={m.scalp} />
                </div>
              </div>
              <div className="flex items-center gap-2">
                <span className="label">Trend</span>
                <div className="w-24">
                  <WeightBar value={m.trend} tone={CYAN} />
                </div>
              </div>
            </div>

            <div className="text-sm mb-4">
              <div className="label mb-1">Reads</div>
              <p className="text-slate-300 leading-relaxed">{m.reads}</p>
            </div>

            <div className="space-y-1.5 mb-4">
              <div className="flex gap-2 text-sm">
                <DirPill dir="LONG" />
                <span className="text-slate-300">when {m.long}</span>
              </div>
              <div className="flex gap-2 text-sm">
                <DirPill dir="SHORT" />
                <span className="text-slate-300">when {m.short}</span>
              </div>
              <div className="flex gap-2 text-sm">
                <DirPill dir="FLAT" />
                <span className="text-slate-300">when {m.flat}</span>
              </div>
            </div>

            <div className="grid sm:grid-cols-2 gap-3">
              <div className="rounded-lg p-3" style={{ background: "rgba(29,158,117,0.08)", border: "1px solid rgba(29,158,117,0.25)" }}>
                <div className="label mb-1" style={{ color: ACCENT_BRIGHT }}>
                  Most useful
                </div>
                <p className="text-xs text-slate-300 leading-relaxed">{m.best}</p>
              </div>
              <div className="rounded-lg p-3" style={{ background: "rgba(226,75,74,0.08)", border: "1px solid rgba(226,75,74,0.25)" }}>
                <div className="label mb-1" style={{ color: "#f87171" }}>
                  Least useful
                </div>
                <p className="text-xs text-slate-300 leading-relaxed">{m.worst}</p>
              </div>
            </div>

            {m.extra && (
              <Callout tone="accent" title={m.extra.title}>
                {m.extra.body}
              </Callout>
            )}
          </Card>
        </Section>
      ))}

      <Callout tone="muted" title="Two more slots exist in the code">
        You may see a RegimeDetector reading and one or two greyed-out models on the Signals page.
        The regime detector is a meta-router that only shapes weights (it never casts a directional
        vote). Any greyed model carries zero weight and does not affect the score — those slots are
        being retired and are intentionally left out of these docs.
      </Callout>
    </>
  );
}
