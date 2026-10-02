"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Search, XCircle, X as XIcon } from "lucide-react";
import { TradingAPI, UsersAPI } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/common/PageHeader";
import { DataTable, type Column } from "@/components/common/DataTable";
import { Pagination } from "@/components/common/Pagination";
import { StatusPill } from "@/components/common/StatusPill";
import { cn } from "@/lib/utils";

function fmtPrice(value: number | string | null | undefined): string {
  const n = typeof value === "string" ? Number(value) : (value ?? 0);
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  });
}

// Why an order happened — the backend's order_reason_code tag. Shown on the
// Executed tab so the operator sees WHY each fill occurred: a user/admin
// place, an auto stop-out / SL / TP, or an admin force-close.
const REASON_META: Record<string, { label: string; cls: string }> = {
  USER: { label: "User", cls: "bg-muted/40 text-muted-foreground ring-border" },
  ADMIN: { label: "Admin", cls: "bg-brand-500/10 text-brand-400 ring-brand-500/30" },
  ADMIN_CLOSE: { label: "Admin close", cls: "bg-amber-500/10 text-amber-400 ring-amber-500/30" },
  STOP_OUT: { label: "Stop-out", cls: "bg-amber-500/10 text-amber-400 ring-amber-500/30" },
  SL_HIT: { label: "Stop loss", cls: "bg-red-500/10 text-red-400 ring-red-500/30" },
  TP_HIT: { label: "Target", cls: "bg-emerald-500/10 text-emerald-400 ring-emerald-500/30" },
  AUTO: { label: "Auto", cls: "bg-muted/40 text-muted-foreground ring-border" },
};

function ReasonChip({ reason }: { reason?: string | null }) {
  const meta = REASON_META[(reason || "").toUpperCase()] ?? {
    label: reason || "—",
    cls: "bg-muted/40 text-muted-foreground ring-border",
  };
  return (
    <span
      className={cn(
        "inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium ring-1 ring-inset",
        meta.cls,
      )}
    >
      {meta.label}
    </span>
  );
}

// Four tabs — all driven by the same /admin/orders endpoint with
// different status filters. Operator dropped the dedicated "Executions"
// tab on 21-May because trade fills are already visible per-order via
// status=EXECUTED, and the standalone trades table was duplicating
// information without the operator-relevant per-user grouping.
type Tab = "pending" | "executed" | "rejected" | "sltp";

const TABS: { id: Tab; label: string; description: string }[] = [
  {
    id: "pending",
    label: "Pending Orders",
    description: "Orders awaiting trigger or fill — PENDING, OPEN, PARTIAL.",
  },
  {
    id: "executed",
    label: "Executed Orders",
    description: "Orders that have fully filled.",
  },
  {
    id: "rejected",
    label: "Rejected Orders",
    description: "Orders rejected by validation (margin shortfall, limits, etc.).",
  },
  {
    id: "sltp",
    label: "SL / TP",
    description: "Orders carrying a stop-loss or target — SL/SL-M or bracket SL/TP.",
  },
];

export default function AdminOrdersPage() {
  return (
    <Suspense fallback={null}>
      <AdminOrdersInner />
    </Suspense>
  );
}

function AdminOrdersInner() {
  const searchParams = useSearchParams();
  const queryUserId = searchParams?.get("user_id") ?? null;
  const queryTab = (searchParams?.get("tab") ?? "pending") as Tab;

  const isValidTab = (t: string): t is Tab =>
    ["pending", "executed", "rejected", "sltp"].includes(t);

  const [tab, setTab] = useState<Tab>(isValidTab(queryTab) ? queryTab : "pending");
  useEffect(() => {
    if (isValidTab(queryTab)) setTab(queryTab);
  }, [queryTab]);

  // Search box lives at the page level (not inside a single tab) so the
  // operator keeps their query when flipping between Pending / Executed
  // / Rejected / SL-TP — "esme sare section me" from the 22-May ask.
  // Input is debounced by 300 ms before being sent to the API so each
  // keystroke doesn't fire its own request.
  const [searchInput, setSearchInput] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  useEffect(() => {
    const id = setTimeout(() => setSearchQuery(searchInput.trim()), 300);
    return () => clearTimeout(id);
  }, [searchInput]);

  const { data: scopedUser } = useQuery({
    queryKey: ["admin", "user", queryUserId],
    queryFn: () => UsersAPI.detail(queryUserId!),
    enabled: !!queryUserId,
    staleTime: 5 * 60_000,
  });

  const active = TABS.find((t) => t.id === tab)!;

  return (
    <div className="space-y-4">
      <PageHeader title="Orders monitor" description={active.description} />

      <TodaySummary />

      {queryUserId && (
        <div className="inline-flex items-center gap-2 rounded-md border border-primary/40 bg-primary/10 px-3 py-1.5 text-xs">
          <span className="text-muted-foreground">Filtered by user:</span>
          <span className="font-semibold text-primary">
            {(scopedUser as any)?.user_code ?? queryUserId.slice(-8)}
            {(scopedUser as any)?.full_name ? ` · ${(scopedUser as any).full_name}` : ""}
          </span>
          <Link
            href={`/orders?tab=${tab}`}
            className="grid size-5 place-items-center rounded text-muted-foreground hover:bg-muted/40 hover:text-foreground"
            aria-label="Clear user filter"
          >
            <XIcon className="size-3" />
          </Link>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <div className="inline-flex flex-wrap rounded-md border border-border bg-muted/30 p-1 text-sm">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => setTab(t.id)}
              className={cn(
                "rounded px-3 py-1.5 transition-colors",
                tab === t.id ? "bg-primary/15 text-primary" : "text-muted-foreground hover:text-foreground"
              )}
            >
              {t.label}
            </button>
          ))}
        </div>

        <div className="relative flex-1 min-w-[220px] max-w-md">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <input
            type="text"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            placeholder="Search by user name, code, or symbol…"
            className="h-9 w-full rounded-md border border-border bg-muted/20 pl-8 pr-8 text-sm outline-none placeholder:text-muted-foreground focus:border-primary"
          />
          {searchInput && (
            <button
              type="button"
              onClick={() => setSearchInput("")}
              aria-label="Clear search"
              className="absolute right-2 top-1/2 grid size-5 -translate-y-1/2 place-items-center rounded text-muted-foreground hover:bg-muted/40 hover:text-foreground"
            >
              <XIcon className="size-3" />
            </button>
          )}
        </div>
      </div>

      <OrdersTable tab={tab} userId={queryUserId} search={searchQuery} />
    </div>
  );
}

