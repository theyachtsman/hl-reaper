"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  createChart, CrosshairMode, LineStyle,
  type IChartApi, type ISeriesApi, type IPriceLine, type Time,
} from "lightweight-charts";
import { api, post, fmtUsd, useActiveCoins, DEFAULT_COINS } from "@/lib/api";
import { useBandStore } from "@/lib/store";

type Candle = {
  time: number; open: number; high: number;
  low: number; close: number; volume: number;
};
type Marker = {
  time: number; type: "entry" | "exit" | "be";
  direction: string | null; price: number | null;
  conf?: number | null; votes?: number | null;
  result?: string; pnl?: number | null; note?: string;
  band?: string | null;
};

// band differentiator: trend markers render larger than scalp; a lowercase
// band letter is appended to the marker text. Keeps the existing shapes/colors.
const bandSize = (b?: string | null) => (b === "trend" ? 2 : 1);
const bandTag = (b?: string | null) =>
  b === "scalp" ? " ·s" : b === "trend" ? " ·t" : "";
type OpenPos = {
  direction: string; entry_price: number; entry_time: number | null;
  unrealized_pnl: number; conf?: number | null; band?: string | null;
  sl?: number | null; tp?: number | null; size?: number | null;
};
type ChartData = {
  coin: string; interval: string; candles: Candle[];
  markers: Marker[]; open_position: OpenPos | null;
};

const INTERVALS = ["1m", "5m", "15m", "1h"];

// theme — matches the Analysis Core 3D look (near-black panel + #1D9E75 green)
const C = {
  panel: "#070a0e", edge: "#1e2a3a", text: "#64748b",
  up: "#1D9E75", down: "#E24B4A",
  entryLong: "#22c98e", entryShort: "#f0625f",
  tp: "#1D9E75", sl: "#E24B4A", trail: "#fbbf24", maxhold: "#94a3b8",
  manual: "#38bdf8",
  // breakeven lock — bright yellow square, distinct from the amber TS circle
  be: "#fde047",
};

// adaptive price formatter for overlay labels (BTC ~64,000 vs DOGE ~0.08)
function fmtPx(p: number): string {
  const dp = Math.abs(p) >= 100 ? 2 : Math.abs(p) >= 1 ? 3 : 5;
  return p.toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
}

// a flat, two-point Baseline series used as a shaded band between `linePx` and
// the `baseline` price. Both top and bottom fills are the same color so the
// band reads correctly whether the line sits above or below the baseline
// (i.e. works for LONG and SHORT). Returned so it can be removed on cleanup.
function mkBand(chart: IChartApi, baseline: number, linePx: number,
                fill: string, lo: Time, hi: Time): ISeriesApi<"Baseline"> {
  const b = chart.addBaselineSeries({
    baseValue: { type: "price", price: baseline },
    topLineColor: "transparent", bottomLineColor: "transparent",
    topFillColor1: fill, topFillColor2: fill,
    bottomFillColor1: fill, bottomFillColor2: fill,
    lastValueVisible: false, priceLineVisible: false,
    crosshairMarkerVisible: false, priceScaleId: "right",
  });
  b.setData([{ time: lo, value: linePx }, { time: hi, value: linePx }]);
  return b;
}

function exitColor(result?: string) {
  if (!result) return C.maxhold;
  if (result.includes("take profit")) return C.tp;
  if (result.includes("trailing")) return C.trail;
  if (result.includes("stop loss")) return C.sl;
  if (result.includes("manual")) return C.manual;
  return C.maxhold;
}

