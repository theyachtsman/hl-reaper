"use client";
/**
 * ConsensusWheel — a live 3D "decision core": the model ensemble as a radial
 * spoke wheel. Seven model nodes orbit a central verdict core; each voting model
 * fires a stream of energy particles down its spoke into the core, coloured by
 * its vote (green LONG / red SHORT). The two permanently-inactive slots (ML,
 * LIQ) sit dim and hollow — honest, never beaming. The core pulses in the
 * aggregate verdict colour with an expanding sonar ring. The whole wheel tilts
 * gently for depth.
 *
 * Pure presentation of /api/tickets data already on the card — no new fetch.
 */
import { useEffect, useRef } from "react";
import * as THREE from "three";

type Ticket = { model: string; direction: string; confidence: number; meta: any };

const PASS = 0x1d9e75, FAIL = 0xe24b4a, FLAT = 0x556070, DEAD = 0x2a3344;
const dirHex = (d?: string) => d === "LONG" ? PASS : d === "SHORT" ? FAIL : FLAT;

// 8 ensemble slots; the 2 marked dead are permanently non-voting (kept for an
// honest picture, matching the model badges / old constellation).
const SLOTS: { model: string; abbr: string; dead?: boolean }[] = [
  { model: "TAModel", abbr: "TA" },
  { model: "MLForecastModel", abbr: "ML", dead: true },
  { model: "MeanReversionModel", abbr: "MR" },
  { model: "FundingRateModel", abbr: "FR" },
  { model: "OrderbookImbalanceModel", abbr: "OB" },
  { model: "VWAPModel", abbr: "VP" },
  { model: "MomentumModel", abbr: "MO" },
  { model: "LiquidationHeatmapModel", abbr: "LIQ", dead: true },
];
const N = SLOTS.length;
const R = 1.6;          // node orbit radius
const PER = 5;          // discrete energy particles per active spoke (spaced, not a solid beam)

// soft radial glow texture so beam particles read as round comets, not squares
function makeDotTexture(): THREE.CanvasTexture {
  const cv = document.createElement("canvas");
  cv.width = cv.height = 64;
  const ctx = cv.getContext("2d")!;
  const g = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
  g.addColorStop(0, "rgba(255,255,255,1)");
  g.addColorStop(0.3, "rgba(255,255,255,0.85)");
  g.addColorStop(1, "rgba(255,255,255,0)");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, 64, 64);
  return new THREE.CanvasTexture(cv);
}

function makeLabelSprite(text: string, hex: number, dim = false): THREE.Sprite {
  const cv = document.createElement("canvas");
  cv.width = 128; cv.height = 64;
  const ctx = cv.getContext("2d")!;
  ctx.font = "bold 40px ui-monospace, monospace";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  const col = `#${hex.toString(16).padStart(6, "0")}`;
  ctx.shadowColor = col;
  ctx.shadowBlur = dim ? 0 : 12;
  ctx.fillStyle = dim ? "#5b6675" : col;
  ctx.fillText(text, 64, 34);
  const tex = new THREE.CanvasTexture(cv);
  tex.anisotropy = 2;
  const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false, opacity: dim ? 0.6 : 0.95 });
  const sp = new THREE.Sprite(mat);
  sp.scale.set(0.62, 0.31, 1);
  return sp;
}

type Refs = {
  renderer: THREE.WebGLRenderer; scene: THREE.Scene; camera: THREE.PerspectiveCamera;
  group: THREE.Group; core: THREE.Mesh; coreMat: THREE.MeshBasicMaterial;
  icos: THREE.Mesh; sonar: THREE.Mesh; sonarMat: THREE.MeshBasicMaterial;
  nodeMats: THREE.MeshBasicMaterial[]; haloMats: THREE.MeshBasicMaterial[];
  spokeGeo: THREE.BufferGeometry; spokeColors: Float32Array;
  beam: THREE.Points; beamGeo: THREE.BufferGeometry;
  beamPos: Float32Array; beamCol: Float32Array; beamActive: boolean[];
  nodePos: THREE.Vector3[]; raf: number;
  votes: { dir: string; conf: number; dead: boolean }[];
  verdictHex: number;
};

