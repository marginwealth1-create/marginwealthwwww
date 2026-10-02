"use client";

import { useEffect, useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { TradingAPI } from "@/lib/api";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

interface RatePosition {
  id: string;
  symbol: string;
  token?: string | number;
  opened_at?: string | null;
  closed_at?: string | null;
  status?: string;
}

interface Props {
  /** Position to show rate history for. Setting to null closes the dialog. */
  position: RatePosition | null;
  onClose: () => void;
}

interface RateRow {
  timestamp: string;
  bid_high: number;
  bid_low: number;
  ask_high: number;
  ask_low: number;
}

interface RatePayload {
  token: string;
  symbol: string;
  window_from: string | null;
  window_to: string | null;
  page: number;
  page_size: number;
  total: number;
  rows: RateRow[];
}

// Rows fetched per request. We DON'T load the whole window up front — the first
// request pulls PAGE_SIZE rows and a "Load more" button appends the next
// PAGE_SIZE on demand (see useInfiniteQuery below). Keeps a wide window cheap.
const PAGE_SIZE = 20;

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

// Snapshots are stored in UTC and this is an India-markets app, so the whole
// modal works in IST explicitly — NEVER the viewer's device timezone. Relying
// on device time made a phone/emulator set to UTC send a window shifted +5:30,
// so the query looked 5.5 h ahead of the real ticks → "No recorded rates"
// even though the data existed (the reported bug).
const IST_OFFSET_MS = 5.5 * 60 * 60 * 1000;

// UTC ISO → "YYYY-MM-DDThh:mm" wall-clock in IST for <input type="datetime-local">.
function isoToLocalInput(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const ist = new Date(d.getTime() + IST_OFFSET_MS); // read UTC parts = IST wall-clock
  return `${ist.getUTCFullYear()}-${pad(ist.getUTCMonth() + 1)}-${pad(ist.getUTCDate())}T${pad(
    ist.getUTCHours(),
  )}:${pad(ist.getUTCMinutes())}`;
}

// IST wall-clock from <input type="datetime-local"> → UTC ISO for the API.
function localInputToIso(v: string): string | undefined {
  if (!v) return undefined;
  const withSecs = v.length === 16 ? `${v}:00` : v; // datetime-local omits seconds
  const d = new Date(`${withSecs}+05:30`); // interpret AS IST, not device-local
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString();
}

function fmtRowTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const date = d.toLocaleDateString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    timeZone: "Asia/Kolkata",
  });
  const time = d.toLocaleTimeString("en-IN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
    timeZone: "Asia/Kolkata",
  });
  return `${date}, ${time}`;
}

function fmtNum(n: number | null | undefined): string {
  const v = Number(n ?? 0);
  return v > 0 ? v.toFixed(2) : "—";
}

