"use client";

import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Pencil, X, Zap } from "lucide-react";
import { InstrumentAPI, OrderAPI, PositionAPI } from "@/lib/api";
import { isOptimisticId, resolveRealPositionId } from "@/lib/positionIds";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn, exactTimestamp, formatINR, formatPrice, isUsdSegment, pnlColor } from "@/lib/utils";
import { isInstrumentMarketOpen, marketLabel } from "@/lib/marketHours";
import { useMarketStream } from "@/lib/useMarketStream";
import { playClosedTone } from "@/lib/trade-audio";
import { usePriceFlash } from "@/lib/usePriceFlash";

/**
 * Resolve the displayed lot count + total quantity for a position / trade
 * row. We prefer values the server echoes back, but fall back to the
 * client-side canonical Indian-index helper when the stored numbers look
 * stale (e.g. position opened before the lot-size backfill landed, where
 * `quantity` was saved as `lots × 1` instead of `lots × 75`).
 *
 *   • `lots`  → integer lot count shown in the new LOT column
 *   • `qty`   → real exchange quantity shown in the SIZE column and used
 *                for the recomputed P/L
 */
function resolveQty(row: any): { lots: number; qty: number; lotSize: number } {
  const rawQty = Math.abs(Number(row?.quantity ?? 0));
  const serverLots = Number(row?.lots ?? 0);
  // Position docs embed the snapshot as `instrument.lot_size`, while orders/
  // trades sometimes serialize the field at the top level. Trust whichever
  // is set — the backend already sources F&O lots from Zerodha's CSV (for
  // NSE/BSE) and the canonical MCX table.
  const lotSize = Number(row?.lot_size ?? row?.instrument?.lot_size ?? 0) || 1;
  let lots = serverLots;
  if (!lots || !Number.isFinite(lots)) {
    lots = lotSize > 0 ? rawQty / lotSize : rawQty;
  }
  lots = Math.abs(lots);
  // SIZE = the stored contract qty when present (already in shares /
  // contracts), otherwise lots × lot_size. We don't round `lots` here
  // because MCX / crypto / forex trade fractional units.
  const qty = rawQty > 0 ? rawQty : lots * lotSize;
  return { lots, qty, lotSize };
}

interface Props {
  positions: any[];
  pendingOrders: any[];
  history: any[];
  cancelled: any[];
  totalPnL: number;
  /** Which tab to land on when the panel first mounts. Defaults to
   *  "positions" but the Orders rail-toggle opens the drawer on "pending"
   *  so the user sees their order book straight away. */
  initialTab?: TabKey;
}

const ONE_CLICK_KEY = "setupfx.terminal.oneClick";

type TabKey = "positions" | "active" | "pending" | "history" | "cancelled";

// 13 columns: TIME · SYM · M · SIDE · LOT · SIZE · ENTRY · CURRENT · S/L · T/P · COMM · P/L · ACTION
// LOT shows the count of lots the trader bought/sold; SIZE shows real
// exchange contracts (lots × canonical lot size). Splitting them lines up
// with how every Indian broker (Zerodha / Upstox / Dhan) displays F&O
// positions and stops the user wondering whether "3" means three lots
// or three contracts.
const COL_TEMPLATE =
  "minmax(80px,80px) minmax(110px,1fr) 50px 60px 50px 70px minmax(80px,1fr) minmax(80px,1fr) minmax(80px,1fr) minmax(80px,1fr) 60px minmax(80px,1fr) minmax(96px,120px)";

// A close that errors with any of these is treated as "don't scare the user,
// just reconcile from the server": the position is either already gone, or
// the close is already in flight, or the response simply didn't reach us
// (network / timeout / 5xx — the B-Book close usually went through anyway).
// Surfacing these as red toasts made users re-tap, which then hit the 10 s
// idempotency lock → "already in flight" → another re-tap, forever. Letting
// the 2 s positions poll settle the truth is both calmer and correct.
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

