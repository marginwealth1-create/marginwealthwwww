"use client";

import { useState, useEffect, useMemo, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  BarChart, Bar, PieChart, Pie, Cell,
  XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from "recharts";
import {
  ArrowDownRight, ArrowUpRight, BookOpen, Briefcase,
  ChevronRight, DollarSign, Download,
  FileSpreadsheet, FileText, Loader2, PieChartIcon,
  RefreshCw, Search, TrendingUp, Trophy, UserPlus, Users, X,
} from "lucide-react";
import {
  AccountsAPI,
  ReportsAdminAPI,
  type AccountEntity,
  type AccountsSummary,
  type BrokerTotals,
  type EntityUserRow,
  type EntityUsersResponse,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PageHeader } from "@/components/common/PageHeader";
import { useAdminAuthStore } from "@/stores/authStore";
import { cn } from "@/lib/utils";

const CHART_GREEN = "#10b981";
const CHART_RED = "#ef4444";
const CHART_BLUE = "#3b82f6";

type TabDef = { value: string; label: string; icon: React.ReactNode };

const SUPER_ADMIN_TABS: TabDef[] = [
  { value: "all_users", label: "All Users", icon: <Users className="size-3.5" /> },
  { value: "brokers", label: "Brokers", icon: <Briefcase className="size-3.5" /> },
  { value: "sub_brokers", label: "Sub-Brokers", icon: <UserPlus className="size-3.5" /> },
];
const ADMIN_TABS: TabDef[] = [
  { value: "all_users", label: "All Users", icon: <Users className="size-3.5" /> },
  { value: "brokers", label: "Brokers", icon: <Briefcase className="size-3.5" /> },
  { value: "sub_brokers", label: "Sub-Brokers", icon: <UserPlus className="size-3.5" /> },
];
const BROKER_TABS: TabDef[] = [
  { value: "all_users", label: "All Users", icon: <Users className="size-3.5" /> },
  { value: "sub_brokers", label: "Sub-Brokers", icon: <UserPlus className="size-3.5" /> },
];

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function downloadCSV(entities: AccountEntity[], grandTotal?: AccountEntity) {
  const rows = grandTotal ? [{ ...grandTotal, name: "GRAND TOTAL", role: "TOTAL" }, ...entities] : entities;
  if (!rows.length) return;
  const headers = [
    "Name", "Role", "Users", "Deposits", "Withdrawals", "Net Deposit",
    "Realized P&L", "Unrealized P&L", "Net P&L", "Brokerage",
    "Total Trades", "Profit Trades", "Loss Trades", "Win Rate %",
    "Volume", "Balance", "Equity", "Open Positions", "Settlement",
  ];
  const csvRows = [
    headers.join(","),
    ...rows.map((e) =>
      [
        `"${e.name || ""}"`, e.role, e.user_count, e.deposits, e.withdrawals,
        e.net_deposit, e.realized_pnl, e.unrealized_pnl, e.net_pnl,
        e.brokerage, e.total_trades, e.profit_trades, e.loss_trades,
        e.win_rate, e.volume, e.balance, e.equity, e.open_positions,
        e.settlement_outstanding,
      ].join(",")
    ),
  ];
  const blob = new Blob([csvRows.join("\n")], { type: "text/csv" });
  downloadBlob(blob, `accounts_${new Date().toISOString().slice(0, 10)}.csv`);
}

/* ═══════════════════════════════════════════════════════════════════ */
/* Main Page                                                          */
/* ═══════════════════════════════════════════════════════════════════ */

export default function AccountsDashboardPage() {
  const admin = useAdminAuthStore((s) => s.admin);
  const role = admin?.role;
  const tabs = role === "SUPER_ADMIN" ? SUPER_ADMIN_TABS
    : role === "BROKER" ? BROKER_TABS
    : ADMIN_TABS;

  const [scope, setScope] = useState("all_users");
  // Default range = current ISO week (Mon → Sun) so the page lands
  // with real filtered data instead of a lifetime aggregate.  Admin
  // can override either bound for ad-hoc periods.
  const _today = new Date();
  const _dow = _today.getDay(); // 0 = Sun
  const _monOffset = _dow === 0 ? -6 : 1 - _dow;
  const _monday = new Date(_today);
  _monday.setDate(_today.getDate() + _monOffset);
  const _sunday = new Date(_monday);
  _sunday.setDate(_monday.getDate() + 6);
  const _iso = (d: Date) => d.toISOString().slice(0, 10);
  const [fromDate, setFromDate] = useState(_iso(_monday));
  const [toDate, setToDate] = useState(_iso(_sunday));
  const [search, setSearch] = useState("");

  // Week-presets dropdown — auto-generated from today: current week,
  // last week, then 4 weekly slices covering the prior ~month so the
  // operator can pick "2 May → 9 May" style ranges in one click instead
  // of typing into the date pickers. Sunday is treated as the week-end
  // (Monday → Sunday) to match the default fromDate / toDate above.
  const weekPresets = useMemo(() => {
    const fmtIst = (d: Date) =>
      d.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
    const out: { key: string; label: string; from: string; to: string }[] = [];
    // Current week = the one already populated on first render
    const curMon = new Date(_monday);
    const curSun = new Date(_sunday);
    out.push({
      key: "current",
      label: `Current week (${fmtIst(curMon)} – ${fmtIst(curSun)})`,
      from: _iso(curMon),
      to: _iso(curSun),
    });
    // Last week
    const lastMon = new Date(curMon); lastMon.setDate(curMon.getDate() - 7);
    const lastSun = new Date(curSun); lastSun.setDate(curSun.getDate() - 7);
    out.push({
      key: "last",
      label: `Last week (${fmtIst(lastMon)} – ${fmtIst(lastSun)})`,
      from: _iso(lastMon),
      to: _iso(lastSun),
    });
    // 4 prior weeks covering the rest of the last ~month
    for (let i = 2; i <= 5; i++) {
      const m = new Date(curMon); m.setDate(curMon.getDate() - 7 * i);
      const s = new Date(curSun); s.setDate(curSun.getDate() - 7 * i);
      out.push({
        key: `w-${i}`,
        label: `${fmtIst(m)} – ${fmtIst(s)}`,
        from: _iso(m),
        to: _iso(s),
      });
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const activePresetKey = (() => {
    const hit = weekPresets.find((p) => p.from === fromDate && p.to === toDate);
    return hit ? hit.key : "";
  })();

  const dateParams = {
    from_date: fromDate || undefined,
    to_date: toDate || undefined,
  };

  const { data, isFetching, refetch } = useQuery<AccountsSummary>({
    queryKey: ["admin", "accounts", "summary", scope, fromDate, toDate],
    queryFn: () =>
      AccountsAPI.summary({
        scope,
        from_date: fromDate || undefined,
        to_date: toDate || undefined,
      }),
  });

  const handleClear = () => {
    setFromDate("");
    setToDate("");
  };

  const gt = data?.grand_total;
  const entities = (data?.entities ?? []).filter((e) => {
    if (!search.trim()) return true;
    const q = search.toLowerCase();
    return (
      (e.name || "").toLowerCase().includes(q) ||
      (e.user_code || "").toLowerCase().includes(q)
    );
  });

  const isBrokerScope = scope === "brokers" || scope === "sub_brokers";

  return (
    <div className="space-y-5">
      <PageHeader
        title="Accounts"
        description="Manage and analyze account data and performance"
        actions={
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => downloadCSV(entities, data?.grand_total)}
              disabled={!data}
            >
              <Download className="size-4 mr-1" /> CSV
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => refetch()}
              disabled={isFetching}
            >
              <RefreshCw className={`size-4 ${isFetching ? "animate-spin" : ""}`} />
            </Button>
          </div>
        }
      />

      {/* ── Search ────────────────────────────────────────── */}
      <div className="rounded-lg border border-border/60 bg-card/40 p-3">
        <label className="mb-1 block text-xs text-muted-foreground">Search</label>
        <Input
          placeholder="Search by user ID..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {/* ── Scope Tabs ────────────────────────────────────── */}
      <div className="flex gap-1.5">
        {tabs.map((t) => (
          <button
            key={t.value}
            onClick={() => setScope(t.value)}
            className={`inline-flex items-center gap-1.5 rounded-full px-4 py-2 text-xs font-semibold transition-colors ${
              scope === t.value
                ? "bg-primary text-primary-foreground shadow-sm"
                : "bg-card/60 text-muted-foreground hover:bg-card hover:text-foreground border border-border/60"
            }`}
          >
            {t.icon}
            {t.label}
          </button>
        ))}
      </div>

      {/* ── Date Filters: week-preset dropdown + manual From / To.
          The preset dropdown was re-introduced on operator request to
          skip the "type six digits into a date picker" workflow for
          common windows. Current week, last week, and 4 prior weekly
          slices (covers the rest of the last month) are auto-generated
          from today. Manual pickers still work for ad-hoc ranges and
          override the dropdown selection. All numbers on the dashboard
          — including Settlement and Actual P&L — recompute on the new
          window. */}
      <div className="flex flex-wrap items-end gap-3 rounded-lg border border-border/60 bg-card/40 p-4">
        <div>
          <label className="mb-1 block text-xs text-muted-foreground">Select Week</label>
          <select
            value={activePresetKey}
            onChange={(e) => {
              const p = weekPresets.find((x) => x.key === e.target.value);
              if (p) {
                setFromDate(p.from);
                setToDate(p.to);
              }
            }}
            className="h-10 rounded-md border border-border bg-background px-3 text-sm"
          >
            <option value="">Custom range…</option>
            {weekPresets.map((p) => (
              <option key={p.key} value={p.key}>{p.label}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted-foreground">From Date</label>
          <input
            type="date"
            value={fromDate}
            onChange={(e) => setFromDate(e.target.value)}
            className="h-10 rounded-md border border-border bg-background px-3 text-sm"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted-foreground">To Date</label>
          <input
            type="date"
            value={toDate}
            onChange={(e) => setToDate(e.target.value)}
            className="h-10 rounded-md border border-border bg-background px-3 text-sm"
          />
        </div>
        {(fromDate || toDate) && (
          <Button variant="ghost" size="sm" className="h-10" onClick={handleClear}>
            <X className="mr-1 size-3" /> Clear Filters
          </Button>
        )}
      </div>

      {/* ── Loading ────────────────────────────────────────── */}
      {isFetching && !data && (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="size-6 animate-spin text-primary" />
        </div>
      )}

      {/* ── Entity Cards / User Table ─────────────────────── */}
      {isBrokerScope ? (
        <BrokerEntities entities={entities} dateParams={dateParams} />
      ) : admin?.id ? (
        <AllUsersTable adminId={admin.id} dateParams={dateParams} />
      ) : null}

      {data && (
        <div className="text-xs text-muted-foreground">
          {data.filter.is_lifetime
            ? "Showing lifetime totals"
            : `Filtered: ${data.filter.from_date || ""} to ${data.filter.to_date || ""}`}
          {" · "}
          {entities.length} entities · {gt?.user_count ?? 0} total users
        </div>
      )}
    </div>
  );
}


/* ═══════════════════════════════════════════════════════════════════ */
/* Broker / Sub-Broker Entity Cards                                    */
/* ═══════════════════════════════════════════════════════════════════ */

const ENTITY_PAGE_SIZE = 10;

function BrokerEntities({
  entities,
  dateParams,
}: {
  entities: AccountEntity[];
  dateParams: { from_date?: string; to_date?: string };
}) {
  const [page, setPage] = useState(1);
  const totalPages = Math.max(1, Math.ceil(entities.length / ENTITY_PAGE_SIZE));
  const safeP = Math.min(page, totalPages);
  const sliced = entities.slice((safeP - 1) * ENTITY_PAGE_SIZE, safeP * ENTITY_PAGE_SIZE);

  const prevLen = useRef(entities.length);
  useEffect(() => {
    if (entities.length !== prevLen.current) { setPage(1); prevLen.current = entities.length; }
  }, [entities.length]);

  if (!entities.length) return (
    <div className="py-8 text-center text-sm text-muted-foreground">No entities found.</div>
  );

  return (
    <div className="space-y-3">
      {sliced.map((entity) => (
        <BrokerEntityCard key={entity.id} entity={entity} dateParams={dateParams} />
      ))}
      {totalPages > 1 && (
        <div className="flex items-center justify-between rounded-lg border border-border/60 bg-card/40 px-4 py-2.5">
          <span className="text-xs text-muted-foreground">
            Entity Page {safeP} ({entities.length} total entities)
          </span>
          <div className="flex gap-1.5">
            <Button variant="outline" size="sm" disabled={safeP <= 1} onClick={() => setPage(safeP - 1)}>
              <ChevronRight className="size-3 rotate-180" /> Previous
            </Button>
            <Button variant="outline" size="sm" disabled={safeP >= totalPages} onClick={() => setPage(safeP + 1)}>
              Next <ChevronRight className="size-3" />
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

function BrokerEntityCard({
  entity: e,
  dateParams,
}: {
  entity: AccountEntity;
  dateParams: { from_date?: string; to_date?: string };
}) {
  const [expanded, setExpanded] = useState(false);
  const [showUsers, setShowUsers] = useState(false);

  // "direct" is not a real ObjectId — skip broker-totals API for it
  const isRealEntity = e.id !== "direct" && e.id.length === 24;

  // Show full name as the primary label and user_code as a smaller
  // secondary line. Operator-flagged: previously only the user_code
  // was visible (BRK15911519) so admins couldn't tell which broker
  // each row belonged to without expanding.
  const displayName = (e.name || "").trim();
  const displayCode = (e.user_code || "").trim();
  // Direct Users virtual row has no real broker → don't echo the
  // placeholder string twice; show just the friendly label.
  const isVirtualDirectRow = e.id === "direct";

  return (
    <div className="rounded-lg border border-border/60 bg-card/40 overflow-hidden">
      {/* Header */}
      <div
        className="flex items-center justify-between px-4 py-3 cursor-pointer hover:bg-card/60 transition-colors"
        onClick={() => setExpanded((v) => !v)}
      >
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10">
            <Users className="size-5 text-primary" />
          </div>
          <div className="min-w-0">
            {isVirtualDirectRow ? (
              <>
                <div className="text-[10px] text-muted-foreground">
                  {expanded ? "Pool" : "Click to expand"}
                </div>
                <div className="text-base font-bold">{displayName || "Direct Users"}</div>
              </>
            ) : (
              <>
                <div className="text-base font-bold leading-tight">
                  {displayName || displayCode || "—"}
                </div>
                {displayCode && (
                  <div className="font-mono text-[11px] text-muted-foreground">
                    {displayCode}
                    {!expanded && (
                      <span className="ml-2 text-muted-foreground/60">
                        · Click to expand
                      </span>
                    )}
                  </div>
                )}
              </>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          {expanded && (
            <>
              <Button
                variant="outline"
                size="sm"
                onClick={(ev) => {
                  ev.stopPropagation();
                  setExpanded(false);
                }}
              >
                <X className="size-3 mr-1" /> Collapse
              </Button>
              {isRealEntity && (
                <>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={async (ev) => {
                      ev.stopPropagation();
                      try {
                        const blob = await AccountsAPI.exportBrokerTotalsExcel(e.id, dateParams);
                        downloadBlob(blob, `${e.user_code || e.name}_summary.xlsx`);
                      } catch {}
                    }}
                  >
                    <Download className="size-3 mr-1" /> Export Excel
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={async (ev) => {
                      ev.stopPropagation();
                      try {
                        const blob = await AccountsAPI.exportBrokerTotalsPdf(e.id, dateParams);
                        downloadBlob(blob, `${e.user_code || e.name}_summary.pdf`);
                      } catch {}
                    }}
                  >
                    <FileText className="size-3 mr-1" /> Export PDF
                  </Button>
                </>
              )}
            </>
          )}
          <ChevronRight className={`size-5 text-muted-foreground transition-transform ${expanded ? "rotate-90" : ""}`} />
        </div>
      </div>

      {expanded && (
        <div className="border-t border-border/60">
          {/* Broker Totals — auto-loads on expand for real entities */}
          {isRealEntity && (
            <BrokerTotalsCard entityId={e.id} dateParams={dateParams} />
          )}

          {/* User Table Section */}
          <div className="border-t border-border/60">
            {!showUsers ? (
              <div className="flex items-center justify-center py-6">
                <div className="text-center">
                  <div className="text-sm text-muted-foreground mb-2">
                    Click below to load users for {e.user_code || e.name}
                  </div>
                  <Button
                    className="bg-primary text-primary-foreground"
                    onClick={() => setShowUsers(true)}
                  >
                    <Users className="size-4 mr-2" /> Load Users
                  </Button>
                </div>
              </div>
            ) : (
              <UserPnlTable entityId={e.id} entityName={e.user_code || e.name} dateParams={dateParams} />
            )}
          </div>
        </div>
      )}
    </div>
  );
}


/* ═══════════════════════════════════════════════════════════════════ */
/* Broker Totals Card (lazy loaded)                                    */
/* ═══════════════════════════════════════════════════════════════════ */

function BrokerTotalsCard({
  entityId,
  dateParams,
}: {
  entityId: string;
  dateParams: { from_date?: string; to_date?: string };
}) {
  const { data, isLoading, refetch } = useQuery<BrokerTotals>({
    queryKey: ["admin", "accounts", "broker-totals", entityId, dateParams.from_date, dateParams.to_date],
    queryFn: () => AccountsAPI.brokerTotals(entityId, dateParams),
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-4">
        <Loader2 className="size-5 animate-spin text-primary" />
      </div>
    );
  }

  if (!data) return null;

  const fmt = (v: string) => {
    const n = parseFloat(v);
    const sign = n > 0 ? "+" : "";
    return `${sign}${Math.abs(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  };
  // Plain-magnitude formatter for the math line under the hero — drops
  // the "+" sign on positive operands so the formula doesn't read as
  // "− +17,64,325.11" (operator complained that the leading + visually
  // cancelled the minus). Negative operands stay marked with "−".
  const fmtAbs = (v: string) => {
    const n = parseFloat(v);
    const prefix = n < 0 ? "−" : "";
    return `${prefix}${Math.abs(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  };
  const color = (v: string) => {
    const n = parseFloat(v);
    return n > 0 ? "text-emerald-500" : n < 0 ? "text-destructive" : "text-foreground/80";
  };

  const actualPnl = parseFloat(data.actual_pnl);
  const actualPositive = actualPnl >= 0;
  return (
    <div className="relative overflow-hidden rounded-xl border border-border/60 bg-gradient-to-br from-card via-card/90 to-card/70 p-4 shadow-sm ring-1 ring-inset ring-white/5 sm:p-5">
      {/* subtle ambient accent */}
      <span
        aria-hidden
        className={cn(
          "pointer-events-none absolute -right-12 -top-12 h-40 w-40 rounded-full blur-3xl",
          actualPositive ? "bg-emerald-500/10" : "bg-rose-500/10",
        )}
      />

      {/* Re-fetch button — top-right corner of the card */}
      <button
        type="button"
        onClick={() => refetch()}
        disabled={isLoading}
        title="Refresh data"
        className="absolute right-4 top-4 grid size-7 place-items-center rounded-md border border-border/60 bg-card/60 text-muted-foreground hover:bg-muted/40 hover:text-foreground disabled:opacity-50 z-10"
      >
        <RefreshCw className={`size-3.5 ${isLoading ? "animate-spin" : ""}`} />
      </button>

      {/* ── Hero: Actual PNL ─────────────────────────────────── */}
      <div className="relative flex items-start justify-between gap-3">
        <div>
          <div className="text-[10px] font-mono font-semibold uppercase tracking-wider text-muted-foreground">
            Actual P&amp;L
          </div>
          <div
            className={cn(
              "mt-0.5 font-tabular text-2xl font-bold tabular-nums sm:text-3xl",
              color(data.actual_pnl),
            )}
          >
            {fmt(data.actual_pnl)}
          </div>
          <div className="mt-0.5 text-[10px] text-muted-foreground">
            = (−Net Client PNL) + Net Client BKG
          </div>
          <div className="mt-1 font-mono text-[10px] tabular-nums text-muted-foreground/80">
            {/* Use the broker-view PNL (= −net_client_pnl) so the math on this
                line actually equals the displayed Actual P&L. Falling back to
                net_client_pnl if an older backend doesn't yet emit the
                broker_view_pnl field. Settlement is NO LONGER subtracted here
                (operator request) — it's shown as its own red-on-loss tile. */}
            {fmtAbs((data as any).broker_view_pnl ?? data.net_client_pnl)} + {fmtAbs(data.net_client_bkg)} ={" "}
            <span className={cn("font-semibold", color(data.actual_pnl))}>{fmt(data.actual_pnl)}</span>
          </div>
        </div>
        <span
          className={cn(
            "shrink-0 rounded-full px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider ring-1 ring-inset",
            actualPositive
              ? "bg-emerald-500/10 text-emerald-500 ring-emerald-500/30"
              : "bg-rose-500/10 text-rose-500 ring-rose-500/30",
          )}
        >
          {actualPositive ? "▲ Profit" : "▼ Loss"}
        </span>
      </div>

      {/* ── Composition tiles ────────────────────────────────── */}
      <div className="relative mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
        <KpiTile label="Net Client PNL" value={fmt(data.net_client_pnl)} valueClass={color(data.net_client_pnl)} />
        <KpiTile label="Net Client BKG" value={fmt(data.net_client_bkg)} valueClass={color(data.net_client_bkg)} />
        {/* Broker take, INVERTED tint (operator): PROFIT (positive) → red,
            LOSS (negative) → green. Negate before `color` to flip it. Matches
            the Positions page Total-of-Both cards. */}
        <KpiTile label="Total of Both" value={fmt(data.total_of_both)} valueClass={color(String(-Number(data.total_of_both)))} hint="PNL + BKG" />
        {/* Settlement is an outflow the broker settles to clients — a LOSS to
            the broker. Operator request: show it in red when it's a loss
            (positive settlement = broker pays out). Inverted tint like
            "Total of Both": positive → red, negative (recovery) → green. */}
        <KpiTile
          label="Settlement"
          value={fmt(data.settlement)}
          valueClass={color(String(-Number(data.settlement)))}
          hint="Booked − Recovered in window (lifetime snapshot if no filter). Red = loss to broker."
        />
      </div>

      {/* ── Sharing + cash flow + user count ──────────────────
          Total Users is a structural metric (how many clients sit
          under this entity) so it lives alongside the other
          "pool overview" tiles. Grid bumped to 5 columns on sm+ so
          all five fit on a single row; mobile (grid-cols-2) wraps
          gracefully into 3 rows. */}
      <div className="relative mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
        <KpiTile
          label="Total Users"
          value={String(data.client_count ?? 0)}
          valueClass="text-primary"
          hint="Clients in pool"
        />
        <KpiTile label="Sharing PNL" value={fmt(data.sharing_pnl)} muted />
        <KpiTile label="Sharing BKG" value={fmt(data.sharing_bkg)} muted />
        <KpiTile
          label="Total Deposits"
          value={fmt(data.total_deposits)}
          valueClass="text-emerald-500"
        />
        <KpiTile
          label="Total Withdrawals"
          value={fmt(data.total_withdrawals)}
          valueClass="text-orange-400"
        />
      </div>
    </div>
  );
}

/** Compact KPI tile used inside BrokerTotalsCard. Two-row layout —
 *  label on top in 9-10 px caps, value below in tabular-nums. */
function KpiTile({
  label,
  value,
  valueClass,
  muted,
  hint,
}: {
  label: string;
  value: string;
  valueClass?: string;
  muted?: boolean;
  hint?: string;
}) {
  return (
    <div
      className={cn(
        "rounded-lg border border-border/40 bg-muted/20 px-2.5 py-2",
        muted && "bg-muted/10",
      )}
      title={hint || label}
    >
      <div className="text-[9px] font-mono font-semibold uppercase tracking-wider text-muted-foreground sm:text-[10px]">
        {label}
      </div>
      <div
        className={cn(
          "mt-0.5 font-tabular text-sm font-bold tabular-nums sm:text-[15px]",
          valueClass,
          muted && !valueClass && "text-foreground/70",
        )}
      >
        {value}
      </div>
    </div>
  );
}

function TotalsRow({
  label, value, fmt, color, info,
}: {
  label: string;
  value: string;
  fmt: (v: string) => string;
  color: (v: string) => string;
  info?: boolean;
}) {
  return (
    <div className="flex justify-between items-center px-3 py-1">
      <span className="text-xs font-mono font-semibold text-muted-foreground tracking-wide">
        {label} {info && <span className="text-muted-foreground/50 cursor-help" title="Net Client PNL (inverted) + Net Client BKG">ⓘ</span>}
      </span>
      <span className={`font-semibold tabular-nums ${color(value)}`}>
        {fmt(value)}
      </span>
    </div>
  );
}


/* ═══════════════════════════════════════════════════════════════════ */
/* User PNL Table (lazy loaded inside broker card)                     */
/* ═══════════════════════════════════════════════════════════════════ */

function UserPnlTable({
  entityId,
  entityName,
  dateParams,
}: {
  entityId: string;
  entityName: string;
  dateParams: { from_date?: string; to_date?: string };
}) {
  const [page, setPage] = useState(1);
  const [userSearch, setUserSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");

  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(userSearch), 400);
    return () => clearTimeout(t);
  }, [userSearch]);

  useEffect(() => { setPage(1); }, [debouncedSearch]);

  const { data, isLoading } = useQuery<EntityUsersResponse>({
    queryKey: ["admin", "accounts", "entity-users", entityId, dateParams.from_date, dateParams.to_date, page, debouncedSearch],
    queryFn: () => AccountsAPI.entityUsers(entityId, {
      ...dateParams,
      page,
      page_size: 10,
      search: debouncedSearch || undefined,
    }),
  });

  const handleDownloadExcel = async () => {
    try {
      const blob = await AccountsAPI.exportEntityUsersExcel(entityId, dateParams);
      downloadBlob(blob, `pnl_all_${entityName}.xlsx`);
    } catch {}
  };
  const handleDownloadPdf = async () => {
    try {
      const blob = await AccountsAPI.exportEntityUsersPdf(entityId, dateParams);
      downloadBlob(blob, `pnl_all_${entityName}.pdf`);
    } catch {}
  };

  const items = data?.items ?? [];
  const meta = data?.meta;

  return (
    <div className="p-4 space-y-3">
      {/* Download all + search */}
      <div className="flex items-center justify-between gap-3">
        <div className="relative flex-1 max-w-xs">
          <Search className="absolute left-2.5 top-2.5 size-4 text-muted-foreground" />
          <Input
            placeholder="Search users..."
            value={userSearch}
            onChange={(e) => setUserSearch(e.target.value)}
            className="pl-9"
          />
        </div>
        <div className="flex gap-1.5">
          <Button variant="outline" size="sm" onClick={handleDownloadExcel}>
            <Download className="size-3 mr-1" /> Download all PnL (Excel)
          </Button>
          <Button variant="outline" size="sm" onClick={handleDownloadPdf}>
            <FileText className="size-3 mr-1" /> Download PDF
          </Button>
        </div>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-6">
          <Loader2 className="size-5 animate-spin text-primary" />
        </div>
      ) : items.length === 0 ? (
        <div className="py-6 text-center text-sm text-muted-foreground">No users found.</div>
      ) : (
        <>
          {/* Desktop table — md and up. Mobile uses a card list below
              so each row's full data is visible without horizontal
              scroll. "PNL − Settlement" column dropped on operator
              request; per-row export buttons unchanged. */}
          <div className="hidden overflow-x-auto rounded-lg border border-border/60 md:block">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border/60 bg-card/60">
                  <th className="px-3 py-2.5 text-left text-xs font-semibold text-muted-foreground">User ID</th>
                  <th className="px-3 py-2.5 text-left text-xs font-semibold text-muted-foreground">Username</th>
                  <th className="px-3 py-2.5 text-left text-xs font-semibold text-muted-foreground">Owner</th>
                  <th className="px-3 py-2.5 text-right text-xs font-semibold text-muted-foreground">Total PNL</th>
                  <th className="px-3 py-2.5 text-right text-xs font-semibold text-muted-foreground">Net PNL <InfoTip text="Sum of realized P&L from closed positions" /></th>
                  <th className="px-3 py-2.5 text-right text-xs font-semibold text-muted-foreground">Net BKG <InfoTip text="Total brokerage charged" /></th>
                  <th className="px-3 py-2.5 text-right text-xs font-semibold text-muted-foreground">Settlement <InfoTip text="Net settlement booked − recovered in the selected period. Without a date filter, falls back to the user's current outstanding wallet balance (lifetime)." /></th>
                  <th className="px-3 py-2.5 text-right text-xs font-semibold text-muted-foreground">Total − Settle <InfoTip text="Total PNL minus Settlement — the row's real take after settlement is netted off" /></th>
                  <th className="px-3 py-2.5 text-center text-xs font-semibold text-muted-foreground">Actions</th>
                </tr>
              </thead>
              <tbody>
                {items.map((u) => (
                  <UserPnlRow key={u.user_id} user={u} entityId={entityId} dateParams={dateParams} />
                ))}
              </tbody>
            </table>
          </div>

          {/* Mobile card list — under md. Each row stacks the
              identity + 4 key numbers in a 2-column grid + action
              buttons below. Avoids horizontal scroll on phones. */}
          <ul className="space-y-2 md:hidden">
            {items.map((u) => (
              <UserPnlCard key={u.user_id} user={u} entityId={entityId} dateParams={dateParams} />
            ))}
          </ul>

          {/* Pagination — numbered with smart truncation. Mobile shows
              just Prev / Page X of Y / Next; desktop shows the full
              numbered strip with the active page highlighted. */}
          {meta && meta.total_pages > 1 && (
            <PaginationBar
              page={meta.page}
              totalPages={meta.total_pages}
              total={meta.total}
              entityName={entityName}
              onJump={setPage}
            />
          )}
        </>
      )}
    </div>
  );
}

/**
 * Numbered pagination strip for the per-user PNL table.
 *
 *   • Mobile (< sm): "‹ Prev · Page X of Y · Next ›" — single row, fits.
 *   • Desktop: « First · ‹ Prev · 1 … 4 5 6 … 11 · Next › · Last »
 *     Current page highlighted with the primary background.
 *
 * `pages()` truncates to at most 1 + (left ellipsis) + 3 + (right
 * ellipsis) + 1 = 7 visible buttons even when totalPages is huge, so
 * the strip never wraps onto two lines.
 */
function PaginationBar({
  page,
  totalPages,
  total,
  entityName,
  onJump,
}: {
  page: number;
  totalPages: number;
  total: number;
  entityName: string;
  onJump: (p: number) => void;
}) {
  function pages(): (number | "…")[] {
    const set = new Set<number>([1, totalPages, page, page - 1, page + 1]);
    const sorted = [...set].filter((n) => n >= 1 && n <= totalPages).sort((a, b) => a - b);
    const out: (number | "…")[] = [];
    for (let i = 0; i < sorted.length; i++) {
      out.push(sorted[i]);
      if (i < sorted.length - 1 && sorted[i + 1] - sorted[i] > 1) out.push("…");
    }
    return out;
  }
  const canPrev = page > 1;
  const canNext = page < totalPages;

  return (
    <div className="flex flex-col items-center gap-2 border-t border-border/40 pt-3 sm:flex-row sm:justify-between">
      <div className="text-xs text-muted-foreground">
        Showing page <span className="font-semibold text-foreground">{page}</span> of{" "}
        <span className="font-semibold text-foreground">{totalPages}</span>
        <span className="hidden sm:inline"> · {total.toLocaleString("en-IN")} total clients for {entityName}</span>
      </div>

      {/* Mobile: just prev / page / next */}
      <div className="flex items-center gap-1.5 sm:hidden">
        <Button variant="outline" size="sm" className="h-8 w-20" disabled={!canPrev} onClick={() => onJump(page - 1)}>
          <ChevronRight className="size-3 rotate-180" /> Prev
        </Button>
        <Button variant="outline" size="sm" className="h-8 w-20" disabled={!canNext} onClick={() => onJump(page + 1)}>
          Next <ChevronRight className="size-3" />
        </Button>
      </div>

      {/* Desktop: numbered strip */}
      <div className="hidden items-center gap-1 sm:flex">
        <Button
          variant="outline"
          size="sm"
          className="h-8 px-2"
          disabled={!canPrev}
          onClick={() => onJump(1)}
          title="First page"
        >
          ««
        </Button>
        <Button
          variant="outline"
          size="sm"
          className="h-8 px-2.5"
          disabled={!canPrev}
          onClick={() => onJump(page - 1)}
        >
          <ChevronRight className="size-3.5 rotate-180" />
        </Button>
        {pages().map((p, i) =>
          p === "…" ? (
            <span key={`e-${i}`} className="px-1 text-xs text-muted-foreground select-none">
              …
            </span>
          ) : (
            <button
              key={p}
              type="button"
              onClick={() => onJump(p)}
              aria-current={p === page ? "page" : undefined}
              className={cn(
                "h-8 min-w-8 rounded-md border px-2.5 text-xs font-medium tabular-nums transition-colors",
                p === page
                  ? "border-primary bg-primary text-primary-foreground shadow-sm"
                  : "border-border bg-card text-muted-foreground hover:border-primary/40 hover:text-foreground",
              )}
            >
              {p}
            </button>
          ),
        )}
        <Button
          variant="outline"
          size="sm"
          className="h-8 px-2.5"
          disabled={!canNext}
          onClick={() => onJump(page + 1)}
        >
          <ChevronRight className="size-3.5" />
        </Button>
        <Button
          variant="outline"
          size="sm"
          className="h-8 px-2"
          disabled={!canNext}
          onClick={() => onJump(totalPages)}
          title="Last page"
        >
          »»
        </Button>
      </div>
    </div>
  );
}


function UserPnlRow({
  user: u,
  entityId,
  dateParams,
}: {
  user: EntityUserRow;
  entityId: string;
  dateParams: { from_date?: string; to_date?: string };
}) {
  const pnlColor = (v: string) => {
    const n = parseFloat(v);
    return n > 0 ? "text-emerald-400" : n < 0 ? "text-destructive" : "";
  };
  const fmtMoney = (v: string) => {
    const n = parseFloat(v);
    return Math.abs(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  };

  return (
    <tr className="border-b border-border/30 hover:bg-card/60 transition-colors">
      <td className="px-3 py-2.5 font-medium">{u.user_code}</td>
      <td className="px-3 py-2.5">{u.username}</td>
      <td className="px-3 py-2.5">
        {u.owner_kind && u.owner_kind !== "Direct" ? (
          <span className="rounded px-1.5 py-0.5 text-[10px] font-medium ring-1 ring-inset ring-border bg-muted/40 whitespace-nowrap">
            {u.owner_kind}: {u.owner_name}
          </span>
        ) : (
          <span className="text-xs text-muted-foreground">Direct</span>
        )}
      </td>
      <td className={`px-3 py-2.5 text-right tabular-nums font-semibold ${pnlColor(u.total_pnl)}`}>{fmtMoney(u.total_pnl)}</td>
      <td className={`px-3 py-2.5 text-right tabular-nums ${pnlColor(u.net_pnl)}`}>{fmtMoney(u.net_pnl)}</td>
      <td className="px-3 py-2.5 text-right tabular-nums">{fmtMoney(u.net_bkg)}</td>
      <td className="px-3 py-2.5 text-right tabular-nums">{fmtMoney(u.settlement)}</td>
      <td className={`px-3 py-2.5 text-right tabular-nums font-semibold ${pnlColor(u.pnl_minus_settlement ?? "0")}`}>
        {fmtMoney(u.pnl_minus_settlement ?? "0")}
      </td>
      <td className="px-3 py-2.5">
        <div className="flex items-center justify-center gap-1">
          <Button
            variant="outline"
            size="sm"
            className="h-7 text-[10px] px-2"
            disabled
            title="Settlement"
          >
            <FileText className="size-3 mr-0.5" /> Settle
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="h-7 text-[10px] px-2"
            onClick={async () => {
              const blob = await AccountsAPI.exportEntityUsersExcel(entityId, dateParams);
              downloadBlob(blob, `pnl_${u.user_code}.xlsx`);
            }}
          >
            <FileSpreadsheet className="size-3 mr-0.5" /> Excel
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="h-7 text-[10px] px-2"
            onClick={async () => {
              const blob = await AccountsAPI.exportEntityUsersPdf(entityId, dateParams);
              downloadBlob(blob, `pnl_${u.user_code}.pdf`);
            }}
          >
            <FileText className="size-3 mr-0.5" /> PDF
          </Button>
          {/* Full FIFO trade book — trade-by-trade incl. WEEKLY SETTLEMENT rows
              (operator: weekly settlement should show in the trade book too). */}
          <Button
            variant="outline"
            size="sm"
            className="h-7 text-[10px] px-2"
            title="Full trade book (incl. weekly settlement)"
            onClick={async () => {
              const blob = await ReportsAdminAPI.tradebookPdf(
                u.user_id,
                dateParams.from_date,
                dateParams.to_date,
              );
              downloadBlob(blob, `tradebook_${u.user_code}.pdf`);
            }}
          >
            <BookOpen className="size-3 mr-0.5" /> Trade Book
          </Button>
        </div>
      </td>
    </tr>
  );
}

/** Mobile card view — replaces the wide table at <md so phones don't
 *  need horizontal scroll. Identity row on top, 2x2 grid of the four
 *  money figures below, action buttons at the bottom. */
function UserPnlCard({
  user: u,
  entityId,
  dateParams,
}: {
  user: EntityUserRow;
  entityId: string;
  dateParams: { from_date?: string; to_date?: string };
}) {
  const pnlColor = (v: string) => {
    const n = parseFloat(v);
    return n > 0 ? "text-emerald-400" : n < 0 ? "text-destructive" : "";
  };
  const fmtMoney = (v: string) => {
    const n = parseFloat(v);
    return Math.abs(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  };
  return (
    <li className="rounded-lg border border-border/60 bg-card/40 p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold">{u.username}</div>
          <div className="mt-0.5 font-mono text-[10px] text-muted-foreground">{u.user_code}</div>
          {u.owner_kind && u.owner_kind !== "Direct" ? (
            <div className="mt-1 inline-block rounded px-1.5 py-0.5 text-[9px] font-medium ring-1 ring-inset ring-border bg-muted/40">
              {u.owner_kind}: {u.owner_name}
            </div>
          ) : null}
        </div>
        <div className={`shrink-0 text-right text-sm font-semibold tabular-nums ${pnlColor(u.total_pnl)}`}>
          ₹{fmtMoney(u.total_pnl)}
          <div className="text-[9px] font-normal uppercase tracking-wider text-muted-foreground">Total PNL</div>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2 rounded-md bg-muted/20 p-2 text-[11px]">
        <div>
          <div className="text-[9px] uppercase tracking-wider text-muted-foreground">Net PNL</div>
          <div className={`mt-0.5 font-semibold tabular-nums ${pnlColor(u.net_pnl)}`}>₹{fmtMoney(u.net_pnl)}</div>
        </div>
        <div>
          <div className="text-[9px] uppercase tracking-wider text-muted-foreground">Net BKG</div>
          <div className="mt-0.5 font-semibold tabular-nums">₹{fmtMoney(u.net_bkg)}</div>
        </div>
        <div>
          <div className="text-[9px] uppercase tracking-wider text-muted-foreground">Settlement</div>
          <div className="mt-0.5 font-semibold tabular-nums">₹{fmtMoney(u.settlement)}</div>
        </div>
        <div>
          <div className="text-[9px] uppercase tracking-wider text-muted-foreground">Total − Settle</div>
          <div className={`mt-0.5 font-semibold tabular-nums ${pnlColor(u.pnl_minus_settlement ?? "0")}`}>
            ₹{fmtMoney(u.pnl_minus_settlement ?? "0")}
          </div>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-1.5">
        <Button variant="outline" size="sm" className="h-8 text-[10px]" disabled>
          <FileText className="size-3 mr-1" /> Settle
        </Button>
        <Button
          variant="outline"
          size="sm"
          className="h-8 text-[10px]"
          onClick={async () => {
            const blob = await AccountsAPI.exportEntityUsersExcel(entityId, dateParams);
            downloadBlob(blob, `pnl_${u.user_code}.xlsx`);
          }}
        >
          <FileSpreadsheet className="size-3 mr-1" /> Excel
        </Button>
        <Button
          variant="outline"
          size="sm"
          className="h-8 text-[10px]"
          onClick={async () => {
            const blob = await AccountsAPI.exportEntityUsersPdf(entityId, dateParams);
            downloadBlob(blob, `pnl_${u.user_code}.pdf`);
          }}
        >
          <FileText className="size-3 mr-1" /> PDF
        </Button>
        {/* Full FIFO trade book — incl. weekly settlement rows. */}
        <Button
          variant="outline"
          size="sm"
          className="h-8 text-[10px]"
          onClick={async () => {
            const blob = await ReportsAdminAPI.tradebookPdf(
              u.user_id,
              dateParams.from_date,
              dateParams.to_date,
            );
            downloadBlob(blob, `tradebook_${u.user_code}.pdf`);
          }}
        >
          <BookOpen className="size-3 mr-1" /> Trade Book
        </Button>
      </div>
    </li>
  );
}


/* ═══════════════════════════════════════════════════════════════════ */
/* All Users — clean PNL table (same layout as broker user table)      */
/* ═══════════════════════════════════════════════════════════════════ */

function AllUsersTable({
  adminId,
  dateParams,
}: {
  adminId: string;
  dateParams: { from_date?: string; to_date?: string };
}) {
  return (
    <div className="space-y-4">
      {/* Pool totals header — same card the Broker / Sub-Broker tabs
          show; rolled up across every CLIENT in the admin's pool and
          gated by the same date filter so the numbers track the
          Fetch Data button. */}
      <BrokerTotalsCard entityId={adminId} dateParams={dateParams} />
      <UserPnlTable entityId={adminId} entityName="All Users" dateParams={dateParams} />
    </div>
  );
}


/* ═══════════════════════════════════════════════════════════════════ */
/* Shared sub-components                                               */
/* ═══════════════════════════════════════════════════════════════════ */

function InfoTip({ text }: { text: string }) {
  return (
    <span className="text-muted-foreground/50 cursor-help ml-0.5" title={text}>ⓘ</span>
  );
}

function SummaryTile({
  icon, label, value, color, prefix, suffix, showSign, decimals = 2,
}: {
  icon: React.ReactNode;
  label: string;
  value: number;
  color: string;
  prefix?: string;
  suffix?: string;
  showSign?: boolean;
  decimals?: number;
}) {
  const display = useAnimatedNumber(value, decimals);
  const sign = showSign && value > 0 ? "+" : showSign && value < 0 ? "" : "";
  return (
    <div className="rounded-lg border border-border/60 bg-card/40 p-3 transition-all hover:border-border">
      <div className="flex items-center gap-1.5 text-[9px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
        {icon}
        {label}
      </div>
      <div className={`mt-1.5 text-lg font-bold tabular-nums ${color}`}>
        {sign}{prefix}{display}{suffix}
      </div>
    </div>
  );
}

function ChartCard({ title, icon, children }: {
  title: string;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="relative overflow-hidden rounded-lg border border-border/60 bg-card/40 p-3">
      <div className="mb-2 flex items-center gap-1.5 text-[10px] font-semibold text-muted-foreground">
        {icon}
        {title}
      </div>
      {children}
    </div>
  );
}

function useAnimatedNumber(target: number, decimals: number = 2): string {
  const [display, setDisplay] = useState(0);
  const frameRef = useRef<number>(0);

  useEffect(() => {
    const duration = 1200;
    const start = display;
    const diff = target - start;
    const startTime = performance.now();

    function animate(now: number) {
      const elapsed = now - startTime;
      const progress = Math.min(elapsed / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 3);
      setDisplay(start + diff * eased);
      if (progress < 1) {
        frameRef.current = requestAnimationFrame(animate);
      }
    }

    frameRef.current = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(frameRef.current);
  }, [target]);

  return Math.abs(display).toLocaleString("en-IN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}
