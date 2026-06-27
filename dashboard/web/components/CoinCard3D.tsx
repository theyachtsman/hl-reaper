"use client";
/**
 * CoinCard3D — one coin's live Analysis Core card.
 *
 * A Three.js scene (price ribbon + particle cloud + orb/rings) fills the card,
 * colour-keyed to the price TREND (green up / red down / grey flat) — or to the
 * verdict direction when the coin is armed / in a position. A transparent UI
 * layer overlays coin/price, a centred verdict pill and a compact gate-status
 * bar; a dark info panel below breaks out the full LONG/SHORT structural gates,
 * the model-vote badges and the agreement count. Verdict + gates come from
 * /api/tickets (the bridge re-runs the real SignalAggregator); price history
 * from /api/chart/{coin}.
 */
import { useEffect, useMemo, useState } from "react";
import clsx from "clsx";
import { usePoll } from "@/lib/api";
import ThreeCanvas, { ColorMode, Intensity } from "@/components/ThreeCanvas";
import RelayCore from "@/components/RelayCore";
import DepthLadder, { Depth } from "@/components/DepthLadder";

export type Gates = { min_confidence: number; min_model_agreement: number };

const REGIME_WORD: Record<string, string> = {
  TRENDING_UP: "uptrend", TRENDING_DOWN: "downtrend",
  RANGING: "ranging", HIGH_VOL: "high volatility",
};

/* pull the short cause out of a bridge block_reason like
 * "LONG blocked (spot not leading perp)" → "spot not leading perp" */
function shortBlock(r?: string): string | null {
  if (!r) return null;
  const m = r.match(/\(([^)]+)\)/);
  return `⛔ ${m ? m[1] : r}`;
}

/* natural-language read on what the decision core is doing while standing down —
 * rotated through one line at a time in the verdict pill, so an idle coin still
 * tells you exactly how far it is from a trade and what's holding it back. */
function standingMessages(v: Verdict | undefined, gC: number, gQ: number): string[] {
  if (!v) return ["decision engine idle"];
  const conf = v.confidence ?? 0;
  const agree = v.agreement ?? 0;
  const dir = v.direction;
  const votes = `votes ${v.long_votes ?? 0}L · ${v.short_votes ?? 0}S · ${v.flat_votes ?? 0}F`;
  const regime = REGIME_WORD[v.regime] ? `regime: ${REGIME_WORD[v.regime]}` : null;
  const msgs: string[] = [];
  if (dir === "LONG" || dir === "SHORT") {
    msgs.push(`${dir === "LONG" ? "▲" : "▼"} ${dir} lean forming`);
    msgs.push(conf < gC
      ? `confidence ${conf.toFixed(2)} — need +${(gC - conf).toFixed(2)}`
      : `confidence ${conf.toFixed(2)} ✓ clears ${gC.toFixed(2)}`);
    msgs.push(agree < gQ
      ? `${agree}/${gQ} models agree — need ${gQ - agree} more`
      : `${agree}/${gQ} models agree ✓`);
    if (v.veto) msgs.push("funding veto — crowded side");
    const blk = shortBlock(v.block_reason);
    if (blk) msgs.push(blk);
  } else {
    msgs.push("scanning — no consensus");
    msgs.push(`confidence ${conf.toFixed(2)} / ${gC.toFixed(2)} gate`);
  }
  msgs.push(votes);
  if (regime) msgs.push(regime);
  return msgs;
}

type Gate = {
  allowed?: boolean; enabled?: boolean; block_reason?: string;
  spot_leading?: boolean; spot_lagging?: boolean;
  oi_rising?: boolean; ob_bid_heavy?: boolean; ob_ask_heavy?: boolean;
  momentum_ok?: boolean;
  spot_ret?: number; perp_ret?: number; oi_change?: number; imbalance?: number;
  move_5m?: number; move_10m?: number; move_15m?: number;
};
export type Verdict = {
  direction: string; confidence: number; agreement: number;
  long_votes: number; short_votes: number; flat_votes: number;
  regime: string; veto: boolean; would_fire: boolean;
  block_reason?: string; long_gate?: Gate | null; short_gate?: Gate | null;
};
type Ticket = { model: string; direction: string; confidence: number; meta: any };

