"use client";

import { ChevronDown, RefreshCw, X } from "lucide-react";

interface Props {
  onClose: () => void;
}

/**
 * Economic-calendar slide-in. The panel layout is in place; until an admin
 * wires a calendar feed (TradingEconomics / Investing / FXStreet etc.) we
 * render an empty state rather than fake events.
 */
export function EconomicCalendarPanel({ onClose }: Props) {
  return (
    <aside className="flex h-full w-80 shrink-0 flex-col border-r border-border bg-card">
      {/* Header */}
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          Economic calendar
        </span>
        <button
          type="button"
          aria-label="Refresh"
          className="ml-auto grid size-7 place-items-center rounded text-muted-foreground hover:bg-muted/40 hover:text-foreground"
        >
          <RefreshCw className="size-3.5" />
        </button>
        <button
          type="button"
          aria-label="Close panel"
          onClick={onClose}
          className="grid size-7 place-items-center rounded text-muted-foreground hover:bg-muted/40 hover:text-foreground"
        >
          <X className="size-4" />
        </button>
      </div>

      {/* Filters */}
      <div className="space-y-2 border-b border-border px-3 py-2">
        <button
          type="button"
          className="flex h-8 w-full items-center justify-between rounded-md border border-border bg-muted/20 px-2 text-xs"
          disabled
        >
          <span>Economic calendar</span>
          <ChevronDown className="size-3.5 text-muted-foreground" />
        </button>
        <button
          type="button"
          className="flex h-8 w-full items-center justify-between rounded-md border border-border bg-background px-2 text-xs"
          disabled
        >
          <span>All countries</span>
          <ChevronDown className="size-3.5 text-muted-foreground" />
        </button>
      </div>

      {/* Column header */}
      <div className="grid grid-cols-[60px_1fr_60px_60px_60px] gap-2 border-b border-border px-3 py-1.5 text-[10px] uppercase tracking-wider text-muted-foreground">
        <span></span>
        <span></span>
        <span className="text-right">Actual</span>
        <span className="text-right">Forecast</span>
        <span className="text-right">Previous</span>
      </div>

      {/* Empty state */}
      <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center">
        <div className="text-sm font-medium">No calendar feed configured</div>
        <p className="text-[11px] leading-relaxed text-muted-foreground">
          Wire an economic-calendar provider (TradingEconomics, Investing, FXStreet, …)
          in admin → Settings to populate this list. We won&apos;t fabricate events here.
        </p>
      </div>
    </aside>
  );
}
