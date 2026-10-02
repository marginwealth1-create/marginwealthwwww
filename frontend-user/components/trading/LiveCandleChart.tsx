"use client";

import { useEffect, useRef } from "react";
import { useTheme } from "next-themes";
import {
  createChart,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type ISeriesApi,
  type Time,
} from "lightweight-charts";

export interface Candle {
  time: number; // unix seconds
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
}

interface Props {
  candles: Candle[];
  livePrice?: number | null;
  /** Fixed pixel height. If omitted (or set to "auto"), the chart fills its parent. */
  height?: number | "auto";
}

/** HSL → RGB (0-255 ints). h: 0-360, s/l: 0-100. */
function hslToRgb(h: number, s: number, l: number): [number, number, number] {
  const sn = Math.max(0, Math.min(100, s)) / 100;
  const ln = Math.max(0, Math.min(100, l)) / 100;
  const k = (n: number) => (n + h / 30) % 12;
  const a = sn * Math.min(ln, 1 - ln);
  const f = (n: number) =>
    ln - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  return [Math.round(f(0) * 255), Math.round(f(8) * 255), Math.round(f(4) * 255)];
}

/**
 * Reads a CSS variable like `--muted-foreground` (which holds bare HSL
 * components, e.g. `"0 0% 63%"`) and returns an `rgb(...)` / `rgba(...)`
 * string. Lightweight-charts v4 ships an older color parser that doesn't
 * accept HSL syntax in either form, so we convert at the JS boundary.
 */
function cssVar(name: string, alpha?: number): string {
  if (typeof window === "undefined") return "transparent";
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  if (!raw) return "transparent";
  // Accept "0 0% 63%" (modern) and "0, 0%, 63%" (legacy)
  const parts = raw.split(/[,\s]+/).filter(Boolean);
  if (parts.length < 3) return "transparent";
  const h = parseFloat(parts[0]);
  const s = parseFloat(parts[1]);
  const l = parseFloat(parts[2]);
  if (!Number.isFinite(h) || !Number.isFinite(s) || !Number.isFinite(l)) return "transparent";
  const [r, g, b] = hslToRgb(h, s, l);
  return alpha === undefined ? `rgb(${r}, ${g}, ${b})` : `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function readPalette() {
  return {
    text: cssVar("--muted-foreground"),
    grid: cssVar("--border", 0.5),
    border: cssVar("--border"),
    up: cssVar("--buy"),
    down: cssVar("--sell"),
    upTransparent: cssVar("--buy", 0.3),
    downTransparent: cssVar("--sell", 0.3),
    volume: cssVar("--muted-foreground", 0.4),
  };
}

/**
 * TradingView Lightweight Charts (MIT) wrapper.
 * Reads colors from CSS variables so it follows the active theme; reapplies
 * options whenever the theme flips between dark and light.
 */
export function LiveCandleChart({ candles, livePrice, height = 360 }: Props) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const candlesRef = useRef<Candle[]>([]);
  candlesRef.current = candles;

  const { resolvedTheme } = useTheme();
  const isAutoHeight = height === "auto";

  // Init / dispose
  useEffect(() => {
    if (!wrapRef.current) return;
    const initialH = isAutoHeight ? wrapRef.current.clientHeight : (height as number);
    const palette = readPalette();
    const chart = createChart(wrapRef.current, {
      width: wrapRef.current.clientWidth,
      height: initialH,
      layout: {
        background: { color: "transparent" },
        textColor: palette.text,
        fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: palette.grid },
        horzLines: { color: palette.grid },
      },
      rightPriceScale: { borderColor: palette.border },
      timeScale: {
        borderColor: palette.border,
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 6,
      },
      crosshair: { mode: 0 },
      handleScroll: {
        mouseWheel: true,
        pressedMouseMove: true,
        horzTouchDrag: true,
        vertTouchDrag: true,
      },
      handleScale: {
        axisPressedMouseMove: { time: true, price: true },
        mouseWheel: true,
        pinch: true,
      },
      autoSize: false,
    });

    const candleSeries = chart.addCandlestickSeries({
      upColor: palette.up,
      downColor: palette.down,
      borderUpColor: palette.up,
      borderDownColor: palette.down,
      wickUpColor: palette.up,
      wickDownColor: palette.down,
      priceFormat: { type: "price", precision: 2, minMove: 0.05 },
    });

    const volumeSeries = chart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "vol",
      color: palette.volume,
    });
    chart.priceScale("vol").applyOptions({
      scaleMargins: { top: 0.85, bottom: 0 },
    });

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volumeSeriesRef.current = volumeSeries;

    const ro = new ResizeObserver(() => {
      if (wrapRef.current) {
        const h = isAutoHeight ? wrapRef.current.clientHeight : (height as number);
        chart.applyOptions({ width: wrapRef.current.clientWidth, height: h });
      }
    });
    ro.observe(wrapRef.current);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      volumeSeriesRef.current = null;
    };
  }, [height, isAutoHeight]);

  // Reapply palette whenever the theme flips
  useEffect(() => {
    const ch = chartRef.current;
    const cs = candleSeriesRef.current;
    const vs = volumeSeriesRef.current;
    if (!ch || !cs || !vs) return;
    // Defer one frame so the new theme class is committed before reading vars
    const id = requestAnimationFrame(() => {
      const p = readPalette();
      ch.applyOptions({
        layout: { background: { color: "transparent" }, textColor: p.text },
        grid: { vertLines: { color: p.grid }, horzLines: { color: p.grid } },
        rightPriceScale: { borderColor: p.border },
        timeScale: { borderColor: p.border },
      });
      cs.applyOptions({
        upColor: p.up,
        downColor: p.down,
        borderUpColor: p.up,
        borderDownColor: p.down,
        wickUpColor: p.up,
        wickDownColor: p.down,
      });
      vs.applyOptions({ color: p.volume });
      // Refresh volume bar tints
      const data = candlesRef.current;
      if (data?.length) {
        vs.setData(
          data.map((c) => ({
            time: c.time as Time,
            value: c.volume ?? 0,
            color: c.close >= c.open ? p.upTransparent : p.downTransparent,
          }))
        );
      }
    });
    return () => cancelAnimationFrame(id);
  }, [resolvedTheme]);

  // Set candles whenever the data array changes
  useEffect(() => {
    const cs = candleSeriesRef.current;
    const vs = volumeSeriesRef.current;
    const ch = chartRef.current;
    if (!cs || !vs || !ch || !candles?.length) return;

    const p = readPalette();
    const candleData: CandlestickData<Time>[] = candles.map((c) => ({
      time: c.time as Time,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }));
    const volData: HistogramData<Time>[] = candles.map((c) => ({
      time: c.time as Time,
      value: c.volume ?? 0,
      color: c.close >= c.open ? p.upTransparent : p.downTransparent,
    }));

    cs.setData(candleData);
    vs.setData(volData);
    ch.timeScale().fitContent();
  }, [candles]);

  // Live LTP — update the last candle's close
  useEffect(() => {
    const cs = candleSeriesRef.current;
    if (!cs || !candles?.length || !livePrice || !Number.isFinite(livePrice)) return;
    const last = candles[candles.length - 1];
    cs.update({
      time: last.time as Time,
      open: last.open,
      high: Math.max(last.high, livePrice),
      low: Math.min(last.low, livePrice),
      close: livePrice,
    });
  }, [livePrice, candles]);

  return (
    <div
      ref={wrapRef}
      className="w-full cursor-grab select-none active:cursor-grabbing"
      style={isAutoHeight ? { height: "100%" } : { height }}
    />
  );
}
