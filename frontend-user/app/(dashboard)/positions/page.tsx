"use client";

import { useEffect, useMemo, useState } from "react";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Inbox, Layers, Lock, LogOut, Pencil, Shield, Target, X } from "lucide-react";
import { OrderAPI, PositionAPI, WalletAPI } from "@/lib/api";
import { resolveRealPositionId as resolveRealPositionIdShared } from "@/lib/positionIds";
import { useMarketStream } from "@/lib/useMarketStream";
import { usePriceFlash } from "@/lib/usePriceFlash";
import { isInstrumentMarketOpen } from "@/lib/marketHours";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ConfirmDialog } from "@/components/common/ConfirmDialog";
import { DataTable, type Column } from "@/components/common/DataTable";

import { StatusPill } from "@/components/common/StatusPill";
import { TradeDetailSheet } from "@/components/trading/TradeDetailSheet";
import { cn, formatINR, formatIST, formatPrice, pnlColor } from "@/lib/utils";

// Unified blotter tabs: Position (open) / Active (per-fill) / Closed
// (today's realised) / Cancelled (orders) / Rejected (orders). Replaces
// the separate /orders page so the trader sees every state in one row.
type TabKey =
  | "position"
  | "active"
  | "pending"
  | "closed"
  | "cancelled"
  | "rejected";

/** Bare-number price formatter — no ₹ / $ prefix on any instrument
 *  price (LTP / bid / ask / avg_price / close). Forex pairs render with
 *  4 decimals, everything else with 2. `quote` is still accepted for
 *  call-site compatibility but ignored (the previous USD/INR branch is
 *  gone now that prices render uniformly). */
function fmtFeedPrice(
  value: string | number | null | undefined,
  _quote?: string,
  segment?: string,
  exchange?: string,
) {
  return formatPrice(value, segment, exchange);
}

/**
 * Extract a readable expiry chip from an NSE/MCX derivative symbol,
 * including the full 4-digit year so a position that carries into the
 * next calendar year is unambiguous on screen.
 *
 *  • Monthly (alphabetic month):   `CRUDEOIL26JUNFUT` / `NIFTY26JUN23700PE`
 *                                  → "26 JUN 2026"
 *  • Weekly NIFTY/BANKNIFTY etc:   `NIFTY2651923800PE` (YY=26, M=5,
 *                                  D=19, strike, side) → "19 MAY 2026"
 *  • Weekly Oct/Nov/Dec:           NSE encodes those single chars as
 *                                  "O" / "N" / "D" in the month slot —
 *                                  same parser handles that path.
 *
 * Returns null for spot stocks, indices, or anything that doesn't match
 * the two encodings above. Operator request: "date show kar, actual
 * data kya hai expiry ka woh dikha" — earlier the chip dropped the
 * year and you couldn't tell a Jun-26 contract from a Jun-27 one.
 */
function extractExpiryLabel(symbol: string | null | undefined): string | null {
  if (!symbol) return null;
  const sym = String(symbol).toUpperCase();
  const MONTH_NAMES = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                       "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
  // YY → 4-digit year. NSE / MCX symbols carry the 2-digit form
  // (`26` for 2026); 20xx is the only century in scope here.
  const fullYear = (yy: string) => `20${yy}`;

  // Monthly pattern — `<UNDERLYING><YY><MMM>...`. The regex anchors on
  // the YY+MMM tuple so it works for any underlying prefix and for both
  // FUT (CRUDEOIL26JUNFUT) and option (NIFTY26JUN23700PE) suffixes.
  const monthly =
    /(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)/.exec(sym);
  if (monthly) return `${monthly[1]} ${monthly[2]} ${fullYear(monthly[1])}`;

  // Weekly numeric pattern — `<ALPHA><YY><M><DD>...<CE|PE>` where M is
  // a single char: 1-9 for Jan-Sep, then O/N/D for Oct/Nov/Dec. So
  // NIFTY2651923800PE = YY 26, M 5, DD 19, strike 23800, PE.
  const weekly =
    /^[A-Z]+(\d{2})([1-9OND])(\d{2})\d+(?:CE|PE)$/.exec(sym);
  if (weekly) {
    const yy = weekly[1];
    const mChar = weekly[2];
    const dd = weekly[3];
    let monthIdx: number;
    if (/^[1-9]$/.test(mChar)) {
      monthIdx = parseInt(mChar, 10) - 1;       // "5" → May (index 4)
    } else if (mChar === "O") {
      monthIdx = 9;                              // Oct
    } else if (mChar === "N") {
      monthIdx = 10;                             // Nov
    } else {
      monthIdx = 11;                             // Dec
    }
    if (monthIdx < 0 || monthIdx > 11) return null;
    return `${dd} ${MONTH_NAMES[monthIdx]} ${fullYear(yy)}`;
  }

  return null;
}

/** Format the REAL contract expiry (backend-supplied ISO `YYYY-MM-DD`) as
 *  `4 SEP 2026`. Preferred over `extractExpiryLabel` — the symbol encodes the
 *  YEAR ("26") not the day for monthly contracts, so parsing it showed the
 *  wrong day (e.g. SILVER26SEPFUT → "26 SEP" when the real expiry is 4 SEP). */
function formatRealExpiry(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso));
  if (!m) return null;
  const MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                  "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
  const day = parseInt(m[3], 10);
  const monIdx = parseInt(m[2], 10) - 1;
  if (monIdx < 0 || monIdx > 11) return null;
  return `${day} ${MONTHS[monIdx]} ${m[1]}`;
}

/** Expiry chip preferring the backend's real expiry date, falling back to
 *  the (best-effort) symbol parse only when the row carries no expiry. */
function expiryChip(row: any): string | null {
  return formatRealExpiry(row?.expiry) ?? extractExpiryLabel(row?.symbol);
}

// Compact pill that translates Position.close_reason into a human label
// with a tone-matching color. Same legal set as
// marginwealth_ind/backend/app/models/position.py:close_reason.
const CLOSE_REASON_META: Record<
  string,
  { label: string; cls: string }
> = {
  USER: { label: "User", cls: "bg-brand-500/10 text-brand-400 ring-brand-500/30" },
  SL_HIT: {
    label: "Stop Loss",
    cls: "bg-sell/10 text-sell ring-sell/30",
  },
  TP_HIT: { label: "Target", cls: "bg-buy/10 text-buy ring-buy/30" },
  STOP_OUT: {
    label: "Stop-out",
    cls: "bg-amber-500/10 text-amber-400 ring-amber-500/30",
  },
  // Carry-forward failure — wallet couldn't cover the overnight margin
  // at EOD rollover, so the platform flattened MIS at market before
  // converting to NRML. Distinct chip so the user can tell this apart
  // from a stop-out (which is a live margin breach during the day).
  CARRY_FORWARD_FAIL: {
    label: "CF · Insufficient funds",
    cls: "bg-amber-500/10 text-amber-400 ring-amber-500/30",
  },
  // Segment doesn't allow overnight at all (intraday-only product).
  EOD_OVERNIGHT_DISABLED: {
    label: "EOD · Intraday only",
    cls: "bg-amber-500/10 text-amber-400 ring-amber-500/30",
  },
  // User left the per-position Carry-Forward toggle OFF, so the EOD rollover
  // squared it off instead of carrying it overnight.
  CARRY_FORWARD_OFF: {
    label: "EOD · Not carried",
    cls: "bg-amber-500/10 text-amber-400 ring-amber-500/30",
  },
  ADMIN_CLOSE: {
    label: "Admin Close",
    cls: "bg-purple-500/10 text-purple-400 ring-purple-500/30",
  },
  // Saturday mark-to-market settlement — position booked + re-opened at the
  // settlement price. Violet so it's clearly a system event, not a user close.
  WEEKLY_SETTLEMENT: {
    label: "Weekly Settle",
    cls: "bg-violet-500/15 text-violet-400 ring-violet-500/40",
  },
  // Contract expired → settled at its clearing price (terminal). Teal, distinct
  // from the amber stop-out / carry-forward chips.
  EXPIRY_SETTLED: {
    label: "Expiry Settled",
    cls: "bg-teal-500/15 text-teal-400 ring-teal-500/40",
  },
  AUTO: {
    label: "Auto",
    cls: "bg-muted/40 text-muted-foreground ring-border",
  },
};

// Format a position/order quantity for display, killing floating-point noise.
// qty is computed as lots × lot_size, and a qty→lots→qty roundtrip on
// fractional lots leaves garbage like 65.00999999999999 (really 65.01, and the
// .01 is itself accumulated lot-rounding error → the user meant 65). We round
// to 3 dp, then SNAP values ≥1 that sit within 0.02 of an integer back to the
// integer (futures qty is whole). Genuine sub-1 crypto/forex sizes (0.001, 0.5)
// are left untouched so they don't collapse to 0.
function fmtQty(q: any): string {
  const n = Math.abs(Number(q) || 0);
  if (n === 0) return "0";
  const r = Math.round(n * 1000) / 1000;
  const nearest = Math.round(r);
  if (n >= 1 && Math.abs(r - nearest) < 0.02) return String(nearest);
  return String(r);
}

// Overnight (carry-forward) margin requirement for a single position or
// active-trade row. PREFERS the backend-computed `holding_margin` value
// (resolved per-position against the user's effective overnight margin
// settings — MCX FUT 70× / NSE OPT 100% / Fixed-per-lot, whatever the
// admin matrix said). The old client-side `intraday × 1.4` heuristic
// was a guess that matched NSE equity but was wildly wrong on MCX
// (operator's CRUDEOIL row showed ₹2,648 instead of ₹13,511).
// Falls back to the locked intraday margin if the backend hasn't
// stamped a value yet (stale cached payloads from before the upgrade).
function holdingMarginFor(row: any): number {
  const stamped = row?.holding_margin;
  if (stamped !== null && stamped !== undefined && stamped !== "") {
    const n = Number(stamped);
    if (Number.isFinite(n) && n > 0) return n;
  }
  return Number(row?.margin_used ?? row?.margin ?? row?.used_margin ?? 0);
}

function CloseReasonChip({ reason }: { reason?: string | null }) {
  if (!reason)
    return <span className="text-muted-foreground/60 text-xs">—</span>;
  const meta = CLOSE_REASON_META[reason] ?? {
    label: reason,
    cls: "bg-muted/40 text-muted-foreground ring-border",
  };
  return (
    <span
      className={cn(
        "inline-flex items-center rounded px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide ring-1 ring-inset",
        meta.cls,
      )}
    >
      {meta.label}
    </span>
  );
}

