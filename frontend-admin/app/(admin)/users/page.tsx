"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Plus, Search, TrendingDown, TrendingUp } from "lucide-react";
import { UsersAPI } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PageHeader } from "@/components/common/PageHeader";
import { DataTable, type Column } from "@/components/common/DataTable";
import { StatusPill } from "@/components/common/StatusPill";
import { UserActionMenu, LiveTradeStatsDialog } from "@/components/admin/UserActionMenu";
import { OwnerBadge } from "@/components/admin/OwnerBadge";
import { LedgerSheet } from "@/components/admin/LedgerSheet";
import { useAdminAuthStore } from "@/stores/authStore";

/**
 * Cadence at which the admin Users table re-fetches per-user live stats
 * (available balance, open P&L, equity).
 *
 * Customer-facing terminals throttle their websocket display at 200 ms
 * (matches the 250 ms backend tick loop). For the admin overview table,
 * 1.5 s strikes a balance: fast enough for "live" feel + red/green color
 * shifts, slow enough that 50-row tables don't hammer the backend.
 *
 * Per row the backend does ONE wallet query + ONE positions query for
 * the whole page (batched via $in), then parallel LTP fan-out across
 * unique tokens — so the per-tick cost scales with unique tokens, not
 * row count.
 */
const LIVE_STATS_REFETCH_MS = 1500;

type LiveStat = {
  user_id: string;
  available_balance: string;
  open_pnl: string;
  equity: string;
  used_margin: string;
  credit_limit: string;
};

