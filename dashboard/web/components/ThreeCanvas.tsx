"use client";
/**
 * ThreeCanvas — one live 3D "analysis core" scene per coin.
 *
 * A colour-keyed 3D price ribbon (last ~60 candle closes) rises out of a grid
 * floor inside a dense, chaotic particle field, with a pulsing orb and (when
 * armed) spinning energy rings. Two independent inputs drive it:
 *   - colorMode  : long (green) / short (red) / neutral (grey) — set by the
 *                  price TREND, or the verdict direction when armed. Colours the
 *                  ribbon, glow line, particles, orb and wireframe, and sets the
 *                  particle drift direction (up / down / random).
 *   - intensity  : neutral / armed / position — brightness, particle opacity,
 *                  ring visibility and pulse/spin speed.
 *
 * The particle field is a single GPU-animated Points cloud (per-particle size,
 * speed and phase → big-and-small chaotic drift, additive-blended) that fills
 * the whole frustum. The scene is built once on mount; price updates rebuild
 * only the ribbon + glow-line geometry, and colour/intensity changes recolour
 * in place. The animation loop reads mutable state from a ref so it never
 * goes stale.
 */
import { useEffect, useRef } from "react";
import * as THREE from "three";

export type ColorMode = "neutral" | "long" | "short";
export type Intensity = "neutral" | "armed" | "position";

const ACCENT: Record<ColorMode, number> = {
  long: 0x1d9e75,
  short: 0xe24b4a,
  neutral: 0x888880,
};
const rgb = (hex: number) =>
  new THREE.Color(hex);

const CHART_H = 2.6;   // taller ribbon (was 1.8)
const CHART_Y0 = -1.3; // baseline (was -0.9)

// widest ribbon that still leaves a slight horizontal margin inside the camera
// frustum at the ribbon's depth — keeps the chart near-full-width without the
// ends clipping at the card edges. A fixed CHART_W=9 overran narrower cards;
// this fits the actual aspect so there's always a little breathing room.
const RIBBON_Z = -0.8, CAM_Z = 4.5, FOV_DEG = 55, CAM_X_DRIFT = 0.25;
function fitChartW(aspect: number): number {
  const dist = CAM_Z - RIBBON_Z;                       // ~5.3 world units
  const vHalf = dist * Math.tan((FOV_DEG * Math.PI / 180) / 2);
  const hHalf = vHalf * aspect;                         // visible half-width
  // back off the camera x-parallax, then ~8% breathing room each side
  const usable = (hHalf - CAM_X_DRIFT) * 0.92;
  return Math.max(4.5, Math.min(8.4, usable * 2));      // cap keeps a margin
}

function buildRibbonGeometry(prices: number[], mode: ColorMode, chartW: number) {
  const geo = new THREE.BufferGeometry();
  if (prices.length < 2) return geo;
  const mn = Math.min(...prices);
  const mx = Math.max(...prices);
  const range = mx - mn || 1;
  const verts: number[] = [], cols: number[] = [], idxs: number[] = [];

  prices.forEach((p, i) => {
    const x = (i / (prices.length - 1)) * chartW - chartW / 2;
    const y = ((p - mn) / range) * CHART_H + CHART_Y0;
    const isUp = i === 0 || p >= prices[i - 1];

    const [r, g, b] = isUp
      ? (mode === "long"  ? [0.105, 0.620, 0.459]
       : mode === "short" ? [0.235, 0.294, 0.290]
       :                     [0.533, 0.533, 0.533])
      : (mode === "short" ? [0.886, 0.294, 0.290]
       : mode === "long"  ? [0.105, 0.294, 0.220]
       :                     [0.400, 0.400, 0.400]);

    verts.push(x, y + 0.04, -0.8);
    cols.push(r, g, b);
    verts.push(x, CHART_Y0, -0.8);
    cols.push(r * 0.3, g * 0.3, b * 0.3);

    if (i < prices.length - 1) {
      const a = i * 2;
      idxs.push(a, a + 1, a + 2, a + 1, a + 3, a + 2);
    }
  });

  geo.setAttribute("position", new THREE.Float32BufferAttribute(verts, 3));
  geo.setAttribute("color", new THREE.Float32BufferAttribute(cols, 3));
  geo.setIndex(idxs);
  return geo;
}

