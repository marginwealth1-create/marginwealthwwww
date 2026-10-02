"use client";

import { useEffect, useState } from "react";
import { Calendar } from "lucide-react";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

export interface DateRange {
  from?: string; // YYYY-MM-DD
  to?: string;
}

interface Props {
  value: DateRange;
  onChange: (next: DateRange) => void;
  className?: string;
  /** Show ONLY the From → To date inputs — no preset pills, no Custom toggle.
   *  Used by the tradebook where the operator wants a plain "from date to
   *  date" download picker and nothing else. */
  simple?: boolean;
}

// Common presets — anything outside this is "Custom" via the date
// inputs. ISO YYYY-MM-DD on the wire, browser-localised in the picker.
const PRESETS: { id: string; label: string; days: number | null }[] = [
  { id: "7d", label: "Last 7 days", days: 7 },
  { id: "30d", label: "Last 30 days", days: 30 },
  { id: "3m", label: "Last 3 months", days: 90 },
  { id: "1y", label: "Last 1 year", days: 365 },
  { id: "all", label: "All time", days: null },
];

function isoDay(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function presetRange(days: number | null): DateRange {
  if (days === null) return {};
  const to = new Date();
  const from = new Date();
  from.setDate(from.getDate() - days);
  return { from: isoDay(from), to: isoDay(to) };
}

export function DateRangeBar({ value, onChange, className, simple }: Props) {
  // Simple mode: just the two date inputs, always visible.
  const [open, setOpen] = useState(false);

  if (simple) {
    return (
      <div className={cn("flex flex-wrap items-center gap-2", className)}>
        <Input
          type="date"
          value={value.from ?? ""}
          onChange={(e) => onChange({ ...value, from: e.target.value || undefined })}
          className="h-9 w-auto text-xs"
          aria-label="From date"
        />
        <span className="text-xs text-muted-foreground">to</span>
        <Input
          type="date"
          value={value.to ?? ""}
          onChange={(e) => onChange({ ...value, to: e.target.value || undefined })}
          className="h-9 w-auto text-xs"
          aria-label="To date"
        />
      </div>
    );
  }

  // Match the active preset by comparing day-spans — keeps the pill
  // highlighted after a reload if the URL/state happens to land on a
  // preset value, without forcing callers to track the preset id.
  function isPresetActive(days: number | null): boolean {
    if (days === null) return !value.from && !value.to;
    if (!value.from || !value.to) return false;
    const expected = presetRange(days);
    return expected.from === value.from && expected.to === value.to;
  }

  return (
    <div className={cn("flex flex-wrap items-center gap-2", className)}>
      <div className="flex flex-wrap gap-1 rounded-md border border-border bg-card p-1">
        {PRESETS.map((p) => {
          const active = isPresetActive(p.days);
          return (
            <button
              key={p.id}
              type="button"
              onClick={() => onChange(presetRange(p.days))}
              className={cn(
                "rounded px-2.5 py-1 text-xs font-medium transition-colors",
                active
                  ? "bg-primary/15 text-primary"
                  : "text-muted-foreground hover:bg-muted/40 hover:text-foreground"
              )}
            >
              {p.label}
            </button>
          );
        })}
      </div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={cn(
          "inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-3 py-1.5 text-xs font-medium",
          "hover:bg-muted/40",
          open && "bg-muted/40"
        )}
      >
        <Calendar className="size-3.5" />
        Custom
      </button>
      {open && (
        <div className="flex flex-wrap items-center gap-2">
          <Input
            type="date"
            value={value.from ?? ""}
            onChange={(e) => onChange({ ...value, from: e.target.value || undefined })}
            className="h-8 w-auto text-xs"
            aria-label="From date"
          />
          <span className="text-xs text-muted-foreground">to</span>
          <Input
            type="date"
            value={value.to ?? ""}
            onChange={(e) => onChange({ ...value, to: e.target.value || undefined })}
            className="h-8 w-auto text-xs"
            aria-label="To date"
          />
        </div>
      )}
    </div>
  );
}

/** Helper to convert a YYYY-MM-DD picker value to a backend-friendly
 *  ISO datetime. From-date pins to start-of-day, to-date pins to
 *  end-of-day so a single-day filter actually includes that day. */
export function toIsoFrom(date?: string): string | undefined {
  if (!date) return undefined;
  return new Date(date + "T00:00:00").toISOString();
}
export function toIsoTo(date?: string): string | undefined {
  if (!date) return undefined;
  return new Date(date + "T23:59:59.999").toISOString();
}
