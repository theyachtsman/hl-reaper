import { PageHeader, Card, ACCENT_BRIGHT } from "../_components/ui";

export const metadata = { title: "Glossary · Docs" };

const TERMS: { term: string; def: React.ReactNode }[] = [
  { term: "Aggregator", def: "The component that combines the five model votes into a single directional signal with a confidence score." },
  { term: "Armed signal", def: "A signal that has cleared every entry gate and is ready to place an order. On the dashboard an armed coin glows." },
  { term: "ATR", def: "Average True Range — a measure of recent volatility. Stop distances are sized as a multiple of ATR, so stops widen in volatile markets and tighten in calm ones." },
  { term: "Band", def: "One of the two parallel strategies: SCALP (5m, fast) or TREND (1h, patient). Each coin can be held by only one band at a time." },
  { term: "Breakeven lock", def: "The first protective stop move: once a trade is far enough in profit (its breakeven R), the stop snaps to entry plus a tiny buffer so the trade can't turn into a loss." },
  { term: "Cascade detection", def: "A market kill-switch: a large open-interest drop plus a sharp price move in a 5-minute window signals a liquidation cascade, closing everything and halting for 2 hours." },
  { term: "Circuit breaker", def: "Any guard that stops trading when a limit is hit — daily/weekly drawdown, per-trade loss, cascade, or flash-crash." },
  { term: "Confidence score", def: "The 0–1 strength of the aggregated signal: the absolute weighted score divided by the total active weight. Must clear the band's floor to trade." },
  { term: "Conviction", def: "Informal term for how strongly the ensemble agrees on a direction — high confidence plus high agreement." },
  { term: "Counter-trend dampening", def: "Reducing the influence of a vote that opposes the confirmed 1h trend. Funding votes against the trend are cut to 0.40× weight; counter-trend orderbook votes to 0.40× confidence; counter-trend scalps to 0.70× confidence." },
  { term: "Ensemble", def: "The collection of five models voting together. No single model decides — the weighted group does." },
  { term: "FLAT vote", def: "A model abstaining (no edge, or confidence below 0.05). FLAT votes are excluded from both the score and the weight total, so they never dilute the models that did vote." },
  { term: "Funding rate", def: "The periodic payment between perp longs and shorts. Positive funding means longs pay shorts (longs crowded); the funding model fades the crowded side." },
  { term: "Maker timeout", def: "When a post-only (maker) limit order fails to fill within its window. Repeated timeouts can trigger the taker fallback." },
  { term: "Model agreement", def: "How many models vote the winning direction. Must meet the band's quorum (scalp 2 / trend 3) to enter." },
  { term: "One-way mode", def: "Hyperliquid's setting where a coin nets to a single position. It's why a coin is owned by at most one band at a time." },
  { term: "R (as in 1.5R)", def: "One unit of initial risk — the distance from entry to the original stop. A 1.5R take-profit targets 1.5× that distance in profit." },
  { term: "Regime", def: "The market state the detector reports for a coin: TRENDING_UP, TRENDING_DOWN, RANGING, or HIGH_VOL. It routes weights and gates mean reversion; it is not a directional vote." },
  { term: "Structural gate", def: "Extra microstructure confirmation the SCALP band requires before entering — spot lead/lag, OI direction, book imbalance, and a momentum cooldown." },
  { term: "Taker fallback", def: "After several consecutive maker timeouts within a window, the bot re-validates the live signal and, if it still holds and the move isn't exhausted, takes the market instead of missing the trade." },
  { term: "Trailing stop", def: "A stop that follows price in the favorable direction once a trade reaches its trail-activation R, trailing by 1.0× ATR and only ever tightening." },
  { term: "TTL (armed-signal)", def: "Time-to-live for an armed setup. If a signal stays armed but unfilled longer than its band TTL (75s), it's dropped as stale rather than filled into a changed market." },
  { term: "UPNL", def: "Unrealized profit/loss — the live mark-to-market on an open position before it's closed." },
];

export default function Glossary() {
  return (
    <>
      <PageHeader
        kicker="Glossary"
        title="Every term, in plain English"
        intro="Definitions for the words you'll meet across the dashboard and these docs. Alphabetical."
      />
      <Card>
        <dl className="divide-y divide-edge/50">
          {TERMS.map((t) => (
            <div key={t.term} className="py-3 grid sm:grid-cols-[170px_1fr] gap-1 sm:gap-4">
              <dt className="font-semibold text-sm" style={{ color: ACCENT_BRIGHT }}>
                {t.term}
              </dt>
              <dd className="text-sm text-slate-400 leading-relaxed">{t.def}</dd>
            </div>
          ))}
        </dl>
      </Card>
    </>
  );
}
