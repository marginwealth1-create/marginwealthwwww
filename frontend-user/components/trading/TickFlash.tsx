"use client";

import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { usePriceFlash } from "@/lib/usePriceFlash";

/**
 * Tints its content green on an up-tick and red on a down-tick, on top of
 * the shared `usePriceFlash` hook.
 *
 * The feed already delivers every real price change — measured against
 * Binance, all distinct movement the 100 ms pump can carry reaches the
 * client. What felt slow was visibility: a one-paisa move on a five-figure
 * price reads as frozen, and a text-colour flash is invisible whenever it
 * matches the base colour (a red bid ticking down). A background tint shows in
 * both directions. It never invents movement; zero / missing prices don't
 * flash.
 *
 * Wrap the price, not the whole row — the tint should sit on the number.
 */
export function TickFlash({
  value,
  children,
  className,
}: {
  value: number | null | undefined;
  children: ReactNode;
  className?: string;
}) {
  const dir = usePriceFlash(value);
  return (
    <span
      className={cn(
        "rounded-[3px] transition-colors duration-300",
        dir === "up" && "bg-buy/25",
        dir === "down" && "bg-sell/25",
        className,
      )}
    >
      {children}
    </span>
  );
}