export default function AdminUsersPage() {
  const me = useAdminAuthStore((s) => s.admin);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState<string>("");
  const [mode, setMode] = useState<"live" | "demo">("live");
  const [page, setPage] = useState(1);
  const [ledgerUser, setLedgerUser] = useState<any | null>(null);
  const [statsUser, setStatsUser] = useState<any | null>(null);
  const pageSize = 20;

  const { data, isFetching } = useQuery({
    queryKey: ["admin", "users", { q, status, mode, page, pageSize }],
    queryFn: () =>
      UsersAPI.list({
        q: q || undefined,
        status: status || undefined,
        mode,
        page,
        page_size: pageSize,
      }),
  });

  // ── Live stats poll ─────────────────────────────────────────────
  // Drives the AVAILABLE / OPEN P&L / EQUITY columns. We pass the ids
  // of the current page so the backend only computes for what's
  // visible. Skips the request entirely while the page list is still
  // loading.
  const visibleUserIds = useMemo<string[]>(
    () => (data?.items ?? []).map((u: any) => u.id),
    [data?.items],
  );

  const liveStatsQuery = useQuery({
    queryKey: ["admin", "users", "live-stats", visibleUserIds.join(",")],
    queryFn: () => UsersAPI.liveStats(visibleUserIds),
    enabled: visibleUserIds.length > 0,
    refetchInterval: LIVE_STATS_REFETCH_MS,
    refetchOnWindowFocus: false,
    staleTime: 0,
  });

  const liveStatsMap = useMemo<Record<string, LiveStat>>(() => {
    const m: Record<string, LiveStat> = {};
    for (const row of liveStatsQuery.data?.items ?? []) {
      m[row.user_id] = row;
    }
    return m;
  }, [liveStatsQuery.data]);

  /**
   * Pick the freshest value for a given user/field. Prefers the live
   * poll, falls back to the value embedded in the user list payload
   * (the static wallet snapshot) so the column never blinks to "—"
   * during the first 1.5 s after page load.
   */
  function pickBalance(r: any): number {
    const live = liveStatsMap[r.id];
    if (live) return Number(live.available_balance) + Number(live.used_margin);
    const w = r.wallet;
    return Number(w?.available_balance ?? 0) + Number(w?.used_margin ?? 0);
  }

  function pickOpenPnl(r: any): number | null {
    const live = liveStatsMap[r.id]?.open_pnl;
    return live != null ? Number(live) : null;
  }

  function pickEquity(r: any): number | null {
    const live = liveStatsMap[r.id];
    if (live) return Number(live.available_balance) + Number(live.used_margin) + Number(live.open_pnl);
    return null;
  }

  function pickMargin(r: any): number {
    const live = liveStatsMap[r.id]?.used_margin;
    if (live != null) return Number(live);
    return Number(r.wallet?.used_margin ?? 0);
  }

  const columns: Column<any>[] = [
    {
      key: "user_code",
      header: "Code",
      render: (r) => <span className="font-mono text-xs">{r.user_code}</span>,
    },
    { key: "full_name", header: "Name" },
    {
      key: "email",
      header: "Email",
      className: "max-w-[240px] truncate",
    },
    { key: "mobile", header: "Mobile" },
    {
      key: "owner",
      header: "Owner",
      render: (r) => <OwnerBadge row={r} me={me} />,
    },
    {
      key: "status",
      header: "Status",
      render: (r) => <StatusPill status={r.status} />,
    },
    {
      key: "ledger",
      header: "LEDGER",
      align: "center",
      render: (r: any) => (
        <Button
          size="sm"
          variant="outline"
          className="h-7 w-7 p-0 font-mono font-semibold border-primary/50 text-primary hover:bg-primary hover:text-primary-foreground"
          title="View ledger / Adjust wallet"
          onClick={(e) => {
            e.stopPropagation();
            setLedgerUser(r);
          }}
        >
          L
        </Button>
      ),
    },
    {
      key: "stats",
      header: "STATS",
      align: "center",
      render: (r: any) => (
        <Button
          size="sm"
          variant="outline"
          className="h-7 w-7 p-0 font-mono font-semibold border-info/50 text-info hover:bg-info hover:text-info-foreground"
          title="Live trading stats"
          onClick={(e) => {
            e.stopPropagation();
            setStatsUser(r);
          }}
        >
          S
        </Button>
      ),
    },
    {
      key: "positions",
      header: "POSITION",
      align: "center",
      render: (r: any) => (
        <Button
          asChild
          size="sm"
          variant="outline"
          className="h-7 w-7 p-0 font-mono font-semibold border-atm/50 text-atm hover:bg-atm hover:text-atm-foreground"
          title="View positions"
          onClick={(e) => e.stopPropagation()}
        >
          <Link href={`/positions?user_id=${r.id}`}>P</Link>
        </Button>
      ),
    },
    {
      key: "balance",
      header: "BALANCE",
      align: "right",
      render: (r: any) => <MoneyCell value={pickBalance(r)} muted />,
    },
    {
      key: "open_pnl",
      header: "OPEN P&L",
      align: "right",
      render: (r: any) => <PnlCell value={pickOpenPnl(r)} />,
    },
    {
      key: "equity",
      header: "EQUITY",
      align: "right",
      render: (r: any) => {
        const bal = pickBalance(r);
        const eq = pickEquity(r);
        return <EquityCell available={bal} equity={eq} />;
      },
    },
    {
      key: "weekly_pnl",
      header: "Weekly PnL",
      align: "right",
      render: (r) => {
        const v = Number(r.wallet?.weekly_closed_pnl ?? 0);
        if (!v) return <span className="text-sm text-muted-foreground">—</span>;
        const up = v > 0;
        return (
          <span
            className={`text-sm font-semibold tabular-nums ${up ? "text-emerald-500" : "text-destructive"}`}
            title="Realised P&L booked this week (Mon → now)"
          >
            {up ? "+" : "−"}₹
            {Math.abs(v).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </span>
        );
      },
    },
    {
      key: "actions",
      header: "",
      align: "right",
      render: (r) => (
        <div className="flex justify-end">
          <UserActionMenu user={r} />
        </div>
      ),
    },
  ];

  const total = data?.meta?.total ?? 0;
  const totalPages = data?.meta?.total_pages ?? 1;
  const isDemo = mode === "demo";

  return (
    <div className="space-y-4">
      <PageHeader
        title={isDemo ? "Demo users" : "All users"}
        description={`${total} ${isDemo ? "demo" : ""} users`}
        actions={
          <Button asChild>
            <Link href="/users/new">
              <Plus className="size-4" /> New user
            </Link>
          </Button>
        }
      />

      {/* Live / Demo mode toggle */}
      <div className="inline-flex rounded-lg border border-border bg-muted/40 p-1">
        <button
          onClick={() => { setMode("live"); setPage(1); }}
          className={`rounded-md px-4 py-1.5 text-xs font-semibold transition-colors ${
            !isDemo
              ? "bg-background shadow text-foreground"
              : "text-muted-foreground hover:text-foreground"
          }`}
        >
          Live
        </button>
        <button
          onClick={() => { setMode("demo"); setPage(1); }}
          className={`rounded-md px-4 py-1.5 text-xs font-semibold transition-colors ${
            isDemo
              ? "bg-amber-500/20 shadow text-amber-500"
              : "text-muted-foreground hover:text-foreground"
          }`}
        >
          Demo
        </button>
      </div>

      {isDemo && (
        <div className="flex items-center gap-2 rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-500">
          <span className="font-semibold">Demo mode</span>
          <span className="text-muted-foreground">— Showing demo accounts only. Data is virtual and does not affect real P&amp;L or payments.</span>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[220px]">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={q}
            onChange={(e) => {
              setPage(1);
              setQ(e.target.value);
            }}
            placeholder="Search code / email / mobile / name"
            className="pl-9"
          />
        </div>
        <select
          value={status}
          onChange={(e) => {
            setPage(1);
            setStatus(e.target.value);
          }}
          className="h-10 rounded-md border border-border bg-background px-3 text-sm"
        >
          <option value="">All statuses</option>
          <option value="ACTIVE">Active</option>
          <option value="PENDING">Pending</option>
          <option value="BLOCKED">Blocked</option>
          <option value="CLOSED">Closed</option>
        </select>
        {!isDemo && <LiveBadge fetching={liveStatsQuery.isFetching} />}
      </div>

      {/* Desktop: full data table. Hidden on phones because 14 columns
          don't fit and the horizontal-scroll experience is awkward
          (operator flagged: "kuch dikhta hi nahi"). */}
      <div className="hidden md:block">
        <DataTable
          columns={columns}
          rows={data?.items}
          keyExtractor={(r) => r.id}
          loading={isFetching && !data}
          empty="No users match the current filters."
        />
      </div>

      {/* Mobile: stacked card list — same data, tap-friendly layout. */}
      <div className="space-y-2 md:hidden">
        {isFetching && !data && (
          <div className="rounded-lg border border-border bg-card p-6 text-center text-sm text-muted-foreground">
            Loading…
          </div>
        )}
        {!isFetching && (!data?.items || data.items.length === 0) && (
          <div className="rounded-lg border border-dashed border-border bg-card p-6 text-center text-sm text-muted-foreground">
            No users match the current filters.
          </div>
        )}
        {data?.items?.map((r: any) => (
          <UserMobileCard
            key={r.id}
            r={r}
            me={me}
            balance={pickBalance(r)}
            openPnl={pickOpenPnl(r)}
            equity={pickEquity(r)}
            onLedger={() => setLedgerUser(r)}
            onStats={() => setStatsUser(r)}
          />
        ))}
      </div>

      {totalPages > 1 && (
        <div className="flex items-center justify-end gap-2 text-xs text-muted-foreground">
          <span>
            Page {page} of {totalPages}
          </span>
          <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
            Prev
          </Button>
          <Button variant="outline" size="sm" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>
            Next
          </Button>
        </div>
      )}

      <LedgerSheet
        open={!!ledgerUser}
        onClose={() => setLedgerUser(null)}
        user={ledgerUser}
      />
      {statsUser && (
        <LiveTradeStatsDialog
          open={!!statsUser}
          userId={statsUser.id}
          userCode={statsUser.user_code}
          fullName={statsUser.full_name || ""}
          onClose={() => setStatsUser(null)}
        />
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────────── */
/* Cells                                                                */
/* ─────────────────────────────────────────────────────────────────── */

function MoneyCell({ value, muted }: { value: number; muted?: boolean }) {
  return (
    <span
      className={`text-sm font-semibold tabular-nums ${
        muted ? "text-foreground/90" : ""
      }`}
    >
      ₹{value.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
    </span>
  );
}

/**
 * Open P&L cell — green for profit, red for loss, muted for exactly 0
 * (or while live stats are still loading). The arrow icon is tinted to
 * match so the cell remains scannable at a glance across many rows.
 */
function PnlCell({ value }: { value: number | null }) {
  if (value == null) {
    return <span className="text-sm text-muted-foreground">—</span>;
  }
  if (value === 0) {
    return (
      <span className="text-sm font-semibold tabular-nums text-muted-foreground">
        ₹0.00
      </span>
    );
  }
  const positive = value > 0;
  const formatted = `₹${Math.abs(value).toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
  return (
    <span
      className={`inline-flex items-center justify-end gap-1 text-sm font-semibold tabular-nums ${
        positive ? "text-emerald-400" : "text-destructive"
      }`}
    >
      {positive ? (
        <TrendingUp className="h-3.5 w-3.5" />
      ) : (
        <TrendingDown className="h-3.5 w-3.5" />
      )}
      {positive ? "+" : "−"}
      {formatted}
    </span>
  );
}

/**
 * "Left balance" — what the user's effective equity is right now
 * (available + open P&L). Colors against the available baseline:
 *   • emerald when equity > available (open P&L is profitable)
 *   • red     when equity < available (open P&L is loss)
 *   • neutral when equal (no open positions / break-even)
 */
function EquityCell({
  available,
  equity,
}: {
  available: number;
  equity: number | null;
}) {
  if (equity == null) {
    return <MoneyCell value={available} muted />;
  }
  const delta = equity - available;
  const tone =
    delta > 0
      ? "text-emerald-400"
      : delta < 0
        ? "text-destructive"
        : "text-foreground/90";
  return (
    <span className={`text-sm font-semibold tabular-nums ${tone}`}>
      ₹{equity.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
    </span>
  );
}

/**
 * Small "Live" indicator above the table — pulses a green dot while the
 * 1.5s poll is in-flight so operators can see at a glance that the
 * live columns are refreshing.
 */
function LiveBadge({ fetching }: { fetching: boolean }) {
  return (
    <span className="ml-auto inline-flex items-center gap-1.5 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-1 text-[10px] font-medium uppercase tracking-wider text-emerald-300">
      <span className="relative inline-flex h-1.5 w-1.5">
        <span
          className={`absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75 ${
            fetching ? "animate-ping" : ""
          }`}
        />
        <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-emerald-400" />
      </span>
      Live · {(LIVE_STATS_REFETCH_MS / 1000).toFixed(1)}s
    </span>
  );
}

/* ─────────────────────────────────────────────────────────────────── */
/* Mobile user card                                                     */
/* ─────────────────────────────────────────────────────────────────── */

/**
 * Compact card representation of a user row for phones (<md). Shows
 * the essentials — identifier, contact, status, owner — plus the
 * three live money metrics in a 3-up mini-grid, and surfaces the same
 * L/S/P quick actions + UserActionMenu the table has on desktop.
 *
 * Reuses the exact same data picks the table does so values never
 * drift between the two views.
 */
function UserMobileCard({
  r,
  me,
  balance,
  openPnl,
  equity,
  onLedger,
  onStats,
}: {
  r: any;
  me: any;
  balance: number;
  openPnl: number | null;
  equity: number | null;
  onLedger: () => void;
  onStats: () => void;
}) {
  const weeklyPnl = Number(r.wallet?.weekly_closed_pnl ?? 0);

  return (
    <div className="overflow-hidden rounded-xl border border-border bg-gradient-to-br from-card to-card/60 p-3 shadow-sm transition-shadow hover:shadow-md">
      {/* Row 1: user_code + status pill on the left, action menu on the
          right. Owner / Transferred chips moved to their own row below
          (operator complaint: code + status + broker-name + transferred
          + menu were colliding on 360 px screens — chips visually overlapped). */}
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5">
          <span className="rounded-md bg-primary/10 px-1.5 py-0.5 font-mono text-[11px] font-semibold text-primary">
            {r.user_code}
          </span>
          <StatusPill status={r.status} />
        </div>
        <div className="shrink-0">
          <UserActionMenu user={r} />
        </div>
      </div>

      {/* Row 2: name + contact info */}
      <div className="mt-1.5 truncate text-sm font-semibold" title={r.full_name}>
        {r.full_name || "—"}
      </div>
      <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-muted-foreground">
        {r.email && <span className="truncate">{r.email}</span>}
        {r.mobile && (
          <a
            href={`tel:${r.mobile}`}
            className="font-tabular text-primary"
            onClick={(e) => e.stopPropagation()}
          >
            {r.mobile}
          </a>
        )}
      </div>

      {/* Row 3: owner / transferred chips — own row so long broker
          names ("Sub-broker shyamlal-trading") don't crash into the
          status pill. Wraps gracefully when both chips are present. */}
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5 [&_span]:max-w-full [&_span]:truncate">
        <OwnerBadge row={r} me={me} />
      </div>

      {/* Money strip — 3 live metrics in a single tight row */}
      <div className="mt-3 grid grid-cols-3 gap-2 rounded-lg bg-muted/30 p-2">
        <MoneyTile label="Balance" value={balance} tone="neutral" />
        <MoneyTile label="Open P&L" value={openPnl} tone="pnl" />
        <MoneyTile label="Equity" value={equity} tone="equity" base={balance} />
      </div>

      {/* Weekly realised P&L (Mon → now) — only render when non-zero */}
      {weeklyPnl !== 0 && (
        <div
          className={`mt-2 flex items-center justify-between rounded-md border px-2.5 py-1.5 text-[11px] ${
            weeklyPnl > 0
              ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
              : "border-destructive/30 bg-destructive/10 text-destructive"
          }`}
        >
          <span>Weekly P&L</span>
          <span className="font-semibold tabular-nums">
            {weeklyPnl > 0 ? "+" : "−"}₹
            {Math.abs(weeklyPnl).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </span>
        </div>
      )}

      {/* Quick actions row — same L / S / P as table desktop, but full
          tap-target friendly with labels. */}
      <div className="mt-3 grid grid-cols-3 gap-2">
        <Button
          size="sm"
          variant="outline"
          className="h-9 border-primary/40 text-primary hover:bg-primary hover:text-primary-foreground"
          onClick={onLedger}
        >
          Ledger
        </Button>
        <Button
          size="sm"
          variant="outline"
          className="h-9 border-info/40 text-info hover:bg-info hover:text-info-foreground"
          onClick={onStats}
        >
          Stats
        </Button>
        <Button asChild size="sm" variant="outline" className="h-9 border-atm/40 text-atm hover:bg-atm hover:text-atm-foreground">
          <Link href={`/positions?user_id=${r.id}`}>Positions</Link>
        </Button>
      </div>
    </div>
  );
}

/**
 * Compact money tile used inside UserMobileCard's 3-up strip. Pure
 * presentation — `tone` selects color tint, `base` lets the equity
 * variant compare against the available balance to pick green/red.
 */
function MoneyTile({
  label,
  value,
  tone,
  base,
}: {
  label: string;
  value: number | null;
  tone: "neutral" | "pnl" | "equity";
  base?: number;
}) {
  const display =
    value == null
      ? "—"
      : `₹${value.toLocaleString("en-IN", {
          minimumFractionDigits: 2,
          maximumFractionDigits: 2,
        })}`;

  let valueClass = "text-foreground";
  if (tone === "pnl" && value != null && value !== 0) {
    valueClass = value > 0 ? "text-emerald-500" : "text-destructive";
  } else if (tone === "equity" && value != null && base != null) {
    const delta = value - base;
    valueClass =
      delta > 0 ? "text-emerald-500" : delta < 0 ? "text-destructive" : "text-foreground";
  }

  return (
    <div className="min-w-0">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className={`mt-0.5 truncate font-tabular text-xs font-semibold ${valueClass}`} title={display}>
        {display}
      </div>
    </div>
  );
}