export default function PositionsPage() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<TabKey>("position");
  // `source` discriminates between the two tabs that share the same edit
  // dialog. Active-trade rows hit the per-trade SL/TP endpoint by their
  // own trade id; position rows hit the per-position endpoint by the
  // position id. Without this tag the dialog used to blindly route every
  // mobile click through the active-trade endpoint, so editing SL/TP
  // from the Position tab on mobile failed with "Trade not found".
  const [editing, setEditing] = useState<
    { row: any; kind: "TP" | "SL"; source: "position" | "active" } | null
  >(null);
  // Slide-up trade card token — mobile-only. When the user taps any
  // position / active-trade card on a phone, open the same
  // TradeDetailSheet used by /marketwatch and /option-chain so they
  // can place a new BUY/SELL on the same instrument without leaving
  // the Positions page. User-flagged: "potion page me kisi bhi
  // potion ko click karne par bhi same buy/sell ke liye card open
  // ho jaisa option chain me kiya hai".
  const [sheetToken, setSheetToken] = useState<string | null>(null);
  // Pending-order edit dialog state. Set when the user taps the pencil
  // on a pending order card; cleared on cancel / successful save.
  const [editingPending, setEditingPending] = useState<any | null>(null);
  // Confirmation dialog for the header "Square off all" action. Opens on
  // tap (mobile + desktop); the actual squareoff fires only on confirm.
  const [confirmAllOpen, setConfirmAllOpen] = useState(false);
  // Row pending the themed single-close confirmation card (desktop only).
  const [closeRow, setCloseRow] = useState<any | null>(null);

  // Close-button entry point. Mobile fires immediately (no popup, per user
  // spec); desktop opens the themed confirmation card instead of the old
  // native confirm().
  function requestClose(r: any) {
    const isMobileUi =
      typeof window !== "undefined" &&
      window.matchMedia("(max-width: 767px)").matches;
    if (isMobileUi) {
      void squareoff(r.id);
      return;
    }
    setCloseRow(r);
  }

  // ── Data ────────────────────────────────────────────────────────────
  // Open positions are always fetched so the tab badge stays current and
  // the Active-tab margin breakdown has fresh used_margin to sum.
  const { data: open, isFetching: openLoading } = useQuery<any[]>({
    queryKey: ["positions", "open"],
    queryFn: () => PositionAPI.open(),
    // Fast 2 s poll. Prod Mongo is single-node (immediate read consistency,
    // NO Atlas replica lag), so the old dataUpdatedAt "pause poll for 3.5 s"
    // dance just made open/close reflect slowly for no benefit. The
    // open/close handlers cancelQueries on tap + fire a short-delay
    // invalidate post-commit, so a fast poll can't resurrect a closed row.
    refetchInterval: 2000,
    // Brief freshness window so an optimistic row written by the trade sheet
    // survives the on-navigate mount without an immediate wipe.
    staleTime: 1500,
  });
  const {
    data: closedPages,
    isFetching: closedLoading,
    fetchNextPage: fetchMoreClosed,
    hasNextPage: hasMoreClosed,
    isFetchingNextPage: loadingMoreClosed,
  } = useInfiniteQuery<{ items: any[]; total: number }>({
    queryKey: ["positions", "closed"],
    queryFn: ({ pageParam }) => PositionAPI.closed(pageParam as number, 25),
    initialPageParam: 1,
    getNextPageParam: (lastPage, allPages) =>
      lastPage.items.length === 25 ? allPages.length + 1 : undefined,
    staleTime: 0,
    enabled: tab === "closed",
  });
  const closed: any[] = closedPages?.pages.flatMap((p) => p.items) ?? [];
  const closedTotal: number = closedPages?.pages[0]?.total ?? 0;
  // Active trades poll in the BACKGROUND (not gated on the active tab) so:
  //   1. the Active badge count is correct before you open the tab, and
  //   2. switching to Active shows the already-warm list instantly instead
  //      of a cold fetch that — with Atlas read-replica lag — left the tab
  //      blank for 6-7 s after placing a trade (user-flagged). A
  //      freshly-placed trade now lands within one 3 s poll, same cadence
  //      as the open-positions list, so Active feels as live as Position.
  const { data: activeTrades, isFetching: activeLoading } = useQuery<any[]>({
    queryKey: ["positions", "active-trades"],
    queryFn: () => PositionAPI.activeTrades(),
    // Fast 2 s poll — see the open-positions query above (single-node Mongo,
    // so no replica-lag flicker to pause around).
    refetchInterval: 2000,
    staleTime: 1500,
  });
  // pnlSummary is used for live USD/INR + wallet strip; always polled
  // (cheap, single endpoint, 5 s) so the header tracker stays current
  // regardless of which tab is open.
  const { data: pnlSummary } = useQuery<any>({
    queryKey: ["positions", "pnl-summary"],
    queryFn: () => PositionAPI.pnlSummary(),
    refetchInterval: 5000,
  });

  // Cancelled / Rejected feed orders, not positions — fold them in here
  // so the user has one unified blotter for every order state. Lazy
  // (only fetched when the tab is selected) and a slow 10 s poll since
  // cancelled/rejected don't change live.
  const { data: cancelled, isFetching: cancelledLoading } = useQuery<any[]>({
    queryKey: ["orders", "CANCELLED"],
    queryFn: () => OrderAPI.list("CANCELLED") as Promise<any[]>,
    refetchInterval: 10000,
    enabled: tab === "cancelled",
  });
  // Pending = OPEN / PENDING / TRIGGERED orders (limit / SL-M waiting
  // for the matching engine to fire). Filtered client-side from the
  // unfiltered /orders list because the backend's status filter
  // accepts only one status at a time while pending here spans 3
  // states. Fast poll (3 s) so newly-placed pending orders appear
  // promptly + cancels reflect immediately.
  const { data: pendingRaw, isFetching: pendingLoading } = useQuery<any[]>({
    queryKey: ["orders", "PENDING-LIKE"],
    queryFn: () => OrderAPI.list() as Promise<any[]>,
    refetchInterval: 3000,
    enabled: tab === "pending",
  });
  const pending = useMemo(
    () =>
      (pendingRaw ?? []).filter((o: any) =>
        ["PENDING", "OPEN", "TRIGGERED"].includes(
          String(o?.status ?? "").toUpperCase(),
        ),
      ),
    [pendingRaw],
  );
  const { data: rejected, isFetching: rejectedLoading } = useQuery<any[]>({
    queryKey: ["orders", "REJECTED"],
    queryFn: () => OrderAPI.list("REJECTED") as Promise<any[]>,
    refetchInterval: 10000,
    enabled: tab === "rejected",
  });

  // Wallet snapshot for the top status strip.
  const { data: wallet } = useQuery<any>({
    queryKey: ["wallet", "summary"],
    queryFn: () => WalletAPI.summary(),
    refetchInterval: 10000,
    staleTime: 5000,
  });

  // ── Counts ──────────────────────────────────────────────────────────
  const counts = {
    closed: closedTotal || closed?.length || 0,
    position: open?.length ?? 0,
    active: activeTrades?.length ?? 0,
    pending: pending?.length ?? 0,
    cancelled: cancelled?.length ?? 0,
    rejected: rejected?.length ?? 0,
  };

  // ── Live WS overlay ─────────────────────────────────────────────────
  // Subscribe to /ws/marketdata for every open-position / active-trade
  // token so the "Current" column updates tick-to-tick (250 ms) just
  // like the trading-terminal positions tab. Without this the table
  // only refreshes when the 3 s REST poll fires, so a fast-moving
  // instrument's LTP looks frozen between polls.
  const streamTokens = useMemo<string[]>(() => {
    const set = new Set<string>();
    for (const p of open ?? []) {
      const t = String(p?.instrument_token ?? "");
      if (t) set.add(t);
    }
    for (const t of activeTrades ?? []) {
      const tok = String(t?.token ?? t?.instrument_token ?? "");
      if (tok) set.add(tok);
    }
    return Array.from(set);
  }, [open, activeTrades]);
  const liveQuotes = useMarketStream(streamTokens);

  /** Side-aware close-side price for a position row.
   *
   * User reported the position card's CURRENT column was labelled "BID"
   * but the value matched the underlying's LTP, not the actual bid
   * shown on a real broker terminal. That mislabel cascaded into P&L
   * too — we computed (ltp - avg) but the realised price on exit is
   * the close-side of the book (bid for BUY, ask for SELL). Same
   * bug the APK had, fixed there in c11d619; this is the web mirror.
   *
   * Priority: live bid/ask (side-aware) → live ltp → stored row.ltp.
   * `side` ("BUY"/"SELL") drives whether we ask for bid or ask. When
   * called without a side (legacy callers / aggregate previews) we
   * fall back to plain ltp, identical to the old behaviour.
   */
  function liveLtpFor(row: any, side?: "BUY" | "SELL"): number {
    // Market-closed gate — when the instrument's segment is past its
    // close-of-day (NSE/BSE 15:30, MCX 23:30, indices session-bound),
    // we IGNORE every live overlay and return the last stored
    // `row.ltp`. The user reported "market band hai fir bhi PnL move
    // kar raha" — root cause was Zerodha/Infoway replaying the final
    // pre-close snapshot every few seconds, and our display picking up
    // each replay as a "new" tick. Freezing at row.ltp matches the
    // exchange's actual state: no transactions are happening, the
    // price MUST not drift on screen.
    //
    // Crypto / forex / spot commodity segments are 24×5 / 24×7 — for
    // those isInstrumentMarketOpen returns true around the clock, so
    // this gate is a no-op there.
    const seg = row?.segment_type ?? row?.segment;
    const exch = row?.exchange;
    if (!isInstrumentMarketOpen(seg, exch)) {
      return Number(row?.ltp ?? 0);
    }
    const tok = String(row?.instrument_token ?? row?.token ?? "");
    if (tok) {
      const tick = liveQuotes.get(tok);
      if (side === "BUY") {
        const liveBid = Number(tick?.bid ?? 0);
        if (liveBid > 0) return liveBid;
      } else if (side === "SELL") {
        const liveAsk = Number(tick?.ask ?? 0);
        if (liveAsk > 0) return liveAsk;
      }
      const liveLtp = Number(tick?.ltp ?? 0);
      if (liveLtp > 0) return liveLtp;
    }
    return Number(row?.ltp ?? 0);
  }

  /** Resolve the side ("BUY"/"SELL") of a position row. Looks at the
   * explicit `opened_side` first (survives close), then `action` (active
   * trades), then the signed quantity (legacy positions without
   * opened_side). Centralises the "which side is this row?" decision so
   * every close-price lookup uses the same answer. */
  function resolveSide(row: any): "BUY" | "SELL" {
    const raw = String(row?.opened_side ?? row?.action ?? row?.side ?? "")
      .toUpperCase();
    if (raw === "BUY" || raw === "SELL") return raw as "BUY" | "SELL";
    const q = Number(row?.quantity ?? 0);
    return q < 0 ? "SELL" : "BUY";
  }

  // ── Header description: M2M snapshot ────────────────────────────────
  // Sum live floating P&L by computing per-row from WS LTP. The stored
  // unrealized_pnl is 3-s stale; using live ticks here keeps the header
  // tracker in lockstep with the table rows below.
  const totalMtm = (open ?? []).reduce((s: number, p: any) => {
    // Realisable P&L if the user squared off every position right now:
    // BUY rows close at the live BID, SELL rows at the live ASK. Falls
    // back to LTP when bid/ask aren't on the tick yet. Matches the
    // matching-engine fill behaviour (matching_engine.execute_market_order
    // uses BID/ASK for execution), so M2M never overstates what the
    // user would actually book.
    const side = resolveSide(p);
    const price = liveLtpFor(p, side) || Number(p.ltp ?? 0);
    const avg = Number(p.avg_price ?? 0);
    const qty = Number(p.quantity ?? 0);
    if (!price || !avg || !qty) return s;
    return s + (price - avg) * qty;
  }, 0);

  // CF Required (carry-forward margin) — sums each non-Infoway position's
  // OVERNIGHT margin estimate, not its current intraday `margin_used`.
  // Previously this just totalled `margin_used` which made it numerically
  // identical to (a subset of) the Used Margin tile — useless as a
  // separate indicator. With `holdingMarginFor` it now answers the
  // question the trader actually asks: "if my MIS positions roll
  // overnight, what margin will the platform need to lock?"
  // Infoway-fed instruments (Forex / Crypto / Stocks / Indices /
  // Commodities) trade in carry-forward mode by default so their
  // `margin_used` IS the carry margin already; counting them here
  // would double-count what the wallet's Used Margin tile already shows.
  const isInfowayPosition = (p: any): boolean => {
    const seg = (p?.segment_type ?? "").toUpperCase();
    const exch = (p?.exchange ?? "").toUpperCase();
    return (
      /CRYPTO|FOREX|FX|CDS|STOCKS|INDICES|COMMODITIES/.test(seg) ||
      exch === "CDS" ||
      exch === "CRYPTO"
    );
  };
  const requiredMargin = useMemo(
    () =>
      (open ?? [])
        .filter((p: any) => !isInfowayPosition(p))
        .reduce((s, p) => s + holdingMarginFor(p), 0),
    [open],
  );

  // ── Actions ─────────────────────────────────────────────────────────

  // Resolve a just-opened (optimistic_…) row to its real DB position id by
  // matching the instrument token in a freshly-refetched open-positions list.
  // The backend ObjectId lands within ~500ms via the WS push / next poll, so
  // a couple of quick refetches almost always finds it — turning an instant
  // open→close into a smooth close instead of "Order still settling".
  // Same resolution the terminal panel uses — see lib/positionIds.ts.
  const resolveRealPositionId = (optimisticId: string) =>
    resolveRealPositionIdShared(qc, optimisticId);

  // A close error we treat as "reconcile, don't alarm": already gone / in
  // flight / network / timeout — the B-Book close usually went through anyway
  // and the 2s poll settles the truth. Stops the re-tap → lock loop.
  function isBenignCloseError(e: any): boolean {
    const msg = String(e?.message ?? "").toLowerCase();
    const code = String(e?.code ?? "").toUpperCase();
    return (
      code === "NETWORK" ||
      msg.includes("in flight") ||
      msg.includes("network") ||
      msg.includes("timeout") ||
      msg.includes("already closed") ||
      msg.includes("not found") ||
      msg.includes("no open position")
    );
  }

  async function squareoff(id: string) {
    // Optimistic rows have synthetic IDs (`optimistic_<ts>`) created by
    // OrderPanel before the backend confirms the trade. They're not real
    // ObjectIds, so hitting /positions/<optimistic_…>/squareoff returns a
    // raw 500 (PydanticObjectId(…) raises InvalidId, which the CORS
    // middleware can't decorate — the browser sees it as a CORS error).
    // Block here and tell the user to wait a beat for the server to land
    // the real row; the WS push usually reconciles within 500 ms.
    let targetId = id;
    if (id.startsWith("optimistic_")) {
      // Just-bought position whose real DB id hasn't reconciled yet. Instead
      // of poll-waiting for the id (the "Closing… settling" lag), close BY
      // TOKEN — the backend resolves the user's open position for that token
      // (with a short retry for the buy commit). Instant close, no wait.
      const cur = qc.getQueryData<any[]>(["positions", "open"]) ?? [];
      const optRow = cur.find((p) => p.id === id);
      const tok = String(optRow?.instrument_token ?? optRow?.token ?? "");
      if (tok) {
        targetId = `token:${tok}`;
      } else {
        // No token on the row (shouldn't happen) — fall back to the resolve
        // poll so the close still works.
        const resolved = await resolveRealPositionId(id);
        if (!resolved) {
          toast.error("Order still settling — try Exit again in a moment");
          return;
        }
        targetId = resolved;
      }
    }
    // Confirmation is handled upstream: desktop opens the themed
    // ConfirmDialog card via requestClose(); mobile fires straight through
    // (user spec: "close karne par pop mat aaye" on phones). The ugly
    // browser confirm() is gone. isMobileUi only drives toast wording now.
    const isMobileUi =
      typeof window !== "undefined" &&
      window.matchMedia("(max-width: 767px)").matches;

    // OPTIMISTIC REMOVE — drop the row from the cache in the SAME frame
    // as the click so the UI feels instant ("close karne me time lag
    // raha hai 200 ms" was the user complaint). Earlier we awaited the
    // API and only invalidated afterwards, so the row sat with its
    // disabled state for the full round-trip. Mirror of the pattern
    // already used in components/trading/PositionsTabs.tsx:squareoff.
    qc.cancelQueries({ queryKey: ["positions", "open"] });
    qc.cancelQueries({ queryKey: ["positions", "summary"] });
    qc.cancelQueries({ queryKey: ["positions", "active-trades"] });
    const posSnapshot = qc.getQueryData<any[]>(["positions", "open"]);
    const tradesSnapshot = qc.getQueryData<any[]>(["positions", "active-trades"]);
    qc.setQueryData<any[]>(["positions", "open"], (old) =>
      Array.isArray(old) ? old.filter((p) => p.id !== id && p.id !== targetId) : [],
    );
    // Drop every active-trade row tied to this position too — same key the
    // Active tab actually reads (`["positions","active-trades"]`). The old
    // code targeted a bare `["active-trades"]` key that no query used, so
    // the Active rows lingered for a full poll after the position vanished.
    qc.setQueryData<any[]>(["positions", "active-trades"], (old) =>
      Array.isArray(old)
        ? old.filter((t) => t.position_id !== id && t.position_id !== targetId)
        : [],
    );

    // Sync toast: pops in the same frame as the click + optimistic
    // remove. Dismissed on rejection so we don't leave a misleading
    // "Closed" up next to a restored row.
    const pendingToastId = toast.success(
      isMobileUi ? "Closed" : "Submitted",
      { duration: 1500 },
    );
    try {
      await PositionAPI.squareoff(targetId);
      qc.invalidateQueries({ queryKey: ["orders"] });
      qc.invalidateQueries({ queryKey: ["wallet"] });
      // Reconcile open + active FAST. The close POST has returned, so on
      // single-node Mongo the row is already committed-closed — a short
      // delayed refetch confirms the removal within ~400 ms instead of
      // waiting for the 2 s poll. cancelQueries above already killed any
      // in-flight poll, so this can't resurrect the row.
      setTimeout(() => {
        qc.invalidateQueries({ queryKey: ["positions", "open"] });
        qc.invalidateQueries({ queryKey: ["positions", "active-trades"] });
      }, 400);
      // Refresh the Closed tab too — delayed retry covers any tiny lag.
      qc.invalidateQueries({ queryKey: ["positions", "closed"] });
      setTimeout(
        () => qc.invalidateQueries({ queryKey: ["positions", "closed"] }),
        1500,
      );
    } catch (e: any) {
      if (isBenignCloseError(e)) {
        // Already-gone / in-flight / network — keep the optimistic removal
        // and let the (paused) poll reconcile. Do NOT invalidate open /
        // active-trades here: an immediate refetch races the close commit
        // and resurrects the row for ~1 s. Only side caches refresh now.
        qc.invalidateQueries({ queryKey: ["positions", "closed"] });
        qc.invalidateQueries({ queryKey: ["wallet"] });
        return;
      }
      // Genuine rejection — restore the row + surface the message.
      if (posSnapshot) qc.setQueryData(["positions", "open"], posSnapshot);
      if (tradesSnapshot) qc.setQueryData(["positions", "active-trades"], tradesSnapshot);
      toast.dismiss(pendingToastId);
      toast.error(e?.message || "Failed");
    }
  }
  async function squareoffAll() {
    if (!open?.length) return;
    // Confirmation is handled by the themed ConfirmDialog wired to the
    // header button (mobile + desktop). This function just performs the
    // squareoff once the user has confirmed.

    // Same optimistic-clear pattern as single squareoff — wipe all open
    // rows the moment the user taps, so the empty state shows
    // instantly. Backend reconciles via the 2 s poll.
    qc.cancelQueries({ queryKey: ["positions", "open"] });
    qc.cancelQueries({ queryKey: ["positions", "summary"] });
    qc.cancelQueries({ queryKey: ["positions", "active-trades"] });
    const posSnapshot = qc.getQueryData<any[]>(["positions", "open"]);
    const tradesSnapshot = qc.getQueryData<any[]>(["positions", "active-trades"]);
    qc.setQueryData<any[]>(["positions", "open"], () => []);
    qc.setQueryData<any[]>(["positions", "active-trades"], () => []);

    const pendingToastId = toast.success(`Squaring off ${open.length}…`, {
      duration: 1500,
    });
    try {
      const r = await PositionAPI.squareoffAll();
      toast.dismiss(pendingToastId);
      const squared = r?.squared_off ?? 0;
      const total = r?.total ?? 0;
      const marketClosed = r?.blocked_by_market_closed ?? 0;
      const holdBlocked = r?.blocked_by_hold_time ?? 0;
      // When NOTHING closed and the only reason was a closed market, show
      // a clear "market band hai" popup instead of a confusing "0/N".
      if (squared === 0 && marketClosed > 0) {
        toast.error(
          `Market is closed — ${marketClosed} position${marketClosed > 1 ? "s" : ""} can only be closed once the market reopens.`,
        );
      } else if (marketClosed > 0) {
        toast.success(
          `Squared off ${squared}/${total}. ${marketClosed} skipped — market closed.`,
        );
      } else if (holdBlocked > 0) {
        toast.success(
          `Squared off ${squared}/${total}. ${holdBlocked} skipped — hold-time not met.`,
        );
      } else {
        toast.success(`Squared off ${squared}/${total}`);
      }
      // Some rows were intentionally left open (market closed / hold-time)
      // — restore the snapshot so the user still sees them instead of an
      // empty list, then let the next poll reconcile.
      if ((marketClosed > 0 || holdBlocked > 0) && posSnapshot) {
        qc.setQueryData(["positions", "open"], posSnapshot);
        if (tradesSnapshot) qc.setQueryData(["positions", "active-trades"], tradesSnapshot);
      }
      qc.invalidateQueries({ queryKey: ["orders"] });
      qc.invalidateQueries({ queryKey: ["wallet"] });
      // Same as single squareoff — pull the freshly-closed rows into the
      // Closed tab at once, with a delayed retry for read-replica lag.
      qc.invalidateQueries({ queryKey: ["positions", "closed"] });
      setTimeout(
        () => qc.invalidateQueries({ queryKey: ["positions", "closed"] }),
        1500,
      );
    } catch (e: any) {
      if (posSnapshot) qc.setQueryData(["positions", "open"], posSnapshot);
      if (tradesSnapshot) qc.setQueryData(["positions", "active-trades"], tradesSnapshot);
      toast.dismiss(pendingToastId);
      toast.error(e?.message || "Failed");
    }
  }
  async function exitActive(id: string) {
    // Mirror the Position-tab `squareoff` pattern so closing an active
    // trade feels — and behaves — identically: optimistic remove +
    // benign-error tolerance + NO immediate active/open invalidate.
    //
    // The old version `await`ed the POST then invalidated `["positions"]`
    // straight away. That refetch raced Mongo Atlas's read replica, which
    // briefly still returned the trade as OPEN, so the row reappeared
    // ("reopen ho jata hai") and sat for ~10 s. With no optimistic remove
    // and no benign-error path, a network blip surfaced a red toast → the
    // user re-tapped → hit the 10 s idempotency lock → "ek baar me close
    // nahi hota". This version drops the row in the same frame and lets
    // the 3 s poll settle the truth.
    const trades = qc.getQueryData<any[]>(["positions", "active-trades"]) ?? [];
    const row = trades.find((t) => t.id === id);
    const positionId = row?.position_id;
    const tradeQty = Math.abs(Number(row?.quantity ?? 0)) || 0;

    qc.cancelQueries({ queryKey: ["positions", "active-trades"] });
    qc.cancelQueries({ queryKey: ["positions", "open"] });
    const tradesSnapshot = qc.getQueryData<any[]>(["positions", "active-trades"]);
    const posSnapshot = qc.getQueryData<any[]>(["positions", "open"]);

    // Optimistic: drop the active-trade row immediately.
    qc.setQueryData<any[]>(["positions", "active-trades"], (old) =>
      Array.isArray(old) ? old.filter((t) => t.id !== id) : [],
    );
    // Optimistic: reduce the parent position's qty (remove it when this
    // was the last open fill) so the Position tab stays in lockstep.
    if (positionId && tradeQty > 0) {
      qc.setQueryData<any[]>(["positions", "open"], (old) => {
        if (!Array.isArray(old)) return [];
        return old
          .map((p) => {
            if (p.id !== positionId) return p;
            const curQty = Number(p.quantity) || 0;
            const sign = curQty >= 0 ? 1 : -1;
            const nextAbs = Math.max(0, Math.abs(curQty) - tradeQty);
            return nextAbs < 1e-9 ? null : { ...p, quantity: nextAbs * sign };
          })
          .filter(Boolean) as any[];
      });
    }

    const pendingToastId = toast.success("Exit placed", { duration: 1500 });
    try {
      await PositionAPI.closeActiveTrade(id);
      qc.invalidateQueries({ queryKey: ["orders"] });
      qc.invalidateQueries({ queryKey: ["wallet"] });
      // Fast reconcile of active + open (single-node Mongo = committed by
      // the time the POST returns). cancelQueries above prevents any
      // in-flight poll from re-adding the row.
      setTimeout(() => {
        qc.invalidateQueries({ queryKey: ["positions", "active-trades"] });
        qc.invalidateQueries({ queryKey: ["positions", "open"] });
      }, 400);
      qc.invalidateQueries({ queryKey: ["positions", "closed"] });
      setTimeout(
        () => qc.invalidateQueries({ queryKey: ["positions", "closed"] }),
        1500,
      );
    } catch (e: any) {
      if (isBenignCloseError(e)) {
        // Already gone / in flight / network — keep the optimistic removal
        // and let the (now-paused) active/open poll reconcile. Do NOT
        // invalidate active-trades / open here: an immediate refetch races
        // the close's commit and RESURRECTS the just-closed row for ~1 s
        // (the exact flicker the user reported). Only the side caches are
        // safe to refresh now.
        qc.invalidateQueries({ queryKey: ["positions", "closed"] });
        qc.invalidateQueries({ queryKey: ["wallet"] });
        return;
      }
      // Genuine rejection — restore the rows + surface the message.
      if (tradesSnapshot) qc.setQueryData(["positions", "active-trades"], tradesSnapshot);
      if (posSnapshot) qc.setQueryData(["positions", "open"], posSnapshot);
      toast.dismiss(pendingToastId);
      toast.error(e?.message || "Failed");
    }
  }

  async function cancelPendingOrder(id: string) {
    // Optimistic-remove the row from the pending list so the user
    // sees the card disappear in the same frame as the tap. Falls
    // back to the snapshot if the backend rejects (rare — usually
    // means the order already filled while the request was in
    // flight).
    qc.cancelQueries({ queryKey: ["orders", "PENDING-LIKE"] });
    const snapshot = qc.getQueryData<any[]>(["orders", "PENDING-LIKE"]);
    qc.setQueryData<any[]>(["orders", "PENDING-LIKE"], (old) =>
      Array.isArray(old) ? old.filter((o) => o.id !== id) : [],
    );
    const tid = toast.success("Order cancelled", { duration: 1500 });
    try {
      await OrderAPI.cancel(id);
      qc.invalidateQueries({ queryKey: ["orders"] });
    } catch (e: any) {
      if (snapshot) qc.setQueryData(["orders", "PENDING-LIKE"], snapshot);
      toast.dismiss(tid);
      toast.error(e?.message || "Cancel failed");
    }
  }

  // ── Columns per tab ─────────────────────────────────────────────────
  const positionCols: Column<any>[] = [
    { key: "symbol", header: "Symbol" },
    { key: "exchange", header: "Exch" },
    {
      key: "quantity",
      header: "Qty",
      align: "right",
      render: (r) => (
        <span className={r.quantity >= 0 ? "text-buy" : "text-sell"}>
          {fmtQty(r.quantity)}
        </span>
      ),
    },
    {
      key: "avg_price",
      header: "Avg",
      align: "right",
      render: (r) =>
        fmtFeedPrice(r.avg_price, r.currency_quote, r.segment_type, r.exchange),
    },
    {
      key: "ltp",
      header: "Current",
      align: "right",
      render: (r) => (
        <CurrentPriceCell
          value={liveLtpFor(r, resolveSide(r))}
          quote={r.currency_quote}
          segment={r.segment_type}
          exchange={r.exchange}
        />
      ),
    },
    {
      key: "unrealized_pnl",
      header: "M2M",
      align: "right",
      render: (r) => {
        // Always recompute on the frontend: live close-side price
        // (BID for BUY, ASK for SELL) so the M2M matches the matching
        // engine's actual fill behaviour. Falls back to row.ltp when
        // bid/ask aren't on the tick yet. Never trust backend's
        // `unrealized_pnl` because legacy builds may still have applied
        // the (now-disabled) FX ×83 conversion to it.
        const side = resolveSide(r);
        const price = liveLtpFor(r, side) || Number(r.ltp ?? 0);
        const avg = Number(r.avg_price ?? 0);
        const qty = Number(r.quantity ?? 0);
        const pnl = (price > 0 && avg > 0 && qty !== 0) ? (price - avg) * qty : 0;
        return (
          <span className={pnlColor(pnl)}>{formatINR(pnl)}</span>
        );
      },
    },
    // Removed REALIZED column from the Open Positions tab — an open
    // position has no realized P&L by definition.
    {
      key: "margin_used",
      header: "Margin",
      align: "right",
      render: (r) => formatINR(r.margin_used),
    },
    // Inline TP / SL edit — click to set, click to edit existing.
    // Matches the Active tab UX so the trader doesn't have to switch
    // tabs to attach brackets to the aggregated position.
    {
      key: "tp",
      header: "TP",
      align: "right",
      render: (r) => (
        <button
          type="button"
          onClick={() => setEditing({ row: r, kind: "TP", source: "position" })}
          className="rounded border border-border px-1.5 py-0.5 text-[11px] font-semibold hover:bg-muted/40"
        >
          {r.target ? Number(r.target).toFixed(2) : "Add +"}
        </button>
      ),
    },
    {
      key: "sl",
      header: "SL",
      align: "right",
      render: (r) => (
        <button
          type="button"
          onClick={() => setEditing({ row: r, kind: "SL", source: "position" })}
          className="rounded border border-border px-1.5 py-0.5 text-[11px] font-semibold hover:bg-muted/40"
        >
          {r.stop_loss ? Number(r.stop_loss).toFixed(2) : "Add +"}
        </button>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      render: (r) => {
        // While the optimistic row is on screen (waiting for the WS push
        // that delivers the real ObjectId), disable the close + edit
        // controls. Tapping them with the synthetic id would 500 on the
        // backend (InvalidId).
        const isOptimistic =
          typeof r.id === "string" && r.id.startsWith("optimistic_");
        return (
          <div className="flex items-center justify-end gap-1.5">
            <Button
              size="icon"
              variant="ghost"
              aria-label="Edit SL / TP"
              title={isOptimistic ? "Waiting for confirmation…" : "Edit SL / TP"}
              onClick={() => setEditing({ row: r, kind: "TP", source: "position" })}
              disabled={isOptimistic}
              className="h-7 w-7"
            >
              <Pencil className="size-3.5" />
            </Button>
            <Button
              size="sm"
              onClick={() => requestClose(r)}
              disabled={isOptimistic}
              title={isOptimistic ? "Waiting for confirmation…" : "Square off"}
              className="h-7 gap-1 rounded-md bg-destructive/15 px-2.5 text-xs font-semibold text-destructive ring-1 ring-inset ring-destructive/30 hover:bg-destructive hover:text-destructive-foreground hover:ring-destructive disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-destructive/15 disabled:hover:text-destructive disabled:hover:ring-destructive/30"
            >
              <X className="size-3.5" /> {isOptimistic ? "…" : "Close"}
            </Button>
          </div>
        );
      },
    },
  ];

  // ── Cancelled / Rejected order columns ─────────────────────────────
  // Shared between both tabs — only the cancelled_at vs rejected reason
  // detail differs, and that's surfaced via the row data itself.
  const orderCols: Column<any>[] = [
    { key: "symbol", header: "Symbol" },
    { key: "exchange", header: "Exch" },
    {
      key: "action",
      header: "Side",
      align: "center",
      render: (r) => <StatusPill status={r.action} />,
    },
    {
      key: "order_type",
      header: "Type",
      align: "center",
      render: (r) => <StatusPill status={r.order_type} />,
    },
    { key: "lots", header: "Lots", align: "right" },
    {
      key: "price",
      header: "Price",
      align: "right",
      render: (r) =>
        formatPrice(
          Number(r.average_price) > 0 ? r.average_price : r.price,
          r.segment,
          r.exchange,
        ),
    },
    {
      key: "status",
      header: "Status",
      render: (r) => <StatusPill status={r.status} />,
    },
    {
      key: "reason",
      header: "Reason",
      render: (r) => (
        <span className="text-[11px] text-muted-foreground">
          {r.rejection_reason ?? "—"}
        </span>
      ),
    },
    {
      key: "created_at",
      header: "Placed",
      render: (r) => (
        <span className="whitespace-nowrap font-tabular text-xs tabular-nums text-foreground">
          {formatIST(r.created_at, { withSeconds: true })}
        </span>
      ),
    },
  ];

  // Closed-tab columns: realized P&L is the headline number, no live LTP
  // because the position is fully exited. Stays in the same table shape
  // so the user's eye doesn't have to relearn the column layout when
  // switching tabs.
  const closedCols: Column<any>[] = [
    { key: "symbol", header: "Symbol" },
    { key: "exchange", header: "Exch" },
    {
      // For a closed row `quantity` has been zeroed by the matching engine.
      // Prefer the preserved `opening_quantity` (peak |qty| during the
      // position's lifecycle) so the user sees the size they actually held.
      key: "quantity",
      header: "Qty",
      align: "right",
      render: (r) => {
        const size = Number(r.opening_quantity ?? Math.abs(r.quantity ?? 0)) || 0;
        // Direction is recorded on the realized leg — positive realized
        // P&L on a long means long, etc. Fall back to neutral coloring
        // when we can't infer.
        const isLong = Number(r.realized_pnl ?? 0) >= 0 ? true : false;
        return (
          <span className={isLong ? "text-buy" : "text-sell"}>{fmtQty(size)}</span>
        );
      },
    },
    {
      key: "avg_price",
      header: "Avg",
      align: "right",
      render: (r) =>
        fmtFeedPrice(r.avg_price, r.currency_quote, r.segment_type, r.exchange),
    },
    {
      key: "ltp",
      header: "Close",
      align: "right",
      render: (r) =>
        fmtFeedPrice(r.ltp, r.currency_quote, r.segment_type, r.exchange),
    },
    {
      key: "realized_pnl",
      header: "Realized P&L",
      align: "right",
      render: (r) => (
        <span className={pnlColor(r.realized_pnl)}>
          {formatINR(r.realized_pnl)}
        </span>
      ),
    },
    {
      // Total brokerage + charges that were deducted across every fill
      // tied to this closed position. Summed server-side from the trades
      // collection (see backend/app/api/v1/user/positions.py:closed_positions).
      key: "charges",
      header: "Brokerage",
      align: "right",
      render: (r) => (
        <span className="font-tabular text-muted-foreground">
          {formatINR(r.charges ?? 0)}
        </span>
      ),
    },
    {
      // Snapshot of SL set on the position at close-time. The live
      // `stop_loss` / `target` fields are wiped on full close to keep
      // future reopens clean, so the backend preserves the values in
      // `close_stop_loss` / `close_target` for display here. The Active
      // tab tints SL red — keep that same convention so users get a
      // consistent SL=red, TP=green colour code across tabs.
      key: "close_stop_loss",
      header: "SL",
      align: "right",
      render: (r) => {
        const sl = r.close_stop_loss ?? r.stop_loss;
        if (!sl || Number(sl) === 0) {
          return <span className="text-muted-foreground/60">—</span>;
        }
        const hit = String(r.close_reason ?? "").toUpperCase() === "SL_HIT";
        return (
          <span
            className={cn(
              "font-tabular text-sell tabular-nums",
              hit && "rounded bg-sell/15 px-1.5 py-0.5 font-semibold",
            )}
            title={hit ? "Trade closed by SL hit" : "SL was set on this trade"}
          >
            {fmtFeedPrice(sl, r.currency_quote, r.segment_type, r.exchange)}
          </span>
        );
      },
    },
    {
      key: "close_target",
      header: "TP",
      align: "right",
      render: (r) => {
        const tp = r.close_target ?? r.target;
        if (!tp || Number(tp) === 0) {
          return <span className="text-muted-foreground/60">—</span>;
        }
        const hit = String(r.close_reason ?? "").toUpperCase() === "TP_HIT";
        return (
          <span
            className={cn(
              "font-tabular text-buy tabular-nums",
              hit && "rounded bg-buy/15 px-1.5 py-0.5 font-semibold",
            )}
            title={hit ? "Trade closed by Target hit" : "Target was set on this trade"}
          >
            {fmtFeedPrice(tp, r.currency_quote, r.segment_type, r.exchange)}
          </span>
        );
      },
    },
    {
      key: "opened_at",
      header: "Open Time",
      render: (r) => (
        <span className="whitespace-nowrap text-xs text-muted-foreground">
          {r.opened_at ? formatIST(r.opened_at, { withSeconds: true }) : "—"}
        </span>
      ),
    },
    {
      key: "closed_at",
      header: "Close Time",
      render: (r) => (
        <span className="whitespace-nowrap text-xs text-muted-foreground">
          {r.closed_at ? formatIST(r.closed_at, { withSeconds: true }) : "—"}
        </span>
      ),
    },
    {
      // Compact tag stamped by the squareoff path. Lets the user see at a
      // glance that a position was closed by their bracket SL/TP while
      // they were away — not by a forgotten manual close.
      key: "close_reason",
      header: "Closed By",
      render: (r) => <CloseReasonChip reason={r.close_reason} />,
    },
  ];

  // Active-trades-tab columns: one row per fill that's still part of an
  // open position. Adds Used Margin / Holding Margin (1.4× for MIS, same
  // for NRML), inline TP / SL edit buttons and a per-fill Exit action.
  const activeCols: Column<any>[] = [
    { key: "symbol", header: "Symbol" },
    { key: "exchange", header: "Exch" },
    {
      key: "action",
      header: "Side",
      align: "center",
      render: (r) => (
        <span
          className={cn(
            "rounded px-1.5 py-0.5 text-[10px] font-bold uppercase",
            String(r.action ?? r.side).toUpperCase() === "BUY"
              ? "bg-buy/15 text-buy"
              : "bg-sell/15 text-sell",
          )}
        >
          {String(r.action ?? r.side).toUpperCase()}
        </span>
      ),
    },
    {
      key: "product_type",
      header: "Prod",
      align: "center",
      render: (r) => (
        <span className="rounded border border-border px-1.5 py-0.5 text-[10px] font-semibold uppercase">
          {r.product_type}
        </span>
      ),
    },
    { key: "quantity", header: "Qty", align: "right" },
    {
      key: "price",
      header: "Entry",
      align: "right",
      render: (r) =>
        fmtFeedPrice(r.price, r.currency_quote, r.segment, r.exchange),
    },
    {
      key: "ltp",
      header: "Current",
      align: "right",
      render: (r) => (
        <CurrentPriceCell
          value={liveLtpFor(r, resolveSide(r))}
          quote={r.currency_quote}
          segment={r.segment}
          exchange={r.exchange}
        />
      ),
    },
    {
      key: "used_margin",
      header: "Used",
      align: "right",
      render: (r) => formatINR(r.margin ?? r.used_margin ?? r.margin_used ?? 0),
    },
    {
      key: "holding_margin",
      header: "Holding",
      align: "right",
      render: (r) => formatINR(holdingMarginFor(r)),
    },
    {
      key: "pnl",
      header: "P&L",
      align: "right",
      render: (r) => {
        // Recompute per-fill at the live close-side (BID for BUY,
        // ASK for SELL) × (price − entry) × qty, with BUY/SELL sign.
        // Matches the matching engine's actual fill behaviour so the
        // P&L equals what the user would book on exit, not the LTP-
        // approximated number.
        // Entry = THIS fill's own entry price (specific-lot close books
        // against the tapped fill, not the position average), falling back to
        // the position avg for synthetic rows that carry no per-fill price.
        const side = resolveSide(r);
        const price = liveLtpFor(r, side) || Number(r.ltp ?? 0);
        const entry = Number(r.price ?? r.avg_price ?? 0);
        const qty = Number(r.quantity ?? 0);
        const dir = side === "SELL" ? -1 : 1;
        const pnl = (price > 0 && entry > 0 && qty !== 0) ? dir * (price - entry) * qty : 0;
        return <span className={pnlColor(pnl)}>{formatINR(pnl)}</span>;
      },
    },
    {
      key: "tp",
      header: "TP",
      align: "right",
      render: (r) => (
        <button
          type="button"
          onClick={() => setEditing({ row: r, kind: "TP", source: "active" })}
          className="rounded border border-border px-1.5 py-0.5 text-[11px] font-semibold hover:bg-muted/40"
        >
          {r.target ? Number(r.target).toFixed(2) : "Add +"}
        </button>
      ),
    },
    {
      key: "sl",
      header: "SL",
      align: "right",
      render: (r) => (
        <button
          type="button"
          onClick={() => setEditing({ row: r, kind: "SL", source: "active" })}
          className="rounded border border-border px-1.5 py-0.5 text-[11px] font-semibold hover:bg-muted/40"
        >
          {r.stop_loss ? Number(r.stop_loss).toFixed(2) : "Add +"}
        </button>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      render: (r) => (
        <Button
          size="sm"
          onClick={() => exitActive(r.id)}
          className="h-7 gap-1 rounded-md bg-destructive/15 px-2.5 text-xs font-semibold text-destructive ring-1 ring-inset ring-destructive/30 hover:bg-destructive hover:text-destructive-foreground hover:ring-destructive"
        >
          <LogOut className="size-3.5" /> Exit
        </Button>
      ),
    },
  ];

  // Closed-tab pagination — client-side. The endpoint already returns
  // only this user's trades and lifetime closed trades for an active
  // trader can easily run into hundreds of rows; rendering them all at
  // once jankifies scroll on phones.
  // Server-side pagination — closed is already the current loaded page slice.
  const pagedClosed = (closed ?? []) as any[];

  // Pick what to render based on the selected tab.
  const tableProps =
    tab === "closed"
      ? { columns: closedCols, rows: pagedClosed, loading: closedLoading && !closed }
      : tab === "active"
        ? {
            columns: activeCols,
            rows: activeTrades,
            loading: activeLoading && !activeTrades,
          }
        : tab === "cancelled"
          ? {
              columns: orderCols,
              rows: cancelled,
              loading: cancelledLoading && !cancelled,
            }
          : tab === "rejected"
            ? {
                columns: orderCols,
                rows: rejected,
                loading: rejectedLoading && !rejected,
              }
            : tab === "pending"
              ? {
                  columns: orderCols,
                  rows: pending,
                  loading: pendingLoading && !pendingRaw,
                }
              : { columns: positionCols, rows: open, loading: openLoading && !open };

  return (
    <div className="space-y-4">
      {/* Page header — title + live snapshot on the left, the destructive
          "Square off all" action pinned top-right. Stays a single row on
          EVERY breakpoint (including mobile) per the user's request, instead
          of the old layout that stacked the button onto its own row below
          the title on phones. Tapping it opens a themed confirmation dialog
          rather than firing instantly. */}
      <header className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">
            Positions
          </h1>
          <p className="mt-0.5 text-sm text-muted-foreground">
            <span className="font-semibold text-foreground/80">
              {counts.position}
            </span>{" "}
            open
            <span className="mx-1.5 text-border">·</span>
            M2M{" "}
            <span
              className={cn(
                "font-tabular font-semibold tabular-nums",
                pnlColor(totalMtm),
              )}
            >
              {totalMtm >= 0 ? "+" : ""}
              {formatINR(totalMtm)}
            </span>
          </p>
        </div>
        <Button
          variant="destructive"
          disabled={!open?.length}
          onClick={() => setConfirmAllOpen(true)}
          className="h-9 shrink-0 gap-1.5 rounded-lg px-3 text-xs font-semibold shadow-sm ring-1 ring-inset ring-white/10 sm:h-10 sm:px-4 sm:text-sm"
        >
          <Layers className="size-4" />
          <span className="whitespace-nowrap">Square off all</span>
        </Button>
      </header>

      {/* Wallet + margin status strip — Balance / Equity / M2M / Used.
          CF Required (carry-forward margin estimate) used to live here
          as a 5th tile but the user asked to drop it from the Positions
          tab: it duplicates the per-row Holding Margin already on every
          card and only matters when reviewing the carry-eligible legs.
          We now hide it on the Position tab and show it on the Active
          tab via `showCfRequired` so the wallet strip stays 4 tiles
          where the trader is just looking at the net position view. */}
      <WalletStatusStrip
        wallet={wallet}
        m2m={totalMtm}
        cfRequired={requiredMargin}
        showCfRequired={tab === "active"}
      />

      {/* Blotter tabs — Position / Active / Closed. Order-state tabs
          (Pending / Cancelled / Rejected) now live on the dedicated
          /orders page. With only 3 tabs they no longer need a scroll
          track: on mobile they fill the row as 3 equal-width segments
          (segmented-control look) so they're evenly spaced instead of
          bunched on the left; md+ falls back to left-aligned underline
          tabs. */}
      <div className="grid grid-cols-3 gap-1 rounded-xl bg-muted/20 p-1 ring-1 ring-inset ring-border/40 md:flex md:items-center md:gap-6 md:rounded-none md:bg-transparent md:p-0 md:ring-0 md:border-b md:border-border">
        <TabBtn
          active={tab === "position"}
          count={counts.position}
          onClick={() => setTab("position")}
        >
          Position
        </TabBtn>
        <TabBtn active={tab === "active"} count={counts.active} onClick={() => setTab("active")}>
          Active
        </TabBtn>
        <TabBtn
          active={tab === "closed"}
          count={counts.closed}
          onClick={() => {
            setTab("closed");
            // Force an immediate refetch when switching to this tab so a
            // just-executed partial close appears without the user having to
            // pull-to-refresh. A 1.5 s delayed retry covers the brief window
            // where Mongo hasn't committed the closing Trade to the read path
            // yet (same pattern used in squareoff() for full closes).
            qc.invalidateQueries({ queryKey: ["positions", "closed"] });
            setTimeout(
              () => qc.invalidateQueries({ queryKey: ["positions", "closed"] }),
              1500,
            );
          }}
        >
          Closed
        </TabBtn>
        {/* Pending / Cancelled / Rejected tabs moved to the dedicated
            /orders page (Open / Executed / Rejected) per the operator —
            this blotter now stays focused on Position / Active / Closed. */}
      </div>

      {/* Closed + Active tabs render a mobile-friendly card list at
          `<md` and the full DataTable from `md+`. Other tabs use the
          table on every breakpoint — they're already compact enough to
          fit on a phone. */}
      {tab === "closed" ? (
        <>
          <div className="md:hidden">
            <ClosedMobileList rows={pagedClosed} loading={closedLoading && !closed} />
          </div>
          <div className="hidden md:block">
            <DataTable
              columns={tableProps.columns}
              rows={tableProps.rows}
              keyExtractor={(r) => r.id}
              loading={tableProps.loading}
            />
          </div>
          {hasMoreClosed && (
            <div className="flex justify-center py-4">
              <button
                onClick={() => fetchMoreClosed()}
                disabled={loadingMoreClosed}
                className="rounded-md border border-border px-5 py-2 text-sm text-muted-foreground hover:bg-muted disabled:opacity-50"
              >
                {loadingMoreClosed
                  ? "Loading…"
                  : `Load more (${pagedClosed.length} of ${closedTotal})`}
              </button>
            </div>
          )}
        </>
      ) : tab === "active" ? (
        <>
          <div className="md:hidden">
            <ActiveMobileList
              variant="active"
              rows={(activeTrades ?? []) as any[]}
              loading={activeLoading && !activeTrades}
              liveLtpFor={liveLtpFor}
              onEdit={(row, kind) => setEditing({ row, kind, source: "active" })}
              onExit={exitActive}
              onTrade={(tok) => setSheetToken(tok)}
              emptyLabel="No active trades"
              emptyHint="Each open fill that makes up your positions appears here."
            />
          </div>
          <div className="hidden md:block">
            <DataTable
              columns={tableProps.columns}
              rows={tableProps.rows}
              keyExtractor={(r) => r.id}
              loading={tableProps.loading}
            />
          </div>
        </>
      ) : tab === "position" ? (
        <>
          <div className="md:hidden">
            <ActiveMobileList
              variant="position"
              rows={(open ?? []) as any[]}
              loading={openLoading && !open}
              liveLtpFor={liveLtpFor}
              onEdit={(row, kind) => setEditing({ row, kind, source: "position" })}
              onExit={squareoff}
              onTrade={(tok) => setSheetToken(tok)}
              emptyLabel="No open positions"
              emptyHint="Your open positions show up here the moment you place a trade."
            />
          </div>
          <div className="hidden md:block">
            <DataTable
              columns={tableProps.columns}
              rows={tableProps.rows}
              keyExtractor={(r) => r.id}
              loading={tableProps.loading}
            />
          </div>
        </>
      ) : tab === "pending" ? (
        <>
          {/* Mobile cards for pending orders — same visual treatment as
              the active-trade cards (BUY/SELL pill + symbol + qty +
              entry → ltp + bottom action row) so the user sees one
              consistent shape across both tabs. Edit pencil opens the
              modify dialog; X cancels via OrderAPI.cancel with the
              optimistic-remove pattern. */}
          <div className="md:hidden">
            <PendingMobileList
              rows={pending}
              loading={pendingLoading && !pendingRaw}
              onEdit={(o) => setEditingPending(o)}
              onCancel={cancelPendingOrder}
            />
          </div>
          <div className="hidden md:block">
            <DataTable
              columns={tableProps.columns}
              rows={tableProps.rows}
              keyExtractor={(r) => r.id}
              loading={tableProps.loading}
            />
          </div>
        </>
      ) : (
        <DataTable
          columns={tableProps.columns}
          rows={tableProps.rows}
          keyExtractor={(r) => r.id}
          loading={tableProps.loading}
        />
      )}

      <EditSlTpDialog
        open={!!editing}
        kind={editing?.kind ?? "TP"}
        row={editing?.row}
        currentLtp={
          editing?.row ? liveLtpFor(editing.row, resolveSide(editing.row)) : 0
        }
        source={editing?.source ?? "active"}
        onClose={() => setEditing(null)}
        onSaved={() => {
          qc.invalidateQueries({ queryKey: ["positions"] });
          setEditing(null);
        }}
      />

      {/* Mobile-only slide-up trade card — opens when a position /
          active-trade card is tapped. `onSwap` lets the in-sheet
          Option Chain picker swap strikes while keeping the sheet
          open. Same component the marketwatch / option-chain /
          terminal pages use, so the BUY/SELL flow stays identical
          everywhere. */}
      <TradeDetailSheet
        token={sheetToken}
        open={!!sheetToken}
        onClose={() => setSheetToken(null)}
        onSwap={(tok) => setSheetToken(tok)}
      />

      {/* Pending-order modify dialog. Lets the user tweak lots, price
          (for LIMIT) or trigger_price (for SL-M) on a still-pending
          order without cancelling + re-placing. */}
      <EditPendingOrderDialog
        order={editingPending}
        onClose={() => setEditingPending(null)}
        onSaved={() => {
          qc.invalidateQueries({ queryKey: ["orders"] });
          setEditingPending(null);
        }}
      />

      {/* Themed confirmation for the header "Square off all" — replaces the
          old browser confirm() (desktop) and the silent fire (mobile). */}
      <ConfirmDialog
        open={confirmAllOpen}
        title="Square off all positions?"
        description={
          <>
            This closes{" "}
            <span className="font-semibold text-foreground">
              all {counts.position} open position
              {counts.position === 1 ? "" : "s"}
            </span>{" "}
            at the current market price. This can&apos;t be undone.
          </>
        }
        confirmLabel="Square off all"
        cancelLabel="Cancel"
        onConfirm={async () => {
          setConfirmAllOpen(false);
          await squareoffAll();
        }}
        onCancel={() => setConfirmAllOpen(false)}
      />

      {/* Themed single-position close confirmation (desktop) — replaces the
          ugly native confirm("Square off this position at market?"). Shows
          the trade context so the user closes the RIGHT row. Mobile bypasses
          this entirely (requestClose fires straight through). */}
      <ConfirmDialog
        open={!!closeRow}
        title="Square off position?"
        description={
          closeRow
            ? (() => {
                // Live M2M — recompute the SAME way the table cell does
                // (close-side bid/ask), never the stale backend unrealized_pnl.
                const side = resolveSide(closeRow);
                const sideLabel = String(side).toUpperCase() === "BUY" ? "BUY" : "SELL";
                const px = liveLtpFor(closeRow, side) || Number(closeRow.ltp ?? 0);
                const avg = Number(closeRow.avg_price ?? 0);
                const qty = Number(closeRow.quantity ?? 0);
                const hasM2m = px > 0 && avg > 0 && qty !== 0;
                const m2m = hasM2m ? (px - avg) * qty : 0;
                return (
                  <div className="space-y-2">
                    <div className="rounded-lg border border-border bg-muted/30 px-3 py-2.5 text-sm">
                      <div className="flex items-center justify-between py-0.5">
                        <span className="text-muted-foreground">Symbol</span>
                        <span className="font-semibold text-foreground">{closeRow.symbol}</span>
                      </div>
                      <div className="flex items-center justify-between py-0.5">
                        <span className="text-muted-foreground">Side</span>
                        <span className={cn("font-semibold", sideLabel === "BUY" ? "text-buy" : "text-sell")}>
                          {sideLabel}
                        </span>
                      </div>
                      <div className="flex items-center justify-between py-0.5">
                        <span className="text-muted-foreground">Quantity</span>
                        <span className="font-medium text-foreground">{Math.abs(qty)}</span>
                      </div>
                      {hasM2m && (
                        <div className="flex items-center justify-between py-0.5">
                          <span className="text-muted-foreground">Live M2M</span>
                          <span className={cn("font-semibold", pnlColor(m2m))}>{formatINR(m2m)}</span>
                        </div>
                      )}
                    </div>
                    <p className="text-xs text-muted-foreground">
                      This closes the position at the current market price.
                    </p>
                  </div>
                );
              })()
            : null
        }
        confirmLabel="Square off"
        cancelLabel="Cancel"
        onConfirm={async () => {
          const row = closeRow;
          setCloseRow(null);
          if (row) await squareoff(row.id);
        }}
        onCancel={() => setCloseRow(null)}
      />
    </div>
  );
}

// ── Sub-components ─────────────────────────────────────────────────────

/**
 * Wallet + margin status strip — Balance / Equity / M2M / Used Margin /
 * CF Required. Shown above the tabs so the trader sees their wallet
 * health on every tab, not just the active-margin breakdown.
 *
 * `m2m` is the live floating P&L computed from WS LTPs in the parent
 * (matches the table rows exactly). `cfRequired` is the carry-forward
 * margin requirement summed across INDIAN segments only — Infoway-fed
 * positions are already in carry mode by default.
 */
function WalletStatusStrip({
  wallet,
  m2m,
  cfRequired,
  showCfRequired = true,
}: {
  wallet: any;
  m2m: number;
  cfRequired: number;
  /** Drop the CF Required + CF Extra tiles when false — used on the
   *  Position tab so the wallet row stays at 4 tiles. Active tab keeps
   *  both on (6 tiles) because that's where carry-forward planning
   *  lives. */
  showCfRequired?: boolean;
}) {
  const available = Number(wallet?.available_balance ?? 0);
  const used = Number(wallet?.used_margin ?? 0);
  const balance = available + used;
  const equity = balance + m2m;
  // CF Extra Needed — the actual CASH SHORTFALL to carry the position(s)
  // overnight. To roll into NRML the user needs `cfRequired` locked; the
  // intraday `used` margin is released and re-locked, so the funds that can
  // meet it are the FULL balance (available + used). If the balance already
  // covers cfRequired the user needs to add nothing → ₹0 (green); otherwise
  // it's the deposit they must top up → red.
  //   balance = ₹12,687, cfRequired = ₹7,500 → extra needed ₹0 (affordable)
  //   balance = ₹5,000,  cfRequired = ₹7,500 → extra needed ₹2,500
  // (Earlier this showed `cfRequired - used` = the additional margin that
  // would be blocked at rollover, but that read as a scary red number even
  // when the wallet could easily afford the carry — operator flagged it as
  // wrong. The shortfall against balance is the meaningful "needed" figure.)
  const cfExtraNeeded = Math.max(0, cfRequired - balance);
  return (
    <div
      className={cn(
        "grid grid-cols-2 gap-2 sm:grid-cols-3",
        showCfRequired ? "lg:grid-cols-6" : "lg:grid-cols-4",
      )}
    >
      <WalletTile label="Balance" value={formatINR(balance)} />
      <WalletTile label="Equity" value={formatINR(equity)} />
      <WalletTile
        label="M2M"
        value={`${m2m >= 0 ? "+" : ""}${formatINR(m2m)}`}
        valueClass={pnlColor(m2m)}
      />
      <WalletTile label="Used Margin" value={formatINR(used)} />
      {showCfRequired && (
        <>
          <WalletTile
            label="CF Required"
            value={formatINR(cfRequired)}
            valueClass={cfRequired > balance ? "text-red-500" : undefined}
          />
          <WalletTile
            label="CF Extra Needed"
            value={formatINR(cfExtraNeeded)}
            valueClass={cfExtraNeeded > 0 ? "text-red-500" : "text-emerald-500"}
          />
        </>
      )}
    </div>
  );
}

function WalletTile({
  label,
  value,
  valueClass,
}: {
  label: string;
  value: string;
  valueClass?: string;
}) {
  return (
    <div className="rounded-lg border border-border/70 bg-gradient-to-b from-card to-card/60 px-3 py-2.5 shadow-sm ring-1 ring-inset ring-white/5">
      <div className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div
        className={cn(
          "mt-1 font-tabular text-sm font-bold tabular-nums",
          valueClass,
        )}
      >
        {value}
      </div>
    </div>
  );
}

function TabBtn({
  active,
  count,
  onClick,
  children,
}: {
  active: boolean;
  count: number;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        // Mobile: rounded-pill segmented control inside a tinted track.
        // `w-full + justify-center` makes each of the 3 tabs claim an equal
        // third of the row (the parent is a grid-cols-3), so they're evenly
        // spread instead of hugging the left edge. Active = solid background
        // pill with primary text. Inactive = foreground text (not muted) so
        // all tab labels stay readable.
        // Desktop (md+): content-width, left-aligned, classic underline.
        "relative -mb-px flex w-full items-center justify-center gap-1.5 whitespace-nowrap rounded-lg px-2 py-1.5 text-sm font-medium transition-all md:w-auto md:shrink-0 md:justify-start md:rounded-none md:px-0 md:pb-2 md:pt-1 md:font-normal",
        active
          ? "bg-primary/15 font-semibold text-primary shadow-sm md:bg-transparent md:text-foreground md:shadow-none"
          : "text-foreground/70 hover:bg-muted/40 hover:text-foreground md:hover:bg-transparent md:text-muted-foreground",
      )}
    >
      {children}
      {count > 0 && (
        <span
          className={cn(
            "rounded-full border px-1.5 text-[10px] font-semibold tabular-nums",
            active
              ? "border-primary/40 bg-primary/15 text-primary"
              : "border-border text-muted-foreground",
          )}
        >
          {count}
        </span>
      )}
      {active && (
        <span className="absolute inset-x-0 -bottom-px hidden h-0.5 rounded-t bg-primary md:block" />
      )}
    </button>
  );
}

