// Inline SVG diagrams, styled with the site palette. Animations are CSS-only
// (the .beam keyframe from styles/globals.css). Server components — no hooks.
import React from "react";
import { ACCENT, ACCENT_BRIGHT, CYAN, RED } from "./ui";

// ---------------------------------------------------------------------------
// Signal pipeline: raw data -> models -> aggregator -> confidence gate ->
// risk engine -> order. Animated flowing beams between stages.
// ---------------------------------------------------------------------------
export function FlowDiagram() {
  const stages = [
    { label: "Market Data", sub: "candles · book · funding · OI · spot", color: CYAN },
    { label: "5 Models", sub: "TA · MeanRev · Funding · OB · VWAP", color: ACCENT_BRIGHT },
    { label: "Aggregator", sub: "weighted vote → confidence", color: ACCENT_BRIGHT },
    { label: "Confidence Gate", sub: "≥ min conf & agreement", color: CYAN },
    { label: "Risk Engine", sub: "guards · stops · state", color: "#fbbf24" },
    { label: "Order", sub: "maker → taker fallback", color: ACCENT_BRIGHT },
  ];
  return (
    <div className="card overflow-x-auto">
      <div className="flex items-stretch gap-1 min-w-[640px]">
        {stages.map((s, i) => (
          <React.Fragment key={s.label}>
            <div
              className="flex-1 rounded-lg p-3 text-center flex flex-col justify-center"
              style={{
                background: "rgba(10,14,20,0.9)",
                border: `1px solid ${s.color}55`,
                boxShadow: `inset 0 0 30px -22px ${s.color}`,
              }}
            >
              <div className="font-semibold text-sm" style={{ color: s.color }}>
                {s.label}
              </div>
              <div className="text-[10px] text-slate-500 mt-1 leading-tight">{s.sub}</div>
            </div>
            {i < stages.length - 1 && (
              <svg width="26" height="56" viewBox="0 0 26 56" className="shrink-0 self-center">
                <line x1="0" y1="28" x2="26" y2="28" stroke="#1e2a3a" strokeWidth="2" />
                <line
                  x1="0"
                  y1="28"
                  x2="26"
                  y2="28"
                  stroke={ACCENT}
                  strokeWidth="2"
                  strokeDasharray="4 4"
                  className="beam"
                />
                <path d="M18,23 L26,28 L18,33" fill="none" stroke={ACCENT} strokeWidth="2" />
              </svg>
            )}
          </React.Fragment>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Risk state machine. Real state names + transition triggers.
// ---------------------------------------------------------------------------
function Node({
  x,
  y,
  w,
  label,
  color,
}: {
  x: number;
  y: number;
  w: number;
  label: string;
  color: string;
}) {
  return (
    <g>
      <rect
        x={x}
        y={y}
        width={w}
        height={36}
        rx={8}
        fill="rgba(10,14,20,0.95)"
        stroke={color}
        strokeWidth={1.5}
      />
      <text
        x={x + w / 2}
        y={y + 23}
        textAnchor="middle"
        fontSize="12"
        fontFamily="ui-monospace, monospace"
        fill={color}
        fontWeight="600"
      >
        {label}
      </text>
    </g>
  );
}

function Edge({
  d,
  label,
  lx,
  ly,
  color = "#64748b",
}: {
  d: string;
  label: string;
  lx: number;
  ly: number;
  color?: string;
}) {
  return (
    <g>
      <path d={d} fill="none" stroke={color} strokeWidth={1.5} markerEnd="url(#arrow)" />
      <text x={lx} y={ly} textAnchor="middle" fontSize="9.5" fill="#94a3b8">
        {label}
      </text>
    </g>
  );
}

export function StateMachineDiagram() {
  return (
    <div className="card overflow-x-auto">
      <svg width="100%" viewBox="0 0 720 380" className="min-w-[680px]">
        <defs>
          <marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
            <path d="M0,0 L6,3 L0,6 Z" fill="#64748b" />
          </marker>
        </defs>

        {/* center: ACTIVE */}
        <Node x={290} y={172} w={140} label="ACTIVE" color={ACCENT_BRIGHT} />

        {/* MANAGING (top) */}
        <Node x={300} y={40} w={120} label="MANAGING" color="#fbbf24" />
        <Edge d="M345,172 C330,130 330,100 345,76" label="daily DD ≥ 5%" lx={250} ly={120} color="#fbbf24" />
        <Edge d="M385,76 C400,110 400,140 385,172" label="new day / resume" lx={470} ly={120} color={ACCENT} />

        {/* RECONNECTING (right) */}
        <Node x={560} y={172} w={140} label="RECONNECTING" color={CYAN} />
        <Edge d="M430,182 L560,182" label="feed stale > 30s" lx={495} ly={172} color={CYAN} />
        <Edge d="M560,200 L430,200" label="feed recovered" lx={495} ly={216} color={ACCENT} />

        {/* COOLDOWN (bottom-left) */}
        <Node x={70} y={300} w={130} label="COOLDOWN" color="#fbbf24" />
        <Edge d="M300,200 C200,250 160,270 150,298" label="weekly DD ≥ 10% (48h)" lx={150} ly={250} color="#fbbf24" />
        <Edge d="M170,300 C230,250 260,225 300,205" label="expires / resume" lx={300} ly={285} color={ACCENT} />

        {/* HALTED (bottom-right) */}
        <Node x={520} y={300} w={130} label="HALTED" color={RED} />
        <Edge d="M420,205 C500,250 540,270 565,298" label="cascade · severe DD · manual" lx={560} ly={262} color={RED} />
        <Edge d="M540,300 C470,255 440,230 410,206" label="window expires / resume" lx={420} ly={330} color={ACCENT} />

        {/* CASCADE_BOUNCE_ACTIVE (top-right) */}
        <Node x={500} y={40} w={190} label="CASCADE_BOUNCE_ACTIVE" color="#a78bfa" />
        <Edge d="M430,176 C500,120 540,90 560,78" label="bounce opens" lx={560} ly={130} color="#a78bfa" />
        <Edge d="M590,76 C560,120 510,150 432,172" label="bounce closed" lx={620} ly={150} color={ACCENT} />
      </svg>
      <div className="text-[11px] text-slate-500 mt-2 px-1">
        Green edges = recovery toward ACTIVE. Amber = soft lockouts (positions still managed). Red =
        hard halt (everything closed).
      </div>
    </div>
  );
}