export default function CandleChart() {
  // coin tabs follow the live active-coins config (Controls toggles) and fall
  // back to the full universe while config loads / on a fetch error
  const activeCoins = useActiveCoins();
  const coins = activeCoins ?? DEFAULT_COINS;

  const [coin, setCoin] = useState("BTC");

  // if the charted coin gets toggled off, snap to the first active coin
  useEffect(() => {
    if (coins.length && !coins.includes(coin)) setCoin(coins[0]);
  }, [coins, coin]);
  const [interval, setInterval] = useState("5m");
  // shared Live-page band context: switching SCALP/TREND sets the chart's
  // DEFAULT timeframe (5m / 1h) and which band's position markers show. It only
  // sets the default on switch — the user can still override the interval after.
  const activeBand = useBandStore((s) => s.activeBand);
  useEffect(() => {
    setInterval(activeBand === "trend" ? "1h" : "5m");
  }, [activeBand]);
  const [isMobile, setIsMobile] = useState(false);
  const [openPos, setOpenPos] = useState<OpenPos | null>(null);
  const [hover, setHover] = useState<Candle | null>(null);
  const [picked, setPicked] = useState<Marker | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // TP/SL/entry overlay toggle — on by default; persisted across mounts.
  const [showTpSl, setShowTpSl] = useState(true);
  // draft TP/SL from the header sliders. null = follow the server value; once
  // the user drags, the draft owns the line until they confirm or it converges
  // back to the server value (after the bot applies the override).
  const [draftTp, setDraftTp] = useState<number | null>(null);
  const [draftSl, setDraftSl] = useState<number | null>(null);
  const [setting, setSetting] = useState(false);

  const wrapRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  // TP/entry/SL dashed price lines + the two shaded band series, tracked so the
  // overlay can be cleared on close, band switch, or toggle-off.
  const overlayLinesRef = useRef<IPriceLine[]>([]);
  const bandSeriesRef = useRef<ISeriesApi<"Baseline">[]>([]);
  const markersRef = useRef<Marker[]>([]);
  // refs that the imperative overlay redraw reads, so dragging a slider can
  // redraw instantly without waiting for the 10s candle poll.
  const openPosRef = useRef<OpenPos | null>(null);
  const candleSpanRef = useRef<[Time, Time]>([0 as Time, 0 as Time]);
  const draftTpRef = useRef<number | null>(null);
  const draftSlRef = useRef<number | null>(null);
  const showTpSlRef = useRef(true);

  // track viewport for mobile layout (height, volume, coin selector)
  useEffect(() => {
    const onResize = () => setIsMobile(window.innerWidth < 640);
    onResize();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // (re)build the chart when coin / interval / mobile changes
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const chart = createChart(el, {
      width: el.clientWidth,
      height: isMobile ? 240 : 380,
      layout: { background: { color: C.panel }, textColor: C.text,
                fontSize: 11 },
      grid: { vertLines: { color: C.edge }, horzLines: { color: C.edge } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: C.edge },
      timeScale: { borderColor: C.edge, timeVisible: true, secondsVisible: false },
    });
    chartRef.current = chart;
    const candleSeries = chart.addCandlestickSeries({
      upColor: C.up, downColor: C.down, borderUpColor: C.up,
      borderDownColor: C.down, wickUpColor: C.up, wickDownColor: C.down,
    });
    candleRef.current = candleSeries;

    if (!isMobile) {
      const vol = chart.addHistogramSeries({
        priceFormat: { type: "volume" }, priceScaleId: "vol",
      });
      vol.priceScale().applyOptions({
        scaleMargins: { top: 0.8, bottom: 0 },
      });
      volRef.current = vol;
    } else {
      volRef.current = null;
    }

    // hover legend (OHLCV) + marker click → ticket panel
    chart.subscribeCrosshairMove((p) => {
      const d = p.seriesData.get(candleSeries) as any;
      if (d && typeof d.open === "number") {
        const v = volRef.current
          ? (p.seriesData.get(volRef.current) as any)?.value : undefined;
        setHover({ time: Number(p.time), open: d.open, high: d.high,
                   low: d.low, close: d.close, volume: v ?? 0 });
      } else setHover(null);
    });
    chart.subscribeClick((p) => {
      if (p.time == null) return;
      const t = Number(p.time);
      const near = markersRef.current.filter((m) => Math.abs(m.time - t) < 1);
      setPicked(near.length ? near[near.length - 1] : null);
    });

    const ro = new ResizeObserver(() => {
      if (chartRef.current && el.clientWidth)
        chartRef.current.applyOptions({ width: el.clientWidth });
    });
    ro.observe(el);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      volRef.current = null;
      // chart.remove() disposes its series/price-lines; just drop the refs.
      overlayLinesRef.current = [];
      bandSeriesRef.current = [];
    };
  }, [coin, interval, isMobile]);

  // clear the TP/SL overlay (lines + band series) from the live chart
  const clearOverlay = () => {
    const series = candleRef.current, chart = chartRef.current;
    if (series) overlayLinesRef.current.forEach((l) => {
      try { series.removePriceLine(l); } catch {}
    });
    if (chart) bandSeriesRef.current.forEach((b) => {
      try { chart.removeSeries(b); } catch {}
    });
    overlayLinesRef.current = [];
    bandSeriesRef.current = [];
  };

  // (re)draw the TP/entry/SL overlay from the current refs. Reads refs (not
  // closure state) so it can be called imperatively on every slider drag for an
  // instant preview, and from the candle poll, without stale values. Draft
  // values (slider) take precedence over the server tp/sl when set.
  const drawOverlayNow = useCallback(() => {
    clearOverlay();
    const series = candleRef.current, chart = chartRef.current;
    const op = openPosRef.current;
    if (!series || !chart || !op || !showTpSlRef.current) return;
    const [lo, hi] = candleSpanRef.current;
    if (!lo || !hi) return;
    const long = op.direction === "LONG";
    const entry = op.entry_price;
    const size = op.size ?? 0;
    const tp = draftTpRef.current ?? op.tp ?? null;
    const sl = draftSlRef.current ?? op.sl ?? null;
    const lines: IPriceLine[] = [];
    lines.push(series.createPriceLine({
      price: entry, color: "#60a5fa", lineWidth: 1,
      lineStyle: LineStyle.Dashed, axisLabelVisible: true,
      title: `ENTRY`,
    }));
    if (tp != null && Number.isFinite(tp)) {
      const gain = (long ? tp - entry : entry - tp) * size;
      lines.push(series.createPriceLine({
        price: tp, color: "#4ade80", lineWidth: 1,
        lineStyle: LineStyle.Dashed, axisLabelVisible: true,
        title: `TP${size ? `  +$${gain.toFixed(2)}` : ""}`,
      }));
      bandSeriesRef.current.push(
        mkBand(chart, entry, tp, "rgba(74,222,128,0.12)", lo, hi));
    }
    if (sl != null && Number.isFinite(sl)) {
      const loss = (long ? entry - sl : sl - entry) * size;
      lines.push(series.createPriceLine({
        price: sl, color: "#f87171", lineWidth: 1,
        lineStyle: LineStyle.Dashed, axisLabelVisible: true,
        title: `SL${size ? `  -$${Math.abs(loss).toFixed(2)}` : ""}`,
      }));
      bandSeriesRef.current.push(
        mkBand(chart, entry, sl, "rgba(248,113,113,0.12)", lo, hi));
    }
    overlayLinesRef.current = lines;
  }, []);

  // data fetch + 10s refresh
  useEffect(() => {
    let live = true;
    const tick = async () => {
      try {
        const d = await api<ChartData>(
          `/api/chart/${coin}?interval=${interval}&limit=200`);
        if (!live || !candleRef.current) return;
        setErr(null);
        candleRef.current.setData(d.candles as any);
        if (volRef.current) {
          volRef.current.setData(d.candles.map((c) => ({
            time: c.time as Time, value: c.volume,
            color: c.close >= c.open ? C.up + "55" : C.down + "55",
          })) as any);
        }

        // markers — keep within candle range, sorted ascending. Band context
        // filters to the active band's markers; unattributed (null-band) legacy
        // markers always stay visible so old history isn't hidden.
        const lo = d.candles[0]?.time ?? 0;
        const hi = d.candles[d.candles.length - 1]?.time ?? 0;
        const bandMarkers = d.markers.filter(
          (m) => !m.band || m.band === activeBand);
        markersRef.current = bandMarkers;
        const lwMarkers = bandMarkers
          .filter((m) => m.time >= lo && m.time <= hi)
          .sort((a, b) => a.time - b.time)
          .map((m) => {
            if (m.type === "entry") {
              const long = m.direction === "LONG";
              return {
                time: m.time as Time,
                position: long ? "belowBar" : "aboveBar",
                color: long ? C.entryLong : C.entryShort,
                shape: long ? "arrowUp" : "arrowDown",
                size: bandSize(m.band),
                text: `${long ? "L" : "S"}${m.conf != null ? " " + m.conf.toFixed(2) : ""}${bandTag(m.band)}`,
              };
            }
            if (m.type === "be") {
              // breakeven profit lock — SL moved to entry+buffer
              const long = m.direction === "LONG";
              return {
                time: m.time as Time,
                position: long ? "belowBar" : "aboveBar",
                color: C.be, shape: "square", size: bandSize(m.band),
                text: `BE${bandTag(m.band)}`,
              };
            }
            const pnl = m.pnl;
            const pnlTxt = pnl != null
              ? ` ${pnl >= 0 ? "+" : ""}$${pnl.toFixed(2)}` : "";
            const tag = m.result?.includes("take profit") ? "TP"
              : m.result?.includes("trailing") ? "TS"
              : m.result?.includes("stop loss") ? "SL"
              : m.result?.includes("max hold") ? "MH"
              : m.result?.includes("manual") ? "MS" : "X";
            return {
              time: m.time as Time, position: "aboveBar",
              color: exitColor(m.result), shape: "circle",
              size: bandSize(m.band),
              text: `${tag}${pnlTxt}${bandTag(m.band)}`,
            };
          });
        candleRef.current.setMarkers(lwMarkers as any);

        // the position belongs to a band too — only draw it in the matching
        // band context (null-band positions show in either).
        const op = d.open_position && (!d.open_position.band
          || d.open_position.band === activeBand) ? d.open_position : null;
        setOpenPos(op);
        openPosRef.current = op;
        candleSpanRef.current = [lo as Time, hi as Time];
        // converge: once the bot has applied a manual override, the server
        // tp/sl match the draft — drop the draft so the slider tracks the live
        // value again (and future trailing-stop moves show). Also clear stale
        // draft if the position closed.
        if (!op) { setDraftTp(null); setDraftSl(null); draftTpRef.current = null; draftSlRef.current = null; }
        else {
          const near = (a: number | null, b?: number | null) =>
            a != null && b != null && Math.abs(a - b) <= Math.abs(b) * 1e-4;
          if (near(draftTpRef.current, op.tp)) { setDraftTp(null); draftTpRef.current = null; }
          if (near(draftSlRef.current, op.sl)) { setDraftSl(null); draftSlRef.current = null; }
        }
        drawOverlayNow();
      } catch (e) {
        if (live) setErr(String(e));
      }
    };
    tick();
    const id = window.setInterval(tick, 10000);
    return () => { live = false; window.clearInterval(id); };
  }, [coin, interval, activeBand, drawOverlayNow]);

  // keep the overlay's toggle ref in sync + redraw immediately on toggle
  useEffect(() => {
    showTpSlRef.current = showTpSl;
    drawOverlayNow();
  }, [showTpSl, drawOverlayNow]);

  // reset the draft sliders when the charted coin changes (new position context)
  useEffect(() => {
    setDraftTp(null); setDraftSl(null);
    draftTpRef.current = null; draftSlRef.current = null;
  }, [coin]);

  const fmt = (n: number | null | undefined, dp = 2) =>
    n == null ? "—" : n.toLocaleString("en-US",
      { minimumFractionDigits: dp, maximumFractionDigits: dp });

  // draft-aware effective TP/SL (slider override falls back to server value)
  const effTp = draftTp ?? openPos?.tp ?? null;
  const effSl = draftSl ?? openPos?.sl ?? null;

  // R:R = TP price distance / SL price distance (size cancels out), draft-aware
  const rr = (() => {
    if (!openPos || effTp == null || effSl == null) return null;
    const reward = Math.abs(effTp - openPos.entry_price);
    const risk = Math.abs(openPos.entry_price - effSl);
    return risk > 0 ? reward / risk : null;
  })();

  // slider bounds: entry ± a span wide enough to comfortably hold the current
  // sl/tp, with the correct side enforced (LONG: tp>entry>sl; SHORT inverse).
  const slCfg = (() => {
    if (!openPos) return null;
    const entry = openPos.entry_price, long = openPos.direction === "LONG";
    const dist = Math.max(
      Math.abs((openPos.tp ?? entry) - entry),
      Math.abs((openPos.sl ?? entry) - entry), entry * 0.01);
    const span = Math.min(entry * 0.5, dist * 1.8);   // cap at ±50%
    const step = span / 500;
    // [min,max] for the TP slider and the SL slider on the correct sides
    const tp = long ? [entry, entry + span] : [entry - span, entry];
    const sl = long ? [entry - span, entry] : [entry, entry + span];
    return { entry, long, step, tpMin: tp[0], tpMax: tp[1], slMin: sl[0], slMax: sl[1] };
  })();

  // commit the draft SL/TP to the bot (executes within one loop, <=10s)
  const commitSltp = async () => {
    if (!openPos || effSl == null || effTp == null) return;
    setSetting(true);
    try {
      await post("/api/bot/command",
        { command: `set_sltp/${coin}/${effSl}/${effTp}` });
    } catch (e) {
      setErr(`set SL/TP failed: ${e}`);
    } finally {
      setSetting(false);
    }
  };
  // confirm enabled only when a slider actually changed from the server value
  const dirtyTp = draftTp != null && openPos?.tp != null
    && Math.abs(draftTp - openPos.tp) > Math.abs(openPos.tp) * 1e-4;
  const dirtySl = draftSl != null && openPos?.sl != null
    && Math.abs(draftSl - openPos.sl) > Math.abs(openPos.sl) * 1e-4;

  return (
    <div className="core-card p-4">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
        {/* coin selector: tabs (desktop) / dropdown (mobile) */}
        {isMobile ? (
          <select value={coin} onChange={(e) => setCoin(e.target.value)}
            className="bg-ink border border-edge rounded-lg px-3 py-1.5 text-sm mono">
            {coins.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        ) : (
          <div className="flex gap-1">
            {coins.map((c) => (
              <button key={c} onClick={() => setCoin(c)}
                className={`px-3 py-1 rounded-lg text-sm font-semibold mono border transition-all duration-200 ${
                  c === coin
                    ? "bg-[#1D9E75]/15 border-[#1D9E75]/45 text-[#22c98e]"
                    : "border-transparent text-slate-400 hover:text-slate-200 hover:border-edge"}`}>
                {c}
              </button>
            ))}
          </div>
        )}
        <div className="flex items-center gap-2">
          <div className="flex gap-1">
            {INTERVALS.map((iv) => (
              <button key={iv} onClick={() => setInterval(iv)}
                className={`px-2.5 py-1 rounded-lg text-xs mono border transition-all duration-200 ${
                  iv === interval
                    ? "bg-[#1D9E75]/15 border-[#1D9E75]/45 text-[#22c98e]"
                    : "border-transparent text-slate-500 hover:text-slate-300 hover:border-edge"}`}>
                {iv}
              </button>
            ))}
          </div>
          {/* TP/SL/entry overlay on-off toggle */}
          <button onClick={() => setShowTpSl((v) => !v)}
            title="toggle TP / entry / SL overlay"
            className={`px-2.5 py-1 rounded-lg text-xs mono border transition-all duration-200 ${
              showTpSl
                ? "bg-[#60a5fa]/15 border-[#60a5fa]/45 text-[#93c5fd]"
                : "border-transparent text-slate-500 hover:text-slate-300 hover:border-edge"}`}>
            TP/SL
          </button>
        </div>
      </div>

      {/* TP/SL adjust sliders — only with an open position + overlay on. Drag to
          preview the lines on the chart in real time, then Set to commit. */}
      {openPos && showTpSl && slCfg && effTp != null && effSl != null && (
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2 mb-2 px-1 py-1.5 rounded-lg border border-edge bg-black/30">
          <span className="text-[9px] uppercase tracking-[0.18em] text-slate-500">adjust</span>
          {/* TP slider */}
          <label className="flex items-center gap-2 min-w-[200px] flex-1">
            <span className="text-[10px] mono font-semibold text-[#4ade80] w-6">TP</span>
            <input type="range" min={slCfg.tpMin} max={slCfg.tpMax} step={slCfg.step}
              value={effTp} className="flex-1 accent-[#4ade80] cursor-pointer"
              onChange={(e) => {
                const v = Number(e.target.value);
                setDraftTp(v); draftTpRef.current = v; drawOverlayNow();
              }} />
            <span className="text-[10px] mono text-slate-300 tabular-nums w-[120px] text-right">
              {fmtPx(effTp)}
              {openPos.size != null && (
                <span className="text-[#4ade80]/80"> +${Math.abs((slCfg.long ? effTp - slCfg.entry : slCfg.entry - effTp) * (openPos.size || 0)).toFixed(2)}</span>
              )}
            </span>
          </label>
          {/* SL slider */}
          <label className="flex items-center gap-2 min-w-[200px] flex-1">
            <span className="text-[10px] mono font-semibold text-[#f87171] w-6">SL</span>
            <input type="range" min={slCfg.slMin} max={slCfg.slMax} step={slCfg.step}
              value={effSl} className="flex-1 accent-[#f87171] cursor-pointer"
              onChange={(e) => {
                const v = Number(e.target.value);
                setDraftSl(v); draftSlRef.current = v; drawOverlayNow();
              }} />
            <span className="text-[10px] mono text-slate-300 tabular-nums w-[120px] text-right">
              {fmtPx(effSl)}
              {openPos.size != null && (
                <span className="text-[#f87171]/80"> -${Math.abs((slCfg.long ? slCfg.entry - effSl : effSl - slCfg.entry) * (openPos.size || 0)).toFixed(2)}</span>
              )}
            </span>
          </label>
          {rr != null && (
            <span className="text-[10px] mono text-slate-400">R:R <span className="text-white/70">1 : {rr.toFixed(1)}</span></span>
          )}
          <button onClick={commitSltp} disabled={(!dirtyTp && !dirtySl) || setting}
            className={`px-3 py-1 rounded-lg text-xs mono font-semibold border transition-all duration-200 ${
              (dirtyTp || dirtySl) && !setting
                ? "bg-[#60a5fa]/15 border-[#60a5fa]/45 text-[#93c5fd] hover:bg-[#60a5fa]/25"
                : "border-edge text-slate-600 cursor-not-allowed"}`}>
            {setting ? "setting…" : "Set TP/SL"}
          </button>
        </div>
      )}

      {/* hover legend + open-position banner */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs mono mb-2 min-h-[18px]">
        {hover ? (
          <>
            <span className="text-slate-400">O <span className="text-slate-200">{fmt(hover.open)}</span></span>
            <span className="text-slate-400">H <span className="text-slate-200">{fmt(hover.high)}</span></span>
            <span className="text-slate-400">L <span className="text-slate-200">{fmt(hover.low)}</span></span>
            <span className="text-slate-400">C <span className="text-slate-200">{fmt(hover.close)}</span></span>
            {!isMobile && <span className="text-slate-400">V <span className="text-slate-200">{fmt(hover.volume, 3)}</span></span>}
          </>
        ) : openPos ? (
          <>
            <span className={openPos.direction === "LONG" ? "text-emerald-400" : "text-red-400"}>
              OPEN {openPos.direction} @ ${fmt(openPos.entry_price)}
              {openPos.conf != null && ` (conf ${openPos.conf.toFixed(2)})`}
              {" · "}uPnL {fmtUsd(openPos.unrealized_pnl)}
            </span>
            {rr != null && showTpSl && (
              <span className="text-slate-400">R:R <span className="text-white/60">1 : {rr.toFixed(1)}</span></span>
            )}
          </>
        ) : (
          <span className="text-slate-600">hover a candle for OHLCV · click a marker for the trade ticket</span>
        )}
      </div>

      <div ref={wrapRef} style={{ width: "100%" }} />

      {err && <div className="text-xs text-red-400 mt-2">{err}</div>}

      {/* clicked-marker ticket panel */}
      {picked && (
        <div className="mt-3 border-t border-edge pt-3 text-sm">
          <div className="flex items-center justify-between">
            <span className="font-semibold">
              {picked.type === "entry" ? "Entry"
                : picked.type === "be" ? "Breakeven lock" : "Exit"} · {coin}
              {picked.direction && (
                <span className={`ml-2 ${picked.direction === "LONG" ? "text-emerald-400" : "text-red-400"}`}>
                  {picked.direction}
                </span>
              )}
              {picked.band && (
                <span className={`ml-2 text-xs uppercase ${picked.band === "scalp" ? "text-cyan-300" : "text-purple-300"}`}>
                  {picked.band}
                </span>
              )}
            </span>
            <button onClick={() => setPicked(null)}
              className="text-slate-500 hover:text-slate-300 text-xs">close ✕</button>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mt-2 mono text-xs">
            {picked.price != null && (
              <div><div className="label">Price</div><div>{fmt(picked.price)}</div></div>
            )}
            {picked.type === "entry" && picked.conf != null && (
              <div><div className="label">Confidence</div><div>{picked.conf.toFixed(3)}</div></div>
            )}
            {picked.type === "entry" && picked.votes != null && (
              <div><div className="label">Votes</div><div>{picked.votes}</div></div>
            )}
            {picked.type === "exit" && picked.result && (
              <div><div className="label">Result</div><div>{picked.result}</div></div>
            )}
            {picked.type === "exit" && picked.pnl != null && (
              <div><div className="label">PnL</div>
                <div className={picked.pnl >= 0 ? "text-emerald-400" : "text-red-400"}>
                  {fmtUsd(picked.pnl)}</div></div>
            )}
            <div><div className="label">Time</div>
              <div>{new Date(picked.time * 1000).toLocaleString("en-US", { hour12: true })}</div></div>
          </div>
          {picked.note && <div className="text-xs text-slate-500 mt-2 mono">{picked.note}</div>}
        </div>
      )}
    </div>
  );
}