export function BidAskHistoryModal({ position, onClose }: Props) {
  const [fromInput, setFromInput] = useState("");
  const [toInput, setToInput] = useState("");
  // `applied` drives the query; the inputs only take effect on "Apply Filters".
  const [applied, setApplied] = useState<{ from?: string; to?: string }>({});

  // Re-seed the window (entry → exit, or now while OPEN) whenever a different
  // position is opened.
  useEffect(() => {
    if (!position) return;
    setFromInput(isoToLocalInput(position.opened_at));
    setToInput(isoToLocalInput(position.closed_at ?? new Date().toISOString()));
    setApplied({
      from: position.opened_at ?? undefined,
      to: position.closed_at ?? undefined,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [position?.id]);

  const {
    data,
    isLoading,
    error,
    isFetching,
    isFetchingNextPage,
    fetchNextPage,
    hasNextPage,
  } = useInfiniteQuery<RatePayload>({
    queryKey: ["admin", "position-rate-history", position?.id, applied.from, applied.to],
    queryFn: ({ pageParam }) =>
      TradingAPI.positionRateHistory(position!.id, {
        from: applied.from,
        to: applied.to,
        page: pageParam as number,
        page_size: PAGE_SIZE,
      }),
    initialPageParam: 1,
    // Next page = one past the pages we've already loaded, but ONLY while the
    // rows we hold are fewer than the server's total. Returning undefined hides
    // the "Load more" button (hasNextPage === false).
    getNextPageParam: (lastPage, allPages) => {
      const loaded = allPages.reduce((n, p) => n + (p.rows?.length ?? 0), 0);
      return loaded < (lastPage.total ?? 0) ? allPages.length + 1 : undefined;
    },
    enabled: !!position,
    staleTime: 1500,
    refetchOnWindowFocus: false,
    // Keep the tail fresh while the position is still OPEN.
    refetchInterval: position?.status === "OPEN" ? 15000 : false,
  });

  function applyFilters() {
    // Changing `applied` swaps the queryKey → the accumulator resets to page 1.
    setApplied({ from: localInputToIso(fromInput), to: localInputToIso(toInput) });
  }
  function resetFilters() {
    setFromInput(isoToLocalInput(position?.opened_at));
    setToInput(isoToLocalInput(position?.closed_at ?? new Date().toISOString()));
    setApplied({
      from: position?.opened_at ?? undefined,
      to: position?.closed_at ?? undefined,
    });
  }

  const rows = data?.pages.flatMap((p) => p.rows ?? []) ?? [];
  const total = data?.pages[0]?.total ?? 0;

  return (
    <Dialog open={!!position} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-3xl w-[95vw] max-h-[88vh] overflow-y-auto p-0">
        <DialogHeader className="px-4 pb-3 pt-4 sm:px-5 sm:pt-5">
          <DialogTitle className="text-base font-semibold">
            Bid/Ask History{" "}
            <span className="font-normal text-muted-foreground">
              — {position?.symbol || data?.pages[0]?.symbol || ""}
            </span>
          </DialogTitle>
        </DialogHeader>

        <div className="px-4 pb-5 sm:px-5">
          {/* ── Time-range filter ─────────────────────────────────── */}
          <div className="mb-2 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="block">
              <span className="mb-1 block text-[10px] uppercase tracking-wider text-muted-foreground">
                From
              </span>
              <input
                type="datetime-local"
                value={fromInput}
                onChange={(e) => setFromInput(e.target.value)}
                className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-[10px] uppercase tracking-wider text-muted-foreground">
                To
              </span>
              <input
                type="datetime-local"
                value={toInput}
                onChange={(e) => setToInput(e.target.value)}
                className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm"
              />
            </label>
          </div>
          <div className="mb-3 flex justify-end gap-2">
            <Button variant="outline" size="sm" onClick={resetFilters}>
              Reset
            </Button>
            <Button size="sm" onClick={applyFilters}>
              Apply Filters
            </Button>
          </div>

          {error && (
            <div className="mb-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {(error as any)?.message || "Failed to load rate history"}
            </div>
          )}

          {isLoading ? (
            <div className="rounded-md border border-border px-3 py-6 text-center text-xs text-muted-foreground">
              Loading…
            </div>
          ) : rows.length === 0 ? (
            <div className="rounded-md border border-border px-3 py-6 text-center text-xs text-muted-foreground">
              No recorded rates for this window yet. Bid/Ask history is captured
              live going forward — a position from before it started, or one with
              no live ticks, has nothing to show.
            </div>
          ) : (
            <>
              {/* ── Desktop / tablet: table ──────────────────────────── */}
              <div className="hidden overflow-x-auto rounded-md border border-border sm:block">
                <table className="min-w-full text-xs">
                  <thead className="bg-muted/30 text-muted-foreground">
                    <tr>
                      <th className="px-3 py-2 text-left">Date &amp; Time</th>
                      <th className="px-3 py-2 text-right">Bid High</th>
                      <th className="px-3 py-2 text-right">Bid Low</th>
                      <th className="px-3 py-2 text-right">Ask High</th>
                      <th className="px-3 py-2 text-right">Ask Low</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {rows.map((r, i) => (
                      <tr key={`${r.timestamp}-${i}`}>
                        <td className="whitespace-nowrap px-3 py-2 text-muted-foreground">
                          {fmtRowTime(r.timestamp)}
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums text-sky-500">
                          {fmtNum(r.bid_high)}
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums text-sky-500">
                          {fmtNum(r.bid_low)}
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums text-red-500">
                          {fmtNum(r.ask_high)}
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums text-red-500">
                          {fmtNum(r.ask_low)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {/* ── Mobile: one card per minute (no horizontal scroll) ── */}
              <div className="space-y-2 sm:hidden">
                {rows.map((r, i) => (
                  <div
                    key={`m-${r.timestamp}-${i}`}
                    className="rounded-lg border border-border bg-muted/20 p-3"
                  >
                    <div className="mb-2 text-xs font-medium text-foreground">
                      {fmtRowTime(r.timestamp)}
                    </div>
                    <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
                      <div className="flex items-center justify-between">
                        <span className="text-muted-foreground">Bid High</span>
                        <span className="tabular-nums text-sky-500">
                          {fmtNum(r.bid_high)}
                        </span>
                      </div>
                      <div className="flex items-center justify-between">
                        <span className="text-muted-foreground">Ask High</span>
                        <span className="tabular-nums text-red-500">
                          {fmtNum(r.ask_high)}
                        </span>
                      </div>
                      <div className="flex items-center justify-between">
                        <span className="text-muted-foreground">Bid Low</span>
                        <span className="tabular-nums text-sky-500">
                          {fmtNum(r.bid_low)}
                        </span>
                      </div>
                      <div className="flex items-center justify-between">
                        <span className="text-muted-foreground">Ask Low</span>
                        <span className="tabular-nums text-red-500">
                          {fmtNum(r.ask_low)}
                        </span>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}

          {/* ── Load-more + count ─────────────────────────────────── */}
          {rows.length > 0 && (
            <div className="mt-3 flex flex-col items-center gap-2">
              {hasNextPage && (
                <Button
                  variant="outline"
                  size="sm"
                  className="w-full sm:w-auto"
                  disabled={isFetchingNextPage}
                  onClick={() => fetchNextPage()}
                >
                  {isFetchingNextPage ? "Loading…" : "Load more"}
                </Button>
              )}
              <span className="text-xs text-muted-foreground">
                Showing {rows.length} of {total} minute{total !== 1 ? "s" : ""}
                {isFetching && !isFetchingNextPage ? " · refreshing…" : ""}
              </span>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
