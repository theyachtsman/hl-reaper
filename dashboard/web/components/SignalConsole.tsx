"use client";
/**
 * SIGNAL CONSOLE — the live scrolling feed that replaces the win-streak ring.
 *
 * Two modes, switched automatically:
 *  - POSITIONS OPEN: streams live per-position marks (uPnL re-marked against the
 *    2.5s pulse mids, % from entry) plus realized-PnL close events as they land.
 *  - FLAT: sweeps the consensus — per coin × band it reports direction,
 *    confidence vs the band's gate, model agreement, regime, and whether it's
 *    ARMED or what's blocking it — interleaved with real order-flow events
 *    (price bursts, resting book walls).
 *
 * Newest line on top, capped buffer, feed-in animation — a busy but real
 * console. Every number comes from the same APIs the rest of the deck uses.
 */
import { useEffect, useRef, useState } from "react";
import clsx from "clsx";
import { usePoll } from "@/lib/api";

const PROFIT = "#22c98e", LOSS = "#f0625f", ACCENT = "#22d3ee", DIM = "#64748b";

type Tone = "good" | "bad" | "accent" | "scalp" | "trend" | "neutral";
type Line = { id: number; t: string; text: string; tone: Tone };

const TONE_COLOR: Record<Tone, string> = {
  good: PROFIT, bad: LOSS, accent: ACCENT,
  scalp: "#22d3ee", trend: "#c084fc", neutral: DIM,
};

type CoinPulse = {
  mid: number; imbalance: number; bid_notional: number; ask_notional: number;
};
type BandVerdict = {
  direction: string; confidence: number; agreement: number;
  regime: string; would_fire: boolean; block_reason?: string | null;
  enabled?: boolean;
};
type TicketsResp = {
  ts: number | null;
  verdicts?: Record<string, { scalp: BandVerdict; trend: BandVerdict }>;
  gates?: {
    scalp: { min_confidence: number; min_model_agreement: number };
    trend: { min_confidence: number; min_model_agreement: number };
  };
  bands?: { scalp: boolean; trend: boolean };
};

const arrow = (d: string) => (d === "LONG" ? "▲" : d === "SHORT" ? "▼" : "·");
const now = () => new Date().toLocaleTimeString("en-US", { hour12: false });