function buildLineGeometry(prices: number[], chartW: number) {
  if (prices.length < 2) return new THREE.BufferGeometry();
  const mn = Math.min(...prices), mx = Math.max(...prices);
  const pts = prices.map((p, i) => new THREE.Vector3(
    (i / (prices.length - 1)) * chartW - chartW / 2,
    ((p - mn) / (mx - mn || 1)) * CHART_H + CHART_Y0,
    -0.78,
  ));
  return new THREE.BufferGeometry().setFromPoints(pts);
}

// chaotic, frustum-filling particle field. Per-particle size/speed/phase give a
// big-and-small turbulent drift; motion runs entirely on the GPU.
const PARTICLE_VERT = `
  uniform float uTime;
  uniform float uDir;
  uniform float uPixelRatio;
  attribute float aSize;
  attribute float aSpeed;
  attribute float aPhase;
  varying float vTw;
  void main() {
    vec3 p = position;
    // turbulent horizontal sway
    p.x += sin(uTime * aSpeed * 0.6 + aPhase) * 0.55;
    p.z += cos(uTime * aSpeed * 0.5 + aPhase * 1.3) * 0.45;
    // vertical drift (up for long / down for short), wrapped over the box
    float range = 5.0;
    float drift = uDir * uTime * aSpeed * 0.45 + aPhase;
    p.y = mod(position.y + drift + 2.5, range) - 2.5;
    p.y += (1.0 - abs(uDir)) * sin(uTime * 0.6 + aPhase) * 0.18; // neutral bob
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    gl_PointSize = aSize * uPixelRatio * (240.0 / -mv.z);
    gl_Position = projectionMatrix * mv;
    vTw = 0.55 + 0.45 * sin(uTime * aSpeed * 1.3 + aPhase * 2.0);
  }
`;
const PARTICLE_FRAG = `
  uniform vec3 uColor;
  uniform float uOpacity;
  varying float vTw;
  void main() {
    vec2 c = gl_PointCoord - 0.5;
    float d = length(c);
    if (d > 0.5) discard;
    float a = smoothstep(0.5, 0.0, d);
    gl_FragColor = vec4(uColor, a * uOpacity * vTw);
  }
`;

