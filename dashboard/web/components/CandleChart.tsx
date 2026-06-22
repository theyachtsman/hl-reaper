"use client";
import { useEffect, useRef, useState } from "react";
import {
  createChart, CrosshairMode, LineStyle,
  type IChartApi, type ISeriesApi, type IPriceLine, type Time,
} from "lightweight-charts";
import { api, fmtUsd, useActiveCoins, DEFAULT_COINS } from "@/lib/api";

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
  unrealized_pnl: number; conf?: number | null;
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
  // breakeven lock — bright yellow square, distinct from the amber TS circle
  be: "#fde047",
};

function exitColor(result?: string) {
  if (!result) return C.maxhold;
  if (result.includes("take profit")) return C.tp;
  if (result.includes("trailing")) return C.trail;
  if (result.includes("stop loss")) return C.sl;
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
  const [isMobile, setIsMobile] = useState(false);
  const [openPos, setOpenPos] = useState<OpenPos | null>(null);
  const [hover, setHover] = useState<Candle | null>(null);
  const [picked, setPicked] = useState<Marker | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const wrapRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const posLineRef = useRef<IPriceLine | null>(null);
  const markersRef = useRef<Marker[]>([]);

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
      posLineRef.current = null;
    };
  }, [coin, interval, isMobile]);

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

        // markers — keep within candle range, sorted ascending
        const lo = d.candles[0]?.time ?? 0;
        const hi = d.candles[d.candles.length - 1]?.time ?? 0;
        markersRef.current = d.markers;
        const lwMarkers = d.markers
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
              : m.result?.includes("max hold") ? "MH" : "X";
            return {
              time: m.time as Time, position: "aboveBar",
              color: exitColor(m.result), shape: "circle",
              size: bandSize(m.band),
              text: `${tag}${pnlTxt}${bandTag(m.band)}`,
            };
          });
        candleRef.current.setMarkers(lwMarkers as any);

        // open-position dashed line
        if (posLineRef.current) {
          candleRef.current.removePriceLine(posLineRef.current);
          posLineRef.current = null;
        }
        setOpenPos(d.open_position);
        if (d.open_position) {
          const long = d.open_position.direction === "LONG";
          posLineRef.current = candleRef.current.createPriceLine({
            price: d.open_position.entry_price,
            color: long ? C.up : C.down,
            lineStyle: LineStyle.Dashed, lineWidth: 1,
            axisLabelVisible: true,
            title: `OPEN ${d.open_position.direction}`,
          });
        }
      } catch (e) {
        if (live) setErr(String(e));
      }
    };
    tick();
    const id = window.setInterval(tick, 10000);
    return () => { live = false; window.clearInterval(id); };
  }, [coin, interval]);

  const fmt = (n: number | null | undefined, dp = 2) =>
    n == null ? "—" : n.toLocaleString("en-US",
      { minimumFractionDigits: dp, maximumFractionDigits: dp });

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
      </div>

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
          <span className={openPos.direction === "LONG" ? "text-emerald-400" : "text-red-400"}>
            OPEN {openPos.direction} @ ${fmt(openPos.entry_price)}
            {openPos.conf != null && ` (conf ${openPos.conf.toFixed(2)})`}
            {" · "}uPnL {fmtUsd(openPos.unrealized_pnl)}
          </span>
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