export default function ConsensusWheel({ tickets, direction }: {
  tickets: Ticket[]; direction?: string;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const refs = useRef<Refs | null>(null);
  const live = useRef({ tickets, direction });
  live.current = { tickets, direction };

  // ---- build scene once ------------------------------------------------
  useEffect(() => {
    const canvas = canvasRef.current!;
    const wrap = wrapRef.current!;

    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setClearColor(0x000000, 0);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 100);
    camera.position.set(0, 0, 5.1);
    camera.lookAt(0, 0, 0);

    const group = new THREE.Group();
    scene.add(group);

    // node positions (top-first, clockwise)
    const nodePos: THREE.Vector3[] = [];
    for (let i = 0; i < N; i++) {
      const a = -Math.PI / 2 + (i / N) * Math.PI * 2;
      nodePos.push(new THREE.Vector3(Math.cos(a) * R, Math.sin(a) * R, 0));
    }

    // spokes — one LineSegments, per-vertex colour (updated on vote change)
    const spokeVerts = new Float32Array(N * 2 * 3);
    const spokeColors = new Float32Array(N * 2 * 3);
    for (let i = 0; i < N; i++) {
      spokeVerts[i * 6 + 0] = nodePos[i].x;
      spokeVerts[i * 6 + 1] = nodePos[i].y;
      spokeVerts[i * 6 + 3] = 0; spokeVerts[i * 6 + 4] = 0; // center
    }
    const spokeGeo = new THREE.BufferGeometry();
    // BufferAttribute references the array directly (Float32BufferAttribute COPIES
    // it) — we mutate spokeColors / beam arrays every frame, so we must reference.
    spokeGeo.setAttribute("position", new THREE.BufferAttribute(spokeVerts, 3));
    spokeGeo.setAttribute("color", new THREE.BufferAttribute(spokeColors, 3));
    const spokes = new THREE.LineSegments(spokeGeo,
      new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.55 }));
    group.add(spokes);

    // ring the nodes sit on
    const orbit = new THREE.Mesh(
      new THREE.RingGeometry(R - 0.012, R + 0.012, 96),
      new THREE.MeshBasicMaterial({ color: 0x334155, transparent: true, opacity: 0.35, side: THREE.DoubleSide }));
    group.add(orbit);

    // model nodes + halos + labels
    const nodeMats: THREE.MeshBasicMaterial[] = [];
    const haloMats: THREE.MeshBasicMaterial[] = [];
    const nodeGeo = new THREE.SphereGeometry(0.16, 20, 20);
    const haloGeo = new THREE.SphereGeometry(0.3, 16, 16);
    SLOTS.forEach((slot, i) => {
      const haloMat = new THREE.MeshBasicMaterial({ color: FLAT, transparent: true, opacity: 0.0 });
      const halo = new THREE.Mesh(haloGeo, haloMat);
      halo.position.copy(nodePos[i]);
      group.add(halo);
      haloMats.push(haloMat);

      const nodeMat = new THREE.MeshBasicMaterial({
        color: slot.dead ? DEAD : FLAT, transparent: true,
        opacity: slot.dead ? 0.4 : 0.85, wireframe: slot.dead,
      });
      const node = new THREE.Mesh(nodeGeo, nodeMat);
      node.position.copy(nodePos[i]);
      node.scale.setScalar(slot.dead ? 0.8 : 1);
      group.add(node);
      nodeMats.push(nodeMat);

      const label = makeLabelSprite(slot.abbr, slot.dead ? DEAD : 0x9fb0c0, slot.dead);
      label.position.copy(nodePos[i]).multiplyScalar(1.22);
      group.add(label);
    });

    // central verdict core (+ wireframe shell + sonar ring)
    const coreMat = new THREE.MeshBasicMaterial({ color: FLAT, transparent: true, opacity: 0.9 });
    const core = new THREE.Mesh(new THREE.SphereGeometry(0.34, 28, 28), coreMat);
    group.add(core);
    const icos = new THREE.Mesh(
      new THREE.IcosahedronGeometry(0.5, 0),
      new THREE.MeshBasicMaterial({ color: FLAT, wireframe: true, transparent: true, opacity: 0.3 }));
    group.add(icos);
    const sonarMat = new THREE.MeshBasicMaterial({ color: FLAT, transparent: true, opacity: 0.4, side: THREE.DoubleSide });
    const sonar = new THREE.Mesh(new THREE.RingGeometry(0.42, 0.46, 48), sonarMat);
    group.add(sonar);

    // energy beam particles (N spokes * PER)
    const M = N * PER;
    const beamPos = new Float32Array(M * 3);
    const beamCol = new Float32Array(M * 3);
    const beamGeo = new THREE.BufferGeometry();
    beamGeo.setAttribute("position", new THREE.BufferAttribute(beamPos, 3));
    beamGeo.setAttribute("color", new THREE.BufferAttribute(beamCol, 3));
    const beam = new THREE.Points(beamGeo, new THREE.PointsMaterial({
      size: 0.24, sizeAttenuation: true, vertexColors: true,
      transparent: true, opacity: 1, depthWrite: false, depthTest: false,
      blending: THREE.AdditiveBlending,
      map: makeDotTexture(),
    }));
    beam.renderOrder = 3; // always stream on top of the ring / nodes / core
    beam.frustumCulled = false;
    group.add(beam);

    refs.current = {
      renderer, scene, camera, group, core, coreMat, icos, sonar, sonarMat,
      nodeMats, haloMats, spokeGeo, spokeColors,
      beam, beamGeo, beamPos, beamCol, beamActive: new Array(N).fill(false),
      nodePos, raf: 0,
      votes: SLOTS.map((s) => ({ dir: "FLAT", conf: 0, dead: !!s.dead })),
      verdictHex: FLAT,
    };

    const resize = () => {
      const w = wrap.clientWidth || 1, h = wrap.clientHeight || 1;
      renderer.setSize(w, h, false);
      camera.aspect = w / h; camera.updateProjectionMatrix();
    };
    resize();
    const ro = new ResizeObserver(resize); ro.observe(wrap);

    const tmp = new THREE.Vector3();
    const clock = new THREE.Clock();
    const animate = () => {
      const r = refs.current!;
      const t = clock.getElapsedTime();

      // very gentle wobble for depth — kept small so labels stay on their nodes
      r.group.rotation.y = Math.sin(t * 0.4) * 0.08;
      r.group.rotation.x = Math.cos(t * 0.32) * 0.05;

      // core pulse + spinning shell + sonar
      r.core.scale.setScalar(1 + Math.sin(t * 2.4) * 0.08);
      r.icos.rotation.y += 0.012; r.icos.rotation.x += 0.006;
      const sp = (t * 0.9) % 1;
      r.sonar.scale.setScalar(1 + sp * 3.4);
      r.sonarMat.opacity = 0.24 * (1 - sp);

      // node idle bob
      for (let i = 0; i < N; i++) {
        const halo = r.haloMats[i];
        if (!r.votes[i].dead && r.votes[i].dir !== "FLAT")
          halo.opacity = 0.12 + Math.sin(t * 3 + i) * 0.06;
      }

      // beams flow node → center for active spokes (slow enough to read as
      // individual particles travelling, not a static beam)
      const flow = t * 0.32;
      for (let s = 0; s < N; s++) {
        const active = r.beamActive[s];
        const node = r.nodePos[s];
        const cHex = r.votes[s].dir === "LONG" ? PASS : r.votes[s].dir === "SHORT" ? FAIL : FLAT;
        const cr = ((cHex >> 16) & 255) / 255, cg = ((cHex >> 8) & 255) / 255, cb = (cHex & 255) / 255;
        for (let k = 0; k < PER; k++) {
          const idx = s * PER + k;
          if (!active) {
            r.beamPos[idx * 3] = 99; // park offscreen so a stale point can't show
            r.beamCol[idx * 3] = r.beamCol[idx * 3 + 1] = r.beamCol[idx * 3 + 2] = 0;
            continue;
          }
          const f = (flow + k / PER) % 1;
          tmp.copy(node).multiplyScalar(1 - f); // lerp toward (0,0,0)
          r.beamPos[idx * 3] = tmp.x; r.beamPos[idx * 3 + 1] = tmp.y; r.beamPos[idx * 3 + 2] = tmp.z;
          // each dot fades in off the node, brightest mid-travel, dimming into the
          // core, plus a fast twinkle so it reads as a living particle, not a beam
          const travel = Math.sin(f * Math.PI);          // 0 at ends, 1 mid-spoke
          const twinkle = 0.65 + 0.35 * Math.sin(t * 6 + idx * 1.7);
          const b = 1.7 * (0.2 + travel * 0.8) * twinkle;
          r.beamCol[idx * 3] = cr * b; r.beamCol[idx * 3 + 1] = cg * b; r.beamCol[idx * 3 + 2] = cb * b;
        }
      }
      (r.beamGeo.getAttribute("position") as THREE.BufferAttribute).needsUpdate = true;
      (r.beamGeo.getAttribute("color") as THREE.BufferAttribute).needsUpdate = true;

      r.renderer.render(r.scene, r.camera);
      r.raf = requestAnimationFrame(animate);
    };
    refs.current.raf = requestAnimationFrame(animate);

    return () => {
      ro.disconnect();
      cancelAnimationFrame(refs.current!.raf);
      scene.traverse((o) => {
        const any = o as any;
        if (any.geometry) any.geometry.dispose();
        if (any.material) {
          const m = any.material;
          if (m.map) m.map.dispose();
          m.dispose();
        }
      });
      renderer.dispose();
      refs.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- apply votes / verdict when tickets change -----------------------
  useEffect(() => {
    const r = refs.current;
    if (!r) return;
    const by: Record<string, Ticket> = {};
    for (const t of tickets) by[t.model] = t;

    SLOTS.forEach((slot, i) => {
      const tk = by[slot.model];
      const dir = slot.dead ? "FLAT" : (tk?.direction ?? "FLAT");
      const conf = tk ? Number(tk.confidence) || 0 : 0;
      r.votes[i] = { dir, conf, dead: !!slot.dead };
      const active = !slot.dead && (dir === "LONG" || dir === "SHORT");
      r.beamActive[i] = active;

      const hex = slot.dead ? DEAD : dirHex(dir);
      r.nodeMats[i].color.setHex(hex);
      r.nodeMats[i].opacity = slot.dead ? 0.4 : active ? 1 : 0.6;
      r.haloMats[i].color.setHex(hex);
      r.haloMats[i].opacity = active ? 0.28 : 0;

      // spoke colour (dim base; bright when voting)
      const cr = ((hex >> 16) & 255) / 255, cg = ((hex >> 8) & 255) / 255, cb = (hex & 255) / 255;
      const sc = slot.dead ? 0.12 : active ? 0.2 : 0.26;
      for (const v of [0, 1]) {
        const o = (i * 2 + v) * 3;
        r.spokeColors[o] = cr * sc; r.spokeColors[o + 1] = cg * sc; r.spokeColors[o + 2] = cb * sc;
      }
    });
    (r.spokeGeo.getAttribute("color") as THREE.BufferAttribute).needsUpdate = true;

    const vh = dirHex(direction);
    r.coreMat.color.setHex(vh);
    r.icos.material instanceof THREE.MeshBasicMaterial && r.icos.material.color.setHex(vh);
    r.sonarMat.color.setHex(vh);
  }, [tickets, direction]);

  return (
    <div ref={wrapRef} className="absolute inset-0">
      <canvas ref={canvasRef} className="block w-full h-full" />
    </div>
  );
}
