"use client";

import { useState } from "react";
import { Plus, X } from "lucide-react";
import { OptionChainPicker } from "@/components/trading/OptionChainPicker";
import { cn } from "@/lib/utils";

export interface ChartTab {
  token: string;
  symbol: string;
  /** Watchlist-item id — kept on the type for callers that still set it,
   *  but the strip itself no longer reads it. Tabs are pure local state. */
  id?: string;
}

interface Props {
  tabs: ChartTab[];
  active: string | null;
  onSelect: (token: string) => void;
  onClose?: (token: string) => void;
  /** Called when the user picks an instrument from the + dialog. Parent
   *  is responsible for actually inserting the tab into its local state. */
  onAdded?: (token: string, symbol: string) => void;
}

/**
 * Chart-tab strip. Tabs represent instruments the user has actively opened
 * on the chart — they're NOT tied to the watchlist. Starring/unstarring an
 * instrument in the Instruments panel doesn't add or remove a tab. The
 * parent (`terminal/page.tsx`) owns the open-tabs state and persists it to
 * localStorage.
 */
export function ChartTabs({ tabs, active, onSelect, onClose, onAdded }: Props) {
  const [pickerOpen, setPickerOpen] = useState(false);

  function addTab(token: string, symbol: string) {
    // If the picked instrument is already a tab, just activate it.
    const existing = tabs.find((t) => t.token === token);
    if (existing) {
      onSelect(token);
      setPickerOpen(false);
      return;
    }
    // Hand off to the parent — it'll add to its local openTabs list and
    // FIFO-evict the oldest if at the cap.
    onAdded?.(token, symbol);
    onSelect(token);
    setPickerOpen(false);
  }

  return (
    <div className="relative flex items-center gap-1 overflow-x-auto border-b border-border bg-card/60 px-2 pt-2 scrollbar-thin">
      {tabs.map((t) => {
        const isActive = t.token === active;
        return (
          <div
            key={t.token}
            onClick={() => onSelect(t.token)}
            className={cn(
              "group flex shrink-0 cursor-pointer items-center gap-2 rounded-t-md px-3 py-1.5 text-xs transition-colors",
              isActive
                ? "border border-b-0 border-border bg-background text-foreground"
                : "text-muted-foreground hover:bg-muted/30 hover:text-foreground"
            )}
          >
            <span className="font-medium">{t.symbol}</span>
            {onClose && (
              <span
                role="button"
                tabIndex={0}
                onClick={(e) => {
                  e.stopPropagation();
                  onClose(t.token);
                }}
                className="rounded p-0.5 text-muted-foreground opacity-60 transition hover:bg-destructive/20 hover:text-destructive hover:opacity-100"
                aria-label="Close tab"
              >
                <X className="size-3" />
              </span>
            )}
          </div>
        );
      })}

      <button
        type="button"
        onClick={() => setPickerOpen(true)}
        className="ml-1 grid size-7 shrink-0 place-items-center rounded text-muted-foreground hover:bg-muted/30 hover:text-foreground"
        aria-label="Add tab"
      >
        <Plus className="size-4" />
      </button>

      <OptionChainPicker
        open={pickerOpen}
        onOpenChange={setPickerOpen}
        onPick={addTab}
      />
    </div>
  );
}
