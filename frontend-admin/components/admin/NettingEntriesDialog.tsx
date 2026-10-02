"use client";

import { useQuery } from "@tanstack/react-query";
import { TradingAPI } from "@/lib/api";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { cn, formatINR } from "@/lib/utils";

interface Props {
  /** Position id to drill into. Setting to null closes the dialog. */
  positionId: string | null;
  onClose: () => void;
}

interface NettingEntry {
  row: number;
  type: "Entry" | "Exit";
  side: "BUY" | "SELL";
  executed_at: string | null;
  volume: number;
  price: number;
  pnl_inr: number | null;
}

interface NettingPayload {
  position_id: string;
  symbol: string;
  exchange: string;
  token: string;
  status: string;
  side: "BUY" | "SELL";
  volume: number;
  avg_entry: number;
  current_price: number;
  total_pnl: number;
  avg_calc_formula: string;
  entries: NettingEntry[];
}

function pnlClass(n: number | null | undefined): string {
  const v = Number(n ?? 0);
  if (v > 0) return "text-emerald-500";
  if (v < 0) return "text-red-500";
  return "text-foreground";
}

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const date = d.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
  const time = d
    .toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: true })
    .toLowerCase();
  return `${date}, ${time}`;
}

export function NettingEntriesDialog({ positionId, onClose }: Props) {
  const { data, isLoading, error } = useQuery<NettingPayload>({
    queryKey: ["admin", "position-netting", positionId],
    queryFn: () => TradingAPI.positionNetting(positionId!),
    enabled: !!positionId,
    // Re-poll while OPEN so the Current price + Total P/L stay live.
    refetchInterval: (q) =>
      q.state.data?.status === "OPEN" ? 3000 : false,
    staleTime: 1500,
    refetchOnWindowFocus: false,
  });

  const isClosed = data?.status === "CLOSED";

  // ── Exit-side stats (used for CLOSED positions) ───────────────────────
  const exits = data?.entries.filter((e) => e.type === "Exit") ?? [];
  const totalExitVol = exits.reduce((s, e) => s + e.volume, 0);
  const avgExitPrice =
    totalExitVol > 0
      ? exits.reduce((s, e) => s + e.price * e.volume, 0) / totalExitVol
      : null;
  const avgExitFormula =
    exits.length > 1 && avgExitPrice !== null
      ? `(${exits
          .map((e) => `${e.volume}×₹${e.price.toFixed(2)}`)
          .join(" + ")}) ÷ ${totalExitVol} = ₹${avgExitPrice.toFixed(2)}`
      : null;

  // ── P/L to display ────────────────────────────────────────────────────
  // CLOSED: sum exit-row pnl_inr (net, brokerage already deducted).
  // OPEN:   backend unrealized_pnl (gross is correct — no closing brokerage yet).
  const displayPnl = data
    ? isClosed
      ? data.entries.reduce((sum, e) => sum + (e.pnl_inr ?? 0), 0)
      : data.total_pnl
    : 0;

  return (
    <Dialog open={!!positionId} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-3xl p-0">
        <DialogHeader className="px-5 pb-3 pt-5">
          <DialogTitle className="text-base font-semibold">
            Netting Entries — {data?.exchange || "—"}{" "}
            <span className="text-muted-foreground font-normal">
              ({data?.token || ""})
            </span>
            {isClosed && (
              <span className="ml-2 rounded-full bg-muted/40 px-2 py-0.5 text-[10px] font-normal text-muted-foreground uppercase tracking-wider">
                Closed
              </span>
            )}
          </DialogTitle>
        </DialogHeader>

        {isLoading && !data && (
          <div className="grid h-32 place-items-center text-sm text-muted-foreground">
            Loading…
          </div>
        )}
        {error && (
          <div className="mx-5 mb-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
            {(error as any)?.message || "Failed to load netting entries"}
          </div>
        )}

        {data && (
          <div className="px-5 pb-5">
            {/* ── Header summary tile ─────────────────────────────── */}
            <div className="mb-3 rounded-md border border-border bg-muted/10 px-4 py-3">
              <div className="grid grid-cols-2 gap-y-2 sm:grid-cols-5">
                <div>
                  <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                    Side
                  </div>
                  <div
                    className={cn(
                      "mt-0.5 text-sm font-semibold",
                      data.side === "BUY" ? "text-emerald-500" : "text-red-500"
                    )}
                  >
                    {data.side}
                  </div>
                </div>

                <div>
                  <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                    Volume
                  </div>
                  <div className="mt-0.5 text-sm font-semibold tabular-nums">
                    {data.volume}
                  </div>
                </div>

                <div>
                  <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                    Avg Entry
                  </div>
                  <div className="mt-0.5 text-sm font-semibold tabular-nums">
                    ₹{data.avg_entry.toFixed(2)}
                  </div>
                </div>

                {/* 4th tile: CLOSED → Avg Exit (explains profit/loss),
                            OPEN  → Current (live LTP) */}
                <div>
                  {isClosed ? (
                    <>
                      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                        Avg Exit
                      </div>
                      <div className="mt-0.5 text-sm font-semibold tabular-nums">
                        {avgExitPrice !== null
                          ? `₹${avgExitPrice.toFixed(2)}`
                          : "—"}
                      </div>
                    </>
                  ) : (
                    <>
                      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                        Current
                      </div>
                      <div className="mt-0.5 text-sm font-semibold tabular-nums">
                        ₹{data.current_price.toFixed(2)}
                      </div>
                    </>
                  )}
                </div>

                {/* 5th tile: CLOSED → Realized P/L (net),
                            OPEN  → Total P/L (unrealized) */}
                <div>
                  <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                    {isClosed ? "Realized P/L" : "Total P/L"}
                  </div>
                  <div
                    className={cn(
                      "mt-0.5 text-sm font-semibold tabular-nums",
                      pnlClass(displayPnl)
                    )}
                  >
                    {formatINR(displayPnl)}
                  </div>
                </div>
              </div>

              {/* For CLOSED: show quick math so no one is confused why
                  avg-entry ≠ avg-exit yet still profitable/loss. */}
              {isClosed && avgExitPrice !== null && (
                <div className="mt-2 border-t border-border/50 pt-2 text-[11px] text-muted-foreground">
                  Avg Entry{" "}
                  <span className="font-medium text-foreground">
                    ₹{data.avg_entry.toFixed(2)}
                  </span>{" "}
                  →{" "}
                  {data.side === "BUY" ? (
                    avgExitPrice > data.avg_entry ? (
                      <>
                        Avg Exit{" "}
                        <span className="font-medium text-emerald-500">
                          ₹{avgExitPrice.toFixed(2)}
                        </span>{" "}
                        <span className="text-emerald-500">
                          (exits above entry avg → net profit)
                        </span>
                      </>
                    ) : (
                      <>
                        Avg Exit{" "}
                        <span className="font-medium text-red-500">
                          ₹{avgExitPrice.toFixed(2)}
                        </span>{" "}
                        <span className="text-red-500">
                          (exits below entry avg → net loss)
                        </span>
                      </>
                    )
                  ) : avgExitPrice < data.avg_entry ? (
                    <>
                      Avg Exit{" "}
                      <span className="font-medium text-emerald-500">
                        ₹{avgExitPrice.toFixed(2)}
                      </span>{" "}
                      <span className="text-emerald-500">
                        (exits below entry avg → net profit)
                      </span>
                    </>
                  ) : (
                    <>
                      Avg Exit{" "}
                      <span className="font-medium text-red-500">
                        ₹{avgExitPrice.toFixed(2)}
                      </span>{" "}
                      <span className="text-red-500">
                        (exits above entry avg → net loss)
                      </span>
                    </>
                  )}
                </div>
              )}
            </div>

            {/* ── Per-fill table ──────────────────────────────────── */}
            <div className="overflow-x-auto rounded-md border border-border">
              <table className="min-w-full text-xs">
                <thead className="bg-muted/30 text-muted-foreground">
                  <tr>
                    <th className="px-3 py-2 text-left">#</th>
                    <th className="px-3 py-2 text-left">Type</th>
                    <th className="px-3 py-2 text-left">Side</th>
                    <th className="px-3 py-2 text-left">Time</th>
                    <th className="px-3 py-2 text-right">Volume</th>
                    <th className="px-3 py-2 text-right">Price</th>
                    <th className="px-3 py-2 text-right">P/L</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {data.entries.length === 0 ? (
                    <tr>
                      <td
                        colSpan={7}
                        className="px-3 py-6 text-center text-muted-foreground"
                      >
                        No fills recorded for this position.
                      </td>
                    </tr>
                  ) : (
                    <>
                      {data.entries.map((e) => (
                        <tr key={e.row}>
                          <td className="px-3 py-2 tabular-nums">{e.row}</td>
                          <td
                            className={cn(
                              "px-3 py-2 font-medium",
                              e.type === "Entry"
                                ? "text-emerald-500"
                                : "text-red-500"
                            )}
                          >
                            {e.type}
                          </td>
                          <td
                            className={cn(
                              "px-3 py-2 font-semibold",
                              e.side === "BUY"
                                ? "text-emerald-500"
                                : "text-red-500"
                            )}
                          >
                            {e.side}
                          </td>
                          <td className="px-3 py-2 text-muted-foreground">
                            {fmtTime(e.executed_at)}
                          </td>
                          <td className="px-3 py-2 text-right tabular-nums">
                            {e.volume}
                          </td>
                          <td className="px-3 py-2 text-right tabular-nums">
                            ₹{e.price.toFixed(2)}
                          </td>
                          <td
                            className={cn(
                              "px-3 py-2 text-right tabular-nums",
                              pnlClass(e.pnl_inr)
                            )}
                          >
                            {e.pnl_inr == null ? "—" : formatINR(e.pnl_inr)}
                          </td>
                        </tr>
                      ))}

                      {/* Exit totals summary row — only when there are exits */}
                      {exits.length > 0 && (
                        <tr className="bg-muted/20 font-medium text-muted-foreground">
                          <td colSpan={4} className="px-3 py-2 text-xs">
                            {exits.length} exit{exits.length > 1 ? "s" : ""} total
                          </td>
                          <td className="px-3 py-2 text-right tabular-nums">
                            {totalExitVol}
                          </td>
                          <td className="px-3 py-2 text-right tabular-nums">
                            {avgExitPrice !== null
                              ? `₹${avgExitPrice.toFixed(2)} avg`
                              : "—"}
                          </td>
                          <td
                            className={cn(
                              "px-3 py-2 text-right tabular-nums",
                              pnlClass(displayPnl)
                            )}
                          >
                            {formatINR(displayPnl)}
                          </td>
                        </tr>
                      )}
                    </>
                  )}
                </tbody>
              </table>
            </div>

            {/* ── Formula boxes ───────────────────────────────────── */}
            <div className="mt-3 space-y-2">
              {data.entries.some((e) => e.type === "Entry") && (
                <div className="rounded-md border border-border bg-muted/10 px-3 py-2 text-[11px] leading-relaxed">
                  <span className="font-medium text-amber-500">
                    Avg Entry Calculation:
                  </span>{" "}
                  <span className="break-words text-foreground/90">
                    {data.avg_calc_formula}
                  </span>
                </div>
              )}

              {avgExitFormula && (
                <div className="rounded-md border border-border bg-muted/10 px-3 py-2 text-[11px] leading-relaxed">
                  <span className="font-medium text-sky-500">
                    Avg Exit Calculation:
                  </span>{" "}
                  <span className="break-words text-foreground/90">
                    {avgExitFormula}
                  </span>
                </div>
              )}
            </div>

            <div className="mt-4 flex justify-end">
              <Button variant="outline" size="sm" onClick={onClose}>
                Close
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