export function PositionsTabs({ positions, pendingOrders, history, cancelled, totalPnL, initialTab = "positions" }: Props) {
  const qc = useQueryClient();
  const [tab, setTab] = useState<TabKey>(initialTab);

  // One-Click trading mode persists across reloads — once a trader opts in,
  // they shouldn't have to re-tick it every session. Window-guarded for SSR.
  const [oneClick, setOneClick] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined") return;
    setOneClick(window.localStorage.getItem(ONE_CLICK_KEY) === "1");
  }, []);
  function toggleOneClick(v: boolean) {
    setOneClick(v);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(ONE_CLICK_KEY, v ? "1" : "0");
    }
    // Broadcast so OrderPanel (separate component tree) can react too.
    window.dispatchEvent(new CustomEvent("oneclick:change", { detail: v }));
  }

  const [editing, setEditing] = useState<any | null>(null);
  // Separate state for the pending-order modify dialog. Reusing the SL/TP
  // `editing` slot would force one dialog to handle two unrelated
  // contracts (position SL/TP vs pending-order price/lots) — cleaner to
  // keep them parallel.
  const [editingOrder, setEditingOrder] = useState<any | null>(null);

  // ── Active Trades: one row per fill that's still part of an open
  // position. Lets the trader close / edit each entry individually instead
  // of dealing with the aggregated weighted-avg position.
  //
  // Polling is paused for 3 s after each optimistic update — same
  // anti-flicker pattern as the terminal page's positions/orders polls.
  // Without this an immediate poll often returns server data that's
  // ~100–500 ms behind a just-written close, briefly resurrecting the
  // row we just removed.
  const { data: activeTrades } = useQuery<any[]>({
    queryKey: ["active-trades"],
    queryFn: () => PositionAPI.activeTrades(),
    refetchInterval: (query: any) => {
      // 2 s baseline, widened to 3.5 s for the 3 s post-optimistic
      // window. Returning `false` here used to permanently stall the
      // polling loop after the first optimistic write — the symptom
      // was an active-trade row reappearing for one tick after close
      // and then never refreshing again.
      const last = (query?.state?.dataUpdatedAt as number) || 0;
      return Date.now() - last < 3000 ? 3500 : 2000;
    },
  });

  // ── Single live-price source for EVERY row + total ──────────────────
  // Positions (aggregate) and Active Trades (per-fill) come from DIFFERENT
  // backend endpoints polled at DIFFERENT times, so the same BTCUSD showed a
  // different CURRENT price and P/L in each tab (and vs the footer). Operator-
  // flagged. Fix: subscribe to the live WS ONCE here and recompute CURRENT +
  // P/L for both row types (and the tab totals) from that ONE snapshot, so
  // every view agrees. Market-closed instruments (NSE/BSE on a weekend) freeze
  // at the server's stored close-side mark — only 24×7 crypto/forex move live.
  const streamTokens = useMemo<string[]>(() => {
    const set = new Set<string>();
    for (const p of positions ?? []) {
      const t = p.token ?? p.instrument_token ?? p.instrument?.token;
      if (t) set.add(String(t));
    }
    for (const t of activeTrades ?? []) {
      const tok = t.token ?? t.instrument_token;
      if (tok) set.add(String(tok));
    }
    return Array.from(set);
  }, [positions, activeTrades]);

  const liveQuotes = useMarketStream(streamTokens);

  // Close-side mark: a long closes at the BID, a short at the ASK — exactly
  // what the backend marks against, so client P/L agrees with the server's.
  function liveLtpFor(row: any, side: "BUY" | "SELL"): number {
    const seg = row?.segment_type ?? row?.segment;
    const exch = row?.exchange;
    // Market closed → keep the server's frozen mark (no weekend drift).
    if (!isInstrumentMarketOpen(seg, exch)) return Number(row?.ltp ?? 0);
    const tok = row?.token ?? row?.instrument_token ?? row?.instrument?.token;
    const q = tok ? liveQuotes.get(String(tok)) : undefined;
    if (!q) return Number(row?.ltp ?? 0);
    const bid = Number(q.bid ?? 0);
    const ask = Number(q.ask ?? 0);
    const ltp = Number(q.ltp ?? 0);
    if (side === "BUY" && bid > 0) return bid;
    if (side === "SELL" && ask > 0) return ask;
    if (ltp > 0) return ltp;
    return Number(row?.ltp ?? 0);
  }

  // Only the ACTIVE TRADES need re-overlaying here: the terminal page already
  // passes `positions` (positionsLive) and `totalPnL` computed from the SAME
  // live WS, so the Positions tab + footer already agree. Active trades are
  // fetched independently inside this component and were NOT overlaid — that
  // was the outlier tab. Override `ltp` + `pnl` with the live close-side price
  // using the existing `derivedPnl` formula (price − avg) × qty — NO new
  // logic — so the Active tab now matches the Positions tab + footer exactly.
  const liveActiveTrades = useMemo(() => (activeTrades ?? []).map((t: any) => {
    const side = String(t.action).toUpperCase() === "BUY" ? "BUY" : "SELL";
    const price = liveLtpFor(t, side);
    if (!(price > 0)) return t;
    const { qty } = resolveQty(t);
    const avg = Number(t.price ?? 0);
    const pnl = (avg > 0 && qty > 0)
      ? (side === "BUY" ? price - avg : avg - price) * qty
      : Number(t.pnl ?? 0);
    return { ...t, ltp: price, pnl };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [activeTrades, liveQuotes]);

  const tabs: { key: TabKey; label: string; count: number }[] = [
    { key: "positions", label: "Positions", count: positions.length },
    { key: "active", label: "Active Trades", count: activeTrades?.length ?? 0 },
    { key: "pending", label: "Pending", count: pendingOrders.length },
    { key: "history", label: "History", count: history.length },
    { key: "cancelled", label: "Cancelled", count: cancelled.length },
  ];

  // ── Live P&L for filled history rows ────────────────────────────
  // Each row is a closed/filled order; the P/L column shows what the
  // trade is worth right now (current_LTP vs fill_price × qty × side).
  const historyTokens = useMemo<string[]>(() => {
    const set = new Set<string>();
    for (const o of history) {
      const tok = o.token || o.instrument_token;
      if (tok) set.add(String(tok));
    }
    return Array.from(set);
  }, [history]);

  const { data: historyQuotes } = useQuery<any[]>({
    queryKey: ["history-quotes", historyTokens.sort().join(",")],
    queryFn: () => InstrumentAPI.quotesBatch(historyTokens),
    enabled: tab === "history" && historyTokens.length > 0,
    refetchInterval: 1500,
    staleTime: 1000,
  });

  const historyLtp = useMemo<Record<string, number>>(() => {
    const m: Record<string, number> = {};
    for (const q of historyQuotes ?? []) {
      const ltp = Number(q.ltp ?? 0);
      if (ltp > 0 && q.token) m[String(q.token)] = ltp;
    }
    return m;
  }, [historyQuotes]);

  // Live USD/INR rate so per-history P&L (and the tab total below) reflect
  // wallet INR for crypto/forex trades, not raw USD.
  const { data: pnlSummary } = useQuery({
    queryKey: ["positions", "pnl-summary"],
    queryFn: () => PositionAPI.pnlSummary(),
    refetchInterval: 10000,
  });
  const usdInr = Number(pnlSummary?.usd_inr_rate ?? 83);

  // Tab-aware P/L: positions tab shows open M2M (already INR from backend);
  // history tab now sums the SERVER-frozen `pnl_inr` per closing order so
  // the footer total matches the per-row figures (also frozen) — no more
  // live drift, no more USD/INR mixing.
  const historyTotalInr = useMemo(() => {
    let sum = 0;
    for (const o of history) {
      const v = o.pnl_inr;
      if (v === null || v === undefined || v === "") continue;
      const n = Number(v);
      if (Number.isFinite(n)) sum += n;
    }
    return sum;
  }, [history]);

  // Active-trades total summed from the SAME live-enriched rows above, so the
  // Active tab total equals the sum of its visible rows AND the Positions tab
  // total (both now derive from the one live WS close-side snapshot).
  const activeTotalInr = useMemo(
    () => (liveActiveTrades ?? []).reduce((s, t: any) => s + Number(t.pnl || 0), 0),
    [liveActiveTrades],
  );

  const tabPnL =
    tab === "positions" ? totalPnL :
    tab === "active" ? activeTotalInr :
    tab === "history" ? historyTotalInr :
    0;

  // ─── Pro-terminal close pattern ──────────────────────────────────────
  // All three close/cancel handlers below are FIRE-AND-FORGET:
  //   1. Optimistic UI update (remove the row immediately)
  //   2. Audio cue (instant feedback)
  //   3. POST in the background (no `await` — button stays responsive)
  //   4. On success → toast + invalidate caches (real fill replaces row)
  //   5. On error → rollback + error toast
  // Backend is fast; the perceived lag was the awaited promise gating the
  // click handler. Now the button releases the moment it's pressed.

  function closeActiveTrade(
    tradeId: string,
    symbol: string,
    positionId?: string,
    tradeQty?: number,
    segmentType?: string,
    exchange?: string,
  ) {
    // Market-hours guard REMOVED for closes. B-Book: users must always
    // be able to exit positions. Server allows is_squareoff anytime.

    // Mobile UX — skip the native confirm() on phones (user spec:
    // "close karne par pop mat aaye, direct close ho jaye"); desktop
    // still gets the confirm step. one-click trade mode already
    // bypasses the confirm regardless of viewport.
    const isMobileUi =
      typeof window !== "undefined" &&
      window.matchMedia("(max-width: 767px)").matches;
    if (!oneClick && !isMobileUi && !confirm(`Close this ${symbol} trade at market?`)) return;
    playClosedTone();

    qc.cancelQueries({ queryKey: ["active-trades"] });
    qc.cancelQueries({ queryKey: ["positions", "open"] });

    const tradesSnapshot = qc.getQueryData<any[]>(["active-trades"]);
    const posSnapshot = qc.getQueryData<any[]>(["positions", "open"]);

    // Optimistic: drop the active-trade row.
    qc.setQueryData<any[]>(["active-trades"], (old) =>
      Array.isArray(old) ? old.filter((t) => t.id !== tradeId) : []
    );

    // Optimistic: reduce the parent position's qty by the trade's qty
    // (or remove the row entirely if this is the last open fill). Keeps
    // the Positions tab in sync with Active Trades without waiting for
    // the next poll.
    if (positionId && tradeQty && tradeQty > 0) {
      qc.setQueryData<any[]>(["positions", "open"], (old) => {
        if (!Array.isArray(old)) return [];
        return old
          .map((p) => {
            if (p.id !== positionId) return p;
            const curQty = Number(p.quantity) || 0;
            const sign = curQty >= 0 ? 1 : -1;
            const nextAbs = Math.max(0, Math.abs(curQty) - tradeQty);
            const nextQty = nextAbs * sign;
            return nextAbs < 1e-9 ? null : { ...p, quantity: nextQty };
          })
          .filter(Boolean) as any[];
      });
    }

    // Pop the close-confirmation toast synchronously so it appears in
    // the same frame as the optimistic row removal. Dismissed on failure.
    const pendingToastId = toast.success(`Closed ${symbol}`, { duration: 1500 });

    PositionAPI.closeActiveTrade(tradeId)
      .then(() => {
        // No active-trades / positions invalidate here — eventual write
        // visibility on Atlas causes a flicker. 2 s poll handles it.
        qc.invalidateQueries({ queryKey: ["orders"] });
        qc.invalidateQueries({ queryKey: ["wallet"] });
        // Refresh the Closed tab so the just-closed slice (partial OR
        // full) shows up immediately instead of needing a manual
        // refresh. Delayed retry covers Atlas read-replica lag.
        qc.invalidateQueries({ queryKey: ["positions", "closed"] });
        setTimeout(
          () => qc.invalidateQueries({ queryKey: ["positions", "closed"] }),
          1500,
        );
      })
      .catch((e: any) => {
        if (isBenignCloseError(e)) {
          // 409 "already in flight" (our own retry / a parallel close) OR a
          // network / timeout / 5xx where the response just didn't reach us —
          // the close is very likely already happening or done. DON'T scare
          // the user or roll the row back: nudge a reconcile and let the 2 s
          // positions poll settle the truth. This is what stops the
          // "Network Error → user retries → already in flight" loop.
          qc.invalidateQueries({ queryKey: ["active-trades"] });
          qc.invalidateQueries({ queryKey: ["positions", "open"] });
          qc.invalidateQueries({ queryKey: ["positions", "closed"] });
          qc.invalidateQueries({ queryKey: ["wallet"] });
          return;
        }
        // Genuine rejection (e.g. "no open position") — restore + surface it.
        if (tradesSnapshot) qc.setQueryData(["active-trades"], tradesSnapshot);
        if (posSnapshot) qc.setQueryData(["positions", "open"], posSnapshot);
        toast.dismiss(pendingToastId);
        toast.error(e.message || "Close failed");
      });
  }

  function squareoff(id: string, symbol: string, segmentType?: string, exchange?: string) {
    // Market-hours guard REMOVED for closes. B-Book: users must always
    // be able to exit positions. Server allows is_squareoff anytime.
    playClosedTone();

    // Cancel BOTH queries — closing a position kills its Active Trades
    // rows too (since they're just the BUY fills against this position).
    qc.cancelQueries({ queryKey: ["positions", "open"] });
    qc.cancelQueries({ queryKey: ["active-trades"] });

    const posSnapshot = qc.getQueryData<any[]>(["positions", "open"]);
    const tradesSnapshot = qc.getQueryData<any[]>(["active-trades"]);

    // Optimistically drop the position row…
    qc.setQueryData<any[]>(["positions", "open"], (old) =>
      Array.isArray(old) ? old.filter((p) => p.id !== id) : []
    );
    // …and every Active Trades row whose position_id matches. Without
    // this the Active Trades tab keeps showing 4 stale BUY rows for
    // ~2 s after the position has already vanished from Positions.
    qc.setQueryData<any[]>(["active-trades"], (old) =>
      Array.isArray(old) ? old.filter((t) => t.position_id !== id) : []
    );

    // Synchronous success toast — pairs with the optimistic position
    // removal so the popup pops in the same frame as the click.
    const pendingToastId = toast.success(`Closed ${symbol} at market`, {
      duration: 1500,
    });

    // A row placed seconds ago still carries its synthetic id; the server
    // can't parse that as an ObjectId and answers "Position not found".
    (isOptimisticId(id)
      ? resolveRealPositionId(qc, id).then((real) => {
          if (!real) throw new Error("Order is still settling — try again in a moment");
          return PositionAPI.squareoff(real);
        })
      : PositionAPI.squareoff(id)
    )
      .then(() => {
        // DO NOT invalidate positions/active-trades here — see OrderPanel
        // comment. Atlas can briefly return the position as still-OPEN
        // immediately after the close write, causing a 1 s flicker where
        // the row reappears. The 2 s polling handles the eventual sync.
        qc.invalidateQueries({ queryKey: ["orders"] });
        qc.invalidateQueries({ queryKey: ["wallet"] });
        // Closed tab must refresh at once so the closed slice appears
        // without a manual refresh. Delayed retry covers replica lag.
        qc.invalidateQueries({ queryKey: ["positions", "closed"] });
        setTimeout(
          () => qc.invalidateQueries({ queryKey: ["positions", "closed"] }),
          1500,
        );
      })
      .catch((e: any) => {
        if (isBenignCloseError(e)) {
          // Already-gone / in-flight / network / timeout — keep the
          // optimistic removal (re-adding a ghost row just makes the user
          // re-tap → hit the lock → loop) and reconcile from the server.
          qc.invalidateQueries({ queryKey: ["positions", "open"] });
          qc.invalidateQueries({ queryKey: ["active-trades"] });
          qc.invalidateQueries({ queryKey: ["positions", "closed"] });
          qc.invalidateQueries({ queryKey: ["wallet"] });
          return;
        }
        // Genuine rejection (e.g. stale-feed "retry in a few seconds") —
        // restore the row + surface the actionable message.
        toast.dismiss(pendingToastId);
        if (posSnapshot) qc.setQueryData(["positions", "open"], posSnapshot);
        if (tradesSnapshot) qc.setQueryData(["active-trades"], tradesSnapshot);
        toast.error(e.message || "Failed");
      });
  }

  function cancel(id: string) {
    qc.cancelQueries({ queryKey: ["orders"] });

    // Optimistic remove the pending order row
    const snapshot = qc.getQueryData<any[]>(["orders"]);
    qc.setQueryData<any[]>(["orders"], (old) =>
      Array.isArray(old) ? old.filter((o) => o.id !== id) : []
    );

    // Synchronous cancel toast — pairs with the optimistic order row
    // removal so the popup appears in the same frame as the click.
    const pendingToastId = toast.success("Order cancelled", { duration: 1200 });

    OrderAPI.cancel(id)
      .then(() => {
        // No orders invalidate — 2 s poll handles reconcile without flicker.
      })
      .catch((e: any) => {
        if (snapshot) qc.setQueryData(["orders"], snapshot);
        toast.dismiss(pendingToastId);
        toast.error(e.message || "Failed");
      });
  }

  return (
    // `min-w-0` so this flex child never pushes its parent past the
    // viewport — the inner `overflow-x-auto` already handles wide-table
    // horizontal scroll; without min-w-0 the 900-px grid template can
    // grow the chart section past its allowance and clip the order panel.
    <div className="flex min-h-0 min-w-0 flex-col rounded-lg border border-border bg-card">
      {/* Tabs row */}
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-2">
        <div className="flex">
          {tabs.map((t) => (
            <button
              key={t.key}
              type="button"
              onClick={() => setTab(t.key)}
              className={cn(
                "relative px-3 py-2 text-xs font-medium transition-colors",
                tab === t.key ? "text-foreground" : "text-muted-foreground hover:text-foreground"
              )}
            >
              {t.label}({t.count})
              {tab === t.key && <span className="absolute inset-x-2 -bottom-px h-0.5 rounded-t bg-primary" />}
            </button>
          ))}
        </div>
        <div className="ml-auto flex items-center gap-3 text-xs text-muted-foreground">
          <button
            type="button"
            onClick={() => toggleOneClick(!oneClick)}
            title={
              oneClick
                ? "One-Click ON — close/cancel actions skip the confirm dialog"
                : "Turn on One-Click to skip the confirm dialog on close/cancel"
            }
            className={cn(
              "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-semibold transition-colors",
              oneClick
                ? "border-amber-500/40 bg-amber-500/15 text-amber-600 dark:text-amber-400"
                : "border-border bg-muted/40 text-muted-foreground hover:text-foreground"
            )}
          >
            <Zap className={cn("size-3", oneClick && "fill-current")} />
            One Click
            <span className={cn("rounded px-1 text-[9px] tracking-wider", oneClick ? "bg-amber-500/30" : "bg-muted")}>
              {oneClick ? "ON" : "OFF"}
            </span>
          </button>
          <span className={cn("font-tabular", pnlColor(tabPnL))}>
            P/L: {tabPnL >= 0 ? "+" : ""}
            {formatINR(tabPnL)}
          </span>
        </div>
      </div>

      {/* Scrollable table area.
       *
       * One scroll container handles BOTH axes (overflow-auto, not
       * separate -x-auto + -y-auto wrappers like before). The previous
       * nested-scroll layout had two issues on Chrome / Edge:
       *   1. The inner `overflow-y-auto` div collapsed to the viewport
       *      width because its parent had a horizontal overflow, so the
       *      rows' minWidth:900 spilled into nowhere and the right-side
       *      columns (S/L, T/P, COMM, P/L, ACTION) rendered but were
       *      visually clipped under the wallet strip / order panel.
       *   2. Header sat OUTSIDE the vertical-scroll div, so it didn't
       *      align with rows once the user scrolled horizontally past
       *      the SYM column.
       * The header is now `sticky top-0` inside the single container so
       * column labels stay visible during vertical scroll AND drift
       * left/right in lockstep with the rows on horizontal scroll.
       *
       * 28vh max-height keeps the chart dominant per the existing
       * design; min-h-[120px] avoids a 0-height blotter on very small
       * screens.
       */}
      <div className="max-h-[28vh] min-h-[120px] overflow-auto scrollbar-thin">
      {/* Header */}
      <div
        className="sticky top-0 z-10 grid items-center gap-2 border-b border-border bg-card px-3 py-1.5 text-[10px] uppercase tracking-wider text-muted-foreground"
        style={{ gridTemplateColumns: COL_TEMPLATE, minWidth: 900 }}
      >
        <span>TIME</span>
        <span>SYM</span>
        <span>M</span>
        <span>SIDE</span>
        <span>LOT</span>
        <span>SIZE</span>
        <span>ENTRY</span>
        <span>CURRENT</span>
        <span>S/L</span>
        <span>T/P</span>
        <span>COMM</span>
        <span className="text-right">P/L</span>
        <span className="text-right">ACTION</span>
      </div>

      {/* Body — rows render inside the same scroll container as the
          header so vertical + horizontal scroll stay in sync. */}
      <div className="min-h-[60px]">
        {tab === "positions" && (
          <Body
            empty="No open positions on this challenge"
            isEmpty={positions.length === 0}
            rows={positions.map((p) => (
              <PositionRow
                key={p.id}
                position={p}
                onEdit={() => setEditing(p)}
                onClose={() => squareoff(p.id, p.symbol, p.segment_type, p.exchange)}
              />
            ))}
          />
        )}
        {tab === "active" && (
          <Body
            empty="No active trades"
            isEmpty={(activeTrades?.length ?? 0) === 0}
            rows={liveActiveTrades.map((t: any) => (
              <ActiveTradeRow
                key={t.id}
                trade={t}
                onEdit={() => setEditing({
                  // Adapt trade row → editing dialog expects position shape
                  id: t.position_id,
                  symbol: t.symbol,
                  quantity: t.action === "BUY" ? t.quantity : -t.quantity,
                  avg_price: t.price,
                  ltp: t.ltp,
                  stop_loss: t.stop_loss,
                  target: t.target,
                  segment_type: t.segment,
                  exchange: t.exchange,
                  // Override the dialog save so it routes to per-trade endpoint
                  __activeTradeId: t.id,
                })}
                onClose={() =>
                  closeActiveTrade(t.id, t.symbol, t.position_id, t.quantity, t.segment, t.exchange)
                }
              />
            ))}
          />
        )}
        {tab === "pending" && (
          <Body
            empty="No pending orders"
            isEmpty={pendingOrders.length === 0}
            rows={pendingOrders.map((o) => {
              const { lots, qty } = resolveQty(o);
              return (
                <Row
                  key={o.id}
                  cells={[
                    o.created_at ? exactTimestamp(o.created_at) : "—",
                    o.symbol,
                    (o.product_type || "MIS").slice(0, 1),
                    <SideBadge key="s" side={o.action} />,
                    lots < 1 ? lots.toFixed(2) : String(lots),
                    qty < 1 ? qty.toFixed(2) : String(qty),
                    formatPrice(o.price, o.segment, o.exchange),
                    "—",
                    "—",
                    "—",
                    "—",
                    <span key="st" className="text-right text-muted-foreground">
                      {o.status}
                    </span>,
                    <RowActions
                      key="a"
                      actions={[
                        // ── Edit pending order (price / trigger / lots) ──
                        // Use case the user reported: BUY LIMIT placed at
                        // 1000, market jumped past 1000 before the order
                        // could fill → user wants to move the limit to
                        // 1005 without cancelling + replacing (which would
                        // lose queue priority + force re-validation of
                        // every cap). Opens an inline dialog with the
                        // current price + lots prefilled.
                        {
                          label: "Edit",
                          icon: Pencil,
                          onClick: () => setEditingOrder(o),
                        },
                        { label: "Cancel", icon: X, color: "destructive", onClick: () => cancel(o.id) },
                      ]}
                    />,
                  ]}
                />
              );
            })}
          />
        )}
        {tab === "history" && (
          <Body
            empty="No history"
            isEmpty={history.length === 0}
            rows={history.map((o) => {
              const { lots, qty } = resolveQty(o);
              // History rows render the realized P&L the server captured at
              // fill time, in INR. Opening fills have `pnl_inr == null` and
              // render as "—" (no P&L until the position is closed). Closing
              // fills carry a frozen, USD-converted, brokerage-net number
              // that doesn't float with live LTP — matches every broker's
              // history blotter and avoids the previous "closed trade still
              // moving in $" bug for Infoway-fed instruments.
              const pnlInrRaw = o.pnl_inr;
              const havePnl = pnlInrRaw !== null && pnlInrRaw !== undefined && pnlInrRaw !== "";
              const pnlInr = havePnl ? Number(pnlInrRaw) : 0;
              return (
                <Row
                  key={o.id}
                  cells={[
                    o.created_at ? exactTimestamp(o.created_at) : "—",
                    o.symbol,
                    (o.product_type || "MIS").slice(0, 1),
                    <SideBadge key="s" side={o.action} />,
                    lots < 1 ? lots.toFixed(2) : String(lots),
                    qty < 1 ? qty.toFixed(2) : String(qty),
                    formatPrice(o.average_price ?? o.price, o.segment, o.exchange),
                    formatPrice(o.average_price ?? o.price, o.segment, o.exchange),
                    "—",
                    "—",
                    formatINR(o.brokerage ?? 0),
                    havePnl ? (
                      <span key="pnl" className={cn("text-right font-tabular", pnlColor(pnlInr))}>
                        {formatINR(pnlInr)}
                      </span>
                    ) : (
                      <span key="pnl" className="text-right text-muted-foreground">—</span>
                    ),
                    <span key="st" className="text-right text-[10px] text-muted-foreground">
                      {o.status}
                    </span>,
                  ]}
                />
              );
            })}
          />
        )}
        {tab === "cancelled" && (
          <Body
            empty="No cancelled orders"
            isEmpty={cancelled.length === 0}
            rows={cancelled.map((o) => {
              const { lots, qty } = resolveQty(o);
              return (
                <Row
                  key={o.id}
                  cells={[
                    o.created_at ? exactTimestamp(o.created_at) : "—",
                    o.symbol,
                    (o.product_type || "MIS").slice(0, 1),
                    <SideBadge key="s" side={o.action} />,
                    lots < 1 ? lots.toFixed(2) : String(lots),
                    qty < 1 ? qty.toFixed(2) : String(qty),
                    formatPrice(o.price, o.segment, o.exchange),
                    "—",
                    "—",
                    "—",
                    "—",
                    <span key="st" className="text-right text-muted-foreground">
                      {o.status}
                    </span>,
                    "—",
                  ]}
                />
              );
            })}
          />
        )}
      </div>{/* end body wrapper */}
      </div>{/* end single overflow-auto container (header + body) */}

      <EditSlTpDialog
        position={editing}
        onClose={() => setEditing(null)}
        onSaved={() => qc.invalidateQueries({ queryKey: ["positions"] })}
      />
      <EditPendingOrderDialog
        order={editingOrder}
        onClose={() => setEditingOrder(null)}
        onSaved={() => {
          // Refresh both the orders list and any cached pending count.
          // The PUT response updates the document so the row re-renders
          // with the new price the moment the cache invalidates.
          qc.invalidateQueries({ queryKey: ["orders"] });
        }}
      />
    </div>
  );
}

function Body({ empty, isEmpty, rows }: { empty: string; isEmpty: boolean; rows: React.ReactNode[] }) {
  if (isEmpty) {
    return <div className="grid h-32 place-items-center text-xs text-muted-foreground">{empty}</div>;
  }
  return <div>{rows}</div>;
}

function Row({ cells }: { cells: React.ReactNode[] }) {
  return (
    <div
      className="grid items-center gap-2 border-b border-border/40 px-3 py-2 text-xs hover:bg-muted/10"
      style={{ gridTemplateColumns: COL_TEMPLATE, minWidth: 900 }}
    >
      {cells.map((c, i) => (
        <span key={i} className="truncate font-tabular">
          {c}
        </span>
      ))}
    </div>
  );
}

function PositionRow({
  position,
  onEdit,
  onClose,
}: {
  position: any;
  onEdit: () => void;
  onClose: () => void;
}) {
  const isBuy = Number(position.quantity) >= 0;
  const seg = position.segment_type;
  const exch = position.exchange;
  const { lots, qty } = resolveQty(position);
  // Recompute P/L from the canonical qty so legacy positions opened
  // pre-fix (stored with quantity = lots × 1) still show the right MTM.
  // Falls back to whatever the server sent when we can't derive both
  // prices on the client (avoids zeroing P/L for non-Indian segments).
  const avg = Number(position.avg_price);
  const ltp = Number(position.ltp);
  const serverPnl = Number(position.unrealized_pnl ?? 0);
  const derivedPnl =
    Number.isFinite(avg) && Number.isFinite(ltp) && qty > 0
      ? (isBuy ? ltp - avg : avg - ltp) * qty
      : serverPnl;
  // Trust the server's P/L — the backend now marks against the CLOSE-side
  // price (bid for a long, ask for a short), so it reflects what the user
  // would actually realise, and `position.ltp` carries that same close-side
  // mark (derivedPnl therefore agrees). Fall back to derived only for a
  // freshly-placed position whose server P/L hasn't landed yet (still 0).
  // The old "larger magnitude wins" rule wrongly surfaced a stale LTP-based
  // loss on thin contracts (SILVERM showed ₹-11,621 vs the real ₹-4,535).
  const displayPnl = serverPnl !== 0 ? serverPnl : derivedPnl;
  return (
    <Row
      cells={[
        position.opened_at ? exactTimestamp(position.opened_at) : "—",
        position.symbol,
        (position.product_type || "MIS").slice(0, 1),
        <SideBadge key="s" side={isBuy ? "BUY" : "SELL"} />,
        lots < 1 ? lots.toFixed(2) : String(lots),
        qty < 1 ? qty.toFixed(2) : String(qty),
        formatPrice(position.avg_price, seg, exch),
        <CurrentPriceCell key="cur" value={Number(position.ltp)} segment={seg} exchange={exch} />,
        position.stop_loss ? formatPrice(position.stop_loss, seg, exch) : "—",
        position.target ? formatPrice(position.target, seg, exch) : "—",
        formatINR(position.charges ?? 0),
        <span key="pnl" className={cn("text-right font-tabular", pnlColor(displayPnl))}>
          {formatINR(displayPnl)}
        </span>,
        <RowActions
          key="a"
          actions={[
            { label: "Edit SL / TP", icon: Pencil, onClick: onEdit },
            { label: "Close", icon: X, color: "destructive", onClick: onClose, showLabel: true },
          ]}
        />,
      ]}
    />
  );
}

/** One row per fill that's still part of an open position. Entry price is
 *  the trade's actual fill price (NOT the position's weighted average), so
 *  the P/L shown here is what the trader sees as the gain on this specific
 *  entry. Closing this row partially closes the underlying position at the
 *  trade's lot count — server settles P&L vs avg price internally. */
function ActiveTradeRow({
  trade,
  onEdit,
  onClose,
}: {
  trade: any;
  onEdit: () => void;
  onClose: () => void;
}) {
  const seg = trade.segment;
  const exch = trade.exchange;
  const { lots, qty } = resolveQty(trade);
  const avg = Number(trade.price);
  const ltp = Number(trade.ltp);
  const isBuy = String(trade.action).toUpperCase() === "BUY";
  const serverPnl = Number(trade.pnl ?? 0);
  const derivedPnl =
    Number.isFinite(avg) && Number.isFinite(ltp) && qty > 0
      ? (isBuy ? ltp - avg : avg - ltp) * qty
      : serverPnl;
  // Trust the server's close-side P/L (see PositionRow above); derived is
  // only a fallback until the first server refresh lands.
  const displayPnl = serverPnl !== 0 ? serverPnl : derivedPnl;
  return (
    <Row
      cells={[
        trade.executed_at ? exactTimestamp(trade.executed_at) : "—",
        trade.symbol,
        (trade.product_type || "MIS").slice(0, 1),
        <SideBadge key="s" side={trade.action as "BUY" | "SELL"} />,
        lots < 1 ? lots.toFixed(2) : String(lots),
        qty < 1 ? qty.toFixed(2) : String(qty),
        formatPrice(trade.price, seg, exch),
        <CurrentPriceCell key="cur" value={Number(trade.ltp)} segment={seg} exchange={exch} />,
        trade.stop_loss ? formatPrice(trade.stop_loss, seg, exch) : "—",
        trade.target ? formatPrice(trade.target, seg, exch) : "—",
        formatINR(trade.brokerage ?? 0),
        <span key="pnl" className={cn("text-right font-tabular", pnlColor(displayPnl))}>
          {displayPnl >= 0 ? "+" : ""}
          {formatINR(displayPnl)}
        </span>,
        <RowActions
          key="a"
          actions={[
            { label: "Edit SL / TP", icon: Pencil, onClick: onEdit },
            { label: "Close", icon: X, color: "destructive", onClick: onClose, showLabel: true },
          ]}
        />,
      ]}
    />
  );
}

function RowActions({
  actions,
}: {
  actions: {
    label: string;
    icon: any;
    color?: "destructive" | "default";
    onClick: () => void;
    showLabel?: boolean;
  }[];
}) {
  return (
    <span className="flex justify-end gap-1">
      {actions.map((a, i) => {
        const Icon = a.icon;
        return (
          <button
            key={i}
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              a.onClick();
            }}
            title={a.label}
            aria-label={a.label}
            className={cn(
              "inline-flex h-7 items-center justify-center gap-1 rounded-md text-[11px] font-semibold transition-colors",
              a.showLabel ? "px-2.5" : "size-6",
              a.color === "destructive"
                ? "bg-destructive/15 text-destructive ring-1 ring-inset ring-destructive/30 hover:bg-destructive hover:text-destructive-foreground hover:ring-destructive"
                : "border border-border bg-background text-muted-foreground hover:bg-accent hover:text-foreground"
            )}
          >
            <Icon className="size-3.5" />
            {a.showLabel && <span>{a.label}</span>}
          </button>
        );
      })}
    </span>
  );
}

