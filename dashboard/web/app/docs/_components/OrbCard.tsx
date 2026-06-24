// A static, on-theme replica of the live Analysis Core card (CoinCard3D +
// RelayCore + DepthLadder) used purely as a documentation visual. Every region
// carries a numbered badge that the /docs/dashboard legend explains. No data,
// no three.js — pure SVG/CSS so it renders identically in the dark theme.
import React from "react";

const GREEN = "#2de8b0";
const ACCENT = "#1D9E75";
const RED = "#f0625f";

function Badge({ n }: { n: number }) {
  return (
    <span
      className="absolute z-10 flex items-center justify-center w-5 h-5 rounded-full text-[11px] font-bold mono"
      style={{
        background: "rgba(7,10,14,0.9)",
        color: GREEN,
        border: `1px solid ${ACCENT}`,
        boxShadow: `0 0 8px ${ACCENT}88`,
      }}
    >
      {n}
    </span>
  );
}

// stylised wireframe orb with the five model nodes orbiting it
function Orb() {
  const cx = 150;
  const cy = 95;
  const r = 52;
  // node positions roughly matching the live card: FR leans SHORT (left),
  // TA leans LONG (right), the rest scattered around the core.
  const nodes = [
    { label: "FR", x: 92, y: 96, color: RED },
    { label: "OB", x: 132, y: 58, color: "#7c8696" },
    { label: "VP", x: 176, y: 66, color: "#7c8696" },
    { label: "TA", x: 206, y: 92, color: GREEN },
    { label: "MR", x: 150, y: 140, color: "#7c8696" },
  ];
  return (
    <svg viewBox="0 0 300 190" className="w-full">
      <defs>
        <radialGradient id="orbglow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor={ACCENT} stopOpacity="0.45" />
          <stop offset="60%" stopColor={ACCENT} stopOpacity="0.12" />
          <stop offset="100%" stopColor={ACCENT} stopOpacity="0" />
        </radialGradient>
      </defs>

      {/* poles */}
      <g>
        <circle cx="34" cy={cy} r="22" fill="none" stroke={RED} strokeOpacity="0.5" strokeWidth="1.5" />
        <text x="34" y={cy - 30} textAnchor="middle" fontSize="9" fill="#9fb0c3" fontFamily="monospace">
          SHORT
        </text>
        <circle cx="266" cy={cy} r="22" fill="none" stroke={GREEN} strokeOpacity="0.6" strokeWidth="1.5" />
        <text x="266" y={cy - 30} textAnchor="middle" fontSize="9" fill="#9fb0c3" fontFamily="monospace">
          LONG
        </text>
      </g>

      {/* core glow + wireframe sphere */}
      <circle cx={cx} cy={cy} r={r + 18} fill="url(#orbglow)" />
      {/* longitude/latitude ellipses */}
      {[0, 1, 2].map((i) => (
        <ellipse
          key={`lon${i}`}
          cx={cx}
          cy={cy}
          rx={r - i * 16}
          ry={r}
          fill="none"
          stroke={ACCENT}
          strokeOpacity={0.5 - i * 0.1}
          strokeWidth="1"
        />
      ))}
      {[0, 1].map((i) => (
        <ellipse
          key={`lat${i}`}
          cx={cx}
          cy={cy}
          rx={r}
          ry={r - 18 - i * 16}
          fill="none"
          stroke={ACCENT}
          strokeOpacity={0.4 - i * 0.1}
          strokeWidth="1"
        />
      ))}
      <circle cx={cx} cy={cy} r={r} fill="none" stroke={ACCENT} strokeOpacity="0.6" strokeWidth="1.2" />
      {/* a few geodesic chords for the "icosahedron" feel */}
      <polygon
        points={`${cx - 30},${cy - 26} ${cx + 28},${cy - 18} ${cx + 18},${cy + 30} ${cx - 32},${cy + 20}`}
        fill="none"
        stroke={ACCENT}
        strokeOpacity="0.3"
        strokeWidth="1"
      />
      {/* lit inner core */}
      <circle cx={cx} cy={cy} r="13" fill={ACCENT} fillOpacity="0.5" />
      <circle cx={cx} cy={cy} r="13" fill="none" stroke={GREEN} strokeOpacity="0.7" strokeWidth="1" />

      {/* model nodes */}
      {nodes.map((nd) => (
        <g key={nd.label}>
          <circle cx={nd.x} cy={nd.y} r="3.4" fill={nd.color} style={{ filter: `drop-shadow(0 0 4px ${nd.color})` }} />
          <text x={nd.x} y={nd.y - 7} textAnchor="middle" fontSize="8" fill="#9fb0c3" fontFamily="monospace" fontWeight="700">
            {nd.label}
          </text>
        </g>
      ))}
    </svg>
  );
}

