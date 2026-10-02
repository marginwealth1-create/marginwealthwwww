"use client";

import { Button } from "@/components/ui/button";

interface PaginationProps {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
  onPageSizeChange?: (size: number) => void;
  pageSizeOptions?: number[];
}

export function Pagination({
  page,
  pageSize,
  total,
  onPageChange,
  onPageSizeChange,
  pageSizeOptions = [25, 50, 100, 200],
}: PaginationProps) {
  if (total <= 0) return null;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const start = (page - 1) * pageSize + 1;
  const end = Math.min(page * pageSize, total);

  return (
    // Mobile (<sm) lays out as: "Showing X–Y of N" full-width, then
    // a wrap-friendly button row underneath. First / Last hide on
    // phones (`hidden sm:inline-flex`) because they always sit next
    // to Prev / Next + the page count, and on a 360px screen they
    // were pushing Last off the right edge (operator screenshot).
    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
      <div className="basis-full sm:basis-auto">
        Showing{" "}
        <span className="font-medium text-foreground">
          {start}–{end}
        </span>{" "}
        of <span className="font-medium text-foreground">{total}</span>
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        {onPageSizeChange && (
          <label className="flex items-center gap-1.5">
            <span>Rows</span>
            <select
              value={pageSize}
              onChange={(e) => {
                onPageChange(1);
                onPageSizeChange(Number(e.target.value));
              }}
              className="h-8 rounded-md border border-border bg-background px-2 text-xs"
            >
              {pageSizeOptions.map((opt) => (
                <option key={opt} value={opt}>
                  {opt}
                </option>
              ))}
            </select>
          </label>
        )}
        <span className="whitespace-nowrap">
          Page {page} of {totalPages}
        </span>
        <Button
          variant="outline"
          size="sm"
          className="hidden sm:inline-flex"
          disabled={page <= 1}
          onClick={() => onPageChange(1)}
        >
          First
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={page <= 1}
          onClick={() => onPageChange(page - 1)}
        >
          Prev
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={page >= totalPages}
          onClick={() => onPageChange(page + 1)}
        >
          Next
        </Button>
        <Button
          variant="outline"
          size="sm"
          className="hidden sm:inline-flex"
          disabled={page >= totalPages}
          onClick={() => onPageChange(totalPages)}
        >
          Last
        </Button>
      </div>
    </div>
  );
}
