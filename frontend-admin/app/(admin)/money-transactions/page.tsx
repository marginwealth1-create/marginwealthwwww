"use client";

import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Download, RefreshCw } from "lucide-react";
import { MoneyAPI } from "@/lib/api";
import { useAdminAuthStore } from "@/stores/authStore";
import { canSee } from "@/lib/permissions";
import { PageHeader } from "@/components/common/PageHeader";
import { DataTable, type Column } from "@/components/common/DataTable";
import { Button } from "@/components/ui/button";
import { cn, formatINR } from "@/lib/utils";
import {
  DEFAULT_PERIOD,
  KpiTile,
  MoneyFilterBar,
  MoneyTabs,
  downloadCSV,
  moneyCell,
  periodKey,
  periodToParams,
  type Period,
} from "@/components/admin/money/MoneyShared";

const OWNER_TONE: Record<string, string> = {
  Broker: "bg-violet-500/10 text-violet-400 ring-violet-500/30",
  "Sub-broker": "bg-brand-500/10 text-brand-400 ring-brand-500/30",
  Admin: "bg-brand-500/10 text-brand-400 ring-brand-500/30",
  Direct: "bg-muted/50 text-muted-foreground ring-border",
};

export default function MoneyTransactionsPage() {
  const router = useRouter();
  const admin = useAdminAuthStore((s) => s.admin);
  const [search, setSearch] = useState("");
  const [period, setPeriod] = useState<Period>(DEFAULT_PERIOD);

  const { data, isFetching, refetch } = useQuery({
    queryKey: ["admin", "money", "users", periodKey(period)],
    queryFn: () => MoneyAPI.users(periodToParams(period)),
    enabled: canSee(admin, "ledger"),
  });

  const rows = useMemo(() => {
    const all = data?.users ?? [];
    const q = search.trim().toLowerCase();
    if (!q) return all;
    return all.filter(
      (u: any) =>
        (u.full_name || "").toLowerCase().includes(q) ||
        (u.user_code || "").toLowerCase().includes(q) ||
        (u.owner_name || "").toLowerCase().includes(q),
    );
  }, [data, search]);

  // Tiles reflect the VISIBLE rows so they always equal the table column sums.
  const totals = useMemo(() => {
    const t = { deposit: 0, add_fund: 0, withdraw: 0, deduct: 0, total_in: 0, total_out: 0, net: 0, settled: 0 };
    for (const r of rows) {
      t.deposit += r.deposit;
      t.add_fund += r.add_fund;
      t.withdraw += r.withdraw;
      t.deduct += r.deduct;
      t.settled += r.settled;
      t.total_in += r.total_in;
      t.total_out += r.total_out;
    }
    t.net = t.total_in - t.total_out;
    return t;
  }, [rows]);

  const cols: Column<any>[] = [
    {
      key: "user",
      header: "User",
      render: (r) => (
        <div className="flex flex-col gap-0.5 leading-tight">
          <span className="font-medium">{r.full_name}</span>
          <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <span className="font-mono">{r.user_code}</span>
            <span
              className={cn(
                "rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide ring-1 ring-inset",
                OWNER_TONE[r.owner_kind] ?? OWNER_TONE.Direct,
              )}
            >
              {r.owner_kind === "Direct" ? "Direct" : `${r.owner_kind}: ${r.owner_name}`}
            </span>
          </span>
        </div>
      ),
    },
    { key: "deposit", header: "Deposit", align: "right", render: (r) => moneyCell(r.deposit, "in") },
    { key: "add_fund", header: "Add fund", align: "right", render: (r) => moneyCell(r.add_fund, "in") },
    { key: "withdraw", header: "Withdraw", align: "right", render: (r) => moneyCell(r.withdraw, "out") },
    { key: "deduct", header: "Deduct", align: "right", render: (r) => moneyCell(r.deduct, "out") },
    { key: "total_in", header: "Total In", align: "right", render: (r) => moneyCell(r.total_in, "in") },
    { key: "total_out", header: "Total Out", align: "right", render: (r) => moneyCell(r.total_out, "out") },
    { key: "net", header: "Net", align: "right", render: (r) => moneyCell(r.net, "net") },
    { key: "settled", header: "Settled", align: "right", render: (r) => moneyCell(r.settled, "settled") },
  ];

  function exportCsv() {
    const headers = ["User", "Code", "Owner", "Deposit", "Add fund", "Withdraw", "Deduct", "Total In", "Total Out", "Net", "Settled"];
    const grand = ["GRAND TOTAL", "", "", totals.deposit, totals.add_fund, totals.withdraw, totals.deduct, totals.total_in, totals.total_out, totals.net, totals.settled];
    const body = rows.map((r: any) => [
      r.full_name, r.user_code, r.owner_kind === "Direct" ? "Direct" : `${r.owner_kind}: ${r.owner_name}`,
      r.deposit, r.add_fund, r.withdraw, r.deduct, r.total_in, r.total_out, r.net, r.settled,
    ]);
    downloadCSV(`money-transactions_${new Date().toISOString().slice(0, 10)}.csv`, headers, [grand, ...body]);
  }

  if (!canSee(admin, "ledger")) {
    return <div className="rounded-xl border border-border bg-card p-6 text-sm text-muted-foreground">You don&apos;t have access to this section.</div>;
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Money Transactions"
        description={`Per-user money movement · ${data?.filter?.label ?? "…"}`}
        actions={
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={exportCsv} disabled={!rows.length}>
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
        searchPlaceholder="Search name / code / broker / admin…"
        period={period}
        onPeriod={setPeriod}
      />

      {/* KPI tiles — roomy 4-per-row grid */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <KpiTile label="Deposit" value={totals.deposit} tone="in" />
        <KpiTile label="Add fund" value={totals.add_fund} tone="in" />
        <KpiTile label="Withdraw" value={totals.withdraw} tone="out" />
        <KpiTile label="Deduct" value={totals.deduct} tone="out" />
        <KpiTile label="Total In" value={totals.total_in} tone="in" />
        <KpiTile label="Total Out" value={totals.total_out} tone="out" />
        <KpiTile label="Net" value={totals.net} tone="net" />
        <KpiTile label="Settled" value={totals.settled} tone="settled" />
      </div>

      <DataTable
        columns={cols}
        rows={rows}
        keyExtractor={(r: any) => r.user_id}
        loading={isFetching && !data}
        empty="No money moved in this period."
        onRowClick={(r: any) => router.push(`/ledger?user_id=${r.user_id}`)}
        rowClassName={() => "cursor-pointer"}
      />
      <p className="text-[11px] text-muted-foreground">
        Showing {rows.length} user{rows.length === 1 ? "" : "s"} who moved money this period · tap a row to open their full ledger ·{" "}
        net total {formatINR(totals.net)}
      </p>
    </div>
  );
}
