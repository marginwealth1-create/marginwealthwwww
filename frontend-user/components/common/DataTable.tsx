"use client";

import { ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface Column<T> {
  key: string;
  header: string;
  render?: (row: T) => ReactNode;
  className?: string;
  width?: string;
  align?: "left" | "right" | "center";
}

interface Props<T> {
  columns: Column<T>[];
  rows: T[] | undefined;
  keyExtractor: (row: T) => string;
  loading?: boolean;
  empty?: ReactNode;
  rowClassName?: (row: T) => string | undefined;
  onRowClick?: (row: T) => void;
  /** Tailwind max-height class applied to the scrollable wrapper.
   *  Default `max-h-[70vh]` so a tall blotter scrolls internally
   *  instead of pushing the page chrome (header / tabs / wallet
   *  strip) off-screen. Pass an empty string to disable. */
  maxHeight?: string;
}

export function DataTable<T>({ columns, rows, keyExtractor, loading, empty, rowClassName, onRowClick, maxHeight = "max-h-[70vh]" }: Props<T>) {
  return (
    <div
      className={cn(
        // `overflow-y-auto` + a max-height = vertical scroll lives
        // INSIDE the table container, so the page header / tabs / wallet
        // strip stay pinned while the blotter rows scroll. `overflow-x-auto`
        // still kicks in for wide column sets on phones.
        "overflow-auto rounded-lg border border-border bg-card scrollbar-thin",
        maxHeight,
      )}
    >
      <table className="min-w-full text-sm">
        {/* `sticky top-0` keeps the column headers visible as the user
            scrolls the body. `bg-card` matches the page background so the
            row text doesn't bleed through behind the sticky header. */}
        <thead className="sticky top-0 z-10 border-b border-border bg-card text-xs uppercase text-muted-foreground shadow-[inset_0_-1px_0_0_hsl(var(--border))]">
          <tr>
            {columns.map((c) => (
              <th
                key={c.key}
                className={cn(
                  "px-3 py-2 font-medium",
                  c.align === "right" && "text-right",
                  c.align === "center" && "text-center",
                  !c.align && "text-left",
                  c.className
                )}
                style={{ width: c.width }}
              >
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {loading && (
            <tr>
              <td colSpan={columns.length} className="px-3 py-12 text-center text-muted-foreground">
                Loading…
              </td>
            </tr>
          )}
          {!loading && (!rows || rows.length === 0) && (
            <tr>
              <td colSpan={columns.length} className="px-3 py-12 text-center text-muted-foreground">
                {empty ?? "No data"}
              </td>
            </tr>
          )}
          {!loading &&
            rows?.map((row) => (
              <tr
                key={keyExtractor(row)}
                onClick={() => onRowClick?.(row)}
                className={cn(
                  "transition-colors hover:bg-muted/40",
                  onRowClick && "cursor-pointer",
                  rowClassName?.(row)
                )}
              >
                {columns.map((c) => (
                  <td
                    key={c.key}
                    // No table-wide `font-tabular` — every cell (including
                    // symbol / status / timestamp columns) was being forced
                    // into `font-variant-numeric: tabular-nums`, which gave
                    // the whole blotter that distinctive monospace-y feel
                    // the user wanted normalised. Individual columns can
                    // still opt in via `c.className` if a specific column
                    // genuinely needs digit alignment.
                    className={cn(
                      "whitespace-nowrap px-3 py-2",
                      c.align === "right" && "text-right",
                      c.align === "center" && "text-center",
                      c.className
                    )}
                  >
                    {c.render ? c.render(row) : (row as any)[c.key]}
                  </td>
                ))}
              </tr>
            ))}
        </tbody>
      </table>
    </div>
  );
}
