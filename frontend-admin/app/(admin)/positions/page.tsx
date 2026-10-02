"use client";

import { Suspense, useEffect, useMemo, useState, type ReactNode } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { AlertOctagon, CalendarDays, History, Info, Loader2, Pencil, RefreshCw, RotateCcw, Search, TrendingDown, TrendingUp, Trash2, X, X as XIcon } from "lucide-react";
import { TradingAPI, UsersAPI } from "@/lib/api";
import { useMarketStream } from "@/lib/useMarketStream";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { PageHeader } from "@/components/common/PageHeader";
import { DataTable, type Column } from "@/components/common/DataTable";
import { Pagination } from "@/components/common/Pagination";
import { NettingEntriesDialog } from "@/components/admin/NettingEntriesDialog";
import { BidAskHistoryModal } from "@/components/admin/BidAskHistoryModal";
import { StatusPill } from "@/components/common/StatusPill";
import { cn, formatINR, pnlColor } from "@/lib/utils";
import { OwnerBadge } from "@/components/admin/OwnerBadge";
import { useAdminAuthStore } from "@/stores/authStore";

/** Bare grouped-number price — no ₹ / $ prefix on any instrument price
 *  (avg / LTP / close). `quote` accepted for call-site compatibility but
 *  ignored. Forex pairs render with 4 decimals, everything else 2. */
function fmtFeedPrice(value: string | number | null | undefined, _quote?: string) {
  const n = typeof value === "string" ? Number(value) : (value ?? 0);
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
}

/** Backend serialises naive UTC; add `Z` if missing before parsing. */
function parseDate(v: string | Date | null | undefined): Date | null {
  if (!v) return null;
  if (v instanceof Date) return isNaN(v.getTime()) ? null : v;
  const hasTz = /[zZ]|[+-]\d{2}:?\d{2}$/.test(v);
  const d = new Date(hasTz ? v : v + "Z");
  return isNaN(d.getTime()) ? null : d;
}

// Color + label for the close_reason chip. Legal tags come from
// Position.close_reason in marginwealth_ind/backend/app/models/position.py.
const CLOSE_REASON_META: Record<
  string,
  { label: string; cls: string }
> = {
  USER: { label: "User", cls: "bg-brand-500/10 text-brand-400 ring-brand-500/30" },
  SL_HIT: {
    label: "Stop Loss",
    cls: "bg-destructive/10 text-destructive ring-destructive/30",
  },
  TP_HIT: {
    label: "Target",
    cls: "bg-buy/10 text-buy ring-buy/30",
  },
  STOP_OUT: {
    label: "Stop-out",
    cls: "bg-amber-500/10 text-amber-400 ring-amber-500/30",
  },
  // Carry-forward failure — user wallet couldn't cover overnight margin
  // at EOD rollover; platform flattened the MIS leg at market.
  CARRY_FORWARD_FAIL: {
    label: "CF · Insufficient funds",
    cls: "bg-amber-500/10 text-amber-400 ring-amber-500/30",
  },
  // Segment doesn't allow overnight at all (intraday-only product).
  EOD_OVERNIGHT_DISABLED: {
    label: "EOD · Intraday only",
    cls: "bg-amber-500/10 text-amber-400 ring-amber-500/30",
  },
  AUTO: {
    label: "Auto",
    cls: "bg-muted/40 text-muted-foreground ring-border",
  },
  WEEKLY_SETTLEMENT: {
    label: "Weekly Settlement",
    cls: "bg-purple-500/10 text-purple-400 ring-purple-500/30",
  },
  ADMIN_CLOSE: {
    label: "Admin Close",
    cls: "bg-orange-500/10 text-orange-400 ring-orange-500/30",
  },
};

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
        "inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ring-1 ring-inset",
        meta.cls,
      )}
    >
      {meta.label}
    </span>
  );
}

// "Opened" column now shows the absolute IST timestamp the trade actually
// started, not the relative duration ("7m" / "13m") it had before. Admin
// asked for "kab open hua, date ke saath" — so the cell renders as
// "17 May, 22:34" (DD MMM, HH:mm in IST 24-h). Backend serialises
// opened_at as naive UTC; parseDate() pins the `Z` so the conversion to
// Asia/Kolkata happens correctly.
function fmtOpenedAt(v: string | Date | null | undefined): string {
  const d = parseDate(v);
  if (!d) return "—";
  return d.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Kolkata",
  });
}

/** Human-readable duration between two timestamps. Used by the Closed
 *  Trades tab's "Holding Time" column so admins can spot positions that
 *  were squared off in seconds (likely a misclick) vs ones held for
 *  hours/days. Granularity is PRECISE down to the second so a "0s"
 *  misclick and a "47s" panic-close are visibly different — operator
 *  flagged 22-May that "<1m" collapsed too many useful states into one
 *  bucket. Format ladder:
 *    under 1 min → "Xs"
 *    under 1 hr  → "Xm Ys"   (or "Xm" when Y == 0)
 *    under 1 day → "Xh Ym"   (or "Xh" when Y == 0)
 *    anything else → "Xd Yh" (or "Xd" when Y == 0) */
function fmtHoldingTime(
  opened: string | Date | null | undefined,
  closed: string | Date | null | undefined,
): string {
  const a = parseDate(opened);
  const b = parseDate(closed);
  if (!a || !b) return "—";
  const ms = b.getTime() - a.getTime();
  if (ms < 0) return "—";
  const totalSec = Math.floor(ms / 1000);
  if (totalSec < 60) return `${totalSec}s`;
  const totalMin = Math.floor(totalSec / 60);
  const secPart = totalSec % 60;
  if (totalMin < 60) {
    return secPart > 0 ? `${totalMin}m ${secPart}s` : `${totalMin}m`;
  }
  const totalHr = Math.floor(totalMin / 60);
  const minPart = totalMin % 60;
  if (totalHr < 24) return minPart > 0 ? `${totalHr}h ${minPart}m` : `${totalHr}h`;
  const days = Math.floor(totalHr / 24);
  const hrPart = totalHr % 24;
  return hrPart > 0 ? `${days}d ${hrPart}h` : `${days}d`;
}

export default function AdminPositionsPage() {
  // useSearchParams must sit inside Suspense for the static prerender
  // to succeed (Next 14 App Router contract).
  return (
    <Suspense fallback={null}>
      <AdminPositionsInner />
    </Suspense>
  );
}

const FAST_CLOSE_SEC = 120;

/** Seconds a closed position was held, or null when either end is missing. */
function holdSeconds(row: any): number | null {
  const a = parseDate(row?.opened_at);
  const b = parseDate(row?.closed_at);
  if (!a || !b) return null;
  const sec = Math.floor((b.getTime() - a.getTime()) / 1000);
  return sec >= 0 ? sec : null;
}

