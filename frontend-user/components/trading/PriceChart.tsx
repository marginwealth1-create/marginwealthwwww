"use client";

import { useEffect, useMemo, useRef } from "react";
import { useTheme } from "next-themes";

/**
 * Lightweight live chart — renders a price-tape line/candle series using
 * an in-memory ring of recent ticks. We use a tiny canvas implementation
 * to avoid pulling in a full charting library on first paint; the design
 * deliberately stays decoupled from backend candle endpoints (Phase 3).
 */

function cssVar(name: string, alpha?: number): string {
  if (typeof window === "undefined") return "transparent";
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  if (!v) return "transparent";
  const csv = v.replace(/\s+/g, ", ");
  return alpha === undefined ? `hsl(${csv})` : `hsla(${csv}, ${alpha})`;
}

export interface ChartTick {
  ts: number;
  price: number;
}

interface Props {
  ticks: ChartTick[];
  height?: number;
}

export function PriceChart({ ticks, height = 320 }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const { resolvedTheme } = useTheme();

  const stats = useMemo(() => {
    if (!ticks.length) return { min: 0, max: 0, last: 0, first: 0 };
    let min = ticks[0].price;
    let max = ticks[0].price;
    for (const t of ticks) {
      if (t.price < min) min = t.price;
      if (t.price > max) max = t.price;
    }
    return { min, max, last: ticks[ticks.length - 1].price, first: ticks[0].price };
  }, [ticks]);

  useEffect(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;

    const dpr = window.devicePixelRatio || 1;
    const w = wrap.clientWidth;
    const h = height;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    canvas.style.width = `${w}px`;
    canvas.style.height = `${h}px`;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const muted = cssVar("--muted-foreground", 0.7);
    const grid = cssVar("--border", 0.6);
    const buy = cssVar("--buy");
    const sell = cssVar("--sell");
    const buyFill0 = cssVar("--buy", 0.35);
    const buyFill1 = cssVar("--buy", 0);
    const sellFill0 = cssVar("--sell", 0.35);
    const sellFill1 = cssVar("--sell", 0);
    const tagFg = cssVar("--background");

    if (ticks.length < 2) {
      ctx.fillStyle = muted;
      ctx.font = "12px Inter, system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("Waiting for ticks…", w / 2, h / 2);
      return;
    }

    // Grid
    ctx.strokeStyle = grid;
    ctx.lineWidth = 1;
    for (let i = 1; i < 5; i++) {
      const y = (h / 5) * i;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
      ctx.stroke();
    }

    const padX = 6;
    const padY = 12;
    const range = Math.max(0.01, stats.max - stats.min);
    const xStep = (w - padX * 2) / Math.max(1, ticks.length - 1);
    const yFor = (p: number) => padY + (h - padY * 2) * (1 - (p - stats.min) / range);

    // Area fill
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    const isUp = stats.last >= stats.first;
    if (isUp) {
      grad.addColorStop(0, buyFill0);
      grad.addColorStop(1, buyFill1);
    } else {
      grad.addColorStop(0, sellFill0);
      grad.addColorStop(1, sellFill1);
    }
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.moveTo(padX, yFor(ticks[0].price));
    for (let i = 1; i < ticks.length; i++) {
      ctx.lineTo(padX + i * xStep, yFor(ticks[i].price));
    }
    ctx.lineTo(padX + (ticks.length - 1) * xStep, h - padY);
    ctx.lineTo(padX, h - padY);
    ctx.closePath();
    ctx.fill();

    // Line
    const trend = isUp ? buy : sell;
    ctx.strokeStyle = trend;
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    ctx.moveTo(padX, yFor(ticks[0].price));
    for (let i = 1; i < ticks.length; i++) {
      ctx.lineTo(padX + i * xStep, yFor(ticks[i].price));
    }
    ctx.stroke();

    // Last tick dot
    const lastX = padX + (ticks.length - 1) * xStep;
    const lastY = yFor(stats.last);
    ctx.fillStyle = trend;
    ctx.beginPath();
    ctx.arc(lastX, lastY, 3, 0, Math.PI * 2);
    ctx.fill();

    // Last price tag
    ctx.fillStyle = trend;
    const tag = stats.last.toFixed(2);
    ctx.font = "12px Inter, system-ui, sans-serif";
    const tagW = ctx.measureText(tag).width + 12;
    ctx.fillRect(w - tagW - 6, lastY - 9, tagW, 18);
    ctx.fillStyle = tagFg;
    ctx.fillText(tag, w - tagW / 2 - 6, lastY + 4);
  }, [ticks, height, stats, resolvedTheme]);

  return (
    <div ref={wrapRef} className="w-full">
      <canvas ref={canvasRef} className="block w-full" />
    </div>
  );
}
