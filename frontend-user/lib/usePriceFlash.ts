"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Tick-flash hook — returns "up" | "down" | "same" based on how the
 * given `value` compares to the previous render's value, then auto-
 * resets to "same" after `decayMs` so the flash is brief.
 *
 * Standard broker UX: when LTP / bid / ask ticks up, the cell flashes
 * green; when it ticks down, it flashes red. After the decay window
 * the cell goes back to its neutral colour so the trader's eye only
 * tracks the actual movements, not the cumulative direction.
 *
 * Usage:
 *   const dir = usePriceFlash(bid);
 *   <span className={dir === "up" ? "text-emerald-500" : dir === "down" ? "text-red-500" : ""}>
 *     {bid}
 *   </span>
 *
 * Returns "same" on the very first render (no prior value to compare
 * against), and also when the value is null/undefined/NaN/0.
 */
export function usePriceFlash(
  value: number | null | undefined,
  decayMs: number = 700,
): "up" | "down" | "same" {
  const [direction, setDirection] = useState<"up" | "down" | "same">("same");
  const prevRef = useRef<number | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const num = Number(value);
    if (!Number.isFinite(num) || num <= 0) {
      prevRef.current = null;
      return;
    }
    const prev = prevRef.current;
    if (prev !== null && num !== prev) {
      const nextDir = num > prev ? "up" : "down";
      setDirection(nextDir);
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => setDirection("same"), decayMs);
    }
    prevRef.current = num;
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [value, decayMs]);

  return direction;
}
