"use client";
/**
 * DepthLadder — compact order-book depth visualizer for one Analysis Core coin.
 *
 * Renders the top-of-book as a mirrored liquidity ladder (bids grow left in
 * green, asks grow right in red, from a center axis), a dominance slider whose
 * knob sits at the live bid-volume fraction, and a spread readout. All data is
 * the bridge's 2.5s L2 pulse (bid_szs/ask_szs/spread_bps/imbalance) — no extra
 * fetch. Styling matches the cards: near-black strip, #1D9E75 / #E24B4A accents.
 */
import clsx from "clsx";

const BID = "#1D9E75", ASK = "#E24B4A";

export type Depth = {
  mid?: number;
  spread_bps?: number;
  imbalance?: number;          // (bidVol - askVol)/(bidVol + askVol), -1..+1
  bid_szs?: number[];          // top levels, best-first
  ask_szs?: number[];
  bid_notional?: number;
  ask_notional?: number;
};

const fmtK = (v?: number) =>
  v == null ? "—"
    : v >= 1000 ? `$${(v / 1000).toFixed(1)}k`
    : `$${v.toFixed(0)}`;

export default function DepthLadder({ depth, embedded }: {
  depth?: Depth;
  /** when true, render transparent (no strip border/bg) so it can sit over the
      3D scene's bottom scrim and read as part of that section */
  embedded?: boolean;
}) {
  const bids = depth?.bid_szs ?? [];
  const asks = depth?.ask_szs ?? [];
  const have = bids.length > 0 && asks.length > 0;
  const maxSz = Math.max(1e-9, ...bids, ...asks);
  const rows = Math.max(bids.length, asks.length);

  const imb = depth?.imbalance ?? 0;       // +bid-heavy .. -ask-heavy
  const bidFrac = Math.min(1, Math.max(0, (imb + 1) / 2));  // = bid vol fraction
  const spread = depth?.spread_bps;
  const heavy = imb >= 0 ? "bid" : "ask";

  return (
    <div className={clsx("px-3 py-2",
      embedded ? "" : "border-t border-edge/60 bg-black/30")}
      style={embedded ? { textShadow: "0 1px 4px rgba(0,0,0,0.95)" } : undefined}>
      {/* header: label + spread readout */}
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-[8px] uppercase tracking-[0.18em] text-slate-500">
          order book depth
        </span>
        <span className="text-[9px] mono text-slate-400">
          spread{" "}
          <span className="text-slate-200 tabular-nums">
            {spread != null ? spread.toFixed(1) : "—"}
          </span>{" "}
          bp
        </span>
      </div>

      {/* mirrored liquidity ladder — bids grow left, asks grow right */}
      {have ? (
        <div className="grid gap-[2px]">
          {Array.from({ length: rows }).map((_, i) => {
            const b = bids[i] ?? 0;
            const a = asks[i] ?? 0;
            return (
              <div key={i} className="grid grid-cols-2 gap-[3px] h-[6px]">
                <div className="relative">
                  <div className="absolute inset-y-0 right-0 rounded-sm transition-[width] duration-300"
                    style={{ width: `${(b / maxSz) * 100}%`, background: BID, opacity: 0.85 }}
                    title={`bid L${i + 1}: ${b}`} />
                </div>
                <div className="relative">
                  <div className="absolute inset-y-0 left-0 rounded-sm transition-[width] duration-300"
                    style={{ width: `${(a / maxSz) * 100}%`, background: ASK, opacity: 0.85 }}
                    title={`ask L${i + 1}: ${a}`} />
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="h-[44px] flex items-center justify-center text-[9px] text-slate-600">
          waiting for book scan…
        </div>
      )}

      {/* dominance slider — knob sits at the live bid-volume fraction */}
      <div className="mt-2">
        <div className="relative h-[5px] rounded-full overflow-hidden bg-white/10">
          <div className="absolute inset-y-0 left-0 transition-[width] duration-300"
            style={{ width: `${bidFrac * 100}%`, background: BID, opacity: 0.55 }} />
          <div className="absolute inset-y-0 right-0 transition-[width] duration-300"
            style={{ width: `${(1 - bidFrac) * 100}%`, background: ASK, opacity: 0.55 }} />
          <div className="absolute top-1/2 w-[3px] h-[10px] rounded-full bg-white transition-[left] duration-300"
            style={{ left: `${bidFrac * 100}%`, transform: "translate(-50%, -50%)",
                     boxShadow: "0 0 6px rgba(255,255,255,0.8)" }} />
        </div>
        <div className="flex items-center justify-between mt-1 text-[8px] mono tabular-nums">
          <span style={{ color: BID }}>
            bid {(bidFrac * 100).toFixed(0)}%
            <span className="text-slate-600"> · {fmtK(depth?.bid_notional)}</span>
          </span>
          <span className={clsx("uppercase tracking-wider",
            imb >= 0 ? "text-emerald-400/80" : "text-red-400/80")}>
            {heavy}-heavy {Math.abs(imb * 100).toFixed(0)}%
          </span>
          <span style={{ color: ASK }}>
            <span className="text-slate-600">{fmtK(depth?.ask_notional)} · </span>
            ask {((1 - bidFrac) * 100).toFixed(0)}%
          </span>
        </div>
      </div>
    </div>
  );
}