function SideBadge({ side }: { side: "BUY" | "SELL" }) {
  return (
    <span
      className={cn(
        "inline-flex w-fit rounded px-1.5 py-0.5 text-[10px] font-semibold",
        side === "BUY" ? "bg-buy/15 text-buy" : "bg-sell/15 text-sell"
      )}
    >
      {side}
    </span>
  );
}

/** P/L cell for the History tab — what would this trade be worth right now
 *  if the user marked-to-market against the live LTP. Coloured green when
 *  positive, red when negative. Source-currency (USD for crypto/forex/CDS,
 *  ₹ otherwise) so it lines up with the price columns in the same row. */
function HistoryPnl({
  pnl,
  segment,
  exchange,
  ltp,
  avg,
  qty,
}: {
  pnl: number;
  segment?: string;
  exchange?: string;
  ltp: number;
  avg: number;
  qty: number;
}) {
  const isProfit = pnl > 0;
  const isLoss = pnl < 0;
  // All P&L is INR-native now (Infoway prices are treated as INR), so the
  // USD branch is gone. `segment` / `exchange` kept on the signature for
  // call-site compatibility.
  void segment;
  void exchange;
  const formatted = `${pnl >= 0 ? "+" : ""}${formatINR(pnl)}`;
  return (
    <span
      title={`LTP ${ltp} − Avg ${avg} × ${qty}`}
      className={cn(
        "inline-block rounded px-1.5 py-0.5 text-right font-tabular text-[11px] font-bold",
        isProfit && "bg-profit/10 text-profit",
        isLoss && "bg-loss/10 text-loss",
        !isProfit && !isLoss && "text-muted-foreground"
      )}
    >
      {formatted}
    </span>
  );
}

