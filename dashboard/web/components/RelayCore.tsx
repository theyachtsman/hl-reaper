"use client";
/**
 * RelayCore — the "Consensus → Gate Relay" for one coin's Analysis Core card.
 *
 * Replaces the old ConsensusWheel + structural-gate info panel with a single
 * live 3D circuit: model nodes orbit a central core, the core fills toward the
 * coin's bias colour as confidence builds, and two gates (SHORT left / LONG
 * right) light up as their real structural signals close — a beam flows from the
 * core into the ACTIVE gate, and a shockwave fires when the circuit closes
 * (would_fire / in position). Unlike the standalone demo this is fully reactive:
 * everything is driven by the live /api/tickets verdict + gate detail, and a
 * gate switched off in Controls renders DISABLED (hazard-striped, no beam).
 *
 * One WebGL context (a straight swap for ConsensusWheel — no extra context).
 */
import { useEffect, useRef } from "react";
import clsx from "clsx";
import * as THREE from "three";

type Gate = {
  allowed?: boolean;
  spot_leading?: boolean; spot_lagging?: boolean;
  oi_rising?: boolean; ob_bid_heavy?: boolean; ob_ask_heavy?: boolean;
  momentum_ok?: boolean;
};
type Ticket = { model: string; direction: string; confidence: number; meta?: any };

const GREEN = 0x1d9e75, RED = 0xe24b4a, GOLD = 0xffd166;
const DIM = 0x3a4250, FLAT = 0x556070, DEAD = 0x2a3344;
const dirHex = (d?: string) => d === "LONG" ? GREEN : d === "SHORT" ? RED : FLAT;

// the 5 active voting models (ML + LiqHeatmap are parked non-voters — not shown)
const SLOTS: { model: string; abbr: string; dead?: boolean }[] = [
  { model: "TAModel", abbr: "TA" },
  { model: "MeanReversionModel", abbr: "MR" },
  { model: "FundingRateModel", abbr: "FR" },
  { model: "OrderbookImbalanceModel", abbr: "OB" },
  { model: "VWAPModel", abbr: "VP" },
];

function glowTexture(): THREE.CanvasTexture {
  const s = 128;
  const c = document.createElement("canvas"); c.width = c.height = s;
  const ctx = c.getContext("2d")!;
  const g = ctx.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2);
  g.addColorStop(0, "rgba(255,255,255,1)");
  g.addColorStop(0.4, "rgba(255,255,255,0.35)");
  g.addColorStop(1, "rgba(255,255,255,0)");
  ctx.fillStyle = g; ctx.fillRect(0, 0, s, s);
  return new THREE.CanvasTexture(c);
}

function labelSprite(text: string, hex: number): THREE.Sprite {
  const cv = document.createElement("canvas");
  const ctx = cv.getContext("2d")!;
  const fs = 40;
  ctx.font = `700 ${fs}px 'JetBrains Mono', monospace`;
  cv.width = Math.ceil(ctx.measureText(text).width) + 12;
  cv.height = fs + 12;
  ctx.font = `700 ${fs}px 'JetBrains Mono', monospace`;
  ctx.fillStyle = "#" + hex.toString(16).padStart(6, "0");
  ctx.textBaseline = "middle";
  ctx.fillText(text, 6, cv.height / 2);
  const tex = new THREE.CanvasTexture(cv); tex.minFilter = THREE.LinearFilter;
  const sp = new THREE.Sprite(new THREE.SpriteMaterial(
    { map: tex, transparent: true, depthWrite: false }));
  sp.scale.set((cv.width / cv.height) * 0.34, 0.34, 1);
  return sp;
}

type GateRefs = {
  group: THREE.Group; ring: THREE.Mesh; ringMat: THREE.MeshBasicMaterial;
  disc: THREE.Mesh; discMat: THREE.MeshBasicMaterial;
  glow: THREE.Sprite; titleMat: THREE.SpriteMaterial;
};