function EditSlTpDialog({
  open,
  kind,
  row,
  source,
  currentLtp,
  onClose,
  onSaved,
}: {
  open: boolean;
  kind: "TP" | "SL";
  row: any;
  source: "position" | "active";
  currentLtp: number;
  onClose: () => void;
  onSaved: () => void;
}) {
  const ltpDir = usePriceFlash(currentLtp);
  const initial =
    kind === "TP"
      ? row?.target != null
        ? String(Number(row.target))
        : ""
      : row?.stop_loss != null
        ? String(Number(row.stop_loss))
        : "";
  const [value, setValue] = useState(initial);
  const [saving, setSaving] = useState(false);

  // Reset value when dialog opens for a different row/kind.
  useMemo(() => {
    setValue(initial);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [row?.id, kind]);

  async function save() {
    if (!row) return;
    const n = value === "" ? null : Number(value);
    if (n !== null && !Number.isFinite(n)) {
      toast.error("Enter a valid number");
      return;
    }
    setSaving(true);
    try {
      const body =
        kind === "TP" ? { target: n as any } : { stop_loss: n as any };
      // Route to the correct endpoint based on which tab opened the
      // dialog. `row.id` is the trade id on the Active tab and the
      // position id on the Position tab — calling the wrong endpoint
      // is what produced the "trade not found" error on mobile before.
      if (source === "active") {
        await PositionAPI.updateActiveTradeSlTp(row.id, body);
      } else {
        await PositionAPI.updateSlTp(row.id, body);
      }
      toast.success(`${kind === "TP" ? "Target" : "Stop loss"} updated`);
      onSaved();
    } catch (e: any) {
      toast.error(e?.message || "Update failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>
            {kind === "TP" ? "Take Profit" : "Stop Loss"} — {row?.symbol ?? ""}
          </DialogTitle>
        </DialogHeader>
        {currentLtp > 0 && (
          <div className="flex items-center justify-between rounded-lg border border-border bg-muted/40 px-3 py-2">
            <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Current price
            </span>
            <span
              className={cn(
                "text-xl font-bold tabular-nums transition-colors",
                ltpDir === "up"
                  ? "text-emerald-500"
                  : ltpDir === "down"
                    ? "text-red-500"
                    : "text-foreground",
              )}
            >
              {fmtFeedPrice(
                currentLtp,
                row?.currency_quote,
                row?.segment_type,
                row?.exchange,
              )}
            </span>
          </div>
        )}
        <div className="space-y-3">
          <Input
            type="number"
            step="0.01"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="Leave blank to clear"
            autoFocus
          />
          <p className="text-[11px] text-muted-foreground">
            When the market crosses this level the position is auto-squared off at market.
          </p>
        </div>
        <DialogFooter>
          <Button variant="outline" type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button type="button" onClick={save} loading={saving}>
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}


/** Tick-flashing "Current" price cell — green when the LTP just ticked
 *  up, red when it ticked down, decays back to neutral after ~700 ms.
 *  Identical UX to the trading-terminal positions table. */
function CurrentPriceCell({
  value,
  quote,
  segment,
  exchange,
}: {
  value: number;
  quote?: string;
  segment?: string;
  exchange?: string;
}) {
  const dir = usePriceFlash(value);
  const flashColor =
    dir === "up" ? "text-emerald-500" : dir === "down" ? "text-red-500" : "";
  return (
    <span
      className={cn(
        "whitespace-nowrap font-tabular tabular-nums transition-colors",
        flashColor,
      )}
    >
      {fmtFeedPrice(value, quote, segment, exchange)}
    </span>
  );
}

/**
 * Mobile-only card list for the Closed tab. Each row stacks side / qty /
 * status / product across the top and condenses the rest into right-
 * aligned `pnl / brokerage`, `entry → close`, and `open time → close
 * time` lines. The 9-column DataTable is unreadable on a phone, so the
 * Closed tab swaps to this presentation under `md:` while desktop keeps
 * the grid for fast scanning across many rows.
 */
function ClosedMobileList({ rows, loading }: { rows: any[]; loading: boolean }) {
  if (loading) {
    return (
      <div className="grid place-items-center py-10 text-sm text-muted-foreground">
        Loading…
      </div>
    );
  }
  if (!rows || rows.length === 0) {
    return (
      <div className="grid place-items-center py-10 text-sm text-muted-foreground">
        No closed positions yet.
      </div>
    );
  }
  return (
    <ul className="space-y-3">
      {rows.map((r) => (
        <ClosedMobileCard key={r.id} row={r} />
      ))}
    </ul>
  );
}

function ClosedMobileCard({ row: r }: { row: any }) {
  // Direction badge — opened_side survives the close (quantity is zero
  // on closed rows). Falls back to inferring from realized P&L when an
  // older row predates the field.
  const sideRaw = String(r.opened_side ?? "").toUpperCase();
  const side: "BUY" | "SELL" =
    sideRaw === "BUY" || sideRaw === "SELL"
      ? (sideRaw as "BUY" | "SELL")
      : Number(r.realized_pnl ?? 0) >= 0
        ? "BUY"
        : "SELL";

  const qty = Math.abs(Number(r.opening_quantity ?? r.quantity ?? 0));
  const pnl = Number(r.realized_pnl ?? 0);
  const charges = Number(r.charges ?? 0);

  const avg = r.avg_price;
  const close = r.ltp;

  // Sub-line: trading_symbol for option/future contracts (e.g.
  // SENSEX25MAY75000CE), exchange otherwise.
  const subLine =
    r.trading_symbol && r.trading_symbol !== r.symbol
      ? r.trading_symbol
      : r.exchange;
  const expiry = expiryChip(r);

  function timeOnly(v: string | null | undefined): string {
    if (!v) return "—";
    // Re-use formatIST then strip the leading "DD Mon, " prefix so the
    // card shows only the clock portion (matches the reference design).
    const full = formatIST(v, { withSeconds: true });
    const parts = full.split(", ");
    return parts.length > 1 ? parts.slice(1).join(", ") : full;
  }

  // Compact "DD Mon" date prefix for the timing row.  Closed cards
  // span multiple trading days once the user has any history, so the
  // earlier time-only render left ambiguity ("09:15 → 09:32" — but
  // which day?).
  function dateOnly(v: string | null | undefined): string {
    if (!v) return "";
    const full = formatIST(v, { withSeconds: false });
    const parts = full.split(", ");
    return parts.length > 1 ? parts[0] : "";
  }

  return (
    <li className="group relative overflow-hidden rounded-xl border border-border/70 bg-gradient-to-b from-card to-card/60 p-3.5 shadow-sm ring-1 ring-inset ring-white/5">
      {/* Subtle accent stripe — BUY = green, SELL = red. Same visual
          language as Active/Position cards for consistency. */}
      <span
        aria-hidden
        className={cn(
          "absolute inset-y-0 left-0 w-0.5",
          side === "BUY" ? "bg-buy/60" : "bg-sell/60",
        )}
      />

      {/* Top row: BUY/SELL · qty · CLOSED · product */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span
            className={cn(
              "rounded-md px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider ring-1 ring-inset",
              side === "BUY"
                ? "bg-buy/10 text-buy ring-buy/30"
                : "bg-sell/10 text-sell ring-sell/30",
            )}
          >
            {side}
          </span>
          <span className="font-tabular text-xs text-muted-foreground">
            <span className="opacity-70">Qty</span>{" "}
            <span className="font-semibold tabular-nums text-foreground/80">
              {fmtQty(qty)}
            </span>
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="rounded-md bg-muted/40 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-muted-foreground ring-1 ring-inset ring-border/70">
            CLOSED
          </span>
          <span className="rounded-md border border-border/70 bg-muted/30 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            {r.product_type}
          </span>
        </div>
      </div>

      {/* Symbol + sub-line on the left, P&L / brokerage on the right */}
      <div className="mt-2 flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="break-all text-[13px] font-bold leading-tight sm:text-sm">
            {r.symbol}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[10px] uppercase tracking-wide text-muted-foreground">
            <span className="truncate">{subLine}</span>
            {expiry ? (
              <span className="rounded bg-primary/10 px-1.5 py-0.5 font-semibold text-primary">
                Exp {expiry}
              </span>
            ) : null}
          </div>
        </div>
        <div className="shrink-0 text-right">
          <div
            className={cn(
              "font-tabular text-sm font-bold tabular-nums",
              pnlColor(pnl),
            )}
          >
            {formatINR(pnl, { withSymbol: false })}
            <span className="text-muted-foreground">
              /{formatINR(charges, { withSymbol: false })}
            </span>
          </div>
          <div className="mt-0.5 font-tabular text-[11px] text-muted-foreground">
            {fmtFeedPrice(avg, r.currency_quote, r.segment_type, r.exchange)}{" "}
            → {fmtFeedPrice(close, r.currency_quote, r.segment_type, r.exchange)}
          </div>
        </div>
      </div>

      {/* Order kind + timing.  Date prefix added so user can spot
          'kab ye trade lagaya tha' on cards from earlier days — was
          showing time-only which was confusing once the same symbol
          had multiple closes across days. */}
      <div className="mt-1.5 flex flex-wrap items-baseline justify-between gap-x-2 gap-y-0.5 text-[10px] text-muted-foreground">
        <span className="uppercase tracking-wider">Market → Market</span>
        <span className="whitespace-nowrap font-tabular">
          <span className="text-muted-foreground/80">{dateOnly(r.opened_at)}</span>{" "}
          {timeOnly(r.opened_at)} → {timeOnly(r.closed_at)}
        </span>
      </div>

      {/* Snapshot of SL/TP the user had set on this position at close
          time. Hidden when both are unset — most casual users don't
          attach brackets and the row stays cleaner. When EITHER was
          set the tile renders both values (the unset side reads "—")
          so the user always sees both legs side by side for easy
          comparison against the actual close price. The leg that
          fired (matching close_reason) gets a colour-filled chip
          treatment so it pops out as "the one that closed the trade". */}
      {(() => {
        const sl = r.close_stop_loss ?? r.stop_loss;
        const tp = r.close_target ?? r.target;
        const hasSL = sl && Number(sl) > 0;
        const hasTP = tp && Number(tp) > 0;
        if (!hasSL && !hasTP) return null;
        const reason = String(r.close_reason ?? "").toUpperCase();
        const slHit = reason === "SL_HIT";
        const tpHit = reason === "TP_HIT";
        return (
          <div className="mt-2 grid grid-cols-2 gap-1.5">
            <div
              className={cn(
                "rounded-md border border-border bg-muted/20 px-2.5 py-1.5",
                slHit && "border-sell/40 bg-sell/10",
              )}
            >
              <div className="text-[9px] uppercase tracking-wider text-muted-foreground">
                {slHit ? "SL · HIT" : "SL"}
              </div>
              <div className={cn("font-tabular text-sm font-semibold tabular-nums", hasSL ? "text-sell" : "text-muted-foreground/60")}>
                {hasSL
                  ? fmtFeedPrice(sl, r.currency_quote, r.segment_type, r.exchange)
                  : "—"}
              </div>
            </div>
            <div
              className={cn(
                "rounded-md border border-border bg-muted/20 px-2.5 py-1.5",
                tpHit && "border-buy/40 bg-buy/10",
              )}
            >
              <div className="text-[9px] uppercase tracking-wider text-muted-foreground">
                {tpHit ? "TP · HIT" : "TP"}
              </div>
              <div className={cn("font-tabular text-sm font-semibold tabular-nums", hasTP ? "text-buy" : "text-muted-foreground/60")}>
                {hasTP
                  ? fmtFeedPrice(tp, r.currency_quote, r.segment_type, r.exchange)
                  : "—"}
              </div>
            </div>
          </div>
        );
      })()}

      {/* Closed-reason chip when present (SL/TP/stop-out flag). Kept in a
          separate line so the main card stays clean for the common
          "USER" close. */}
      {r.close_reason && r.close_reason !== "USER" && (
        <div className="mt-1.5">
          <CloseReasonChip reason={r.close_reason} />
        </div>
      )}
    </li>
  );
}

/**
 * Mobile-only card list for the Active Trades tab. Stacks BUY/SELL · qty
 * · product · time across the top, big symbol + contract sub-line on
 * the left, P&L + bid/ask transition on the right, then Used / Holding
 * margin tiles paired with TP / SL / Exit controls. Desktop continues
 * to render the wide DataTable for many-row scanning.
 */
function ActiveMobileList({
  rows,
  loading,
  liveLtpFor,
  onEdit,
  onExit,
  onTrade,
  emptyLabel = "Nothing here yet",
  emptyHint,
  variant = "active",
}: {
  rows: any[];
  loading: boolean;
  // Now side-aware — pass "BUY"/"SELL" to get the real close-side
  // price (BID for BUY, ASK for SELL) instead of plain LTP.
  liveLtpFor: (row: any, side?: "BUY" | "SELL") => number;
  onEdit: (row: any, kind: "TP" | "SL") => void;
  onExit: (id: string) => void;
  /** "position" → the redesigned Position-tab card (no margin tiles, bold
   *  TP/SL/Exit). "active" → the original Active-trades card, unchanged. */
  variant?: "position" | "active";
  /** Mobile: tap on the card body fires this with the row's
   *  instrument token so the parent can open the slide-up
   *  TradeDetailSheet. Inner TP/SL/Exit buttons stopPropagation
   *  so they don't fire trade open simultaneously. */
  onTrade?: (token: string) => void;
  /** Tab-aware empty-state copy — the Position tab reads "No open
   *  positions" while the Active tab reads "No active trades". */
  emptyLabel?: string;
  emptyHint?: string;
}) {
  if (loading) {
    return (
      <div className="grid place-items-center py-10 text-sm text-muted-foreground">
        Loading…
      </div>
    );
  }
  if (!rows || rows.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-2.5 rounded-xl border border-dashed border-border/70 bg-muted/10 px-6 py-12 text-center">
        <div className="grid size-11 place-items-center rounded-full bg-muted/40 text-muted-foreground ring-1 ring-inset ring-border/60">
          <Inbox className="size-5" />
        </div>
        <p className="text-sm font-semibold text-foreground/90">{emptyLabel}</p>
        {emptyHint ? (
          <p className="max-w-[16rem] text-xs leading-relaxed text-muted-foreground">
            {emptyHint}
          </p>
        ) : null}
      </div>
    );
  }
  return (
    <ul className="space-y-3">
      {rows.map((r) => {
        // Pass side so the helper returns BID for BUY rows, ASK for
        // SELL — the same close-side price the matching engine fills
        // at. Without this the card was showing LTP under a "BID"
        // label, which mismatched both the broker's quote and the
        // P&L the user would actually realise on exit.
        const sideRaw = String(r?.opened_side ?? r?.action ?? r?.side ?? "")
          .toUpperCase();
        const side: "BUY" | "SELL" =
          sideRaw === "BUY" || sideRaw === "SELL"
            ? (sideRaw as "BUY" | "SELL")
            : Number(r?.quantity ?? 0) < 0
              ? "SELL"
              : "BUY";
        return (
          <ActiveMobileCard
            key={r.id}
            row={r}
            liveLtp={liveLtpFor(r, side)}
            onEdit={onEdit}
            onExit={onExit}
            onTrade={onTrade}
            variant={variant}
          />
        );
      })}
    </ul>
  );
}

function ActiveMobileCard({
  row: r,
  liveLtp,
  onEdit,
  onExit,
  onTrade,
  variant = "active",
}: {
  row: any;
  liveLtp: number;
  onEdit: (row: any, kind: "TP" | "SL") => void;
  onExit: (id: string) => void;
  onTrade?: (token: string) => void;
  variant?: "position" | "active";
}) {
  // The same card now drives both Active-trades rows (per-fill) AND
  // Position rows (per-instrument net). Field-name shims keep the
  // accessor identical for both shapes:
  //   • side       — `action`/`side` on active rows, `opened_side` on
  //                  positions; fall back to quantity-sign for legacy
  //                  position rows written before opened_side existed.
  //   • entry      — `price` on active rows, `avg_price` on positions.
  //   • timestamp  — `executed_at` (active) vs `opened_at` (position).
  //   • segment    — `segment` vs `segment_type`.
  const rawSide = (r.action ?? r.side ?? r.opened_side ?? "").toString().toUpperCase();
  const signedQty = Number(r.quantity ?? 0);
  const side: "BUY" | "SELL" =
    rawSide === "BUY" || rawSide === "SELL"
      ? (rawSide as "BUY" | "SELL")
      : signedQty >= 0
        ? "BUY"
        : "SELL";
  const qty = Math.abs(signedQty);
  const entry = Number(r.avg_price ?? r.price ?? 0);
  const ltp = liveLtp || Number(r.ltp ?? 0);
  const ts = r.executed_at ?? r.opened_at ?? null;
  const seg = r.segment ?? r.segment_type;
  // Same per-fill P&L formula the desktop column uses — raw INR, FX
  // disabled. Keeps mobile + desktop totals in lockstep.
  const dir = side === "SELL" ? -1 : 1;
  const pnl =
    ltp > 0 && entry > 0 && qty !== 0 ? dir * (ltp - entry) * qty : 0;

  // Margin figures — only rendered on the Active variant (the Position
  // card drops them per the user's request for a cleaner layout).
  const used = Number(r.margin ?? r.used_margin ?? r.margin_used ?? 0);
  const holding = holdingMarginFor(r);

  // Lock icon on the Position card = this instrument's market is closed
  // right now, so a market exit can't fill until it reopens.
  const marketClosed = !isInstrumentMarketOpen(seg, r.exchange);

  // Time-only renderer — strips the "DD Mon, " date prefix that
  // formatIST produces so the card matches the reference design's
  // clock-only style.
  function timeOnly(v: string | null | undefined): string {
    if (!v) return "—";
    const full = formatIST(v, { withSeconds: true });
    const parts = full.split(", ");
    return parts.length > 1 ? parts.slice(1).join(", ").replace(" IST", "") : full;
  }

  const subLine =
    r.trading_symbol && r.trading_symbol !== r.symbol
      ? r.trading_symbol
      : r.exchange;
  // Expiry derived from the symbol's monthly token (CRUDEOIL26JUNFUT
  // → "26 JUN"). Renders as a small chip next to the sub-line so the
  // user can see at a glance how far out the contract is — was
  // missing entirely on the mobile card before, the only way to tell
  // was to mentally parse the symbol.
  const expiry = expiryChip(r);

  // Tap-anywhere-on-card → open the slide-up trade sheet for this
  // instrument so the user can place a fresh BUY / SELL on the same
  // symbol without leaving the Positions page. Inner action buttons
  // (TP / SL / Exit) stopPropagation so they don't double-fire.
  const tradeToken =
    String(r?.instrument_token ?? r?.token ?? r?.instrument?.token ?? "") || "";
  const cardOpensTrade = !!onTrade && !!tradeToken;
  return (
    <li
      className={cn(
        "group relative overflow-hidden rounded-xl border border-border/70 bg-gradient-to-b from-card to-card/60 p-3.5 shadow-sm ring-1 ring-inset ring-white/5",
        cardOpensTrade && "cursor-pointer transition-all hover:border-primary/40 hover:shadow-md active:scale-[0.997]",
      )}
      onClick={cardOpensTrade ? () => onTrade!(tradeToken) : undefined}
    >
      {/* Subtle accent stripe on the left edge — BUY = green, SELL = red.
          Reads at a glance without occupying horizontal space. */}
      <span
        aria-hidden
        className={cn(
          "absolute inset-y-0 left-0 w-0.5",
          side === "BUY" ? "bg-buy/70" : "bg-sell/70",
        )}
      />

      {variant === "position" ? (
        /* ── POSITION card — premium broker-grade layout (Exness/Bybit-style
            reference). Row 1: side badge + symbol · live P&L + lock. Row 2:
            qty · product · time. Divider. Entry / LTP rows. Then ONE row of
            three equal outline buttons: TP (green) · SL (amber) · EXIT (red),
            each with a react (lucide) icon. */
        <>
          <div className="flex items-center justify-between gap-2">
            <div className="flex min-w-0 items-center gap-2">
              <span
                className={cn(
                  "rounded-md px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider ring-1 ring-inset",
                  side === "BUY"
                    ? "bg-buy/10 text-buy ring-buy/30"
                    : "bg-sell/10 text-sell ring-sell/30",
                )}
              >
                {side}
              </span>
              <span className="truncate text-[15px] font-bold leading-tight">
                {r.symbol}
              </span>
            </div>
            <div className="flex shrink-0 items-center gap-1.5">
              <span
                className={cn(
                  "font-tabular text-[15px] font-bold tabular-nums",
                  pnlColor(pnl),
                )}
              >
                {pnl >= 0 ? "+" : ""}
                {formatINR(pnl)}
              </span>
              {marketClosed ? (
                <Lock className="size-3.5 text-muted-foreground/70" />
              ) : null}
            </div>
          </div>

          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
            <span>
              <span className="opacity-70">Qty</span>{" "}
              <span className="font-semibold text-foreground/80">{fmtQty(qty)}</span>
            </span>
            <span className="opacity-40">•</span>
            <span className="font-semibold uppercase tracking-wide">{r.product_type}</span>
            <span className="opacity-40">•</span>
            <span className="font-tabular">{timeOnly(ts)}</span>
            {expiry ? (
              <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-primary">
                Exp {expiry}
              </span>
            ) : null}
          </div>

          <div className="my-2.5 border-t border-border/60" />

          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <span className="text-xs text-muted-foreground">Entry</span>
              <span className="font-tabular text-sm font-bold tabular-nums text-foreground">
                {fmtFeedPrice(entry, r.currency_quote, seg, r.exchange)}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-xs text-muted-foreground">LTP</span>
              <span
                className={cn(
                  "font-tabular text-sm font-bold tabular-nums",
                  pnlColor(pnl),
                )}
              >
                {fmtFeedPrice(ltp, r.currency_quote, seg, r.exchange)}
              </span>
            </div>
          </div>

          <div className="mt-3 grid grid-cols-3 gap-2">
            {/* TP — green outline. Shows the level when set, else "TP". */}
            <button
              type="button"
              onClick={(e) => {
                // stopPropagation so a single tap doesn't ALSO open the
                // tap-anywhere trade sheet on the card wrapper.
                e.stopPropagation();
                onEdit(r, "TP");
              }}
              className="flex h-9 items-center justify-center gap-1.5 rounded-[10px] border border-buy/40 bg-buy/5 px-1 text-xs font-bold text-buy transition-colors hover:bg-buy/15 active:scale-[0.98]"
            >
              <Target className="size-3.5" />
              <span className="font-tabular tabular-nums">
                {r.target ? Number(r.target).toFixed(2) : "TP"}
              </span>
            </button>
            {/* SL — amber/orange outline. */}
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onEdit(r, "SL");
              }}
              className="flex h-9 items-center justify-center gap-1.5 rounded-[10px] border border-amber-500/45 bg-amber-500/5 px-1 text-xs font-bold text-amber-600 transition-colors hover:bg-amber-500/15 active:scale-[0.98] dark:text-amber-400"
            >
              <Shield className="size-3.5" />
              <span className="font-tabular tabular-nums">
                {r.stop_loss ? Number(r.stop_loss).toFixed(2) : "SL"}
              </span>
            </button>
            {/* EXIT — red outline. */}
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onExit(r.id);
              }}
              className="flex h-9 items-center justify-center gap-1.5 rounded-[10px] border border-sell/45 bg-sell/5 px-1 text-xs font-bold text-sell transition-colors hover:bg-sell hover:text-white active:scale-[0.98]"
            >
              <LogOut className="size-3.5" />
              EXIT
            </button>
          </div>
        </>
      ) : (
        /* ── ACTIVE card — UNCHANGED. Original top + margins + TP/SL/Exit. */
        <>
        {/* Top row: BUY/SELL · qty · NRML/MIS · time */}
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <span
              className={cn(
                "rounded-md px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider ring-1 ring-inset",
                side === "BUY"
                  ? "bg-buy/10 text-buy ring-buy/30"
                  : "bg-sell/10 text-sell ring-sell/30",
              )}
            >
              {side}
            </span>
            <span className="font-tabular text-xs text-muted-foreground">
              <span className="opacity-70">Qty</span>{" "}
              <span className="font-semibold tabular-nums text-foreground/80">
                {fmtQty(qty)}
              </span>
            </span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="rounded-md border border-border/70 bg-muted/30 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
              {r.product_type}
            </span>
            <span className="rounded-md border border-border/70 bg-muted/20 px-1.5 py-0.5 font-tabular text-[10px] text-muted-foreground">
              {timeOnly(ts)}
            </span>
          </div>
        </div>

        <div className="mt-2 flex items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <div className="break-all text-[13px] font-bold leading-tight sm:text-sm">
              {r.symbol}
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[10px] uppercase tracking-wide text-muted-foreground">
              <span className="truncate">{subLine}</span>
              {expiry ? (
                <span className="rounded bg-primary/10 px-1.5 py-0.5 font-semibold text-primary">
                  Exp {expiry}
                </span>
              ) : null}
            </div>
          </div>
          <div className="shrink-0 text-right">
            <div
              className={cn(
                "font-tabular text-base font-bold tabular-nums",
                pnlColor(pnl),
              )}
            >
              {formatINR(pnl, { withSymbol: false })}
            </div>
            <div className="mt-0.5 font-tabular text-[11px] text-muted-foreground">
              {fmtFeedPrice(entry, r.currency_quote, seg, r.exchange)}{" "}
              → {fmtFeedPrice(ltp, r.currency_quote, seg, r.exchange)}{" "}
              <span className="uppercase">{side === "BUY" ? "BID" : "ASK"}</span>
            </div>
          </div>
        </div>

        <div className="mt-3 grid grid-cols-2 gap-2">
          <div className="space-y-1.5">
            <div className="rounded-md border border-border bg-muted/20 px-2.5 py-1.5">
              <div className="text-[9px] uppercase tracking-wider text-muted-foreground">
                Used Margin
              </div>
              <div className="font-tabular text-sm font-semibold tabular-nums">
                {formatINR(used)}
              </div>
            </div>
            <div className="rounded-md border border-border bg-muted/20 px-2.5 py-1.5">
              <div className="text-[9px] uppercase tracking-wider text-muted-foreground">
                Holding Margin
              </div>
              <div className="font-tabular text-sm font-semibold tabular-nums">
                {formatINR(holding)}
              </div>
            </div>
          </div>
          <div className="space-y-1.5">
            <div className="grid grid-cols-2 gap-1.5">
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  onEdit(r, "TP");
                }}
                className="rounded-md border border-dashed border-border px-2 py-1.5 text-[11px] font-semibold hover:bg-muted/40"
              >
                TP{" "}
                <span className="font-tabular tabular-nums">
                  {r.target ? Number(r.target).toFixed(2) : "Add +"}
                </span>
              </button>
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  onEdit(r, "SL");
                }}
                className="rounded-md border border-dashed border-border px-2 py-1.5 text-[11px] font-semibold hover:bg-muted/40"
              >
                SL{" "}
                <span className="font-tabular tabular-nums">
                  {r.stop_loss ? Number(r.stop_loss).toFixed(2) : "Add +"}
                </span>
              </button>
            </div>
            <Button
              size="sm"
              onClick={(e) => {
                e.stopPropagation();
                onExit(r.id);
              }}
              className="h-9 w-full gap-1 rounded-md bg-destructive/15 px-2.5 text-xs font-semibold text-destructive ring-1 ring-inset ring-destructive/30 hover:bg-destructive hover:text-destructive-foreground hover:ring-destructive"
            >
              <LogOut className="size-3.5" /> Exit
            </Button>
          </div>
        </div>
        </>
      )}

      {/* Per-position Carry-Forward toggle — only when the admin enabled the
          feature. Default OFF ⇒ EOD squares off; ON ⇒ overnight carry. */}
      {r.carry_forward_enabled ? <CarryForwardToggle row={r} /> : null}
    </li>
  );
}