const ACCENT: Record<ColorMode, string> = {
  long: "#1D9E75", short: "#E24B4A", neutral: "#888880",
};
const PASS = "#1D9E75", FAIL = "#E24B4A";
const ACTIVE_MODELS = 6; // TA / MR / FR / OB / VP / MO — the directional ensemble
const TREND_DEADBAND = 0.0025; // |Δ| below this over the window reads as flat

const pct = (v?: number, dp = 3) =>
  v == null ? "—" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(dp)}%`;

export default function CoinCard3D({ coin, mid, verdict, tickets, position, gates, gatesEnabled, depth }: {
  coin: string; mid?: number; verdict?: Verdict;
  tickets: Ticket[]; position?: "LONG" | "SHORT" | null; gates?: Gates;
  gatesEnabled?: { long: boolean; short: boolean }; depth?: Depth;
}) {
  const [hovered, setHovered] = useState(false);
  const { data: chart } = usePoll<{ candles: { close: number }[] }>(
    `/api/chart/${coin}?interval=5m&limit=60`, 10000);

  const prices = useMemo(
    () => (chart?.candles ?? []).map((c) => c.close).filter((n) => Number.isFinite(n)),
    [chart]);
  const change = prices.length > 1 ? (prices[prices.length - 1] - prices[0]) / prices[0] : 0;

  const armedDir = position ? position
    : verdict?.would_fire ? verdict.direction : null;
  const trend: ColorMode = change > TREND_DEADBAND ? "long"
    : change < -TREND_DEADBAND ? "short" : "neutral";
  const colorMode: ColorMode = armedDir === "LONG" ? "long"
    : armedDir === "SHORT" ? "short" : trend;
  const intensity: Intensity = position ? "position"
    : verdict?.would_fire ? "armed" : "neutral";
  const accent = ACCENT[colorMode];
  const glow = colorMode !== "neutral";

  const lg = verdict?.long_gate ?? undefined;
  const sg = verdict?.short_gate ?? undefined;

  // a fixed headline only when committed/armed; otherwise standing down → the
  // pill rotates through live natural-language status lines (see below).
  const verdictText =
    position === "LONG" ? "▲ IN TRADE — LONG"
    : position === "SHORT" ? "▼ IN TRADE — SHORT"
    : verdict?.would_fire && verdict.direction === "LONG" ? "▲ LONG ARMED"
    : verdict?.would_fire && verdict.direction === "SHORT" ? "▼ SHORT ARMED"
    : null;
  const pillArmed = position != null || !!verdict?.would_fire;
  const standing = !pillArmed;

  const gC = gates?.min_confidence ?? 0.4;
  const gQ = gates?.min_model_agreement ?? 3;
  const messages = useMemo(
    () => standingMessages(verdict, gC, gQ), [verdict, gC, gQ]);
  const [msgIdx, setMsgIdx] = useState(0);
  useEffect(() => {
    if (!standing || messages.length <= 1) return;
    const id = setInterval(() => setMsgIdx((i) => i + 1), 2800);
    return () => clearInterval(id);
  }, [standing, messages.length]);
  const rotMsg = messages[msgIdx % messages.length];
  const pillColor = position === "LONG" || (verdict?.would_fire && verdict.direction === "LONG") ? PASS
    : position === "SHORT" || (verdict?.would_fire && verdict.direction === "SHORT") ? FAIL
    : "#cbd5e1";

  const conf = verdict?.confidence ?? 0;
  const dir = verdict?.direction ?? "FLAT";

  return (
    <div
      className="relative rounded-xl overflow-hidden transition-shadow"
      style={{
        border: `1px solid ${glow ? accent + "cc" : "#1e2a3a"}`,
        boxShadow: glow
          ? `0 0 20px -6px ${accent}99, inset 0 0 40px -22px ${accent}`
          : "none",
      }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      {/* 3D scene */}
      <div className="relative h-[288px] max-[640px]:h-[228px]">
        {prices.length > 1
          ? <ThreeCanvas prices={prices} colorMode={colorMode} intensity={intensity} hovered={hovered} />
          : <div className="absolute inset-0 bg-black/60 animate-pulse" />}

        {/* overlay — pinned scrims keep the 3D middle clear */}
        <div className="absolute inset-0 pointer-events-none"
          style={{ textShadow: "0 1px 5px rgba(0,0,0,0.95)" }}>
          {/* top: coin header */}
          <div className="absolute inset-x-0 top-0 px-3 pt-2.5 pb-7
            bg-gradient-to-b from-black/70 via-black/20 to-transparent flex items-baseline gap-2">
            <span className="font-bold text-white">{coin}</span>
            <span className="mono text-sm text-white tabular-nums">
              {mid != null ? mid.toLocaleString() : "—"}
            </span>
            <span className={clsx("mono text-[11px] tabular-nums",
              change >= 0 ? "text-emerald-300" : "text-red-300")}>
              {change >= 0 ? "+" : ""}{(change * 100).toFixed(2)}%
            </span>
          </div>

          {/* centred verdict pill — fixed headline when armed/in-trade, else a
              rotating natural-language status of the decision core */}
          <div className="absolute inset-x-0 top-[34%] flex justify-center px-3">
            <span className={clsx("text-[12px] mono px-3.5 py-1 rounded-full whitespace-nowrap max-w-full overflow-hidden text-ellipsis",
              pillArmed && "armed")}
              style={{
                color: pillColor,
                background: "rgba(8,11,16,0.5)",
                backdropFilter: "blur(6px)",
                border: `1px solid ${pillArmed ? pillColor + "aa" : "#33415566"}`,
              }}>
              {verdictText ?? (
                <span key={msgIdx} className="status-in inline-block">
                  <span className="text-slate-500">› </span>{rotMsg}
                </span>
              )}
            </span>
          </div>

          {/* cons/conf moved OUT of the 3D overlay → solid strip below the
              depth ladder (see below), so depth now sits directly under the
              scene and the consensus/confidence read-outs sit beneath it. */}
        </div>

        {hovered && verdict && (
          <div className="absolute top-11 left-3 z-10 rounded-lg p-2 text-[9px] mono text-slate-300
            bg-black/85 border border-edge pointer-events-none">
            {/* SCALP BAND RETIRED 2026-06-26 — structural-gate detail rows
                (spot/perp/OI/imbalance/moves) removed with the gates. */}
            <div className="text-slate-400">regime {verdict.regime || "—"}</div>
            <div className="text-slate-400">votes L{verdict.long_votes} S{verdict.short_votes} F{verdict.flat_votes}</div>
          </div>
        )}

        {/* live order-book depth — pinned to the bottom of the 3D scene over a
            scrim so it reads as part of this section, right under the chart */}
        <div className="absolute inset-x-0 bottom-0 pt-10
          bg-gradient-to-t from-black/90 via-black/65 to-transparent">
          <DepthLadder depth={depth} embedded />
        </div>
      </div>

      {/* consensus core — confidence-led read-out + model-vote agreement live
          inside RelayCore's overlay now, so the old duplicate strip is gone. */}
      <RelayCore
        direction={dir}
        confidence={conf}
        agreement={verdict?.agreement ?? 0}
        activeModels={ACTIVE_MODELS}
        tickets={tickets}
        longGate={lg}
        shortGate={sg}
        gatesEnabled={gatesEnabled}
        position={position ?? null}
        wouldFire={!!verdict?.would_fire}
        confGate={gC}
      />
    </div>
  );
}