export default function OrbCard() {
  return (
    <div
      className="relative rounded-xl overflow-hidden mx-auto"
      style={{
        maxWidth: 340,
        background: "#070a0e",
        border: `1px solid ${ACCENT}cc`,
        boxShadow: `0 0 24px -6px ${ACCENT}99, inset 0 0 50px -30px ${ACCENT}`,
      }}
    >
      {/* 1 · header */}
      <div className="relative px-3 pt-2.5 pb-2 flex items-baseline gap-2">
        <Badge n={1} />
        <span className="font-bold text-white text-sm">BTC</span>
        <span className="mono text-sm text-white tabular-nums">62,947.5</span>
        <span className="mono text-[11px] tabular-nums text-emerald-300">+1.53%</span>
      </div>

      {/* 2 · verdict pill */}
      <div className="relative flex justify-center px-3 pb-2">
        <Badge n={2} />
        <span
          className="text-[12px] mono px-3.5 py-1 rounded-full whitespace-nowrap"
          style={{ color: "#cbd5e1", background: "rgba(8,11,16,0.6)", border: "1px solid #33415566" }}
        >
          <span className="text-slate-500">› </span>confidence 0.69 ✓ clears 0.49
        </span>
      </div>

      {/* 3 · price ribbon */}
      <div className="relative h-14 px-1">
        <Badge n={3} />
        <svg viewBox="0 0 320 56" preserveAspectRatio="none" className="w-full h-full">
          <defs>
            <linearGradient id="ribbon" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={ACCENT} stopOpacity="0.35" />
              <stop offset="100%" stopColor={ACCENT} stopOpacity="0" />
            </linearGradient>
          </defs>
          <path
            d="M0,40 L30,36 L60,38 L90,28 L120,30 L150,18 L180,24 L210,12 L240,20 L270,10 L300,16 L320,8 V56 H0 Z"
            fill="url(#ribbon)"
          />
          <path
            d="M0,40 L30,36 L60,38 L90,28 L120,30 L150,18 L180,24 L210,12 L240,20 L270,10 L300,16 L320,8"
            fill="none"
            stroke={GREEN}
            strokeWidth="1.5"
          />
        </svg>
      </div>

      {/* 4 · order book depth */}
      <div className="relative px-3 py-2" style={{ background: "rgba(0,0,0,0.4)" }}>
        <Badge n={4} />
        <div className="flex items-center justify-between text-[9px] mono text-slate-400 mb-1">
          <span className="uppercase tracking-wider">order book depth</span>
          <span>
            spread <span className="text-slate-200">16.7 bp</span>
          </span>
        </div>
        <div className="flex h-2 rounded-full overflow-hidden">
          <div style={{ width: "31%", background: `${ACCENT}` }} />
          <div style={{ width: "3%", background: "transparent" }} />
          <div style={{ width: "66%", background: RED }} />
        </div>
        <div className="flex items-center justify-between text-[8.5px] mono mt-1">
          <span style={{ color: ACCENT }}>bid 20% · $4.0k</span>
          <span style={{ color: RED }}>ASK-HEAVY 44%</span>
          <span style={{ color: RED }}>$30.4k · ask 7×</span>
        </div>
      </div>

      {/* 5 · consensus dots + 6 · confidence bar */}
      <div className="px-3 py-2 border-t border-edge/60" style={{ background: "rgba(0,0,0,0.3)" }}>
        <div className="relative flex items-center gap-2 text-[10px] mono mb-1.5">
          <Badge n={5} />
          <span className="w-9 text-slate-300 pl-6">cons</span>
          <span className="flex items-center gap-1">
            {[GREEN, "#3a4250", "#3a4250", "#3a4250", "#3a4250"].map((c, i) => (
              <span
                key={i}
                className="inline-block w-[7px] h-[7px] rounded-full"
                style={
                  c === "#3a4250"
                    ? { background: "#ffffff14", border: "1px solid #ffffff22" }
                    : { background: c, boxShadow: `0 0 5px ${c}` }
                }
              />
            ))}
          </span>
          <span className="ml-auto tabular-nums" style={{ color: GREEN }}>
            1L · 0S · 4F <span className="text-slate-400">· 1/5</span>
          </span>
        </div>
        <div className="relative flex items-center gap-2 text-[10px] mono">
          <Badge n={6} />
          <span className="w-9 text-slate-300 pl-6">conf</span>
          <div className="relative h-[3px] flex-1 rounded-full bg-white/15">
            <div className="absolute inset-y-0 left-0 rounded-full" style={{ width: "69%", background: ACCENT }} />
          </div>
          <span className="text-slate-100 tabular-nums">0.69</span>
        </div>
      </div>

      {/* 7 · consensus headline + 8 · orb / 9 · gates */}
      <div className="relative border-t border-edge pt-2" style={{ background: "#070a0e" }}>
        <div className="relative text-center">
          <Badge n={7} />
          <div className="text-[10px] mono uppercase tracking-[0.14em]" style={{ color: GREEN }}>
            consensus 1/5 LONG
          </div>
          <div className="mt-0.5 flex items-baseline justify-center gap-1.5 leading-none">
            <span className="text-[22px] font-bold mono tabular-nums" style={{ color: GREEN, textShadow: `0 0 12px ${GREEN}66` }}>
              0.69
            </span>
            <span className="text-[8px] mono uppercase tracking-wider text-slate-500">conf · gate 0.49</span>
          </div>
        </div>
        <div className="relative">
          <span className="absolute left-2 top-2">
            <Badge n={8} />
          </span>
          <span className="absolute right-2 top-2">
            <Badge n={9} />
          </span>
          <Orb />
        </div>
      </div>
    </div>
  );
}