export default function RelayCore({
  direction, confidence, agreement, activeModels = 5, tickets,
  longGate, shortGate, gatesEnabled, position, wouldFire, confGate = 0.4,
}: {
  direction: string; confidence: number; agreement: number; activeModels?: number;
  tickets: Ticket[]; longGate?: Gate; shortGate?: Gate;
  gatesEnabled?: { long: boolean; short: boolean };
  position?: "LONG" | "SHORT" | null; wouldFire?: boolean; confGate?: number;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const sceneRef = useRef<any>(null);
  const flashRef = useRef<HTMLDivElement>(null);

  // ---- live target state (read by the animation loop) ----
  const longSig = [longGate?.spot_leading, longGate?.oi_rising,
    longGate?.ob_bid_heavy, longGate?.momentum_ok];
  const shortSig = [shortGate?.spot_lagging, shortGate?.oi_rising,
    shortGate?.ob_ask_heavy, shortGate?.momentum_ok];
  const longOff = !!gatesEnabled && !gatesEnabled.long;
  const shortOff = !!gatesEnabled && !gatesEnabled.short;
  const armedDir = position ?? (wouldFire ? direction : null);

  // the core leans toward whichever side the ensemble is tilting WHILE voting:
  // the verdict's own direction if it has resolved one, else the majority of the
  // live model votes (so the core fills even before a hard verdict forms / when
  // a gate is switched off — gates only gate firing, not the consensus lean).
  const voteByModel = new Map(tickets.map((t) => [t.model, t.direction]));
  const slotVotes = SLOTS.map((s) => voteByModel.get(s.model));
  const longV = slotVotes.filter((d) => d === "LONG").length;
  const shortV = slotVotes.filter((d) => d === "SHORT").length;
  const coreDir = direction !== "FLAT" ? direction
    : longV > shortV ? "LONG" : shortV > longV ? "SHORT" : "FLAT";
  // how full it reads: blend confidence-toward-gate with the vote share, with a
  // floor, so a forming lean is visibly non-zero (raw confidence sits low).
  const leanV = Math.max(longV, shortV);
  const confProg = confidence / Math.max(0.05, confGate);
  const coreFill = coreDir === "FLAT" ? 0
    : Math.min(1, 0.15 + 0.85 * Math.max(confProg, leanV / Math.max(1, activeModels)));

  const targetVal = {
    direction, confidence, agreement, coreDir, coreFill,
    voteHex: SLOTS.map((s) => dirHex(voteByModel.get(s.model))),
    // fill from the real structural signals even when a gate is OFF (the bridge
    // always sends the detail) — a disabled gate still shows consensus building.
    longProg: longSig.filter(Boolean).length / 4,
    shortProg: shortSig.filter(Boolean).length / 4,
    longOff, shortOff, armedDir,
  };
  const target = useRef(targetVal);
  target.current = targetVal;

  // ---- shock trigger on entering armed ----
  const prevArmed = useRef<string | null>(null);
  useEffect(() => {
    if (armedDir && armedDir !== prevArmed.current) {
      const s = sceneRef.current;
      if (s) {
        const gate = armedDir === "LONG" ? s.gateLong : s.gateShort;
        s.shock.position.copy(gate.group.position);
        s.shock.scale.set(1, 1, 1);
        s.shock.material.color.setHex(dirHex(armedDir));
        s.shock.material.opacity = 0.9;
        s.shockActive = true;
      }
      if (flashRef.current) {
        flashRef.current.textContent = `● armed · ${armedDir}`;
        flashRef.current.style.color = armedDir === "LONG" ? "#2de8b0" : "#f0625f";
        flashRef.current.classList.add("relay-flash-show");
        setTimeout(() => flashRef.current?.classList.remove("relay-flash-show"), 1400);
      }
    }
    prevArmed.current = armedDir;
  }, [armedDir]);

  // ---- one-time scene build ----
  useEffect(() => {
    const canvas = canvasRef.current!;
    const wrap = wrapRef.current!;
    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.75));
    renderer.setClearColor(0x000000, 0);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(46, 1, 0.1, 100);
    camera.position.set(0, 1.05, 7.0);
    camera.lookAt(0, 0, 0);

    const GLOW = glowTexture();
    const mkGlow = (hex: number, sc: number) => {
      const sp = new THREE.Sprite(new THREE.SpriteMaterial({
        map: GLOW, color: hex, transparent: true,
        blending: THREE.AdditiveBlending, depthWrite: false }));
      sp.scale.set(sc, sc, 1); return sp;
    };

    // core
    const core = new THREE.Group(); scene.add(core);
    const coreWireMat = new THREE.MeshBasicMaterial({ color: DIM, wireframe: true, transparent: true, opacity: 0.55 });
    const coreWire = new THREE.Mesh(new THREE.IcosahedronGeometry(0.95, 1), coreWireMat); core.add(coreWire);
    const coreSolidMat = new THREE.MeshBasicMaterial({ color: DIM, transparent: true, opacity: 0.85 });
    const coreSolid = new THREE.Mesh(new THREE.IcosahedronGeometry(0.5, 0), coreSolidMat); core.add(coreSolid);
    const coreGlow = mkGlow(DIM, 4.4); core.add(coreGlow);

    // nodes
    const nodeRadius = 1.9;
    const nodes = SLOTS.map((s, i) => {
      const g = new THREE.Group();
      const mat = new THREE.MeshBasicMaterial({
        color: s.dead ? DEAD : DIM, transparent: true,
        opacity: s.dead ? 0.5 : 0.9, wireframe: !!s.dead });
      const mesh = new THREE.Mesh(new THREE.SphereGeometry(s.dead ? 0.07 : 0.085, 14, 14), mat);
      g.add(mesh);
      const label = labelSprite(s.abbr, s.dead ? DEAD : 0x5d6b7d);
      label.position.set(0, 0.26, 0); label.scale.multiplyScalar(0.62);
      g.add(label);
      scene.add(g);
      return { g, mesh, mat, label, angle: (i / SLOTS.length) * Math.PI * 2,
               baseY: Math.sin(i * 1.7) * 0.3, dead: !!s.dead };
    });

    // gates
    const mkGate = (x: number, titleHex: number, title: string): GateRefs => {
      const group = new THREE.Group(); group.position.set(x, 0, 0);
      const ringMat = new THREE.MeshBasicMaterial({ color: DIM, transparent: true, opacity: 0.55 });
      const ring = new THREE.Mesh(new THREE.TorusGeometry(0.8, 0.04, 14, 40), ringMat); group.add(ring);
      const discMat = new THREE.MeshBasicMaterial({ color: titleHex, transparent: true, opacity: 0 });
      const disc = new THREE.Mesh(new THREE.CircleGeometry(0.72, 40), discMat);
      disc.scale.set(0.01, 0.01, 1); group.add(disc);
      const glow = mkGlow(titleHex, 0.1); group.add(glow);
      const t = labelSprite(title, 0x9fb0c3); t.position.set(0, 1.18, 0); t.scale.multiplyScalar(0.7);
      group.add(t);
      scene.add(group);
      return { group, ring, ringMat, disc, discMat, glow, titleMat: t.material as THREE.SpriteMaterial };
    };
    const gateShort = mkGate(-2.7, RED, "SHORT");
    const gateLong = mkGate(2.7, GREEN, "LONG");

    // beam (core -> gate)
    const mkBeam = (tx: number, hex: number) => {
      const curve = new THREE.CatmullRomCurve3([
        new THREE.Vector3(0, 0, 0),
        new THREE.Vector3(tx * 0.5, 0.7, 0),
        new THREE.Vector3(tx * 0.9, 0, 0)]);
      const N = 34;
      const positions = new Float32Array(N * 3);
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      const mat = new THREE.PointsMaterial({ color: hex, size: 0.085, transparent: true,
        opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false, sizeAttenuation: true });
      const points = new THREE.Points(geo, mat); scene.add(points);
      const offsets = Array.from({ length: N }, () => Math.random());
      return { curve, points, mat, positions, offsets, N };
    };
    const beamShort = mkBeam(-2.7, RED);
    const beamLong = mkBeam(2.7, GREEN);

    // shock ring
    const shockMat = new THREE.MeshBasicMaterial({ color: GREEN, transparent: true, opacity: 0, side: THREE.DoubleSide });
    const shock = new THREE.Mesh(new THREE.RingGeometry(0.5, 0.62, 40), shockMat);
    scene.add(shock);

    sceneRef.current = {
      renderer, scene, camera, core, coreWireMat, coreSolidMat, coreGlow,
      nodes, gateShort, gateLong, beamShort, beamLong, shock, shockActive: false,
      curC: new THREE.Color(DIM), curShort: new THREE.Color(DIM),
      curLong: new THREE.Color(DIM), raf: 0,
    };

    const resize = () => {
      const w = wrap.clientWidth || 1, h = wrap.clientHeight || 1;
      renderer.setSize(w, h, false);
      camera.aspect = w / h; camera.updateProjectionMatrix();
    };
    resize();
    const ro = new ResizeObserver(resize); ro.observe(wrap);

    const updBeam = (beam: any, t: number, intensity: number, speed: number) => {
      beam.mat.opacity = intensity;
      for (let i = 0; i < beam.N; i++) {
        const u = (beam.offsets[i] + t * speed) % 1;
        const p = beam.curve.getPointAt(u);
        beam.positions[i * 3] = p.x; beam.positions[i * 3 + 1] = p.y; beam.positions[i * 3 + 2] = p.z;
      }
      beam.points.geometry.attributes.position.needsUpdate = true;
    };

    const clock = new THREE.Clock();
    const animate = () => {
      const r = sceneRef.current; if (!r) return;
      const t = clock.getElapsedTime(), d = clock.getDelta();
      const st = target.current;
      const dc = dirHex(st.coreDir);

      // nodes orbit + colour
      const rot = t * 0.12;
      r.nodes.forEach((n: any, i: number) => {
        const a = n.angle + rot;
        n.g.position.set(Math.cos(a) * nodeRadius, n.baseY + Math.sin(t * 0.6 + i) * 0.07, Math.sin(a) * nodeRadius * 0.42);
        if (!n.dead) n.mat.color.lerp(new THREE.Color(st.voteHex[i]), 0.08);
      });

      // core colour: armed -> gold pulse, else fill toward bias by confidence
      const armed = st.armedDir != null;
      let coreTarget: THREE.Color;
      if (armed) {
        const pulse = 0.5 + 0.5 * Math.sin(t * 4);
        coreTarget = new THREE.Color(dirHex(st.armedDir)).lerp(new THREE.Color(GOLD), pulse * 0.7);
      } else {
        coreTarget = new THREE.Color(DIM).lerp(new THREE.Color(dc), st.coreFill);
      }
      r.curC.lerp(coreTarget, 0.1);
      r.coreWireMat.color.copy(r.curC); r.coreSolidMat.color.copy(r.curC);
      (r.coreGlow.material as THREE.SpriteMaterial).color.copy(r.curC);
      r.core.scale.setScalar(1 + (armed ? 0 : st.coreFill * 0.14)
        + Math.sin(t * (armed ? 4 : 2)) * 0.04);
      r.core.rotation.y += d * 0.15; r.core.rotation.x += d * 0.03;

      // gates
      const applyGate = (gate: GateRefs, prog: number, off: boolean, baseHex: number,
                         active: boolean, cur: THREE.Color) => {
        const open = prog >= 1;
        // a disabled gate still FILLS with its structural progress (so you can
        // watch consensus build exactly like an enabled gate), just dimmer and
        // without the firing spin/extra glow since it can't actually fire.
        const dim = off ? 0.5 : 1;
        cur.lerp(new THREE.Color(DIM).lerp(new THREE.Color(baseHex), prog), 0.1);
        gate.ringMat.color.copy(cur);
        gate.ringMat.opacity = (0.5 + prog * 0.45) * dim;
        gate.discMat.color.setHex(baseHex);
        gate.discMat.opacity += (prog * 0.7 * dim - gate.discMat.opacity) * 0.12;
        gate.disc.scale.setScalar(0.2 + prog * 0.78);
        (gate.glow.material as THREE.SpriteMaterial).color.setHex(baseHex);
        gate.glow.scale.setScalar(0.1 + prog * (active && !off ? 2.2 : 1.0));
        gate.titleMat.opacity = (0.5 + prog * 0.5) * (off ? 0.7 : 1);
        if (open && active && !off) gate.group.rotation.z += d * 0.4;
        else if (off) gate.group.rotation.z = 0;
      };
      const dir = st.coreDir;
      applyGate(r.gateShort, st.shortProg, st.shortOff, RED, dir === "SHORT", r.curShort);
      applyGate(r.gateLong, st.longProg, st.longOff, GREEN, dir === "LONG", r.curLong);

      // arc flows from the core to whichever side consensus leans, as it builds —
      // visible regardless of the gate toggle (dimmer when that gate is off, like
      // the ring) so you can watch the circuit form even with gates disabled.
      const beamFor = (side: "LONG" | "SHORT", prog: number, off: boolean) =>
        dir === side
          ? Math.min(1, 0.32 + 0.68 * Math.max(prog, st.coreFill)) * (off ? 0.6 : 1)
          : 0;
      updBeam(r.beamLong, t, beamFor("LONG", st.longProg, st.longOff), 0.5 + st.longProg * 0.9);
      updBeam(r.beamShort, t, beamFor("SHORT", st.shortProg, st.shortOff), 0.5 + st.shortProg * 0.9);

      // shock
      if (r.shockActive) {
        r.shock.scale.x += d * 3.0; r.shock.scale.y += d * 3.0;
        r.shock.material.opacity -= d * 1.0;
        if (r.shock.material.opacity <= 0) { r.shockActive = false; r.shock.material.opacity = 0; }
      }

      r.renderer.render(r.scene, r.camera);
      r.raf = requestAnimationFrame(animate);
    };
    sceneRef.current.raf = requestAnimationFrame(animate);

    return () => {
      ro.disconnect();
      cancelAnimationFrame(sceneRef.current.raf);
      scene.traverse((o: any) => {
        if (o.geometry) o.geometry.dispose();
        if (o.material) {
          if (o.material.map) o.material.map.dispose();
          o.material.dispose();
        }
      });
      GLOW.dispose();
      renderer.dispose();
      sceneRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- DOM gate chip (signals + badge), reactive incl. disabled ----
  const Chip = ({ side, gate, sig, labels, off }: {
    side: "short" | "long"; gate?: Gate; sig: (boolean | undefined)[];
    labels: string[]; off: boolean;
  }) => {
    const accent = side === "long" ? "text-[#2de8b0]" : "text-[#f0625f]";
    const dotOn = side === "long" ? "#2de8b0" : "#f0625f";
    return (
      <div className={clsx("relative rounded-md border bg-black/55 px-2 py-1.5 w-[112px]",
        off ? "border-white/10" : "border-edge")}>
        <div className="flex items-center justify-between mb-1">
          <span className={clsx("text-[8px] uppercase tracking-wider", accent)}>
            {side === "long" ? "▲ long" : "▼ short"}
          </span>
          <span className={clsx("text-[7px] px-1 py-px rounded-full border tracking-wide uppercase",
            off ? "border-white/15 text-slate-500"
              : gate?.allowed ? "border-current text-current " + accent
                : "border-white/15 text-slate-400")}>
            {off ? "off" : gate?.allowed ? "open" : "blocked"}
          </span>
        </div>
        <div className="grid grid-cols-2 gap-x-1.5 gap-y-0.5">
          {labels.map((l, i) => (
            <div key={l} className="flex items-center gap-1 text-[7.5px] text-slate-500">
              <span className="inline-block w-[5px] h-[5px] rounded-full shrink-0"
                style={sig[i] && !off ? { background: dotOn, boxShadow: `0 0 4px ${dotOn}` }
                  : { background: "#ffffff14", border: "1px solid #ffffff20" }} />
              <span className="truncate">{l}</span>
            </div>
          ))}
        </div>
        {off && (
          <div className="gate-off absolute inset-0 rounded-md flex items-center justify-center">
            <span className="text-[8px] mono uppercase tracking-[0.18em] text-slate-300/90">⊘ gate off</span>
          </div>
        )}
      </div>
    );
  };

  return (
    <div ref={wrapRef} className="relative h-[300px] bg-[#070a0e] border-t border-edge overflow-hidden">
      <canvas ref={canvasRef} className="block w-full h-full" />
      {/* tally + prominent confidence — the "so what" of the consensus core */}
      <div className="absolute top-2 left-0 right-0 text-center pointer-events-none">
        <div className="text-[10px] mono uppercase tracking-[0.14em]"
          style={{ color: direction === "LONG" ? "#2de8b0" : direction === "SHORT" ? "#f0625f" : "#9fb0c3" }}>
          consensus {agreement}/{activeModels} {direction === "FLAT" ? "scanning" : direction}
        </div>
        <div className="mt-0.5 flex items-baseline justify-center gap-1.5 leading-none">
          <span className="text-[22px] font-bold mono tabular-nums"
            style={{ color: direction === "LONG" ? "#2de8b0" : direction === "SHORT" ? "#f0625f" : "#cbd5e1",
                     textShadow: direction === "FLAT" ? "none"
                       : `0 0 12px ${direction === "LONG" ? "#2de8b066" : "#f0625f66"}` }}>
            {confidence.toFixed(2)}
          </span>
          <span className="text-[8px] mono uppercase tracking-wider text-slate-500">
            conf · gate {confGate.toFixed(2)}
          </span>
        </div>
      </div>
      {/* armed flash */}
      <div ref={flashRef} className="relay-flash absolute left-0 right-0 top-[38%] text-center
        text-[15px] font-bold mono uppercase tracking-wide pointer-events-none" />
      {/* gate chips */}
      <div className="absolute bottom-2.5 left-2.5 pointer-events-none">
        <Chip side="short" gate={shortGate} off={shortOff}
          sig={shortSig} labels={["spot lag", "OI↑fall", "ask-heavy", "no dump"]} />
      </div>
      <div className="absolute bottom-2.5 right-2.5 pointer-events-none">
        <Chip side="long" gate={longGate} off={longOff}
          sig={longSig} labels={["spot lead", "OI↑", "bid-heavy", "no pump"]} />
      </div>
    </div>
  );
}