// ── Edit a pending LIMIT / SL-M order ────────────────────────────────
// The user reported: "BUY LIMIT @ 1000 placed, market jumped to 1010
// before fill, mujhe ab limit 1005 ki lagani — wahi se edit kar saku".
// Modify endpoint already exists on the backend (`PUT /user/orders/{id}`)
// and accepts `{ lots?, price?, trigger_price? }`. The dialog below
// prefills with the current values, validates direction + range
// client-side, then submits.
function EditPendingOrderDialog({
  order,
  onClose,
  onSaved,
}: {
  order: any | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [price, setPrice] = useState<string>("");
  const [trigger, setTrigger] = useState<string>("");
  const [lots, setLots] = useState<string>("");
  const [saving, setSaving] = useState(false);
  const [synced, setSynced] = useState<string | null>(null);

  // Live LTP for the order's instrument — drives the "current market"
  // hint + the marketable-limit guard below. 1.5 s refetch matches the
  // OrderPanel cadence so the comparison reference doesn't drift.
  const { data: quote } = useQuery<any>({
    queryKey: ["quote", order?.instrument_token ?? order?.token],
    queryFn: () => InstrumentAPI.quote(order!.instrument_token ?? order!.token),
    enabled: !!order?.id && (!!order?.instrument_token || !!order?.token),
    refetchInterval: 1500,
    staleTime: 1000,
  });

  // Sync once per opened order — same pattern as EditSlTpDialog. Reseed
  // when a different pending row is clicked while the dialog was open.
  if (order && synced !== order.id) {
    setPrice(order.price != null ? String(Number(order.price)) : "");
    setTrigger(
      order.trigger_price != null && Number(order.trigger_price) > 0
        ? String(Number(order.trigger_price))
        : "",
    );
    setLots(order.lots != null ? String(Number(order.lots)) : "");
    setSynced(order.id);
  }
  if (!order && synced !== null) {
    setPrice("");
    setTrigger("");
    setLots("");
    setSynced(null);
  }

  const orderType: string = String(order?.order_type ?? "LIMIT").toUpperCase();
  const isLimit = orderType === "LIMIT";
  const isSlm = orderType === "SL_M" || orderType === "SL-M" || orderType === "SLM";
  const isSl = orderType === "SL";

  async function save() {
    if (!order) return;

    // ── Validation ──────────────────────────────────────────────────
    // Catch bad input here so the user gets a clear toast instead of a
    // generic backend rejection (and avoids a round-trip).
    const priceNum = price ? Number(price) : NaN;
    const triggerNum = trigger ? Number(trigger) : NaN;
    const lotsNum = lots ? Number(lots) : NaN;

    if (isLimit) {
      if (!Number.isFinite(priceNum) || priceNum <= 0) {
        toast.error("Enter a valid limit price");
        return;
      }
    }
    if (isSlm || isSl) {
      if (!Number.isFinite(triggerNum) || triggerNum <= 0) {
        toast.error("Enter a valid trigger price");
        return;
      }
      if (isSl && (!Number.isFinite(priceNum) || priceNum <= 0)) {
        toast.error("Enter a valid limit price");
        return;
      }
    }
    if (Number.isFinite(lotsNum) && lotsNum <= 0) {
      toast.error("Lots must be greater than 0");
      return;
    }

    // Marketable-LIMIT guard — same logic OrderPanel runs at placement
    // time. A BUY LIMIT at or above the current ask (or SELL LIMIT at
    // or below the current bid) would fill IMMEDIATELY on the very
    // next pending-poller tick — at which point the user might as well
    // have placed a MARKET. We warn instead of blocking so a trader
    // who explicitly wants to "lock in better than market" still can,
    // but the common mistake gets caught.
    if (isLimit && Number.isFinite(priceNum)) {
      const side = String(order.action ?? "").toUpperCase();
      const ask = Number(quote?.ask ?? quote?.ltp ?? 0);
      const bid = Number(quote?.bid ?? quote?.ltp ?? 0);
      if (side === "BUY" && ask > 0 && priceNum >= ask) {
        toast.error(
          `BUY LIMIT ${priceNum} is at or above the current ask ${ask} — this would fill immediately. To wait for a lower price, lower the limit; to wait for a higher price, switch to an SL-M order.`,
          { duration: 6000 },
        );
        return;
      }
      if (side === "SELL" && bid > 0 && priceNum <= bid) {
        toast.error(
          `SELL LIMIT ${priceNum} is at or below the current bid ${bid} — this would fill immediately. To wait for a higher price, raise the limit; to wait for a lower price, switch to an SL-M order.`,
          { duration: 6000 },
        );
        return;
      }
    }

    // Lower-bound sanity on the new size if user edited lots.
    if (Number.isFinite(lotsNum) && lotsNum > 0) {
      const filled = Number(order.filled_quantity ?? 0);
      if (filled > 0) {
        // Partially-filled order: can't shrink below what's already filled.
        const lotSize = Number(order.instrument?.lot_size ?? order.lot_size ?? 1) || 1;
        const minLots = filled / lotSize;
        if (lotsNum < minLots) {
          toast.error(
            `Already filled ${filled} qty (${minLots.toFixed(2)} lots) — new lots must be ≥ that.`,
          );
          return;
        }
      }
    }

    setSaving(true);
    try {
      // Only send the fields the user actually changed — keeps the
      // backend's modify endpoint idempotent and avoids accidentally
      // zeroing trigger_price when editing a pure LIMIT.
      const body: Record<string, number> = {};
      if (isLimit && Number.isFinite(priceNum)) body.price = priceNum;
      if ((isSlm || isSl) && Number.isFinite(triggerNum))
        body.trigger_price = triggerNum;
      if (isSl && Number.isFinite(priceNum)) body.price = priceNum;
      if (Number.isFinite(lotsNum) && lotsNum > 0) body.lots = lotsNum;
      if (Object.keys(body).length === 0) {
        toast.error("Nothing to update");
        setSaving(false);
        return;
      }
      await OrderAPI.modify(order.id, body);
      toast.success("Order updated");
      onSaved();
      onClose();
    } catch (e: any) {
      toast.error(e.message || "Failed to update");
    } finally {
      setSaving(false);
    }
  }

  const liveLtp = Number(quote?.ltp ?? 0);

  return (
    <Dialog
      open={!!order}
      onOpenChange={(v) => {
        if (!v) onClose();
      }}
    >
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>
            Edit {orderType} — {order?.symbol}
          </DialogTitle>
          <DialogDescription className="space-y-1">
            <div className="text-[11px] text-muted-foreground">
              {order?.action} · {order?.product_type} · placed{" "}
              {order?.created_at ? exactTimestamp(order.created_at) : "—"}
            </div>
            {liveLtp > 0 && (
              <div className="text-[11px]">
                Current price:{" "}
                <span className="font-tabular font-semibold text-foreground">
                  {formatPrice(liveLtp, order?.segment, order?.exchange)}
                </span>
              </div>
            )}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3 py-2">
          {(isLimit || isSl) && (
            <div className="space-y-1">
              <Label htmlFor="edit-pending-price" className="text-xs">
                Limit price
              </Label>
              <Input
                id="edit-pending-price"
                value={price}
                onChange={(e) => setPrice(e.target.value.replace(/[^\d.]/g, ""))}
                inputMode="decimal"
                placeholder="0.00"
                className="font-tabular"
              />
            </div>
          )}
          {(isSlm || isSl) && (
            <div className="space-y-1">
              <Label htmlFor="edit-pending-trigger" className="text-xs">
                Trigger price
              </Label>
              <Input
                id="edit-pending-trigger"
                value={trigger}
                onChange={(e) => setTrigger(e.target.value.replace(/[^\d.]/g, ""))}
                inputMode="decimal"
                placeholder="0.00"
                className="font-tabular"
              />
            </div>
          )}
          <div className="space-y-1">
            <Label htmlFor="edit-pending-lots" className="text-xs">
              Lots
            </Label>
            <Input
              id="edit-pending-lots"
              value={lots}
              onChange={(e) => setLots(e.target.value.replace(/[^\d.]/g, ""))}
              inputMode="decimal"
              placeholder="1"
              className="font-tabular"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Update order"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function EditSlTpDialog({
  position,
  onClose,
  onSaved,
}: {
  position: any | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const qc = useQueryClient();
  const [sl, setSl] = useState<string>("");
  const [tp, setTp] = useState<string>("");
  const [saving, setSaving] = useState(false);
  const [synced, setSynced] = useState<string | null>(null);

  // Sync once per opened position
  if (position && synced !== position.id) {
    setSl(position.stop_loss ? String(Number(position.stop_loss)) : "");
    setTp(position.target ? String(Number(position.target)) : "");
    setSynced(position.id);
  }
  if (!position && synced !== null) {
    setSl("");
    setTp("");
    setSynced(null);
  }

  async function save() {
    if (!position) return;

    setSaving(true);
    try {
      const body = {
        stop_loss: sl ? Number(sl) : null,
        target: tp ? Number(tp) : null,
      };
      // Active-trade rows tag themselves with __activeTradeId so we can route
      // through the per-trade endpoint (which still hits the parent position
      // server-side, but keeps the API surface symmetric for future per-leg
      // SL/TP support).
      if (position.__activeTradeId) {
        await PositionAPI.updateActiveTradeSlTp(position.__activeTradeId, body);
      } else {
        // Setting SL/TP the instant a trade is taken hits the row while it is
        // still optimistic, and the synthetic id can't be looked up server
        // side — that is the "Position not found" toast.
        let targetId = String(position.id);
        if (isOptimisticId(targetId)) {
          const real = await resolveRealPositionId(qc, targetId);
          if (!real) {
            toast.error("Order is still settling — set SL / TP again in a moment");
            return;
          }
          targetId = real;
        }
        await PositionAPI.updateSlTp(targetId, body);
      }
      toast.success("SL / TP updated");
      onSaved();
      onClose();
    } catch (e: any) {
      toast.error(e.message || "Failed to update");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog
      open={!!position}
      onOpenChange={(v) => {
        if (!v) onClose();
      }}
    >
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>Edit SL / TP — {position?.symbol}</DialogTitle>
          <DialogDescription>
            {position && (
              <>
                {Number(position.quantity) >= 0 ? "Long" : "Short"}{" "}
                {Math.abs(Number(position.quantity ?? 0))} @{" "}
                {formatPrice(position?.avg_price, position.segment_type, position.exchange)} · LTP{" "}
                {formatPrice(position?.ltp, position.segment_type, position.exchange)}
              </>
            )}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label>Stop loss</Label>
            <Input
              type="number"
              step="0.01"
              value={sl}
              onChange={(e) => setSl(e.target.value)}
              placeholder="Leave blank to clear"
            />
          </div>
          <div className="space-y-1.5">
            <Label>Target price</Label>
            <Input
              type="number"
              step="0.01"
              value={tp}
              onChange={(e) => setTp(e.target.value)}
              placeholder="Leave blank to clear"
            />
          </div>
          <p className="text-[11px] text-muted-foreground">
            When LTP crosses these levels the position is auto-squared off at market.
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


/** CURRENT price cell — flashes green when LTP ticks up, red when it
 *  ticks down, then decays back to neutral after ~700 ms. Matches
 *  every Indian broker's market-watch UX so the trader's eye catches
 *  price movement at a glance without comparing two static numbers. */
function CurrentPriceCell({
  value,
  segment,
  exchange,
}: {
  value: number;
  segment?: string;
  exchange?: string;
}) {
  const dir = usePriceFlash(value);
  const flashColor =
    dir === "up"
      ? "text-emerald-500"
      : dir === "down"
        ? "text-red-500"
        : "";
  return (
    <span className={cn("text-right font-tabular tabular-nums transition-colors", flashColor)}>
      {formatPrice(value, segment, exchange)}
    </span>
  );
}