export default function SignalConsole({
  positions, pulse, fills,
}: {
  positions: any[];
  pulse: { coins: Record<string, CoinPulse> } | undefined;
  fills: { recent: any[] } | undefined;
}) {
  const { data: tickets } = usePoll<TicketsResp>("/api/tickets", 4000);
  const [lines, setLines] = useState<Line[]>([]);
  const idRef = useRef(0);
  const sweepRef = useRef(0);            // rotates through coins while scanning
  const posRef = useRef(0);             // rotates through open positions
  const prevMid = useRef<Record<string, number>>({});
  const seenClose = useRef<Set<string> | null>(null);

  const push = (items: { text: string; tone: Tone }[]) => {
    if (!items.length) return;
    const t = now();
    setLines((prev) => {
      const fresh = items.map((it) => ({ id: idRef.current++, t, ...it }));
      return [...fresh, ...prev].slice(0, 16);
    });
  };

  const hasPos = positions.length > 0;

  /* ---- order-flow events + live position marks (pulse cadence 2.5s) ---- */
  useEffect(() => {
    if (!pulse?.coins) return;
    const out: { text: string; tone: Tone }[] = [];

    // strongest flow event across coins (burst or resting wall)
    let best: { score: number; text: string } | null = null;
    for (const [coin, p] of Object.entries(pulse.coins)) {
      const pm = prevMid.current[coin];
      prevMid.current[coin] = p.mid;
      if (pm) {
        const bps = ((p.mid - pm) / pm) * 10_000;
        if (Math.abs(bps) >= 5 && (!best || Math.abs(bps) > best.score))
          best = { score: Math.abs(bps),
            text: `${coin} ${bps > 0 ? "▲" : "▼"} ${bps > 0 ? "+" : ""}${bps.toFixed(1)}bp burst` };
      }
      const wall = p.imbalance > 0 ? p.bid_notional : p.ask_notional;
      if (Math.abs(p.imbalance) >= 0.6 && wall >= 4000 &&
          (!best || Math.abs(p.imbalance) * 4 > best.score))
        best = { score: Math.abs(p.imbalance) * 4,
          text: `${coin} ${p.imbalance > 0 ? "bid" : "ask"} wall $${(wall / 1000).toFixed(1)}k · skew ${p.imbalance > 0 ? "+" : ""}${(p.imbalance * 100).toFixed(0)}%` };
    }
    if (best) out.push({ text: best.text, tone: "accent" });

    // one rotating open position, re-marked live
    if (hasPos) {
      const p = positions[posRef.current % positions.length];
      posRef.current++;
      const mid = pulse.coins[p.coin]?.mid;
      const upnl = mid ? p.szi * (mid - p.entry_px) : p.unrealized_pnl;
      const pct = mid && p.entry_px ? ((mid - p.entry_px) / p.entry_px) * 100 * Math.sign(p.szi) : 0;
      const long = p.szi > 0;
      const band = p.band ? `·${p.band}` : "";
      out.push({
        text: `${p.coin} ${long ? "LONG" : "SHORT"}${band} ${upnl >= 0 ? "+" : "-"}$${Math.abs(upnl).toFixed(2)} ${pct >= 0 ? "+" : ""}${pct.toFixed(2)}% mark ${mid ? mid.toLocaleString() : "—"}`,
        tone: upnl >= 0 ? "good" : "bad",
      });
    }
    push(out);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pulse]);

  /* ---- realized PnL closes as they land (fills cadence 10s) ---- */
  useEffect(() => {
    if (!fills?.recent) return;
    const closes = fills.recent.filter((f) => f.closed_pnl !== 0);
    const key = (f: any) => `${f.ts}|${f.coin}|${f.px}|${f.closed_pnl}`;
    if (seenClose.current === null) { seenClose.current = new Set(closes.map(key)); return; }
    const fresh = closes.filter((f) => !seenClose.current!.has(key(f)));
    closes.forEach((f) => seenClose.current!.add(key(f)));
    push(fresh.slice(0, 4).map((f) => ({
      text: `CLOSE ${f.coin} ${f.closed_pnl > 0 ? "+" : "-"}$${Math.abs(f.closed_pnl).toFixed(2)} · ${f.closed_pnl > 0 ? "win" : "loss"}`,
      tone: f.closed_pnl > 0 ? "good" : "bad",
    })));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fills]);

  /* ---- consensus sweep when flat (tickets cadence 4s) ---- */
  useEffect(() => {
    if (hasPos) return;                 // positions mode owns the feed
    const v = tickets?.verdicts;
    const g = tickets?.gates;
    if (!v || !g) return;
    const coins = Object.keys(v);
    if (!coins.length) return;
    const out: { text: string; tone: Tone }[] = [];

    // every 5th sweep, emit a portfolio consensus summary
    if (sweepRef.current % 5 === 0) {
      let armed = 0, sConf = 0, tConf = 0, n = 0;
      for (const c of coins) {
        if (v[c].scalp?.would_fire) armed++;
        if (v[c].trend?.would_fire) armed++;
        sConf += v[c].scalp?.confidence ?? 0;
        tConf += v[c].trend?.confidence ?? 0;
        n++;
      }
      out.push({
        text: `CONSENSUS ${armed} armed · scalp avg ${(sConf / n).toFixed(2)} · trend avg ${(tConf / n).toFixed(2)} · ${n} coins`,
        tone: "accent",
      });
    }

    // one coin per sweep, both bands
    const coin = coins[sweepRef.current % coins.length];
    sweepRef.current++;
    (["scalp", "trend"] as const).forEach((band) => {
      if (tickets?.bands && !tickets.bands[band]) return;   // band disabled
      const bv = v[coin][band];
      if (!bv) return;
      const gate = g[band];
      const conf = bv.confidence.toFixed(2);
      if (bv.direction === "FLAT") {
        out.push({ text: `${coin} ${band} · flat ${bv.regime || ""} ${conf}`, tone: band });
      } else if (bv.would_fire) {
        out.push({
          text: `${coin} ${band} ${arrow(bv.direction)} ${bv.direction} ARMED ${conf} · ${bv.agreement}/${gate.min_model_agreement}`,
          tone: "good",
        });
      } else if (bv.block_reason) {
        out.push({ text: `${coin} ${band} ✗ ${bv.direction} ${bv.block_reason}`, tone: "bad" });
      } else {
        const gap = (gate.min_confidence - bv.confidence);
        const why = gap > 0 ? `${gap.toFixed(2)} to arm`
          : `${bv.agreement}/${gate.min_model_agreement} votes`;
        out.push({
          text: `${coin} ${band} ${arrow(bv.direction)} ${bv.direction} ${conf} · ${why}`,
          tone: band,
        });
      }
    });
    push(out);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tickets]);

  const mode = hasPos ? "LIVE POSITIONS" : "SCANNING CONSENSUS";

  return (
    <div className="core-card overflow-hidden flex flex-col absolute inset-0">
      {/* header */}
      <div className="flex items-center gap-2 px-3 pt-2.5 pb-1.5 shrink-0">
        <span className="pulse-icon inline-block w-[5px] h-[5px] rounded-full"
          style={{ background: hasPos ? PROFIT : ACCENT,
                   boxShadow: `0 0 6px ${hasPos ? PROFIT : ACCENT}` }} />
        <span className="label">Signal Feed</span>
        <span className="ml-auto text-[8px] uppercase tracking-[0.18em]"
          style={{ color: hasPos ? PROFIT : ACCENT }}>{mode}</span>
      </div>

      {/* scrolling lines (newest on top) */}
      <div className="relative flex-1 min-h-0 overflow-hidden px-3 pb-2">
        <div className="absolute inset-x-0 bottom-0 h-10 bg-gradient-to-t from-[#070a0e] to-transparent z-[1] pointer-events-none" />
        <div className="flex flex-col gap-[3px]">
          {lines.length === 0 && (
            <div className="text-[10px] text-slate-600 mono py-2">
              <span className="inline-flex gap-0.5 mr-1" style={{ color: ACCENT }}>
                <span className="scan-dot">·</span><span className="scan-dot">·</span><span className="scan-dot">·</span>
              </span>
              initializing signal feed…
            </div>
          )}
          {lines.map((l) => (
            <div key={l.id} className="feed-in flex items-baseline gap-2 mono text-[10.5px] leading-tight">
              <span className="text-slate-600 tabular-nums shrink-0">{l.t}</span>
              <span className="shrink-0" style={{ color: TONE_COLOR[l.tone] }}>›</span>
              <span className="truncate" style={{
                color: TONE_COLOR[l.tone],
                textShadow: l.tone === "accent" || l.tone === "good"
                  ? `0 0 7px ${TONE_COLOR[l.tone]}55` : undefined,
              }}>{l.text}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