/** Short Web-Audio beep on toggle — higher pitch for ON, lower for OFF.
 *  No asset needed; silently no-ops if the browser blocks audio. */
function playCarryBeep(on: boolean) {
  try {
    const AC = (window as any).AudioContext || (window as any).webkitAudioContext;
    if (!AC) return;
    const ctx = new AC();
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.connect(g);
    g.connect(ctx.destination);
    o.type = "sine";
    o.frequency.value = on ? 880 : 420;
    g.gain.setValueAtTime(0.0001, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.16, ctx.currentTime + 0.01);
    g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.2);
    o.start();
    o.stop(ctx.currentTime + 0.22);
    o.onended = () => ctx.close();
  } catch {
    /* audio blocked — ignore */
  }
}

function CarryForwardToggle({ row }: { row: any }) {
  const qc = useQueryClient();
  const [on, setOn] = useState(!!row.user_carry_forward);
  const [busy, setBusy] = useState(false);
  // Seed from the server ONLY when the card is for a different position. A
  // refetch of the SAME position must not stomp the toggle the user just set —
  // that was the "tapped ON but it flips back OFF" bug: the next positions
  // poll (which may not yet reflect the write) reset the optimistic state.
  useEffect(() => {
    setOn(!!row.user_carry_forward);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [row.id]);

  async function toggle(next: boolean) {
    if (busy) return;
    setBusy(true);
    setOn(next); // optimistic — no popup, flips immediately
    playCarryBeep(next);
    try {
      await PositionAPI.setCarryForward(row.id, next);
      toast.success(
        next ? "Carry Forward ON — overnight hold" : "Carry Forward OFF — squares off at close",
      );
      qc.invalidateQueries({ queryKey: ["positions"] });
    } catch (e: any) {
      setOn(!next); // revert on failure
      toast.error(e?.message || "Could not update carry-forward");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      onClick={(e) => e.stopPropagation()}
      className={cn(
        "mt-2.5 flex items-center justify-between rounded-lg border px-3 py-2",
        on ? "border-amber-500/40 bg-amber-500/10" : "border-border bg-muted/20",
      )}
    >
      <div className="flex items-center gap-1.5">
        <span aria-hidden>⚡</span>
        <span className="text-xs font-semibold">Carry Forward</span>
        <span className={cn("text-[11px] font-bold", on ? "text-amber-500" : "text-muted-foreground")}>
          {on ? "ON" : "OFF"}
        </span>
      </div>
      <button
        type="button"
        disabled={busy}
        aria-pressed={on}
        aria-label={on ? "Turn carry-forward off" : "Turn carry-forward on"}
        onClick={(e) => {
          e.stopPropagation();
          void toggle(!on);
        }}
        className={cn(
          "relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors disabled:opacity-50",
          on ? "bg-emerald-500" : "bg-muted-foreground/30",
        )}
      >
        <span
          className={cn(
            "inline-block size-4 rounded-full bg-white shadow transition-transform",
            on ? "translate-x-6" : "translate-x-1",
          )}
        />
      </button>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────
// Pending-order mobile list + card + edit dialog
// ─────────────────────────────────────────────────────────────────
function PendingMobileList({
  rows,
  loading,
  onEdit,
  onCancel,
}: {
  rows: any[];
  loading: boolean;
  onEdit: (order: any) => void;
  onCancel: (id: string) => void;
}) {
  if (loading) {
    return (
      <div className="grid place-items-center py-10 text-sm text-muted-foreground">
        Loading…
      </div>
    );
  }
  if (!rows || rows.length === 0) {
    return (
      <div className="grid place-items-center py-10 text-sm text-muted-foreground">
        No pending orders.
      </div>
    );
  }
  return (
    <ul className="space-y-3">
      {rows.map((o) => (
        <PendingOrderCard
          key={o.id}
          o={o}
          onEdit={() => onEdit(o)}
          onCancel={() => onCancel(o.id)}
        />
      ))}
    </ul>
  );
}

function PendingOrderCard({
  o,
  onEdit,
  onCancel,
}: {
  o: any;
  onEdit: () => void;
  onCancel: () => void;
}) {
  const side: "BUY" | "SELL" =
    String(o?.action ?? "").toUpperCase() === "SELL" ? "SELL" : "BUY";
  const lots = Number(o?.lots ?? 0);
  const qty = Number(o?.quantity ?? lots * Number(o?.lot_size ?? 1));
  const orderType = String(o?.order_type ?? "").toUpperCase();
  const price = Number(o?.price ?? 0);
  const trigger = Number(o?.trigger_price ?? 0);
  const ts = o?.created_at ?? o?.placed_at ?? null;
  const status = String(o?.status ?? "").toUpperCase();
  return (
    <li className="group relative overflow-hidden rounded-xl border border-border/70 bg-gradient-to-b from-card to-card/60 p-3.5 shadow-sm ring-1 ring-inset ring-white/5">
      <span
        aria-hidden
        className={cn(
          "absolute inset-y-0 left-0 w-0.5",
          side === "BUY" ? "bg-buy/60" : "bg-sell/60",
        )}
      />
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span
            className={cn(
              "rounded-md px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider ring-1 ring-inset",
              side === "BUY"
                ? "bg-buy/10 text-buy ring-buy/30"
                : "bg-sell/10 text-sell ring-sell/30",
            )}
          >
            {side}
          </span>
          <span className="font-tabular text-xs text-muted-foreground">
            <span className="opacity-70">Qty</span>{" "}
            <span className="font-semibold tabular-nums text-foreground/80">
              {fmtQty(qty || lots)}
            </span>
          </span>
          <span className="rounded-md border border-border/70 bg-muted/30 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            {orderType || "—"}
          </span>
        </div>
        <span className="rounded-md bg-amber-500/10 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-amber-500 ring-1 ring-inset ring-amber-500/30">
          {status || "PENDING"}
        </span>
      </div>

      <div className="mt-2 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-base font-bold leading-tight">
            {o?.symbol ?? o?.instrument?.symbol ?? "—"}
          </div>
          <div className="mt-0.5 text-[11px] uppercase tracking-wide text-muted-foreground">
            {o?.exchange ?? o?.segment ?? "—"}
            {ts && (
              <span className="ml-2 font-tabular normal-case text-muted-foreground/80">
                {formatIST(ts, { withSeconds: false })}
              </span>
            )}
          </div>
        </div>
        <div className="shrink-0 text-right">
          {orderType === "LIMIT" && (
            <div className="font-tabular text-sm font-semibold tabular-nums">
              ₹{price.toFixed(2)}
              <span className="ml-1 text-[10px] font-normal uppercase text-muted-foreground">
                limit
              </span>
            </div>
          )}
          {(orderType === "SL_M" || orderType === "SL") && (
            <div className="font-tabular text-sm font-semibold tabular-nums">
              ₹{trigger.toFixed(2)}
              <span className="ml-1 text-[10px] font-normal uppercase text-muted-foreground">
                trigger
              </span>
            </div>
          )}
          {orderType === "MARKET" && (
            <div className="text-xs text-muted-foreground">market</div>
          )}
        </div>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={onEdit}
          className="h-9 gap-1 rounded-md text-xs font-semibold"
        >
          <Pencil className="size-3.5" /> Edit
        </Button>
        <Button
          size="sm"
          onClick={onCancel}
          className="h-9 gap-1 rounded-md bg-destructive/15 text-xs font-semibold text-destructive ring-1 ring-inset ring-destructive/30 hover:bg-destructive hover:text-destructive-foreground hover:ring-destructive"
        >
          <X className="size-3.5" /> Cancel
        </Button>
      </div>
    </li>
  );
}

function EditPendingOrderDialog({
  order,
  onClose,
  onSaved,
}: {
  order: any | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [lots, setLots] = useState<string>("");
  const [price, setPrice] = useState<string>("");
  const [trigger, setTrigger] = useState<string>("");
  const [busy, setBusy] = useState(false);

  // Re-seed the form whenever a new order opens the dialog.
  useMemo(() => {
    if (!order) return;
    setLots(String(order.lots ?? ""));
    setPrice(String(order.price ?? ""));
    setTrigger(String(order.trigger_price ?? ""));
  }, [order?.id]);

  if (!order) return null;
  const orderType = String(order.order_type ?? "").toUpperCase();
  const showPrice = orderType === "LIMIT";
  const showTrigger = orderType === "SL_M" || orderType === "SL";

  async function submit() {
    const lotsN = Number(lots);
    if (!Number.isFinite(lotsN) || lotsN <= 0) {
      toast.error("Lots must be > 0");
      return;
    }
    const body: any = { lots: lotsN };
    if (showPrice) {
      const pN = Number(price);
      if (!Number.isFinite(pN) || pN <= 0) {
        toast.error("Limit price must be > 0");
        return;
      }
      body.price = pN;
    }
    if (showTrigger) {
      const tN = Number(trigger);
      if (!Number.isFinite(tN) || tN <= 0) {
        toast.error("Trigger price must be > 0");
        return;
      }
      body.trigger_price = tN;
    }
    setBusy(true);
    try {
      await OrderAPI.modify(order.id, body);
      toast.success("Order updated");
      onSaved();
    } catch (e: any) {
      toast.error(e?.message || "Modify failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={!!order} onOpenChange={(o) => { if (!o) onClose(); }}>
      <DialogContent className="max-w-sm gap-3 p-5">
        <DialogTitle className="text-base font-semibold">
          Edit {order.symbol ?? "order"}
        </DialogTitle>

        <div className="rounded-lg border border-border bg-muted/30 px-3 py-2 text-xs">
          <div className="flex items-center justify-between">
            <span className="text-muted-foreground">Side</span>
            <span
              className={cn(
                "font-semibold",
                String(order.action).toUpperCase() === "BUY" ? "text-buy" : "text-sell",
              )}
            >
              {String(order.action).toUpperCase()}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-muted-foreground">Type</span>
            <span className="font-semibold">{orderType || "—"}</span>
          </div>
        </div>

        <div className="space-y-3">
          <div className="space-y-1">
            <Label className="text-xs">Lots</Label>
            <Input
              inputMode="decimal"
              value={lots}
              onChange={(e) =>
                setLots(e.target.value.replace(/[^0-9.]/g, "").replace(/(\..*)\./g, "$1"))
              }
              className="h-10"
              autoFocus
            />
          </div>
          {showPrice && (
            <div className="space-y-1">
              <Label className="text-xs">Limit price</Label>
              <Input
                inputMode="decimal"
                value={price}
                onChange={(e) =>
                  setPrice(e.target.value.replace(/[^0-9.]/g, "").replace(/(\..*)\./g, "$1"))
                }
                className="h-10"
              />
            </div>
          )}
          {showTrigger && (
            <div className="space-y-1">
              <Label className="text-xs">Trigger price</Label>
              <Input
                inputMode="decimal"
                value={trigger}
                onChange={(e) =>
                  setTrigger(e.target.value.replace(/[^0-9.]/g, "").replace(/(\..*)\./g, "$1"))
                }
                className="h-10"
              />
            </div>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 pt-1">
          <Button variant="ghost" size="sm" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button size="sm" onClick={submit} loading={busy} disabled={busy}>
            Save changes
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
