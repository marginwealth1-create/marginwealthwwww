"use client";

import { useState } from "react";
import Link from "next/link";
import { Briefcase } from "lucide-react";
import { TradeDetailSheet } from "@/components/trading/TradeDetailSheet";
import { cn } from "@/lib/utils";

interface Props {
  instrument: any;
  ltp: number;
  bid?: number | null;
  ask?: number | null;
}

/**
 * Mobile-only bottom strip on the chart card: a Positions shortcut on the
 * left + compact SELL / BUY buttons. Tapping a side opens the full order
 * card (TradeDetailSheet) preselected to that side, where the trader sets
 * lot / qty, MARKET/LIMIT, SL/TP and confirms — the card owns all the order
 * logic (margin check, market-hours guard, audio cue, submit). The inline
 * lot stepper + instant-order were removed per the operator.
 */
export function MobileQuickTradeBar({ instrument, ltp, bid, ask }: Props) {
  const [sheetSide, setSheetSide] = useState<"BUY" | "SELL" | null>(null);

  const seg = (instrument?.segment ?? "").toUpperCase();
  const exch = (instrument?.exchange ?? "").toUpperCase();
  const isCrypto = seg.includes("CRYPTO") || exch === "CRYPTO";
  const isForex = seg.includes("FOREX") || seg.includes("FX") || exch === "CDS";
  const priceDecimals = isCrypto ? 2 : isForex ? 4 : 2;

  const sellPrice = bid ?? ltp ?? 0;
  const buyPrice = ask ?? ltp ?? 0;
  const fmt = (n: number) => Number(n || 0).toFixed(priceDecimals);

  const disabled = !instrument?.token;

  return (
    <>
      <div className="shrink-0 bg-card lg:hidden">
        <div className="grid grid-cols-[auto_1fr_1fr] items-stretch gap-2 border-t border-border px-2.5 py-2 pb-[max(0.5rem,env(safe-area-inset-bottom))]">
          {/* Positions shortcut */}
          <Link
            href="/positions"
            aria-label="Positions"
            title="Positions"
            className="flex items-center justify-center rounded-lg border border-border bg-muted/40 px-3 text-foreground transition-opacity active:opacity-70"
          >
            <Briefcase className="size-4" />
          </Link>

          <button
            type="button"
            onClick={() => !disabled && setSheetSide("SELL")}
            disabled={disabled}
            aria-label={`Sell ${instrument?.symbol ?? ""}`}
            className={cn(
              "flex flex-col items-center justify-center rounded-lg bg-sell px-3 py-1.5 text-white shadow-sm transition-opacity active:opacity-80",
              disabled && "opacity-50",
            )}
          >
            <span className="text-[9px] font-semibold uppercase tracking-wider opacity-90">
              Sell
            </span>
            <span className="font-tabular text-sm font-bold tabular-nums leading-tight">
              {fmt(sellPrice)}
            </span>
          </button>

          <button
            type="button"
            onClick={() => !disabled && setSheetSide("BUY")}
            disabled={disabled}
            aria-label={`Buy ${instrument?.symbol ?? ""}`}
            className={cn(
              "flex flex-col items-center justify-center rounded-lg bg-buy px-3 py-1.5 text-white shadow-sm transition-opacity active:opacity-80",
              disabled && "opacity-50",
            )}
          >
            <span className="text-[9px] font-semibold uppercase tracking-wider opacity-90">
              Buy
            </span>
            <span className="font-tabular text-sm font-bold tabular-nums leading-tight">
              {fmt(buyPrice)}
            </span>
          </button>
        </div>
      </div>

      {/* Order card — opens preselected to the tapped side. */}
      <TradeDetailSheet
        token={instrument?.token ?? null}
        open={sheetSide !== null}
        initialSide={sheetSide ?? "BUY"}
        onClose={() => setSheetSide(null)}
      />
    </>
  );
}
