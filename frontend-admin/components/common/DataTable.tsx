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
   *  Default `max-h-[70vh]` so a long admin blotter scrolls internally
   *  instead of pushing the PageHeader / filter row off-screen. Pass
   *  an empty string to disable. */
  maxHeight?: string;
  /** Table width class. Default `min-w-full` stretches the table to fill the
   *  card, which JUSTIFIES columns and leaves big gaps when the content is
   *  narrower than the card. Pass `w-max` to size the table to its content
   *  (tight columns, horizontal scroll only when it overflows). */
  tableClassName?: string;
}

export function DataTable<T>({ columns, rows, keyExtractor, loading, empty, rowClassName, onRowClick, maxHeight = "max-h-[70vh]", tableClassName = "min-w-full" }: Props<T>) {
  return (
    <div
      className={cn(
        // Vertical scroll lives inside the table container so the admin
        // page header + filters stay pinned. Horizontal scroll still
        // kicks in for wide column sets on smaller screens.
        "overflow-auto rounded-lg border border-border bg-card",
        maxHeight,
      )}
    >
      <table className={cn("text-sm", tableClassName)}>
        {/* Sticky header — column labels stay visible while rows scroll. */}
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
                    className={cn(
                      "whitespace-nowrap px-3 py-2 font-tabular",
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
