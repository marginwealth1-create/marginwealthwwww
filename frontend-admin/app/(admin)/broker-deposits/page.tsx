"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, Download, RefreshCw, Users } from "lucide-react";
import { MoneyAPI } from "@/lib/api";
import { useAdminAuthStore } from "@/stores/authStore";
import { canSee } from "@/lib/permissions";
import { PageHeader } from "@/components/common/PageHeader";
import { Button } from "@/components/ui/button";
import { cn, formatINR } from "@/lib/utils";
import {
  DEFAULT_PERIOD,
  KpiTile,
  MoneyFilterBar,
  MoneyTabs,
  downloadCSV,
  periodKey,
  periodToParams,
  type Period,
} from "@/components/admin/money/MoneyShared";

type Broker = {
  broker_id: string;
  user_code: string;
  full_name: string;
  is_sub: boolean;
  parent_broker_id: string | null;
  deposit: number;
  deposit_direct: number;
  users_count: number;
};

export default function BrokerDepositsPage() {
  const admin = useAdminAuthStore((s) => s.admin);
  const [search, setSearch] = useState("");
  const [period, setPeriod] = useState<Period>(DEFAULT_PERIOD);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const { data, isFetching, refetch } = useQuery({
    queryKey: ["admin", "money", "brokers", periodKey(period)],
    queryFn: () => MoneyAPI.brokers(periodToParams(period)),
    enabled: canSee(admin, "ledger"),
  });

  const brokers: Broker[] = data?.brokers ?? [];
  const totals = data?.totals ?? { total_deposit: 0, admin_add_fund: 0, brokers: 0, users_under_brokers: 0 };

  // Build the parent→children map; a broker whose parent is out of scope
  // becomes a root so the tree stays connected.
  const { childrenOf, roots } = useMemo(() => {
    const ids = new Set(brokers.map((b) => b.broker_id));
    const kids = new Map<string, Broker[]>();
    const rootList: Broker[] = [];
    for (const b of brokers) {
      const pid = b.parent_broker_id && ids.has(b.parent_broker_id) ? b.parent_broker_id : null;
      if (pid) {
        if (!kids.has(pid)) kids.set(pid, []);
        kids.get(pid)!.push(b);
      } else {
        rootList.push(b);
      }
    }
    return { childrenOf: kids, roots: rootList };
  }, [brokers]);

  const q = search.trim().toLowerCase();
  const matches = q
    ? brokers.filter(
        (b) => b.full_name.toLowerCase().includes(q) || b.user_code.toLowerCase().includes(q),
      )
    : null;

  function toggle(id: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function exportCsv() {
    const headers = ["Broker", "Code", "Type", "Users", "Deposit (subtree)", "Deposit (direct)"];
    const grand = ["TOTAL DEPOSIT", "", "", totals.users_under_brokers, totals.total_deposit, ""];
    const body = brokers.map((b) => [b.full_name, b.user_code, b.is_sub ? "Sub-broker" : "Broker", b.users_count, b.deposit, b.deposit_direct]);
    downloadCSV(`broker-deposits_${new Date().toISOString().slice(0, 10)}.csv`, headers, [grand, ...body]);
  }

  if (!canSee(admin, "ledger")) {
    return <div className="rounded-xl border border-border bg-card p-6 text-sm text-muted-foreground">You don&apos;t have access to this section.</div>;
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Broker Deposits"
        description={`Per-broker deposit roll-up · ${data?.filter?.label ?? "…"}`}
        actions={
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={exportCsv} disabled={!brokers.length}>
              <Download className="size-4" /> CSV
            </Button>
            <Button variant="outline" size="sm" onClick={() => refetch()} disabled={isFetching}>
              <RefreshCw className={cn("size-4", isFetching && "animate-spin")} />
            </Button>
          </div>
        }
      />

      <MoneyTabs />

      <MoneyFilterBar
        search={search}
        onSearch={setSearch}
        searchPlaceholder="Search broker name / code…"
        period={period}
        onPeriod={setPeriod}
      />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <KpiTile label="Total Deposit" value={totals.total_deposit} tone="in" />
        <KpiTile label="Admin Add Fund" value={totals.admin_add_fund} tone="muted" />
        <KpiTile label="Brokers" value={totals.brokers} tone="muted" money={false} />
        <KpiTile label="Users under brokers" value={totals.users_under_brokers} tone="muted" money={false} />
      </div>

      {isFetching && !data ? (
        <div className="rounded-xl border border-border bg-card p-6 text-center text-sm text-muted-foreground">Loading…</div>
      ) : brokers.length === 0 ? (
        <div className="rounded-xl border border-border bg-card p-6 text-center text-sm text-muted-foreground">
          No broker deposits in this period.
        </div>
      ) : matches ? (
        // Search → flat list of matching brokers
        <div className="space-y-2">
          {matches.length === 0 ? (
            <div className="rounded-xl border border-border bg-card p-6 text-center text-sm text-muted-foreground">No brokers match.</div>
          ) : (
            matches.map((b) => <BrokerCard key={b.broker_id} broker={b} depth={0} hasChildren={false} expanded={false} onToggle={() => {}} />)
          )}
        </div>
      ) : (
        // Tree
        <div className="space-y-2">
          {roots.map((b) => (
            <BrokerTreeNode
              key={b.broker_id}
              broker={b}
              depth={0}
              childrenOf={childrenOf}
              expanded={expanded}
              onToggle={toggle}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function BrokerTreeNode({
  broker,
  depth,
  childrenOf,
  expanded,
  onToggle,
}: {
  broker: Broker;
  depth: number;
  childrenOf: Map<string, Broker[]>;
  expanded: Set<string>;
  onToggle: (id: string) => void;
}) {
  const kids = childrenOf.get(broker.broker_id) ?? [];
  const isOpen = expanded.has(broker.broker_id);
  return (
    <div>
      <BrokerCard
        broker={broker}
        depth={depth}
        hasChildren={kids.length > 0}
        expanded={isOpen}
        onToggle={() => onToggle(broker.broker_id)}
      />
      {isOpen && kids.length > 0 && (
        <div className="mt-2 space-y-2">
          {kids.map((k) => (
            <BrokerTreeNode
              key={k.broker_id}
              broker={k}
              depth={depth + 1}
              childrenOf={childrenOf}
              expanded={expanded}
              onToggle={onToggle}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function BrokerCard({
  broker,
  depth,
  hasChildren,
  expanded,
  onToggle,
}: {
  broker: Broker;
  depth: number;
  hasChildren: boolean;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <div
      className="flex items-center gap-3 rounded-xl border border-border bg-card p-3"
      style={{ marginLeft: depth * 20 }}
    >
      <button
        type="button"
        onClick={onToggle}
        disabled={!hasChildren}
        className={cn(
          "grid size-6 shrink-0 place-items-center rounded-md transition-colors",
          hasChildren ? "hover:bg-accent" : "opacity-0",
        )}
        aria-label={expanded ? "Collapse" : "Expand"}
      >
        <ChevronRight className={cn("size-4 transition-transform", expanded && "rotate-90")} />
      </button>
      <div className="grid size-9 shrink-0 place-items-center rounded-full bg-primary/10 text-sm font-bold text-primary">
        {(broker.full_name || broker.user_code || "?").charAt(0).toUpperCase()}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate font-medium">{broker.full_name}</span>
          <span
            className={cn(
              "shrink-0 rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide ring-1 ring-inset",
              broker.is_sub ? "bg-brand-500/10 text-brand-400 ring-brand-500/30" : "bg-violet-500/10 text-violet-400 ring-violet-500/30",
            )}
          >
            {broker.is_sub ? "Sub-broker" : "Broker"}
          </span>
        </div>
        <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
          <span className="font-mono">{broker.user_code}</span>
          <span className="inline-flex items-center gap-0.5">
            <Users className="size-3" /> {broker.users_count}
          </span>
        </div>
      </div>
      <div className="shrink-0 text-right">
        <div className="text-lg font-bold tabular-nums text-emerald-500">{formatINR(broker.deposit)}</div>
        {broker.deposit_direct > 0 && broker.deposit_direct !== broker.deposit && (
          <div className="text-[10px] text-muted-foreground">direct {formatINR(broker.deposit_direct)}</div>
        )}
      </div>
    </div>
  );
}