// Today's trading summary — a small strip of stat cards above the tabs so
// the operator sees the day's activity at a glance: how many fills happened
// (total / buy / sell) and how many orders are still pending. Driven by
// /admin/orders/stats (IST day, admin-scoped, demo excluded) and refreshed
// every 10 s so it tracks live trading without a page reload.
function TodaySummary() {
  const { data } = useQuery({
    queryKey: ["admin", "orders", "stats"],
    queryFn: () => TradingAPI.ordersStats(),
    refetchInterval: 10000,
  });

  const cards: { label: string; value?: number; cls: string }[] = [
    { label: "Total Trades", value: data?.total_trades, cls: "text-primary" },
    { label: "Buy", value: data?.buy_trades, cls: "text-profit" },
    { label: "Sell", value: data?.sell_trades, cls: "text-loss" },
    { label: "Pending", value: data?.pending_orders, cls: "text-amber-400" },
  ];

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 text-xs">
        <span className="inline-flex items-center rounded bg-primary/10 px-2 py-0.5 font-semibold uppercase tracking-wide text-primary">
          Today
        </span>
        {data?.date_ist && <span className="text-muted-foreground">{data.date_ist}</span>}
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {cards.map((c) => (
          <div key={c.label} className="rounded-lg border border-border bg-muted/20 px-4 py-3">
            <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{c.label}</div>
            <div className={cn("mt-1 text-2xl font-semibold tabular-nums", c.cls)}>
              {c.value ?? "—"}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function OrdersTable({
  tab,
  userId,
  search,
}: {
  tab: Tab;
  userId?: string | null;
  search?: string;
}) {
  const qc = useQueryClient();
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);

  useEffect(() => {
    setPage(1);
  }, [tab, userId, search]);

  const apiParams = useMemo<Record<string, any>>(() => {
    const base: Record<string, any> = {
      page,
      page_size: pageSize,
      user_id: userId || undefined,
      // Backend ignores `q` shorter than 2 chars; omit entirely so the
      // query-key stays stable across empty-search renders and React
      // Query doesn't refetch on every initial keystroke.
      q: search && search.length >= 2 ? search : undefined,
    };
    if (tab === "pending") base.statuses = "PENDING,OPEN,PARTIAL";
    else if (tab === "executed") base.status = "EXECUTED";
    else if (tab === "rejected") base.status = "REJECTED";
    else if (tab === "sltp") base.sl_tp = true;
    return base;
  }, [tab, userId, search, page, pageSize]);

  const { data, isFetching } = useQuery({
    queryKey: ["admin", "orders", apiParams],
    queryFn: () => TradingAPI.orders(apiParams),
    refetchInterval: 5000,
  });

  async function cancelOrder(id: string) {
    if (!confirm("Force-cancel this order?")) return;
    try {
      await TradingAPI.forceCancel(id);
      toast.success("Cancelled");
      qc.invalidateQueries({ queryKey: ["admin", "orders"] });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  // Common columns sit at the front: user (name + code stacked so the
  // operator sees who placed it without needing to memorise codes),
  // then instrument + side + qty. Tab-specific extras get appended,
  // and every tab ends with a Date / Time column per the 21-May
  // request ("orde id remoev karek name likho user ka and then data
  // and time rahe ga").
  const cols: Column<any>[] = useMemo(() => {
    const base: Column<any>[] = [
      {
        key: "user",
        header: "User",
        render: (r) => (
          <div className="flex flex-col leading-tight">
            <span className="font-medium">{r.user_name || "—"}</span>
            <span className="text-[11px] text-muted-foreground">{r.user_code || r.user_id?.slice(-6)}</span>
          </div>
        ),
      },
      { key: "symbol", header: "Symbol" },
      { key: "exchange", header: "Exch" },
      { key: "action", header: "Side", render: (r) => <StatusPill status={r.action} /> },
      { key: "order_type", header: "Type", render: (r) => <StatusPill status={r.order_type} /> },
      { key: "lots", header: "Lots", align: "right" },
      { key: "quantity", header: "Qty", align: "right" },
    ];

    if (tab === "pending") {
      base.push(
        { key: "price", header: "Limit", align: "right", render: (r) => fmtPrice(r.price) },
        {
          key: "trigger_price",
          header: "Trigger",
          align: "right",
          render: (r) =>
            Number(r.trigger_price ?? 0) > 0 ? fmtPrice(r.trigger_price) : <span className="text-muted-foreground">—</span>,
        },
        { key: "filled_quantity", header: "Filled", align: "right" },
        { key: "status", header: "Status", render: (r) => <StatusPill status={r.status} /> },
      );
    } else if (tab === "executed") {
      base.push(
        { key: "average_price", header: "Fill", align: "right", render: (r) => fmtPrice(r.average_price) },
        { key: "filled_quantity", header: "Filled", align: "right" },
        { key: "reason", header: "Reason", render: (r) => <ReasonChip reason={r.reason} /> },
      );
    } else if (tab === "rejected") {
      base.push(
        { key: "price", header: "Price", align: "right", render: (r) => fmtPrice(r.price) },
        {
          key: "rejection_reason",
          header: "Reason",
          render: (r) => (
            <span className="text-xs text-loss" title={r.rejection_reason || ""}>
              {r.rejection_reason || "—"}
            </span>
          ),
        },
      );
    } else if (tab === "sltp") {
      base.push(
        { key: "average_price", header: "Entry", align: "right", render: (r) => fmtPrice(r.average_price || r.price) },
        {
          key: "trigger_price",
          header: "Trigger",
          align: "right",
          render: (r) =>
            Number(r.trigger_price ?? 0) > 0 ? fmtPrice(r.trigger_price) : <span className="text-muted-foreground">—</span>,
        },
        {
          key: "bracket_stop_loss",
          header: "SL",
          align: "right",
          render: (r) =>
            r.bracket_stop_loss ? (
              <span className="font-tabular text-loss">{fmtPrice(r.bracket_stop_loss)}</span>
            ) : (
              <span className="text-muted-foreground">—</span>
            ),
        },
        {
          key: "bracket_target",
          header: "Target",
          align: "right",
          render: (r) =>
            r.bracket_target ? (
              <span className="font-tabular text-profit">{fmtPrice(r.bracket_target)}</span>
            ) : (
              <span className="text-muted-foreground">—</span>
            ),
        },
        { key: "status", header: "Status", render: (r) => <StatusPill status={r.status} /> },
      );
    }

    // Date / time column — for Executed orders the fill time is more
    // meaningful than placement time, so prefer executed_at and fall
    // back to created_at. Other tabs use placement time.
    base.push({
      key: "when",
      header: "Date / Time",
      render: (r) => {
        const ts = tab === "executed" ? r.executed_at || r.created_at : r.created_at;
        if (!ts) return <span className="text-muted-foreground">—</span>;
        const d = new Date(ts);
        return (
          <div className="flex flex-col leading-tight">
            <span>{d.toLocaleDateString()}</span>
            <span className="text-[11px] text-muted-foreground">{d.toLocaleTimeString()}</span>
          </div>
        );
      },
    });

    base.push({
      key: "actions",
      header: "",
      align: "right",
      render: (r) =>
        ["OPEN", "PENDING", "PARTIAL"].includes(r.status) ? (
          <Button variant="ghost" size="icon" onClick={() => cancelOrder(r.id)} aria-label="Cancel">
            <XCircle className="size-4 text-destructive" />
          </Button>
        ) : null,
    });

    return base;
  }, [tab]);

  return (
    <div className="space-y-3">
      <div className="text-xs text-muted-foreground">{data?.meta?.total ?? 0} orders</div>
      <DataTable columns={cols} rows={data?.items} keyExtractor={(r) => r.id} loading={isFetching && !data} />
      <Pagination
        page={page}
        pageSize={pageSize}
        total={data?.meta?.total ?? 0}
        onPageChange={setPage}
        onPageSizeChange={setPageSize}
        pageSizeOptions={[25, 50, 100, 200]}
      />
    </div>
  );
}