function buildParticles(accent: number, pixelRatio: number, opacity: number, dir: number) {
  const N = 1200;
  const pos = new Float32Array(N * 3);
  const size = new Float32Array(N);
  const speed = new Float32Array(N);
  const phase = new Float32Array(N);
  for (let i = 0; i < N; i++) {
    pos[i * 3]     = (Math.random() - 0.5) * 8.4; // wide: fill frustum
    pos[i * 3 + 1] = (Math.random() - 0.5) * 5.0;
    pos[i * 3 + 2] = (Math.random() - 0.5) * 4.4;
    // mostly tiny, a chaotic few large (gl_PointSize ≈ aSize * pr * 240/dist)
    size[i] = 0.015 + Math.pow(Math.random(), 4) * 0.22;
    speed[i] = 0.3 + Math.random() * 1.8;
    phase[i] = Math.random() * Math.PI * 2;
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  geo.setAttribute("aSize", new THREE.BufferAttribute(size, 1));
  geo.setAttribute("aSpeed", new THREE.BufferAttribute(speed, 1));
  geo.setAttribute("aPhase", new THREE.BufferAttribute(phase, 1));
  const uniforms = {
    uTime: { value: 0 },
    uDir: { value: dir },
    uPixelRatio: { value: pixelRatio },
    uColor: { value: rgb(accent) },
    uOpacity: { value: opacity },
  };
  const mat = new THREE.ShaderMaterial({
    uniforms, vertexShader: PARTICLE_VERT, fragmentShader: PARTICLE_FRAG,
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
  });
  return { points: new THREE.Points(geo, mat), geo, mat, uniforms };
}

type SceneRefs = {
  renderer: THREE.WebGLRenderer;
  scene: THREE.Scene;
  camera: THREE.PerspectiveCamera;
  ribbon: THREE.Mesh;
  ribbonMat: THREE.MeshBasicMaterial;
  priceLine: THREE.Line;
  priceLineMat: THREE.LineBasicMaterial;
  particles: THREE.Points;
  particleGeo: THREE.BufferGeometry;
  particleMat: THREE.ShaderMaterial;
  pUniforms: { uTime: { value: number }; uDir: { value: number };
    uPixelRatio: { value: number }; uColor: { value: THREE.Color }; uOpacity: { value: number } };
  orb: THREE.Mesh;
  orbMat: THREE.MeshBasicMaterial;
  wire: THREE.Mesh;
  wireMat: THREE.MeshBasicMaterial;
  rings: THREE.Mesh[];
  chartW: number;
  raf: number;
};

export default function ThreeCanvas({
  prices, colorMode, intensity, hovered = false,
}: { prices: number[]; colorMode: ColorMode; intensity: Intensity; hovered?: boolean }) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const refs = useRef<SceneRefs | null>(null);
  const live = useRef({ colorMode, intensity, hovered });
  live.current = { colorMode, intensity, hovered };
  const pricesRef = useRef(prices);
  pricesRef.current = prices;

  const dirOf = (cm: ColorMode) => cm === "long" ? 1 : cm === "short" ? -1 : 0;
  const opacityOf = (it: Intensity) => it === "position" ? 0.8 : it === "armed" ? 0.6 : 0.42;

  // ---- one-time scene construction ------------------------------------
  useEffect(() => {
    const canvas = canvasRef.current!;
    const wrap = wrapRef.current!;
    const accent = ACCENT[colorMode];
    const calm = intensity === "neutral";
    let chartW = fitChartW((wrap.clientWidth || 1) / (wrap.clientHeight || 1));

    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    const pr = Math.min(window.devicePixelRatio, 2);
    renderer.setPixelRatio(pr);
    renderer.setClearColor(0x000000, calm ? 0.62 : 0.8);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(55, 1, 0.1, 100);
    camera.position.set(0, 0.4, 4.5);
    camera.lookAt(0, 0, 0);

    // grid floor
    const grid = new THREE.GridHelper(10, 16, 0x222222, 0x111111);
    grid.position.set(0, -1.3, -0.5);
    (grid.material as THREE.Material).transparent = true;
    (grid.material as THREE.Material).opacity = 0.3;
    scene.add(grid);

    // ribbon
    const ribbonMat = new THREE.MeshBasicMaterial({
      vertexColors: true, transparent: true, opacity: 0.72, side: THREE.DoubleSide,
    });
    const ribbon = new THREE.Mesh(buildRibbonGeometry(prices, colorMode, chartW), ribbonMat);
    scene.add(ribbon);

    // glowing price line
    const priceLineMat = new THREE.LineBasicMaterial({
      color: accent, transparent: true, opacity: 0.95,
    });
    const priceLine = new THREE.Line(buildLineGeometry(prices, chartW), priceLineMat);
    scene.add(priceLine);

    // chaotic frustum-filling particle field (GPU-animated)
    const P = buildParticles(accent, pr, opacityOf(intensity), dirOf(colorMode));
    scene.add(P.points);

    // central orb
    const orbMat = new THREE.MeshBasicMaterial({
      color: accent, transparent: true, opacity: calm ? 0.2 : 0.5,
    });
    const orb = new THREE.Mesh(new THREE.SphereGeometry(0.15, 24, 24), orbMat);
    orb.position.set(0, 0, 1.2);
    scene.add(orb);

    // background wireframe sphere
    const wireMat = new THREE.MeshBasicMaterial({
      color: accent, wireframe: true, transparent: true, opacity: calm ? 0.04 : 0.08,
    });
    const wire = new THREE.Mesh(new THREE.SphereGeometry(0.6, 10, 7), wireMat);
    scene.add(wire);

    // energy rings (hidden when calm)
    const rings: THREE.Mesh[] = [];
    [0.5, 0.9].forEach((rad) => {
      const m = new THREE.MeshBasicMaterial({
        color: accent, transparent: true, opacity: 0.1, side: THREE.DoubleSide,
      });
      const ring = new THREE.Mesh(new THREE.RingGeometry(rad, rad + 0.03, 48), m);
      ring.position.set(0, 0, 0.4);
      ring.visible = !calm;
      scene.add(ring);
      rings.push(ring);
    });

    refs.current = {
      renderer, scene, camera, ribbon, ribbonMat, priceLine, priceLineMat,
      particles: P.points, particleGeo: P.geo, particleMat: P.mat, pUniforms: P.uniforms,
      orb, orbMat, wire, wireMat, rings, chartW, raf: 0,
    };

    const resize = () => {
      const w = wrap.clientWidth || 1;
      const h = wrap.clientHeight || 1;
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      // refit the ribbon to the new aspect so the margin stays consistent
      const nw = fitChartW(w / h);
      const r = refs.current;
      if (r && Math.abs(nw - r.chartW) > 0.05) {
        r.chartW = nw;
        const pr = pricesRef.current;
        r.ribbon.geometry.dispose();
        r.ribbon.geometry = buildRibbonGeometry(pr, live.current.colorMode, nw);
        r.priceLine.geometry.dispose();
        r.priceLine.geometry = buildLineGeometry(pr, nw);
      }
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(wrap);

    const clock = new THREE.Clock();
    const animate = () => {
      const r = refs.current!;
      const t = clock.getElapsedTime();
      const cm = live.current.colorMode;
      const inPos = live.current.intensity === "position";

      r.pUniforms.uTime.value = t;
      r.pUniforms.uDir.value = dirOf(cm);
      r.particles.rotation.y = Math.sin(t * 0.08) * 0.15;

      // orb pulse (faster in a live position)
      r.orb.scale.setScalar(1 + Math.sin(t * (inPos ? 4 : 2)) * 0.12);

      // wireframe slow rotation
      r.wire.rotation.y += 0.002;
      r.wire.rotation.x += 0.001;

      // energy rings: clockwise short, counter-clockwise long; 2x in position
      const spin = (cm === "short" ? -1 : 1) * (inPos ? 0.03 : 0.015);
      r.rings.forEach((ring, i) => {
        ring.rotation.z += spin * (i === 0 ? 1 : 0.7);
        ring.rotation.x = Math.sin(t * 0.3 + i) * 0.4;
      });

      // camera parallax drift + hover zoom
      const targetZ = 4.5 - (live.current.hovered ? 0.4 : 0);
      r.camera.position.x = Math.sin(t * 0.25) * 0.25;
      r.camera.position.y = 0.4 + Math.cos(t * 0.2) * 0.12;
      r.camera.position.z += (targetZ - r.camera.position.z) * 0.08;
      r.camera.lookAt(0, 0, 0);

      r.renderer.render(r.scene, r.camera);
      r.raf = requestAnimationFrame(animate);
    };
    refs.current.raf = requestAnimationFrame(animate);

    return () => {
      ro.disconnect();
      cancelAnimationFrame(refs.current!.raf);
      ribbon.geometry.dispose();
      priceLine.geometry.dispose();
      P.geo.dispose();
      grid.geometry.dispose();
      (grid.material as THREE.Material).dispose();
      orb.geometry.dispose();
      wire.geometry.dispose();
      rings.forEach((rg) => { rg.geometry.dispose(); (rg.material as THREE.Material).dispose(); });
      ribbonMat.dispose(); priceLineMat.dispose(); P.mat.dispose();
      orbMat.dispose(); wireMat.dispose();
      renderer.dispose();
      refs.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- rebuild ribbon + line geometry when prices change --------------
  useEffect(() => {
    const r = refs.current;
    if (!r) return;
    r.ribbon.geometry.dispose();
    r.ribbon.geometry = buildRibbonGeometry(prices, colorMode, r.chartW);
    r.priceLine.geometry.dispose();
    r.priceLine.geometry = buildLineGeometry(prices, r.chartW);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prices]);

  // ---- recolour / re-intensify when colour or intensity changes -------
  useEffect(() => {
    const r = refs.current;
    if (!r) return;
    const accent = ACCENT[colorMode];
    const calm = intensity === "neutral";

    r.renderer.setClearColor(0x000000, calm ? 0.62 : 0.8);
    r.priceLineMat.color.setHex(accent);
    r.pUniforms.uColor.value.setHex(accent);
    r.pUniforms.uOpacity.value = opacityOf(intensity);
    r.pUniforms.uDir.value = dirOf(colorMode);
    r.orbMat.color.setHex(accent);
    r.orbMat.opacity = calm ? 0.2 : 0.5;
    r.wireMat.color.setHex(accent);
    r.wireMat.opacity = calm ? 0.04 : 0.08;
    r.rings.forEach((ring) => {
      ring.visible = !calm;
      (ring.material as THREE.MeshBasicMaterial).color.setHex(accent);
    });
    r.ribbon.geometry.dispose();
    r.ribbon.geometry = buildRibbonGeometry(prices, colorMode, r.chartW);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [colorMode, intensity]);

  return (
    <div ref={wrapRef} className="absolute inset-0">
      <canvas ref={canvasRef} className="block w-full h-full" />
    </div>
  );
}
