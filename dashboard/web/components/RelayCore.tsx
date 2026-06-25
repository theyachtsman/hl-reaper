"use client";
/**
 * RelayCore — the consensus core for one coin's Analysis Core card.
 *
 * A single live 3D scene: the model nodes orbit a central core, and the core
 * fills toward the coin's bias colour as confidence builds (gold pulse when
 * armed / in position). The overlay leads with the live weighted confidence and
 * its gate, with the model-vote agreement shown beneath as a compact supplement.
 * Everything is driven by the live /api/tickets verdict. The structural gate
 * detail still lives in the two corner chips (which read DISABLED when a gate is
 * switched off in Controls).
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

// --- fidget-spin tuning ----------------------------------------------------
// The core orb spins like a fidget spinner: a trackball drag rotates it about
// whatever axis your swipe implies (grab from anywhere), and on release it
// keeps that angular velocity and coasts down with friction. The model nodes
// keep their own independent orbit and are NOT affected.
const AUTO_SPEED = 0.12;   // idle drift (rad/sec) once it's spun all the way down
const DRAG_RAD_PER_PX = 0.011; // screen px → rotation radians while dragging
const THROW_BOOST = 2.2;   // multiplier on release velocity → faster fling spin
const FRICTION = 0.985;    // momentum retained per (1/60)s — high = long fidget coast
const MAX_OMEGA = 70;      // cap on throw speed (rad/sec) so a hard flick stays sane
const IDLE_FLOOR = 0.18;   // |omega| below this → momentum done, fall back to drift
const TAP_PX = 6;          // total movement under this counts as a tap, not a drag

// the 6 active voting models (ML + LiqHeatmap are parked non-voters — not shown)
const SLOTS: { model: string; abbr: string; dead?: boolean }[] = [
  { model: "TAModel", abbr: "TA" },
  { model: "MeanReversionModel", abbr: "MR" },
  { model: "FundingRateModel", abbr: "FR" },
  { model: "OrderbookImbalanceModel", abbr: "OB" },
  { model: "VWAPModel", abbr: "VP" },
  { model: "MomentumModel", abbr: "MO" },
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

export default function RelayCore({
  direction, confidence, agreement, activeModels = 6, tickets,
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

  // overlay colour + gate-clear state for the confidence-led read-out
  const headColor = direction === "LONG" ? "#2de8b0"
    : direction === "SHORT" ? "#f0625f" : "#cbd5e1";
  const confCleared = confidence >= confGate;

  const targetVal = {
    direction, confidence, agreement, coreDir, coreFill,
    voteHex: SLOTS.map((s) => dirHex(voteByModel.get(s.model))),
    armedDir,
  };
  const target = useRef(targetVal);
  target.current = targetVal;

  // ---- armed flash on entering armed ----
  const prevArmed = useRef<string | null>(null);
  useEffect(() => {
    if (armedDir && armedDir !== prevArmed.current) {
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
    // pan the view up (camera + target raised by the same amount = pure vertical
    // pan, no tilt) so the sphere sits lower in the box, clear of the top
    // confidence overlay rather than centred behind it.
    camera.position.set(0, 1.75, 7.0);
    camera.lookAt(0, 0.7, 0);

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
    // invisible hit-sphere, sized to the visible orb (not the wide glow halo),
    // so only a pointer landing ON the orb starts a spin — the rest of the
    // canvas (gates, nodes, empty space) is no longer draggable. It's a child of
    // `core`, so it tracks the orb's scale (confidence pulse) automatically.
    const coreHit = new THREE.Mesh(
      new THREE.SphereGeometry(1.0, 16, 16),
      new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false }));
    core.add(coreHit);

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

    sceneRef.current = {
      renderer, scene, camera, core, coreWireMat, coreSolidMat, coreGlow,
      nodes,
      curC: new THREE.Color(DIM), raf: 0,
      // fidget-spin state, driven by the pointer handlers, read by animate()
      spin: {
        dragging: false,
        lastX: 0, lastY: 0,      // previous pointer position
        moved: 0,                // total px travelled this gesture (tap vs drag)
        samples: [] as { t: number; x: number; y: number }[], // recent positions for fling velocity
        omega: new THREE.Vector3(), // angular velocity: axis * rad/sec (world space)
      },
    };

    const resize = () => {
      const w = wrap.clientWidth || 1, h = wrap.clientHeight || 1;
      renderer.setSize(w, h, false);
      camera.aspect = w / h; camera.updateProjectionMatrix();
    };
    resize();
    const ro = new ResizeObserver(resize); ro.observe(wrap);

    // ---- drag / swipe to spin the orb (pointer = mouse + touch) ----
    // Trackball model: a swipe rotates the orb about the axis perpendicular to
    // the drag, so you can grab any point on the sphere and spin it any
    // direction. Axis is (-dy, -dx, 0): screen-Y is down, so this makes the
    // surface follow the finger (swipe right → spins right, swipe up → up).
    const dragAxis = new THREE.Vector3();
    const dragQuat = new THREE.Quaternion();
    // raycast a pointer event into the scene and return true iff it lands on the
    // orb hit-sphere — gates the spin so the orb alone is interactive.
    const ndc = new THREE.Vector2();
    const raycaster = new THREE.Raycaster();
    const overOrb = (e: PointerEvent) => {
      const rect = canvas.getBoundingClientRect();
      if (!rect.width || !rect.height) return false;
      ndc.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      ndc.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(ndc, camera);
      return raycaster.intersectObject(coreHit, false).length > 0;
    };
    const onPointerDown = (e: PointerEvent) => {
      const sp = sceneRef.current?.spin; if (!sp) return;
      if (!overOrb(e)) return;       // pointer missed the orb — ignore entirely
      sp.dragging = true;
      sp.lastX = e.clientX; sp.lastY = e.clientY;
      sp.moved = 0;
      sp.samples = [{ t: performance.now(), x: e.clientX, y: e.clientY }];
      sp.omega.set(0, 0, 0);               // grabbing the orb freezes any coast
      try { canvas.setPointerCapture(e.pointerId); } catch {}
      canvas.style.cursor = "grabbing";
    };
    const onPointerMove = (e: PointerEvent) => {
      const sp = sceneRef.current?.spin; if (!sp) return;
      if (!sp.dragging) {
        // hover feedback: grab cursor only while actually over the orb
        canvas.style.cursor = overOrb(e) ? "grab" : "default";
        return;
      }
      const dx = e.clientX - sp.lastX, dy = e.clientY - sp.lastY;
      sp.lastX = e.clientX; sp.lastY = e.clientY;
      sp.moved += Math.abs(dx) + Math.abs(dy);
      const dist = Math.hypot(dx, dy);
      if (dist >= 1e-4) {
        dragAxis.set(-dy, -dx, 0).normalize();
        dragQuat.setFromAxisAngle(dragAxis, dist * DRAG_RAD_PER_PX);
        sceneRef.current.core.quaternion.premultiply(dragQuat); // rotate live, world space
      }
      // keep a short history of recent positions for the release fling velocity
      const now = performance.now();
      sp.samples.push({ t: now, x: e.clientX, y: e.clientY });
      while (sp.samples.length > 2 && now - sp.samples[0].t > 120) sp.samples.shift();
    };
    const onPointerUp = (e: PointerEvent) => {
      const sp = sceneRef.current?.spin; if (!sp || !sp.dragging) return;
      sp.dragging = false;
      canvas.style.cursor = "grab";
      try { canvas.releasePointerCapture(e.pointerId); } catch {}
      if (sp.moved < TAP_PX) { sp.omega.set(0, 0, 0); return; } // tap = stop, no fling
      // fling velocity = average over the last ~110ms (immune to the finger
      // naturally slowing in the final frame before lift).
      const now = performance.now(), s = sp.samples;
      let i = 0; while (i < s.length - 1 && now - s[i].t > 110) i++;
      const a = s[i], b = s[s.length - 1];
      const dt = (b.t - a.t) / 1000;
      if (dt > 0) {
        const vx = (b.x - a.x) / dt, vy = (b.y - a.y) / dt; // px/sec
        const speedPx = Math.hypot(vx, vy);
        if (speedPx > 5) {
          dragAxis.set(-vy, -vx, 0).normalize();
          sp.omega.copy(dragAxis).multiplyScalar(Math.min(MAX_OMEGA, speedPx * DRAG_RAD_PER_PX * THROW_BOOST));
        } else sp.omega.set(0, 0, 0);
      } else sp.omega.set(0, 0, 0);
    };
    canvas.style.cursor = "default";
    canvas.addEventListener("pointerdown", onPointerDown);
    canvas.addEventListener("pointermove", onPointerMove);
    canvas.addEventListener("pointerup", onPointerUp);
    canvas.addEventListener("pointercancel", onPointerUp);

    const spinQuat = new THREE.Quaternion();
    const spinAxis = new THREE.Vector3();
    const Y_AXIS = new THREE.Vector3(0, 1, 0);
    const clock = new THREE.Clock();
    const animate = () => {
      const r = sceneRef.current; if (!r) return;
      // NOTE: getElapsedTime() internally calls getDelta(), so calling getDelta()
      // after it returns ~0. Take the delta first, then read the accumulated time.
      const d = clock.getDelta(), t = clock.elapsedTime;
      const st = target.current;
      const dc = dirHex(st.coreDir);

      // fidget spin: while dragging, onPointerMove rotates the orb live. On
      // release it keeps the swipe's angular velocity and coasts down with
      // friction; once spun all the way down it settles into a slow idle drift.
      // (Only the core orb spins — the model nodes keep their own orbit below.)
      const sp = r.spin;
      if (!sp.dragging) {
        const speed = sp.omega.length();
        if (speed > IDLE_FLOOR) {
          spinAxis.copy(sp.omega).normalize();
          spinQuat.setFromAxisAngle(spinAxis, speed * d);
          r.core.quaternion.premultiply(spinQuat);
          sp.omega.multiplyScalar(Math.pow(FRICTION, d * 60)); // frame-rate-independent decay
        } else {
          sp.omega.set(0, 0, 0);
          spinQuat.setFromAxisAngle(Y_AXIS, AUTO_SPEED * d);   // gentle resting drift
          r.core.quaternion.premultiply(spinQuat);
        }
      }

      // nodes orbit + colour (independent of the orb spin)
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
      // orb orientation is driven by the fidget-spin quaternion above

      r.renderer.render(r.scene, r.camera);
      r.raf = requestAnimationFrame(animate);
    };
    sceneRef.current.raf = requestAnimationFrame(animate);

    return () => {
      ro.disconnect();
      canvas.removeEventListener("pointerdown", onPointerDown);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerup", onPointerUp);
      canvas.removeEventListener("pointercancel", onPointerUp);
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
      <canvas ref={canvasRef} className="block w-full h-full"
        style={{ touchAction: "none" }} />
      {/* confidence leads; model-vote agreement is the supplement beneath it */}
      <div className="absolute top-3 left-0 right-0 flex flex-col items-center pointer-events-none">
        <span className="text-[8px] mono uppercase tracking-[0.24em] text-slate-500">
          confidence
        </span>
        <span className="text-[30px] font-bold mono tabular-nums leading-none mt-0.5"
          style={{ color: headColor,
                   textShadow: direction === "FLAT" ? "none" : `0 0 14px ${headColor}55` }}>
          {confidence.toFixed(2)}
        </span>
        {/* gate progress: fill = confidence, tick = gate threshold */}
        <div className="relative mt-2 h-[4px] w-[156px] rounded-full bg-white/10">
          <div className="absolute inset-y-0 left-0 rounded-full transition-[width] duration-500"
            style={{ width: `${Math.min(100, confidence * 100)}%`, background: headColor,
                     boxShadow: confCleared ? `0 0 6px ${headColor}` : "none" }} />
          <div className="absolute -top-[3px] h-[10px] w-px"
            style={{ left: `${Math.min(100, confGate * 100)}%`, background: "rgba(226,232,240,0.85)" }} />
        </div>
        <span className="mt-1 text-[8px] mono uppercase tracking-wider"
          style={{ color: confCleared ? headColor : "#64748b" }}>
          {confCleared ? "clears" : "needs"} gate {confGate.toFixed(2)}
        </span>
        {/* supplement: per-model votes + agreement tally */}
        <div className="mt-2.5 flex items-center gap-1.5">
          <span className="flex items-center gap-1">
            {slotVotes.map((v, i) => {
              const c = v === "LONG" ? "#2de8b0" : v === "SHORT" ? "#f0625f" : null;
              return (
                <span key={i} className="inline-block w-[6px] h-[6px] rounded-full"
                  style={c ? { background: c, boxShadow: `0 0 4px ${c}` }
                           : { background: "#ffffff14", border: "1px solid #ffffff22" }} />
              );
            })}
          </span>
          <span className="text-[9px] mono tracking-wide"
            style={{ color: direction === "FLAT" ? "#94a3b8" : headColor }}>
            {agreement}/{activeModels} {direction === "FLAT" ? "· scanning" : `agree ${direction}`}
          </span>
        </div>
      </div>
      {/* armed flash */}
      <div ref={flashRef} className="relay-flash absolute left-0 right-0 top-[38%] text-center
        text-[15px] font-bold mono uppercase tracking-wide pointer-events-none" />
      {/* gate chips — hidden entirely when that gate is disabled */}
      {!shortOff && (
      <div className="absolute bottom-2.5 left-2.5 pointer-events-none">
        <Chip side="short" gate={shortGate} off={shortOff}
          sig={shortSig} labels={["spot lag", "OI↑fall", "ask-heavy", "no dump"]} />
      </div>
      )}
      {!longOff && (
      <div className="absolute bottom-2.5 right-2.5 pointer-events-none">
        <Chip side="long" gate={longGate} off={longOff}
          sig={longSig} labels={["spot lead", "OI↑", "bid-heavy", "no pump"]} />
      </div>
      )}
    </div>
  );
}
