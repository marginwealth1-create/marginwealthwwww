"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { RefreshCw, CheckCircle2, BadgeDollarSign } from "lucide-react";

import { ManagementAPI } from "@/lib/api";
import { useAdminAuthStore } from "@/stores/authStore";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { PageHeader } from "@/components/common/PageHeader";
import { DataTable, type Column } from "@/components/common/DataTable";

// Returns the ISO date (YYYY-MM-DD) of the IST Monday for a given date.
function istMonday(d: Date): string {
  // Convert to IST first, then snap to Monday.
  const utcMs = d.getTime() + d.getTimezoneOffset() * 60_000;
  const ist = new Date(utcMs + 5.5 * 60 * 60_000);
  const day = ist.getUTCDay(); // 0=Sun..6=Sat
  const offset = day === 0 ? -6 : 1 - day; // Mon = day 1
  ist.setUTCDate(ist.getUTCDate() + offset);
  return ist.toISOString().slice(0, 10);
}

function inr(value: string | number | undefined | null): string {
  const n = Number(value ?? 0);
  if (!isFinite(n)) return "—";
  return n.toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

export default function SettlementsPage() {
  const qc = useQueryClient();
  const admin = useAdminAuthStore((s) => s.admin);
  const [weekStart, setWeekStart] = useState<string>(() => istMonday(new Date()));

  const { data, isFetching } = useQuery({
    queryKey: ["admin", "settlements", weekStart],
    queryFn: () => ManagementAPI.listSettlements(weekStart),
    enabled: admin?.role === "SUPER_ADMIN",
  });

  const recomputeMut = useMutation({
    mutationFn: () => ManagementAPI.recomputeSettlements({ week_start: weekStart }),
    onSuccess: (res) => {
      toast.success(`Recomputed. ${res.frozen_skipped ?? 0} frozen rows skipped.`);
      qc.invalidateQueries({ queryKey: ["admin", "settlements", weekStart] });
    },
    onError: (e: any) => toast.error(e.message),
  });

  const finalizeMut = useMutation({
    mutationFn: (id: string) => ManagementAPI.finalizeSettlement(id),
    onSuccess: () => {
      toast.success("Finalized");
      qc.invalidateQueries({ queryKey: ["admin", "settlements", weekStart] });
    },
    onError: (e: any) => toast.error(e.message),
  });

  const markPaidMut = useMutation({
    mutationFn: (id: string) => ManagementAPI.markPaid(id),
    onSuccess: () => {
      toast.success("Marked paid");
      qc.invalidateQueries({ queryKey: ["admin", "settlements", weekStart] });
    },
    onError: (e: any) => toast.error(e.message),
  });

  const totals = data?.totals;

  const rows = useMemo(() => data?.items ?? [], [data]);

  if (admin?.role !== "SUPER_ADMIN") {
    return (
      <div className="rounded-md border border-border bg-card p-6 text-sm text-muted-foreground">
        Only the super admin can view weekly settlements.
      </div>
    );
  }

  const cols: Column<any>[] = [
    { key: "sub_admin_code", header: "Code" },
    { key: "sub_admin_name", header: "Sub-admin" },
    { key: "user_count", header: "Users", align: "right" },
    {
      key: "gross_user_loss_inr",
      header: "User loss",
      align: "right",
      render: (r) => `₹ ${inr(r.gross_user_loss_inr)}`,
    },
    {
      key: "gross_user_profit_inr",
      header: "User profit",
      align: "right",
      render: (r) => `₹ ${inr(r.gross_user_profit_inr)}`,
    },
    {
      key: "total_brokerage_inr",
      header: "Brokerage",
      align: "right",
      render: (r) => `₹ ${inr(r.total_brokerage_inr)}`,
    },
    {
      key: "net_house_pnl_inr",
      header: "Net house P&L",
      align: "right",
      render: (r) => (
        <span className={Number(r.net_house_pnl_inr) >= 0 ? "text-emerald-500" : "text-red-500"}>
          ₹ {inr(r.net_house_pnl_inr)}
        </span>
      ),
    },
    {
      key: "pnl_share_pct_snapshot",
      header: "Share %",
      align: "right",
      render: (r) => `${r.pnl_share_pct_snapshot}%`,
    },
    {
      key: "sub_admin_share_inr",
      header: "Sub-admin share",
      align: "right",
      render: (r) => `₹ ${inr(r.sub_admin_share_inr)}`,
    },
    {
      key: "status",
      header: "Status",
      render: (r) => {
        const cls =
          r.status === "PAID"
            ? "bg-emerald-500/10 text-emerald-500"
            : r.status === "FINALIZED"
              ? "bg-blue-500/10 text-blue-400"
              : "bg-amber-500/10 text-amber-400";
        return <span className={`rounded px-2 py-0.5 text-xs ${cls}`}>{r.status}</span>;
      },
    },
    {
      key: "actions",
      header: "",
      align: "right",
      render: (r) => (
        <div className="flex justify-end gap-1">
          {r.status === "PENDING" && (
            <Button
              variant="ghost"
              size="icon"
              onClick={() => finalizeMut.mutate(r.id)}
              aria-label="Finalize"
              title="Finalize"
            >
              <CheckCircle2 className="size-4 text-blue-400" />
            </Button>
          )}
          {r.status === "FINALIZED" && (
            <Button
              variant="ghost"
              size="icon"
              onClick={() => markPaidMut.mutate(r.id)}
              aria-label="Mark paid"
              title="Mark paid"
            >
              <BadgeDollarSign className="size-4 text-emerald-500" />
            </Button>
          )}
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader
        title="Weekly settlements"
        actions={
          <div className="flex items-end gap-3">
            <div className="space-y-1.5">
              <Label className="text-xs">Week starting (IST Mon)</Label>
              <Input
                type="date"
                value={weekStart}
                onChange={(e) => setWeekStart(istMonday(new Date(e.target.value)))}
                className="h-10 w-44"
              />
            </div>
            <Button onClick={() => recomputeMut.mutate()} disabled={recomputeMut.isPending}>
              <RefreshCw className="size-4" /> Recompute
            </Button>
          </div>
        }
      />

      {data && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <SummaryCard label="Total users" value={String(totals?.user_count ?? 0)} />
          <SummaryCard
            label="Net house P&L"
            value={`₹ ${inr(totals?.net_house_pnl_inr)}`}
            tone={Number(totals?.net_house_pnl_inr ?? 0) >= 0 ? "good" : "bad"}
          />
          <SummaryCard
            label="Total sub-admin payout"
            value={`₹ ${inr(totals?.sub_admin_share_inr)}`}
          />
        </div>
      )}

      <DataTable
        columns={cols}
        rows={rows}
        keyExtractor={(r) => r.id}
        loading={isFetching && !data}
      />

      {data && (
        <div className="text-xs text-muted-foreground">
          Window: {new Date(data.period_start).toLocaleString()} →{" "}
          {new Date(data.period_end).toLocaleString()}
        </div>
      )}
    </div>
  );
}

function SummaryCard({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "good" | "bad";
}) {
  const toneCls =
    tone === "good" ? "text-emerald-500" : tone === "bad" ? "text-red-500" : "text-foreground";
  return (
    <div className="rounded-md border border-border bg-card p-4">
      <div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className={`mt-1 text-2xl font-semibold ${toneCls}`}>{value}</div>
    </div>
  );
}