function AdminPositionsInner() {
  const qc = useQueryClient();
  const me = useAdminAuthStore((s) => s.admin);
  // Super-admin's Positions page shows ONLY the super-admin's OWN direct users
  // (never any sub-admin's clients) — table + Net-P&L cards both scoped via
  // own_scope. No-op for regular admins/brokers (backend gates on role).
  const isSuperAdmin = me?.role === "SUPER_ADMIN";
  const ownScope = isSuperAdmin || undefined;
  const searchParams = useSearchParams();
  const queryUserId = searchParams?.get("user_id") ?? null;
  // A trade-alert toast's "View" deep-links straight at the position it
  // fired for: `?user_id=…&q=<symbol>&tab=closed` when the fill closed it.
  const [tab, setTab] = useState<"open" | "closed">(
    searchParams?.get("tab") === "closed" ? "closed" : "open",
  );
  // FIFO closed view — shows the SAME per-opening-fill rows the USER sees in
  // their Closed history (one row per opening-fill × closing-fill pairing),
  // instead of one aggregated row per position. Only meaningful for a single
  // user (the FIFO blotter is per-user), so it's offered only when a user is
  // in scope (`?user_id=`).
  // Default ON: when an admin opens a specific user's positions, the Closed
  // tab should match that user's own Closed history fill-for-fill (operator
  // ask: "user aur admin dono side closed me same dikhe" — admin was showing
  // one aggregated all-qty row per position instead of the per-fill rows).
  // Harmless when no user is in scope — `closedFifoMode` also gates on
  // `queryUserId`, so the all-positions view still uses the aggregate path.
  const [fifoView, setFifoView] = useState(true);

  // Search + filter + pagination state. Hoisted above the queries because
  // the Closed tab paginates + searches SERVER-side, so its query depends
  // on these. The Open tab still filters/paginates client-side (it needs
  // the whole set in one shot for the live M2M aggregate + WS subscribe).
  const [search, setSearch] = useState(searchParams?.get("q") ?? "");
  // Type filter — one dropdown that narrows by PRODUCT (NRML/MIS/CNC) or by
  // ORDER TYPE (MARKET/LIMIT/SL_M). "ALL" = no filter.
  const [typeFilter, setTypeFilter] = useState("ALL");
  // Fast-close filter (CLOSED tab) — seconds, or "ALL". Trades closed inside
  // FAST_CLOSE_SEC are highlighted even when the filter is off, so a scalping
  // pattern is visible while scrolling the normal book.
  const [holdFilter, setHoldFilter] = useState("ALL");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  // Closed-tab date filter (IST YYYY-MM-DD). Empty → backend default window
  // (last week + current week). Set → closed_at bounded by these dates.
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");

  // Re-read the deep-link on every navigation. Next keeps this component
  // mounted when the URL changes within the same route, so the useState
  // seeds above only fire once — without this, a second trade-alert "View"
  // (different user / symbol) would change the URL and nothing else.
  const spKey = searchParams?.toString() ?? "";
  useEffect(() => {
    const sp = new URLSearchParams(spKey);
    setSearch(sp.get("q") ?? "");
    setTab(sp.get("tab") === "closed" ? "closed" : "open");
    setPage(1);
  }, [spKey]);
  // Closed search hits the backend — debounce so we don't fire a request
  // on every keystroke.
  const [debouncedSearch, setDebouncedSearch] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search.trim()), 300);
    return () => clearTimeout(t);
  }, [search]);
  const PRODUCT_TYPES = ["MIS", "NRML", "CNC"];
  // Product filters resolve server-side; order-type variants (MARKET/
  // LIMIT/SL_M) are derived from trades and stay client-side on the page.
  const serverProduct = PRODUCT_TYPES.includes(typeFilter) ? typeFilter : undefined;

  // Resolve the scoped user's code/name for the filter pill — opaque
  // ObjectIds are useless to an admin scanning the page.
  const { data: scopedUser } = useQuery({
    queryKey: ["admin", "user", queryUserId],
    queryFn: () => UsersAPI.detail(queryUserId!),
    enabled: !!queryUserId,
    staleTime: 5 * 60_000,
  });

  const { data: openRows, isFetching: openLoading } = useQuery({
    queryKey: ["admin", "positions", "OPEN", queryUserId],
    queryFn: () => TradingAPI.positions({ status: "OPEN", user_id: queryUserId || undefined, own_scope: ownScope }),
    refetchInterval: 5000,
  });

  // Closed Trades — SERVER-side paginated + searched. Only `pageSize` rows
  // are fetched AND enriched per request, so the tab lands near-instantly
  // even on books with thousands of closed positions (the old path pulled
  // 500 rows + a weeks-wide trade fan-out for the brokerage column).
  // `placeholderData: keepPreviousData` keeps the current page on screen
  // while the next one loads, so paging doesn't flash blank.
  // FIFO mode only applies with a single user in scope (per-user blotter).
  const closedFifoMode = tab === "closed" && fifoView && !!queryUserId;
  const { data: closedPage, isFetching: closedLoading } = useQuery({
    queryKey: [
      "admin", "positions", "CLOSED", queryUserId,
      page, pageSize, debouncedSearch, serverProduct ?? "",
      fromDate, toDate, holdFilter,
      closedFifoMode ? "fifo" : "agg",
    ],
    queryFn: () =>
      closedFifoMode
        ? TradingAPI.closedFifo({
            user_id: queryUserId!,
            page,
            page_size: pageSize,
          })
        : TradingAPI.positionsPaged({
            status: "CLOSED",
            user_id: queryUserId || undefined,
            page,
            page_size: pageSize,
            q: debouncedSearch || undefined,
            product: serverProduct,
            from_date: fromDate || undefined,
            to_date: toDate || undefined,
            max_hold_sec: holdFilter === "ALL" ? undefined : Number(holdFilter),
            own_scope: ownScope,
          }),
    refetchInterval: tab === "closed" ? 30000 : false,
    enabled: tab === "closed",
    placeholderData: (prev) => prev,
  });
  const closedRows = closedPage?.rows;
  const closedTotal = closedPage?.total ?? 0;

  const rawRows = tab === "open" ? openRows : closedRows;
  const isFetching = tab === "open" ? openLoading : closedLoading;

  // Live PnL overlay — subscribe to every open position's instrument
  // token via the WS pump (250 ms server tick, throttled to ~500 ms
  // display by the hook). Without this the admin table refreshed at
  // the REST poll's 5 s cadence while the user app screen the admin
  // was comparing against ticked sub-second — operators complained
  // "PnL admin me slow ho raha hai". Overlay rebuilds `ltp` +
  // `unrealized_pnl` per row using the same close-side rule the
  // backend matching engine fills at (long → bid, short → ask, LTP
  // fallback) so the number lines up exactly with what the user app
  // shows.
  const openTokens = useMemo(
    () =>
      Array.from(
        new Set(
          (openRows ?? [])
            .map((r: any) => String(r.instrument_token ?? r.token ?? ""))
            .filter(Boolean),
        ),
      ),
    [openRows],
  );
  // Always subscribe to open position tokens so the Open PNL summary
  // card stays live even when the admin is on the Closed tab.
  const liveQuotes = useMarketStream(openTokens);

  const openRowsLive = useMemo(() => {
    if (liveQuotes.size === 0) return openRows;
    return (openRows ?? []).map((r: any) => {
      const tok = String(r.instrument_token ?? r.token ?? "");
      const live = tok ? liveQuotes.get(tok) : undefined;
      if (!live) return r;
      const qty = Number(r.quantity ?? 0);
      const liveLtp = Number(live.ltp ?? 0);
      const restLtp = Number(r.ltp ?? 0);
      // ANCHOR to the backend's authoritative unrealized_pnl (computed
      // close-side + spike-filtered in the REST poll) and only NUDGE it by
      // how far the LIVE last-traded price has moved since that snapshot.
      // Driving the live delta off the single LTP series avoids the old
      // `(isLong ? bid : ask) || ltp` recompute, whose close-side price
      // FLIPPED between bid and ltp every 500 ms on illiquid option books
      // (thin/absent bid) — swinging the displayed PnL across zero
      // (0 → 4000, 200 → 2000) even though the feed itself is clean
      // (0 spike-filtered ticks, 0 tick_overruns). Re-anchors on every REST
      // poll, so it stays exact at poll boundaries and smooth in between.
      if (!(liveLtp > 0) || !(restLtp > 0) || !Number.isFinite(qty)) return r;
      const basePnl = Number(r.unrealized_pnl ?? 0);
      const livePnl = basePnl + (liveLtp - restLtp) * qty;
      return { ...r, ltp: liveLtp, unrealized_pnl: livePnl };
    });
  }, [openRows, liveQuotes, tab]);

  const rawRowsLive = tab === "open" ? openRowsLive : closedRows;

  // Rows for the visible table.
  //  • CLOSED: server already filtered by search + product and returned
  //    just this page, so we only apply the page-local order-type filter
  //    (MARKET/LIMIT/SL_M) on top. Search/product are NOT re-applied here.
  //  • OPEN: full client-side filter (search + product/order-type) over the
  //    WS-overlaid rows — the whole set is loaded so this stays snappy.
  const data = useMemo(() => {
    if (tab === "closed") {
      let base = closedRows ?? [];
      if (typeFilter !== "ALL" && !PRODUCT_TYPES.includes(typeFilter)) {
        base = base.filter(
          (r: any) => String(r.order_type ?? "").toUpperCase() === typeFilter,
        );
      }
      return base;
    }
    let base = rawRowsLive ?? [];
    if (typeFilter !== "ALL") {
      base = base.filter((r: any) =>
        PRODUCT_TYPES.includes(typeFilter)
          ? String(r.product_type ?? "").toUpperCase() === typeFilter
          : String(r.order_type ?? "").toUpperCase() === typeFilter,
      );
    }
    const q = search.trim().toLowerCase();
    if (!q) return base;
    return base.filter((r: any) => {
      const code = String(r.user_code ?? "").toLowerCase();
      const name = String(r.user_name ?? "").toLowerCase();
      const uidTail = String(r.user_id ?? "").slice(-8).toLowerCase();
      const sym = String(r.symbol ?? "").toLowerCase();
      return code.includes(q) || name.includes(q) || uidTail.includes(q) || sym.includes(q);
    });
  }, [tab, closedRows, rawRowsLive, search, typeFilter]);

  const { data: pnl, isFetching: pnlFetching } = useQuery({
    queryKey: ["admin", "positions", "pnl-summary", queryUserId],
    queryFn: () =>
      TradingAPI.pnlSummary(
        queryUserId ? { user_id: queryUserId, own_scope: ownScope } : { own_scope: ownScope },
      ),
    refetchInterval: 300000,       // 5 min — weekly P&L doesn't need 30s refresh
    staleTime: 290000,
    refetchOnWindowFocus: false,   // don't re-fetch on tab switch
  });
  // Show spinner only on initial load, not on background silent refetches.
  const pnlLoading = pnlFetching && !pnl;

  // ── Close / square-off confirmation dialog ────────────────────
  const [closeTarget, setCloseTarget] = useState<any | null>(null);
  const [closing, setClosing] = useState(false);

  async function confirmSquareoff() {
    if (!closeTarget) return;
    setClosing(true);
    try {
      await TradingAPI.squareoff(closeTarget.id);
      toast.success("Squared off");
      qc.invalidateQueries({ queryKey: ["admin", "positions"] });
      qc.invalidateQueries({ queryKey: ["admin", "accounts"] });
      setCloseTarget(null);
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setClosing(false);
    }
  }

  async function remove(id: string, sym: string, status?: string, qty?: number) {
    // Delete on OPEN rows is a sharper edge than delete on closed rows:
    // the backend will RELEASE the locked margin back to available_balance
    // but will NOT book any PnL (the position never closed at a real
    // market price). Closed rows still get their realised PnL reversed
    // via the existing REVERSAL ledger entry. Show the right warning so
    // the operator picks the right tool.
    const isOpenForceDelete =
      status === "OPEN" && qty != null && Math.abs(Number(qty)) > 1e-9;
    const message = isOpenForceDelete
      ? (
        `Force-delete OPEN position ${sym}?\n\n` +
        `This will:\n` +
        `  • Release the locked margin back to the user's wallet\n` +
        `  • NOT book any PnL (the position never closed at a market price)\n` +
        `  • Delete the row permanently\n\n` +
        `Use ONLY for stale / corrupt rows. For a normal exit, click Close instead.`
      )
      : `Permanently delete position ${sym}? This wipes the record without squaring off — use only for bad data.`;
    if (!confirm(message)) return;
    try {
      await TradingAPI.deletePosition(id);
      toast.success(
        isOpenForceDelete
          ? "Position deleted · margin released to wallet"
          : "Position deleted",
      );
      qc.invalidateQueries({ queryKey: ["admin", "positions"] });
      qc.invalidateQueries({ queryKey: ["admin", "accounts"] });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function emergencyAll() {
    if (!confirm("⚠ EMERGENCY: Square off ALL open positions across the platform?")) return;
    try {
      const r = await TradingAPI.emergencySquareoffAll();
      toast.success(`Squared off ${r.placed}/${r.total} positions`);
      qc.invalidateQueries({ queryKey: ["admin", "positions"] });
      qc.invalidateQueries({ queryKey: ["admin", "accounts"] });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  // ── Bulk delete (Closed Trades) ───────────────────────────────────
  // Select multiple closed rows and delete them in one call. The backend
  // reuses the SAME per-row delete path, so each one's P&L + brokerage is
  // reversed in the user's wallet and wiped from tradebook / accounts —
  // identical to a one-off delete, no separate bulk math.
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkDeleting, setBulkDeleting] = useState(false);
  const toggleSelect = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  async function bulkDelete() {
    const ids = Array.from(selected);
    if (ids.length === 0) return;
    if (
      !confirm(
        `Permanently delete ${ids.length} selected closed trade(s)?\n\n` +
          `Each one's realised P&L + brokerage is reversed in the user's wallet ` +
          `and the row is wiped from tradebook / positions / accounts. Cannot be undone.`,
      )
    )
      return;
    setBulkDeleting(true);
    try {
      const r = await TradingAPI.bulkDeletePositions(ids);
      toast.success(
        `Deleted ${r.deleted}/${r.total}.` + (r.failed?.length ? ` ${r.failed.length} failed.` : ""),
      );
      setSelected(new Set());
      qc.invalidateQueries({ queryKey: ["admin", "positions"] });
      qc.invalidateQueries({ queryKey: ["admin", "accounts"] });
    } catch (e: any) {
      toast.error(e?.message || "Bulk delete failed");
    } finally {
      setBulkDeleting(false);
    }
  }

  // ── Edit modal ────────────────────────────────────────────────────
  const [editing, setEditing] = useState<any | null>(null);
  // Position-id of the row whose Netting Entries drilldown is open. `null`
  // when the dialog is closed. Drives both Open + Closed tabs since the
  // backend endpoint supports both.
  const [nettingId, setNettingId] = useState<string | null>(null);
  const [rateHistoryRow, setRateHistoryRow] = useState<any | null>(null);
  const [form, setForm] = useState<{
    avg_price: string;
    quantity: string;
    opened_at: string;
    stop_loss: string;
    target: string;
    // Closed-only fields.
    realized_pnl: string;
    close_reason: string;
    close_price: string;  // ltp override for closed rows
  }>({
    avg_price: "",
    quantity: "",
    opened_at: "",
    stop_loss: "",
    target: "",
    realized_pnl: "",
    close_reason: "",
    close_price: "",
  });
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!editing) return;
    setForm({
      avg_price: String(editing.avg_price ?? ""),
      quantity: String(editing.quantity ?? ""),
      opened_at: editing.opened_at
        ? new Date(parseDate(editing.opened_at) ?? editing.opened_at).toISOString().slice(0, 16)
        : "",
      stop_loss: editing.stop_loss != null ? String(editing.stop_loss) : "",
      target: editing.target != null ? String(editing.target) : "",
      realized_pnl: editing.realized_pnl != null ? String(editing.realized_pnl) : "",
      close_reason: editing.close_reason ?? "",
      close_price: editing.ltp != null ? String(editing.ltp) : "",
    });
  }, [editing]);

  async function saveEdit() {
    if (!editing) return;
    const isClosed = editing.status === "CLOSED";
    const patch: Record<string, any> = {};
    // Open-row edits are meaningless on closed rows (avg/qty/opened_at/SL/TP
    // describe a live position). Only forward them when the row is still
    // OPEN — otherwise the backend would reject `quantity` change on a
    // CLOSED row anyway.
    if (!isClosed) {
      if (form.avg_price !== "" && form.avg_price !== String(editing.avg_price))
        patch.avg_price = form.avg_price;
      if (form.quantity !== "" && Number(form.quantity) !== Number(editing.quantity))
        patch.quantity = Number(form.quantity);
      if (form.opened_at) {
        const iso = new Date(form.opened_at).toISOString();
        if (iso !== editing.opened_at) patch.opened_at = iso;
      }
      if (form.stop_loss === "") patch.stop_loss = null;
      else if (Number(form.stop_loss) !== Number(editing.stop_loss ?? 0))
        patch.stop_loss = Number(form.stop_loss);
      if (form.target === "") patch.target = null;
      else if (Number(form.target) !== Number(editing.target ?? 0))
        patch.target = Number(form.target);
    } else {
      // Closed-row corrections — open/close price edits auto-recalculate
      // realized_pnl on the backend; direct P&L override also supported.
      const priceChanged =
        form.avg_price !== "" && form.avg_price !== String(editing.avg_price ?? "");
      const closeChanged =
        form.close_price !== "" && form.close_price !== String(editing.ltp ?? "");
      if (priceChanged) patch.avg_price = Number(form.avg_price);
      if (closeChanged) patch.close_price = Number(form.close_price);
      // Direct P&L override only if prices weren't changed
      if (
        !priceChanged && !closeChanged &&
        form.realized_pnl !== "" &&
        Number(form.realized_pnl) !== Number(editing.realized_pnl ?? 0)
      ) {
        patch.realized_pnl = Number(form.realized_pnl);
      }
      if (form.close_reason !== (editing.close_reason ?? "")) {
        patch.close_reason = form.close_reason || null;
      }
    }

    if (Object.keys(patch).length === 0) {
      toast.info("Nothing changed");
      setEditing(null);
      return;
    }
    setSaving(true);
    try {
      await TradingAPI.editPosition(editing.id, patch);
      toast.success(
        isClosed
          ? "Closed trade updated — wallet auto-adjusted via reversal"
          : "Position updated — user terminal will refresh live",
      );
      qc.invalidateQueries({ queryKey: ["admin", "positions"] });
      qc.invalidateQueries({ queryKey: ["admin", "accounts"] });
      setEditing(null);
    } catch (e: any) {
      toast.error(e.message || "Edit failed");
    } finally {
      setSaving(false);
    }
  }

  async function reopen(p: any) {
    // Reopen confirmation — this writes a wallet REVERSAL of the
    // realised P&L and restores the position to OPEN with re-blocked
    // margin. Show the exact reversal amount in the confirm so the
    // admin knows the wallet impact before clicking through.
    const realized = Number(p.realized_pnl ?? 0);
    const direction = realized > 0 ? "debit" : "credit";
    const msg =
      `Reopen ${p.symbol}?\n\n` +
      `This will:\n` +
      `  • Set the position back to OPEN with the original ${Math.abs(
        Number(p.opening_quantity ?? p.quantity ?? 0),
      )} qty\n` +
      `  • ${direction.charAt(0).toUpperCase() + direction.slice(1)} ` +
      `₹${Math.abs(realized).toFixed(2)} on the user's wallet (REVERSAL)\n` +
      `  • Re-block the margin\n` +
      `\nUse only to undo a wrong close (false stop-out, misclick).`;
    if (!window.confirm(msg)) return;
    try {
      await TradingAPI.reopenPosition(p.id);
      toast.success(`${p.symbol} reopened — wallet reversed`);
      qc.invalidateQueries({ queryKey: ["admin", "positions"] });
      qc.invalidateQueries({ queryKey: ["admin", "accounts"] });
    } catch (e: any) {
      toast.error(e.message || "Reopen failed");
    }
  }

  // Apply the same in-page search to the OPEN rows so the Open PNL
  // card matches the table even when the admin's on the Closed tab
  // — typing "CL49179" should narrow both the visible rows AND the
  // PNL aggregate to that user's exposure.
  const filteredOpenRows = useMemo(() => {
    // Use the WS-overlaid `openRowsLive` so the Open-PnL header tile
    // ticks at the same 500 ms cadence as the table rows below — was
    // `openRows` (raw REST) which made the header lag 5 s behind the
    // body and confused operators ("number niche change ho rha, upar
    // wahi pe ruka hai").
    const base = openRowsLive ?? openRows ?? [];
    const q = search.trim().toLowerCase();
    if (!q) return base;
    return base.filter((r: any) => {
      const code = String(r.user_code ?? "").toLowerCase();
      const name = String(r.user_name ?? "").toLowerCase();
      const uidTail = String(r.user_id ?? "").slice(-8).toLowerCase();
      const sym = String(r.symbol ?? "").toLowerCase();
      return code.includes(q) || name.includes(q) || uidTail.includes(q) || sym.includes(q);
    });
  }, [openRowsLive, openRows, search]);

  const totalPnl = filteredOpenRows.reduce(
    (s: number, r: any) => s + Number(r.unrealized_pnl || 0),
    0
  );

  // Pagination. CLOSED is paginated SERVER-side (rows already represent the
  // current page), so we use them as-is. OPEN is the full set loaded in one
  // shot (live M2M aggregate needs all of it), so it's sliced client-side.
  // Reset to page 1 whenever the result set the user is paging through
  // changes — tab / scoped user / search / type filter.
  useEffect(() => {
    setPage(1);
  }, [tab, queryUserId, debouncedSearch, typeFilter, fromDate, toDate]);
  const pagedData = useMemo(() => {
    const all = data ?? [];
    if (tab === "closed") return all; // server already returned just this page
    const start = (page - 1) * pageSize;
    return all.slice(start, start + pageSize);
  }, [tab, data, page, pageSize]);

  // Selection is only for Closed Trades; clear it whenever the visible set
  // changes (tab / page / filter / user) so a stale id never gets deleted.
  const showSelect = tab === "closed";
  useEffect(() => {
    setSelected(new Set());
  }, [tab, page, debouncedSearch, typeFilter, queryUserId]);
  const allOnPageSelected =
    (pagedData?.length ?? 0) > 0 && (pagedData ?? []).every((r: any) => selected.has(String(r.id)));
  const toggleAllOnPage = () =>
    setSelected(
      allOnPageSelected ? new Set() : new Set((pagedData ?? []).map((r: any) => String(r.id))),
    );

  // ── Original column set, plus a new "Hold Time" + polished action buttons ──
  const selectCol: Column<any> = {
    key: "__select",
    header: "",
    width: "36px",
    render: (r: any) => (
      <input
        type="checkbox"
        aria-label="Select row"
        checked={selected.has(String(r.id))}
        onClick={(e) => e.stopPropagation()}
        onChange={() => toggleSelect(String(r.id))}
        className="size-4 cursor-pointer accent-primary"
      />
    ),
  };
  const cols: Column<any>[] = [
    {
      key: "user",
      header: "User",
      render: (r: any) => (
        <div className="flex flex-col leading-tight">
          <span className="text-sm">{r.user_name || "—"}</span>
          <span className="font-mono text-[10px] text-muted-foreground">
            {r.user_code || r.user_id?.slice(-6)}
          </span>
        </div>
      ),
    },
    { key: "owner", header: "Owner", render: (r: any) => <OwnerBadge row={r} me={me} /> },
    { key: "symbol", header: "Symbol" },
    { key: "exchange", header: "Exch" },
    {
      key: "product_type",
      header: "Prod",
      render: (r: any) => (
        <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
          {r.product_type ?? "—"}
        </span>
      ),
    },
    {
      key: "order_type",
      header: "Order Type",
      render: (r: any) => {
        const ot = String(r.order_type ?? "").toUpperCase();
        const label = ot === "SL_M" ? "SL-M" : ot === "MARKET" ? "Market" : ot === "LIMIT" ? "Limit" : "—";
        return (
          <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
            {label}
          </span>
        );
      },
    },
    {
      // Direction the user opened on. `opened_side` is stable across the
      // position's lifecycle (preserved by position_service even after a
      // full close drops `quantity` to 0), so the Closed Trades view shows
      // the original BUY/SELL just as clearly as the Open Trades view.
      key: "opened_side",
      header: "Side",
      render: (r: any) => {
        const raw = (r.opened_side || (Number(r.quantity) >= 0 ? "BUY" : "SELL"))
          .toString()
          .toUpperCase();
        const isBuy = raw === "BUY";
        return (
          <span
            className={cn(
              "inline-flex min-w-[44px] items-center justify-center rounded px-1.5 py-0.5 text-[11px] font-semibold",
              isBuy
                ? "bg-emerald-500/15 text-emerald-500"
                : "bg-red-500/15 text-red-500",
            )}
          >
            {raw}
          </span>
        );
      },
    },
    {
      key: "quantity",
      header: "Qty",
      align: "right" as const,
      // For CLOSED rows `quantity` is 0 (FIFO flattens it on the close leg).
      // `opening_quantity` is preserved by position_service — that's the size
      // the user actually traded. Direction comes from `opened_side` so the
      // colour stays correct even though the signed qty is 0.
      render: (r: any) => {
        const isClosed = r.status === "CLOSED";
        const displayQty = isClosed
          ? Math.abs(Number(r.opening_quantity ?? 0))
          : Number(r.quantity);
        const direction = isClosed
          ? String(r.opened_side || "").toUpperCase() === "SELL"
            ? -1
            : 1
          : Number(r.quantity) >= 0
            ? 1
            : -1;
        return (
          <span className={direction >= 0 ? "text-buy" : "text-sell"}>
            {displayQty}
          </span>
        );
      },
    },
    {
      // Volume = total lot count (contracts ÷ lot_size). Renders "—" for
      // equity rows where lot_size is 1 (Qty already conveys the size).
      // Cell carries a small (i) icon as a visual affordance — clicking
      // anywhere in the row opens the per-position Netting Entries
      // breakdown, and admins were missing the click cue without an
      // explicit indicator. Tabular-nums alignment keeps a column of
      // mixed integer / decimal values visually clean.
      key: "volume",
      header: "Volume",
      align: "right" as const,
      render: (r: any) => {
        const isClosed = r.status === "CLOSED";
        const rawQty = isClosed
          ? Math.abs(Number(r.opening_quantity ?? 0))
          : Math.abs(Number(r.quantity));
        const lotSize = Number(r.lot_size ?? r.instrument?.lot_size ?? 1) || 1;
        if (lotSize <= 1 || rawQty <= 0) {
          return (
            <span className="inline-flex items-center justify-end gap-1.5">
              <span>—</span>
              <Info
                className="size-3 text-muted-foreground/60"
                aria-label="Click row to view netting entries"
              />
            </span>
          );
        }
        const lots = rawQty / lotSize;
        const text = Number.isInteger(lots) ? String(lots) : lots.toFixed(2);
        return (
          <span
            className="inline-flex items-center justify-end gap-1.5"
            title="Click row to view netting entries"
          >
            <span className="tabular-nums">{text}</span>
            <Info className="size-3 text-primary/70" />
          </span>
        );
      },
    },
    {
      key: "avg_price",
      header: "Open Price",
      align: "right" as const,
      // Closed tab: opening time (date + HH:mm IST) sits under the price so the
      // admin reads WHEN the trade opened without a separate column.
      render: (r: any) =>
        tab === "closed" ? (
          <div className="flex flex-col items-end leading-tight">
            <span className="tabular-nums">{fmtFeedPrice(r.avg_price, r.currency_quote)}</span>
            <span className="text-[10px] font-normal text-muted-foreground">
              {fmtOpenedAt(r.opened_at)}
            </span>
          </div>
        ) : (
          fmtFeedPrice(r.avg_price, r.currency_quote)
        ),
    },
    {
      // For closed positions, `ltp` was set to the actual close price by
      // position_service.apply_trade — so the same field doubles as "Close".
      // Header swaps too so the Closed Trades tab reads just "Close"
      // (user feedback: "close price likho bs upar me") and the Open
      // Trades tab still reads "LTP" since that's the live mark.
      key: "ltp",
      header: tab === "closed" ? "Close" : "LTP",
      align: "right" as const,
      // Closed tab: close time (date + HH:mm IST) under the close price.
      render: (r: any) =>
        tab === "closed" ? (
          <div className="flex flex-col items-end leading-tight">
            <span className="tabular-nums" title="Closing price">
              {fmtFeedPrice(r.ltp, r.currency_quote)}
            </span>
            <span className="text-[10px] font-normal text-muted-foreground">
              {fmtOpenedAt(r.closed_at)}
            </span>
          </div>
        ) : (
          <span title="Live LTP">{fmtFeedPrice(r.ltp, r.currency_quote)}</span>
        ),
    },
    // Status column — hidden on the Closed tab (every row is CLOSED there, so
    // the pill is pure noise; operator asked to remove it). Kept on Open/ALL.
    ...(tab === "closed"
      ? []
      : [
          {
            key: "status",
            header: "Status",
            render: (r: any) => <StatusPill status={r.status} />,
          },
        ]),
    {
      key: "realized_pnl",
      header: "Realized",
      align: "right" as const,
      // GROSS realised P&L — straight from `position.realized_pnl`,
      // before brokerage / other charges are subtracted. The "Net
      // P&L" column further right does the subtraction explicitly
      // so the admin can see the arithmetic happen on screen:
      //
      //     REALIZED (gross) − BROKERAGE = NET P&L
      //
      // Previously this column quietly displayed net by subtracting
      // charges, which confused operators who reconciled the row
      // (e.g. open 317.85 → close 324.15 × 60 lots = ₹378 gross, but
      // the column showed ₹258 because we'd already netted out
      // ₹120 of brokerage). Showing gross here keeps the column's
      // name honest and lets the new Net P&L column carry the
      // final figure.
      render: (r: any) => {
        const gross = Number(r.realized_pnl ?? 0);
        return (
          <span className={pnlColor(gross)}>
            {formatINR(gross)}
          </span>
        );
      },
    },
    // M2M column is only meaningful on OPEN positions (mark-to-market on
    // the still-live price). For CLOSED rows it's mathematically 0 by
    // definition (qty = 0), so we hide the column entirely on the Closed
    // Trades tab — user explicitly asked for it removed there ("close
    // trade se m2m bhi remove kar dena"). The realised number on the
    // closed row already tells the full story.
    ...(tab === "closed"
      ? []
      : [
          {
            key: "unrealized_pnl",
            header: "M2M",
            align: "right" as const,
            render: (r: any) => (
              <span className={pnlColor(r.unrealized_pnl)}>
                {formatINR(r.unrealized_pnl)}
              </span>
            ),
          },
        ]),
    {
      // Total brokerage paid across this position's lifecycle (open
      // leg + close leg if closed). Backend's `charges` field already
      // sums every Trade row within the position's open-close window
      // — see _charges_for() in admin/trading.py. User feedback:
      // "margin column hata ke total close+open brokerage dikhao" —
      // margin tells the admin nothing on the Closed tab (it's 0
      // there anyway) and on the Open tab it duplicates info already
      // visible in the user's wallet strip; total brokerage is what
      // the admin actually audits.
      key: "charges",
      header: "Brokerage",
      align: "right" as const,
      render: (r: any) => (
        <span
          title="Total brokerage (open leg + close leg)"
          className="tabular-nums"
        >
          {formatINR(Number(r.charges ?? 0))}
        </span>
      ),
    },
    {
      key: "net_pnl",
      header: "Net P&L",
      align: "right" as const,
      render: (r: any) => {
        const isOpen = r.status === "OPEN";
        const gross = isOpen
          ? Number(r.unrealized_pnl ?? 0)
          : Number(r.realized_pnl ?? 0);
        const charges = Number(r.charges ?? 0);
        const net = gross - charges;
        return (
          <span
            className={`${pnlColor(net)} font-semibold`}
            title={isOpen ? `M2M ${formatINR(gross)} − Bkg ${formatINR(charges)}` : `Realized ${formatINR(gross)} − Bkg ${formatINR(charges)}`}
          >
            {formatINR(net)}
          </span>
        );
      },
    },
    // Standalone "Opened" column — only on Open/ALL. On the Closed tab the
    // opening time already sits under the Open Price cell, so this is dropped
    // to cut the column count (operator: "bahut gap hai, page perfect karo").
    ...(tab === "closed"
      ? []
      : [
          {
            key: "opened_at",
            header: "Opened",
            render: (r: any) => (
              <span className="whitespace-nowrap font-tabular">
                {fmtOpenedAt(r.opened_at)}
              </span>
            ),
          },
        ]),
    // Wallet After — Closed tab only. User's available_balance right AFTER
    // this trade closed. Bold so it stands out (operator: "wallet balance
    // dikhe bold me sahi se").
    ...(tab === "closed"
      ? [
          {
            key: "wallet_after",
            header: "Wallet After",
            align: "right" as const,
            render: (r: any) => (
              <span
                className="whitespace-nowrap font-tabular font-bold"
                title="User's wallet balance right after this trade closed"
              >
                {r.wallet_after != null ? formatINR(Number(r.wallet_after)) : "—"}
              </span>
            ),
          },
        ]
      : []),
    // Holding Time — only meaningful on the Closed Trades tab where
    // both ends of the window exist. Helps the admin spot 30-second
    // misclicks vs multi-hour intentional holds at a glance.
    ...(tab === "closed"
      ? [
          {
            key: "holding_time",
            header: "Holding Time",
            align: "right" as const,
            render: (r: any) => {
              const sec = holdSeconds(r);
              const fast = sec !== null && sec < FAST_CLOSE_SEC;
              return (
                <span
                  className={cn(
                    "whitespace-nowrap font-tabular",
                    fast ? "font-semibold text-atm" : "text-muted-foreground",
                  )}
                  title={
                    fast
                      ? `Closed within ${FAST_CLOSE_SEC / 60} min of opening`
                      : (r.closed_at ?? undefined)
                  }
                >
                  {fmtHoldingTime(r.opened_at, r.closed_at)}
                </span>
              );
            },
          },
        ]
      : []),
    // Only meaningful for CLOSED rows. Renders the close_reason as a
    // color-coded chip so super-admins can spot at a glance which
    // closes were user-initiated vs bracket auto-fires vs stop-outs.
    {
      key: "close_reason",
      header: "Closed By",
      render: (r: any) =>
        r.status === "CLOSED" ? (
          <CloseReasonChip reason={r.close_reason} />
        ) : (
          <span className="text-muted-foreground/40">—</span>
        ),
    },
    {
      key: "actions",
      header: "",
      align: "right" as const,
      // `e.stopPropagation()` on every action button so clicking them
      // doesn't also fire the row-level netting drilldown dialog (the
      // <tr> has its own onClick now). Without this, "Close" would
      // squareoff AND pop open the breakdown — visually noisy and the
      // edit form would land on a faded background.
      render: (r: any) => {
        // FIFO / weekly-settlement rows carry SYNTHETIC ids (fifo_… /
        // wsettle_…), not real position ObjectIds — edit / reopen / delete
        // operate on a Position doc and can't target a per-fill row (the
        // backend now 400s such ids instead of 500ing). Hide the actions on
        // these; switch FIFO view OFF to act on the aggregated position.
        const rid = String(r.id ?? "");
        if (rid.startsWith("fifo_") || rid.startsWith("wsettle_")) {
          return <span className="text-muted-foreground/30">—</span>;
        }
        return (
        <div
          className="flex items-center justify-end gap-1.5"
          onClick={(e) => e.stopPropagation()}
        >
          {r.status === "OPEN" && (
            <>
              <Button
                size="sm"
                onClick={(e) => {
                  e.stopPropagation();
                  setEditing(r);
                }}
                className="h-7 gap-1 rounded-md bg-brand-600 px-2.5 text-xs font-semibold text-white hover:bg-brand-700"
              >
                <Pencil className="size-3.5" /> Edit
              </Button>
              <Button
                size="sm"
                onClick={(e) => {
                  e.stopPropagation();
                  setCloseTarget(r);
                }}
                className="h-7 gap-1 rounded-md bg-destructive px-2.5 text-xs font-semibold text-destructive-foreground hover:bg-destructive/90"
              >
                <X className="size-3.5" /> Close
              </Button>
            </>
          )}
          {r.status === "CLOSED" && (
            <>
              {/* Edit closed trade — admin can correct realised P&L
                  and relabel close_reason. Wallet auto-adjusts via
                  REVERSAL on the backend so the running balance
                  stays consistent with the new figure. */}
              <Button
                size="sm"
                onClick={(e) => {
                  e.stopPropagation();
                  setEditing(r);
                }}
                aria-label="Edit closed trade"
                title="Edit realised P&L / close reason"
                className="h-7 gap-1 rounded-md bg-brand-600 px-2.5 text-xs font-semibold text-white hover:bg-brand-700"
              >
                <Pencil className="size-3.5" /> Edit
              </Button>
              {/* Reopen — flip CLOSED → OPEN with the original qty,
                  reverse the wallet P&L impact, re-block margin.
                  Used to undo a false stop-out / misclick close. */}
              <Button
                size="sm"
                onClick={(e) => {
                  e.stopPropagation();
                  reopen(r);
                }}
                aria-label="Reopen position"
                title="Reopen this position (reverses wallet P&L)"
                className="h-7 gap-1 rounded-md bg-amber-600 px-2.5 text-xs font-semibold text-white hover:bg-amber-700"
              >
                <RotateCcw className="size-3.5" /> Reopen
              </Button>
            </>
          )}
          {/* Rate History — per-minute bid/ask high-low over the position's
              entry→exit window (custom time filter inside). Shown for BOTH
              open and closed rows. */}
          <Button
            size="sm"
            onClick={(e) => {
              e.stopPropagation();
              setRateHistoryRow(r);
            }}
            aria-label="Rate history"
            title="Bid/Ask rate history (entry → exit)"
            className="h-7 gap-1 rounded-md bg-muted px-2.5 text-xs font-semibold text-foreground ring-1 ring-inset ring-border hover:bg-muted/70"
          >
            <History className="size-3.5" /> Rate History
          </Button>
          <Button
            size="sm"
            onClick={(e) => {
              e.stopPropagation();
              remove(r.id, r.symbol, r.status, r.quantity);
            }}
            aria-label="Delete record"
            title="Delete record (no square-off)"
            className="size-7 rounded-md bg-destructive/15 p-0 text-destructive ring-1 ring-inset ring-destructive/30 hover:bg-destructive hover:text-destructive-foreground hover:ring-destructive"
          >
            <Trash2 className="size-3.5" />
          </Button>
        </div>
        );
      },
    },
  ].filter(
    // Defensive belt-and-braces filter: even if the conditional spread
    // above (which inserts the M2M col only on the open tab) ever
    // regresses, this guarantees the M2M column never reaches the
    // table on the Closed Trades tab. User explicitly asked for the
    // column to disappear there twice — keeping both gates ensures
    // a single stray rewrite can't bring it back.
    (c) => !(tab === "closed" && c.key === "unrealized_pnl"),
  );

  return (
    <div className="space-y-4">
      <PageHeader
        title="Position Management"
        description={`${openRows?.length ?? 0} open · Live M2M: ${formatINR(totalPnl)}`}
        actions={
          <Button
            variant="destructive"
            size="sm"
            onClick={emergencyAll}
            className="h-8 px-2 text-xs sm:h-9 sm:px-3 sm:text-sm"
            title="Emergency square-off all positions"
          >
            <AlertOctagon className="size-3.5 sm:size-4" />
            {/* Compact label on phones — full text from sm+ */}
            <span className="sm:hidden">Square-off</span>
            <span className="hidden sm:inline">Emergency square-off all</span>
          </Button>
        }
      />

      {queryUserId && (
        <div className="inline-flex items-center gap-2 rounded-md border border-primary/40 bg-primary/10 px-3 py-1.5 text-xs">
          <span className="text-muted-foreground">Filtered by user:</span>
          <span className="font-semibold text-primary">
            {(scopedUser as any)?.user_code ?? queryUserId.slice(-8)}
            {(scopedUser as any)?.full_name ? ` · ${(scopedUser as any).full_name}` : ""}
          </span>
          <Link
            href="/positions"
            className="grid size-5 place-items-center rounded text-muted-foreground hover:bg-muted/40 hover:text-foreground"
            aria-label="Clear user filter"
          >
            <XIcon className="size-3" />
          </Link>
        </div>
      )}

      {/* ── PnL summary cards ─────────────────────────────────────── */}
      {/* 3-up even on phones — operator wants all three M2M figures
          visible at a glance without scrolling. Cards become more
          compact on <sm (see PnlCard styles). */}
      <section className="grid grid-cols-3 gap-2 sm:gap-3">
        {/* Open PNL: use the live, filter-aware sum of the visible
            open-trade rows rather than the platform-wide summary
            endpoint. The summary endpoint sums ALL open positions
            across every user — so when the page was scoped via
            `?user_id=…` the card kept showing the global number while
            the table below showed only the scoped user's. `totalPnl`
            is recomputed from `openRows` which already honours the
            user filter, so the card and the table now stay in
            lockstep regardless of scope. */}
        <PnlCard
          label="Open PNL"
          value={totalPnl}
          hint={
            queryUserId
              ? "Unrealised M2M on this user's open positions"
              : "Unrealised M2M on currently open positions"
          }
          icon={totalPnl >= 0 ? TrendingUp : TrendingDown}
        />
        <PnlCard
          // Headline = Total of Both (broker take = −Net Client PNL + Brokerage),
          // the SAME figure the Accounts dashboard shows for this Mon→Sun window.
          // Breakdown (Net P&L + Brokerage) sits in the hint so the two pages
          // reconcile line-for-line. Date range on top mirrors Accounts.
          label={
            pnl?.week_label
              ? `This Week — Total of Both (${pnl.week_label})`
              : "This Week — Total of Both"
          }
          // Broker take: PROFIT (positive) → red, LOSS (negative) → green.
          value={pnl?.week_total_of_both ?? 0}
          invertColor
          hint={
            <>
              Net P&amp;L{" "}
              <span className={pnlColor(pnl?.week_net_client_pnl ?? 0)}>
                {formatINR(pnl?.week_net_client_pnl ?? 0)}
              </span>{" "}
              + Brokerage {formatINR(pnl?.week_brokerage ?? 0)}
            </>
          }
          icon={(pnl?.week_total_of_both ?? 0) >= 0 ? TrendingUp : TrendingDown}
          loading={pnlLoading}
        />
        <PnlCard
          label={
            pnl?.last_week_label
              ? `Last Week — Total of Both (${pnl.last_week_label})`
              : "Last Week — Total of Both"
          }
          value={pnl?.last_week_total_of_both ?? 0}
          invertColor
          hint={
            <>
              Net P&amp;L{" "}
              <span className={pnlColor(pnl?.last_week_net_client_pnl ?? 0)}>
                {formatINR(pnl?.last_week_net_client_pnl ?? 0)}
              </span>{" "}
              + Brokerage {formatINR(pnl?.last_week_brokerage ?? 0)}
            </>
          }
          icon={CalendarDays}
          loading={pnlLoading}
        />
      </section>

      {/* ── Tabs + in-page user/symbol search ──────────────────────── */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="inline-flex rounded-md border border-border bg-muted/30 p-1 text-sm">
          {(["open", "closed"] as const).map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => setTab(t)}
              className={cn(
                "rounded px-3 py-1.5 transition-colors",
                tab === t ? "bg-primary/15 text-primary" : "text-muted-foreground hover:text-foreground"
              )}
            >
              {t === "open" ? "Open Trades" : "Closed Trades"}
            </button>
          ))}
        </div>

        {/* Bulk-select + delete — Closed Trades only. Select rows then delete
            them in one call (each reverses P&L + brokerage in the wallet). */}
        {showSelect && (
          <div className="inline-flex items-center gap-2">
            <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-muted-foreground">
              <input
                type="checkbox"
                checked={allOnPageSelected}
                onChange={toggleAllOnPage}
                className="size-4 cursor-pointer accent-primary"
              />
              Select all
            </label>
            {selected.size > 0 && (
              <button
                type="button"
                onClick={bulkDelete}
                disabled={bulkDeleting}
                className="inline-flex items-center gap-1.5 rounded-md bg-destructive px-3 py-1.5 text-sm font-semibold text-destructive-foreground transition-colors hover:bg-destructive/90 disabled:opacity-50"
              >
                <Trash2 className="size-3.5" />
                {bulkDeleting ? "Deleting…" : `Delete selected (${selected.size})`}
              </button>
            )}
          </div>
        )}

        {/* FIFO view toggle — only for a single user in scope (the FIFO
            blotter is per-user). ON → show the SAME per-opening-fill rows the
            user sees in their own Closed history instead of one aggregated
            row per position. */}
        {tab === "closed" && queryUserId && (
          <button
            type="button"
            onClick={() => {
              setFifoView((v) => !v);
              setPage(1);
            }}
            title="Show the per-opening-fill FIFO breakdown exactly as the user sees it in their Closed history"
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm transition-colors",
              fifoView
                ? "border-primary/40 bg-primary/15 text-primary"
                : "border-border bg-muted/30 text-muted-foreground hover:text-foreground"
            )}
          >
            FIFO view: {fifoView ? "ON" : "OFF"}
          </button>
        )}

        {/* Date range (Closed) + type filter grouped on one row — the type
            dropdown tucks to the right (ml-auto) instead of taking its own
            line, and the compact date inputs fit beside it on mobile. */}
        <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto sm:flex-1">
          {tab === "closed" && (
            <div className="inline-flex items-center gap-1 text-xs text-muted-foreground">
              <input
                type="date"
                value={fromDate}
                max={toDate || undefined}
                onChange={(e) => setFromDate(e.target.value)}
                className="h-9 w-[128px] rounded-md border border-border bg-background px-2 text-sm text-foreground"
                aria-label="From date"
              />
              <span>→</span>
              <input
                type="date"
                value={toDate}
                min={fromDate || undefined}
                onChange={(e) => setToDate(e.target.value)}
                className="h-9 w-[128px] rounded-md border border-border bg-background px-2 text-sm text-foreground"
                aria-label="To date"
              />
              {(fromDate || toDate) && (
                <button
                  type="button"
                  onClick={() => {
                    setFromDate("");
                    setToDate("");
                  }}
                  className="inline-flex items-center rounded p-1 hover:text-foreground"
                  title="Clear date filter"
                  aria-label="Clear date filter"
                >
                  <X className="size-3.5" />
                </button>
              )}
            </div>
          )}

          {/* Type filter — product (NRML/MIS/CNC) or order kind (Market/Limit/SL-M). */}
          <select
            value={typeFilter}
            onChange={(e) => {
              setTypeFilter(e.target.value);
              setPage(1);
            }}
            className="ml-auto h-9 rounded-md border border-border bg-background px-2 text-sm text-foreground"
            aria-label="Filter by type"
          >
            <option value="ALL">All types</option>
            <option value="NRML">NRML (carry)</option>
            <option value="MIS">MIS (intraday)</option>
            <option value="CNC">CNC (delivery)</option>
            <option value="MARKET">Market orders</option>
            <option value="LIMIT">Limit orders</option>
            <option value="SL_M">SL-M orders</option>
          </select>
          {/* Fast-close filter — server-side, because the closed book is paginated. */}
          {tab === "closed" && (
            <select
              value={holdFilter}
              onChange={(e) => {
                setHoldFilter(e.target.value);
                setPage(1);
              }}
              className="h-9 rounded-md border border-border bg-background px-2 text-sm text-foreground"
              aria-label="Filter by holding time"
            >
              <option value="ALL">Any holding time</option>
              <option value="120">Closed under 2 min</option>
              <option value="300">Closed under 5 min</option>
              <option value="900">Closed under 15 min</option>
            </select>
          )}
        </div>

        <div className="relative w-full sm:w-72">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search user code / name / symbol"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-8 pr-8"
          />
          {search && (
            <button
              type="button"
              onClick={() => setSearch("")}
              aria-label="Clear search"
              className="absolute right-2 top-1/2 grid size-5 -translate-y-1/2 place-items-center rounded text-muted-foreground hover:bg-muted/40 hover:text-foreground"
            >
              <X className="size-3" />
            </button>
          )}
        </div>

        {search && (
          <div className="basis-full text-xs text-muted-foreground">
            {tab === "closed"
              ? `${closedTotal} closed rows match · search "${search}"`
              : `Showing ${data?.length ?? 0} of ${rawRows?.length ?? 0} open rows · search "${search}"`}
          </div>
        )}
      </div>

      {/* Desktop: full data table */}
      <div className="hidden md:block">
        <DataTable
          columns={showSelect ? [selectCol, ...cols] : cols}
          rows={pagedData}
          keyExtractor={(r) => r.id}
          loading={isFetching && !data}
          // Size to content so columns pack tightly instead of justify-spreading
          // across the full card width (operator: "bahut gap hai").
          tableClassName="w-max"
          onRowClick={(r: any) => setNettingId(String(r.id))}
          rowClassName={(r) => {
            if (tab === "closed") {
              const sec = holdSeconds(r);
              return sec !== null && sec < FAST_CLOSE_SEC ? "bg-atm/10" : undefined;
            }
            if (Number(r.unrealized_pnl) < -Number(r.margin_used) * 0.5) return "bg-destructive/5";
            if (Number(r.unrealized_pnl) < -Number(r.margin_used) * 0.25) return "bg-atm/5";
            return undefined;
          }}
        />
      </div>

      {/* Mobile: stacked position cards — same data, tap-friendly */}
      <div className="space-y-2 md:hidden">
        {isFetching && !data && (
          <div className="rounded-lg border border-border bg-card p-6 text-center text-sm text-muted-foreground">
            Loading…
          </div>
        )}
        {!isFetching && (!pagedData || pagedData.length === 0) && (
          <div className="rounded-lg border border-dashed border-border bg-card p-6 text-center text-sm text-muted-foreground">
            No positions to show.
          </div>
        )}
        {pagedData?.map((r: any) => (
          <PositionMobileCard
            key={r.id}
            r={r}
            tab={tab}
            me={me}
            selectable={showSelect}
            selected={selected.has(String(r.id))}
            onToggleSelect={() => toggleSelect(String(r.id))}
            onOpenNetting={() => setNettingId(String(r.id))}
            onOpenRateHistory={() => setRateHistoryRow(r)}
            onEdit={() => setEditing(r)}
            onClose={() => setCloseTarget(r)}
            onReopen={() => reopen(r)}
            onDelete={() => remove(r.id, r.symbol, r.status, r.quantity)}
          />
        ))}
      </div>

      <Pagination
        page={page}
        pageSize={pageSize}
        total={tab === "closed" ? closedTotal : (data?.length ?? 0)}
        onPageChange={setPage}
        onPageSizeChange={setPageSize}
        pageSizeOptions={[25, 30, 50, 100, 200]}
      />

      {/* Row-click opens the per-position fill breakdown. Same dialog
          serves Open + Closed tabs — the backend endpoint handles both
          statuses and the modal renders identically. */}
      <NettingEntriesDialog
        positionId={nettingId}
        onClose={() => setNettingId(null)}
      />

      {/* Per-minute Bid/Ask "Rate History" for one position — same modal for
          Open + Closed rows; the backend defaults the window to entry→exit
          (now while open) and the modal exposes a custom time filter. */}
      <BidAskHistoryModal
        position={
          rateHistoryRow
            ? {
                id: String(rateHistoryRow.id),
                symbol: String(rateHistoryRow.symbol ?? ""),
                token: rateHistoryRow.instrument_token ?? rateHistoryRow.token,
                opened_at: rateHistoryRow.opened_at ?? null,
                closed_at: rateHistoryRow.closed_at ?? null,
                status: rateHistoryRow.status,
              }
            : null
        }
        onClose={() => setRateHistoryRow(null)}
      />

      {/* ── Close / Square-off confirmation dialog ─────────────── */}
      <Dialog open={!!closeTarget} onOpenChange={(o) => !o && setCloseTarget(null)}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-destructive">
              <AlertOctagon className="size-5" />
              Close Position
            </DialogTitle>
            <DialogDescription asChild>
              <div className="space-y-3 pt-2">
                <p>Square off this position at current market price?</p>
                {closeTarget && (
                  <div className="rounded-lg border bg-muted/30 p-3 text-sm">
                    <div className="grid grid-cols-2 gap-y-1.5">
                      <span className="text-muted-foreground">Symbol</span>
                      <span className="font-semibold">{closeTarget.symbol}</span>
                      <span className="text-muted-foreground">Side</span>
                      <span className={cn("font-semibold", Number(closeTarget.quantity) > 0 ? "text-emerald-500" : "text-red-500")}>
                        {Number(closeTarget.quantity) > 0 ? "BUY" : "SELL"}
                      </span>
                      <span className="text-muted-foreground">Qty</span>
                      <span className="font-semibold">{Math.abs(Number(closeTarget.quantity))}</span>
                      <span className="text-muted-foreground">Entry Price</span>
                      <span className="font-semibold">{fmtFeedPrice(closeTarget.avg_price)}</span>
                      <span className="text-muted-foreground">Current LTP</span>
                      <span className="font-semibold">{fmtFeedPrice(closeTarget.ltp)}</span>
                      <span className="text-muted-foreground">Unrealised P&L</span>
                      <span className={cn("font-semibold", pnlColor(Number(closeTarget.unrealized_pnl ?? 0)))}>
                        {formatINR(Number(closeTarget.unrealized_pnl ?? 0))}
                      </span>
                    </div>
                    {closeTarget.user_name && (
                      <p className="mt-2 border-t pt-2 text-xs text-muted-foreground">
                        User: <span className="font-medium text-foreground">{closeTarget.user_code} · {closeTarget.user_name}</span>
                      </p>
                    )}
                  </div>
                )}
              </div>
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="gap-2 sm:gap-0">
            <Button variant="outline" onClick={() => setCloseTarget(null)} disabled={closing}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={confirmSquareoff} disabled={closing}>
              {closing ? "Closing…" : "Close Position"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── Edit position / closed trade dialog ───────────────── */}
      <Dialog open={!!editing} onOpenChange={(o) => !o && setEditing(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>
              {editing?.status === "CLOSED" ? "Edit closed trade" : "Edit position"}
            </DialogTitle>
            <DialogDescription asChild>
              <div>
                {editing && (
                  <div className="rounded-lg border bg-muted/30 p-2.5 text-sm">
                    <span className="font-semibold">{editing.symbol}</span>
                    <span className="text-muted-foreground"> · {editing.product_type} · qty {editing.quantity}</span>
                  </div>
                )}
                <p className="mt-1.5 text-[11px]">
                  {editing?.status === "CLOSED"
                    ? "Wallet auto-adjusts via REVERSAL when realised P&L changes."
                    : "User receives a live update — no refresh needed."}
                </p>
              </div>
            </DialogDescription>
          </DialogHeader>

          {editing?.status === "CLOSED" ? (
            <div className="grid gap-3 py-2 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label>Open price</Label>
                <Input
                  type="number"
                  step="0.01"
                  value={form.avg_price}
                  onChange={(e) =>
                    setForm((p) => ({ ...p, avg_price: e.target.value }))
                  }
                  placeholder="Entry / avg price"
                />
              </div>
              <div className="space-y-1.5">
                <Label>Close price</Label>
                <Input
                  type="number"
                  step="0.01"
                  value={form.close_price}
                  onChange={(e) =>
                    setForm((p) => ({ ...p, close_price: e.target.value }))
                  }
                  placeholder="Exit price"
                />
              </div>
              <div className="space-y-1.5 sm:col-span-2">
                <p className="text-[10px] text-muted-foreground">
                  Changing open/close price auto-recalculates Realised P&L and adjusts wallet via REVERSAL.
                </p>
              </div>
              <div className="space-y-1.5 sm:col-span-2">
                <Label>Realised P&L override <span className="text-muted-foreground">(only if not editing prices above)</span></Label>
                <Input
                  type="number"
                  step="0.01"
                  value={form.realized_pnl}
                  onChange={(e) =>
                    setForm((p) => ({ ...p, realized_pnl: e.target.value }))
                  }
                  placeholder="e.g. 258.00 for a profit, -540.00 for a loss"
                />
              </div>
              <div className="space-y-1.5 sm:col-span-2">
                <Label>Closed by (relabel)</Label>
                <select
                  value={form.close_reason}
                  onChange={(e) =>
                    setForm((p) => ({ ...p, close_reason: e.target.value }))
                  }
                  className="h-9 w-full rounded-md border border-border bg-background px-2 text-sm"
                >
                  <option value="">— unset —</option>
                  <option value="USER">USER (user-initiated)</option>
                  <option value="SL_HIT">SL_HIT (stop-loss hit)</option>
                  <option value="TP_HIT">TP_HIT (target hit)</option>
                  <option value="STOP_OUT">STOP_OUT (risk auto-flatten)</option>
                  <option value="CARRY_FORWARD_FAIL">CARRY_FORWARD_FAIL (insufficient overnight margin)</option>
                  <option value="EOD_OVERNIGHT_DISABLED">EOD_OVERNIGHT_DISABLED (intraday-only segment)</option>
                  <option value="ADMIN">ADMIN (manual close)</option>
                  <option value="EOD">EOD (auto-rollover)</option>
                </select>
              </div>
            </div>
          ) : (
            <div className="grid gap-3 py-2 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label>Entry price</Label>
                <Input
                  type="number"
                  step="0.05"
                  value={form.avg_price}
                  onChange={(e) => setForm((p) => ({ ...p, avg_price: e.target.value }))}
                />
              </div>
              <div className="space-y-1.5">
                <Label>Quantity (signed)</Label>
                <Input
                  type="number"
                  step="any"
                  value={form.quantity}
                  onChange={(e) => setForm((p) => ({ ...p, quantity: e.target.value }))}
                />
              </div>
              <div className="space-y-1.5 sm:col-span-2">
                <Label>Opened at</Label>
                <Input
                  type="datetime-local"
                  value={form.opened_at}
                  onChange={(e) => setForm((p) => ({ ...p, opened_at: e.target.value }))}
                />
              </div>
              <div className="space-y-1.5">
                <Label>Stop loss (blank = clear)</Label>
                <Input
                  type="number"
                  step="0.05"
                  value={form.stop_loss}
                  onChange={(e) => setForm((p) => ({ ...p, stop_loss: e.target.value }))}
                />
              </div>
              <div className="space-y-1.5">
                <Label>Target (blank = clear)</Label>
                <Input
                  type="number"
                  step="0.05"
                  value={form.target}
                  onChange={(e) => setForm((p) => ({ ...p, target: e.target.value }))}
                />
              </div>
            </div>
          )}

          <DialogFooter>
            <Button variant="outline" onClick={() => setEditing(null)} disabled={saving}>
              Cancel
            </Button>
            <Button onClick={saveEdit} loading={saving}>
              Save changes
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────
// PnL summary card
// ─────────────────────────────────────────────────────────────────
function PnlCard({
  label,
  value,
  hint,
  icon: Icon,
  fetched,
  loading,
  onFetch,
  invertColor,
}: {
  label: string;
  value: number | string;
  hint?: ReactNode;
  icon?: any;
  fetched?: boolean;
  loading?: boolean;
  onFetch?: () => void;
  // Invert the headline tint: used on the broker Total-of-Both cards where the
  // operator wants PROFIT (positive) in RED and LOSS (negative) in GREEN.
  invertColor?: boolean;
}) {
  const n = Number(value ?? 0);
  const colorClass = pnlColor(invertColor ? -n : n);
  const fullText = formatINR(n);
  const isLazy = onFetch !== undefined;
  const showFetch = isLazy && !fetched;
  return (
    <Card className="min-w-0">
      <CardHeader className="flex flex-row items-start justify-between gap-1 p-2 pb-1 sm:p-4 sm:pb-2">
        <CardDescription className="text-[9px] uppercase tracking-wider leading-tight sm:text-sm sm:normal-case sm:tracking-normal">
          {label}
        </CardDescription>
        {isLazy ? (
          <button
            type="button"
            onClick={onFetch}
            disabled={loading}
            title={fetched ? "Refresh P&L" : "Fetch P&L data"}
            className="grid size-5 shrink-0 place-items-center rounded text-muted-foreground hover:bg-muted/40 hover:text-foreground disabled:opacity-50"
          >
            {loading
              ? <Loader2 className="size-3.5 animate-spin" />
              : <RefreshCw className="size-3.5" />}
          </button>
        ) : (
          Icon && <Icon className={cn("hidden size-4 shrink-0 sm:block", colorClass)} />
        )}
      </CardHeader>
      <CardContent className="p-2 pt-0 sm:p-4 sm:pt-0">
        {showFetch ? (
          <button
            type="button"
            onClick={onFetch}
            className="flex items-center gap-1.5 text-[11px] text-muted-foreground hover:text-foreground sm:text-sm"
          >
            <RefreshCw className="size-3 sm:size-3.5" />
            Click to fetch
          </button>
        ) : loading && !fetched ? (
          <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground sm:text-sm">
            <Loader2 className="size-3 animate-spin sm:size-3.5" />
            Loading…
          </div>
        ) : (
          <div
            className={cn(
              "font-tabular text-[12px] font-bold leading-tight sm:text-2xl sm:font-semibold",
              colorClass,
            )}
            title={fullText}
          >
            {fullText}
          </div>
        )}
        {hint && !showFetch && (
          <div className="mt-1 block text-[10px] leading-tight text-muted-foreground sm:text-[11px]">
            {hint}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/* ─────────────────────────────────────────────────────────────────── */
/* Mobile position card                                                 */
/* ─────────────────────────────────────────────────────────────────── */

/**
 * Phone-friendly card for a single position row. Compresses the 14
 * columns the desktop table shows into a 4-section card layout:
 *   1. Header strip — user + symbol + side + status pill
 *   2. Price strip — Qty · Avg Price · LTP/Close (3-up)
 *   3. P&L strip — Realized / M2M · Brokerage · Net P&L
 *   4. Footer — opened/holding meta + per-status action buttons
 *
 * Status accent bar on the left edge mirrors the deposit/withdrawal
 * cards for visual continuity across the admin panel.
 */
function PositionMobileCard({
  r,
  tab,
  me,
  selectable = false,
  selected = false,
  onToggleSelect,
  onOpenNetting,
  onOpenRateHistory,
  onEdit,
  onClose,
  onReopen,
  onDelete,
}: {
  r: any;
  tab: "open" | "closed";
  me: any;
  selectable?: boolean;
  selected?: boolean;
  onToggleSelect?: () => void;
  onOpenNetting: () => void;
  onOpenRateHistory: () => void;
  onEdit: () => void;
  onClose: () => void;
  onReopen: () => void;
  onDelete: () => void;
}) {
  const isOpen = r.status === "OPEN";
  const isClosed = r.status === "CLOSED";

  const rawSide = (r.opened_side || (Number(r.quantity) >= 0 ? "BUY" : "SELL"))
    .toString()
    .toUpperCase();
  const isBuy = rawSide === "BUY";

  const displayQty = isClosed
    ? Math.abs(Number(r.opening_quantity ?? 0))
    : Math.abs(Number(r.quantity));

  const gross = isOpen
    ? Number(r.unrealized_pnl ?? 0)
    : Number(r.realized_pnl ?? 0);
  const charges = Number(r.charges ?? 0);
  const net = gross - charges;

  // Risk tint reuses the same threshold the desktop row-highlight uses
  // so the visual signal is consistent across viewports.
  const margin = Number(r.margin_used || 0);
  const danger = isOpen && Number(r.unrealized_pnl) < -margin * 0.5;
  const warn = !danger && isOpen && Number(r.unrealized_pnl) < -margin * 0.25;

  // Left accent — destructive on heavy loss, amber on mild loss,
  // emerald on closed, primary on healthy open.
  const accent = danger
    ? "before:bg-destructive"
    : warn
      ? "before:bg-amber-500"
      : isClosed
        ? "before:bg-emerald-500"
        : "before:bg-primary";

  return (
    <div
      className={cn(
        "relative overflow-hidden rounded-xl border border-border bg-card pl-4 shadow-sm transition-shadow hover:shadow-md before:absolute before:inset-y-0 before:left-0 before:w-1",
        accent,
        danger && "bg-destructive/5",
        warn && "bg-amber-500/5",
      )}
      role="button"
      tabIndex={0}
      onClick={onOpenNetting}
      onKeyDown={(e) => { if (e.key === "Enter") onOpenNetting(); }}
    >
      {/* ── Row 1: status + side badges  |  Net P&L ── */}
      <div className="flex items-center justify-between gap-2 px-3 pt-3">
        <div className="flex flex-wrap items-center gap-1.5">
          {selectable && (
            <input
              type="checkbox"
              aria-label="Select trade"
              checked={selected}
              onClick={(e) => e.stopPropagation()}
              onChange={() => onToggleSelect?.()}
              className="size-4 cursor-pointer accent-primary"
            />
          )}
          <StatusPill status={r.status} />
          <span
            className={cn(
              "inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-bold",
              isBuy
                ? "bg-emerald-500/15 text-emerald-500"
                : "bg-red-500/15 text-red-500",
            )}
          >
            {rawSide}
          </span>
          <OwnerBadge row={r} me={me} />
        </div>
        <div className="shrink-0 text-right">
          <div className="text-[9px] uppercase tracking-wider text-muted-foreground">
            Net P&L
          </div>
          <div
            className={cn("tabular-nums text-lg font-bold leading-tight", pnlColor(net))}
            title={`${isOpen ? "M2M" : "Realized"} ${formatINR(gross)} − Bkg ${formatINR(charges)}`}
          >
            {formatINR(net)}
          </div>
        </div>
      </div>

      {/* ── Row 2: symbol + exchange ── */}
      <div className="mt-1.5 flex items-center gap-2 px-3">
        <span className="truncate text-sm font-bold leading-tight" title={r.symbol}>
          {r.symbol}
        </span>
        {r.exchange && (
          <span className="shrink-0 rounded bg-muted/60 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wider text-muted-foreground">
            {r.exchange}
          </span>
        )}
      </div>

      {/* ── Row 3: user name + code ── */}
      <div className="mt-0.5 flex items-center gap-1.5 px-3 text-[11px] text-muted-foreground">
        <span className="truncate font-medium">{r.user_name || "—"}</span>
        <span className="shrink-0 font-mono text-[10px]">{r.user_code || r.user_id?.slice(-6)}</span>
      </div>

      {/* ── Price strip ── */}
      <div className="mx-3 mt-2.5 grid grid-cols-3 divide-x divide-border rounded-lg border border-border bg-muted/20 text-[11px]">
        <div className="min-w-0 px-2 py-1.5">
          <div className="text-[9px] uppercase tracking-wider text-muted-foreground">Qty</div>
          <div className={cn("truncate tabular-nums text-xs font-bold", isBuy ? "text-emerald-500" : "text-red-500")}>
            {displayQty}
          </div>
        </div>
        <div className="min-w-0 px-2 py-1.5">
          <div className="text-[9px] uppercase tracking-wider text-muted-foreground">Avg</div>
          <div className="truncate tabular-nums text-xs font-semibold" title={String(r.avg_price)}>
            {fmtFeedPrice(r.avg_price, r.currency_quote)}
          </div>
          {isClosed && (
            <div className="truncate text-[9px] text-muted-foreground">{fmtOpenedAt(r.opened_at)}</div>
          )}
        </div>
        <div className="min-w-0 px-2 py-1.5">
          <div className="text-[9px] uppercase tracking-wider text-muted-foreground">
            {isClosed ? "Close" : "LTP"}
          </div>
          <div className="truncate tabular-nums text-xs font-semibold" title={String(r.ltp)}>
            {fmtFeedPrice(r.ltp, r.currency_quote)}
          </div>
          {isClosed && (
            <div className="truncate text-[9px] text-muted-foreground">{fmtOpenedAt(r.closed_at)}</div>
          )}
        </div>
      </div>

      {/* ── P&L breakdown + open time ── */}
      <div className="mt-2 grid grid-cols-3 gap-x-3 px-3 text-[11px]">
        <div className="min-w-0">
          <div className="text-[9px] uppercase tracking-wider text-muted-foreground">
            {isOpen ? "M2M" : "Realized"}
          </div>
          <div className={cn("truncate tabular-nums text-xs font-semibold", pnlColor(gross))}>
            {formatINR(gross)}
          </div>
        </div>
        <div className="min-w-0">
          <div className="text-[9px] uppercase tracking-wider text-muted-foreground">Brokerage</div>
          <div className="truncate tabular-nums text-xs font-semibold">{formatINR(charges)}</div>
        </div>
        {isClosed ? (
          <div className="min-w-0">
            <div className="text-[9px] uppercase tracking-wider text-muted-foreground">Wallet After</div>
            <div className="truncate tabular-nums text-xs font-bold" title="Wallet balance right after this trade closed">
              {r.wallet_after != null ? formatINR(Number(r.wallet_after)) : "—"}
            </div>
          </div>
        ) : (
          <div className="min-w-0">
            <div className="text-[9px] uppercase tracking-wider text-muted-foreground">Opened</div>
            <div className="truncate text-[10px] text-muted-foreground">{fmtOpenedAt(r.opened_at)}</div>
          </div>
        )}
      </div>

      {/* ── Closed meta: held time + close reason ── */}
      {isClosed && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5 px-3 text-[11px] text-muted-foreground">
          <span className="rounded bg-muted/50 px-1.5 py-0.5 text-[10px]" title={r.closed_at ?? undefined}>
            Held {fmtHoldingTime(r.opened_at, r.closed_at)}
          </span>
          {r.close_reason && <CloseReasonChip reason={r.close_reason} />}
        </div>
      )}

      {/* ── Actions ── stops propagation so netting dialog doesn't open */}
      <div
        className="mt-3 flex items-center gap-2 border-t border-border px-3 py-2.5"
        onClick={(e) => e.stopPropagation()}
      >
        {isOpen && (
          <>
            <Button
              size="sm"
              variant="outline"
              className="h-8 flex-1 gap-1 border-brand-500/40 text-brand-500 hover:bg-brand-500 hover:text-white"
              onClick={onEdit}
            >
              <Pencil className="size-3" /> Edit
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="h-8 flex-1 gap-1 border-destructive/40 text-destructive hover:bg-destructive hover:text-destructive-foreground"
              onClick={onClose}
            >
              <X className="size-3" /> Close
            </Button>
          </>
        )}
        {isClosed && (
          <>
            <Button
              size="sm"
              variant="outline"
              className="h-8 flex-1 gap-1 border-brand-500/40 text-brand-500 hover:bg-brand-500 hover:text-white"
              onClick={onEdit}
            >
              <Pencil className="size-3" /> Edit
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="h-8 flex-1 gap-1 border-amber-500/40 text-amber-500 hover:bg-amber-500 hover:text-white"
              onClick={onReopen}
            >
              <RotateCcw className="size-3" /> Reopen
            </Button>
          </>
        )}
        <Button
          size="sm"
          variant="outline"
          aria-label="Delete"
          className="h-8 w-8 shrink-0 border-destructive/30 p-0 text-destructive hover:bg-destructive hover:text-destructive-foreground"
          onClick={onDelete}
        >
          <Trash2 className="size-3.5" />
        </Button>
      </div>

      {/* Rate History — full-width, shown for both open + closed. */}
      <div className="px-3 pb-2.5" onClick={(e) => e.stopPropagation()}>
        <Button
          size="sm"
          variant="outline"
          className="h-8 w-full gap-1"
          onClick={onOpenRateHistory}
        >
          <History className="size-3" /> Rate History
        </Button>
      </div>
    </div>
  );
}

