"use client";

import { useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  ArrowLeft,
  LogIn,
  Users,
  Wallet,
  TrendingUp,
  TrendingDown,
  Activity,
  ClipboardList,
} from "lucide-react";

import { ManagementAPI, setTokens } from "@/lib/api";
import { useAdminAuthStore } from "@/stores/authStore";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/common/PageHeader";
import { DataTable, type Column } from "@/components/common/DataTable";
import { STORAGE_KEYS } from "@/lib/constants";
import type { AdminUser } from "@/types";

function inr(value: string | number | undefined | null): string {
  const n = Number(value ?? 0);
  if (!isFinite(n)) return "—";
  return `₹ ${n.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

export default function SubAdminDetailPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const params = useParams<{ id: string }>();
  const id = params?.id;
  const admin = useAdminAuthStore((s) => s.admin);

  const { data, isFetching } = useQuery({
    queryKey: ["admin", "sub-admin-report", id],
    queryFn: () => ManagementAPI.subAdminReport(id!),
    enabled: !!id && admin?.role === "SUPER_ADMIN",
    refetchInterval: 10000,
  });

  const [loggingIn, setLoggingIn] = useState(false);

  if (admin?.role !== "SUPER_ADMIN") {
    return (
      <div className="rounded-md border border-border bg-card p-6 text-sm text-muted-foreground">
        Only the super admin can view sub-admin reports.
      </div>
    );
  }

  async function loginAsSubAdmin() {
    // No confirm popup — clicking the button is the confirmation. The
    // toast on success + clearing the QueryClient cache makes the
    // session swap obvious.
    setLoggingIn(true);
    try {
      const r = await ManagementAPI.impersonateSubAdmin(id!);
      // Stash the super-admin tokens under a "previous session" key so a
      // small banner / future "switch back" UI can find them. Best-effort
      // — if it fails (private mode, quota), impersonation still proceeds.
      try {
        const prevAccess = window.localStorage.getItem(STORAGE_KEYS.accessToken);
        const prevRefresh = window.localStorage.getItem(STORAGE_KEYS.refreshToken);
        const prevAdmin = window.localStorage.getItem("nb.admin.auth");
        if (prevAccess && prevRefresh) {
          window.localStorage.setItem(
            "nb.admin.impersonatorSession",
            JSON.stringify({
              access: prevAccess,
              refresh: prevRefresh,
              admin: prevAdmin,
              ts: Date.now(),
            }),
          );
        }
      } catch {
        /* ignore */
      }
      // Adopt the sub-admin's tokens and seed the auth store.
      setTokens(r.access_token, r.refresh_token);
      const next: AdminUser = {
        id: r.admin.id,
        user_code: r.admin.user_code,
        email: r.admin.email,
        full_name: r.admin.full_name,
        role: r.admin.role,
        last_login_at: null,
        admin_permissions: r.admin.admin_permissions ?? null,
        pnl_share_pct: r.admin.pnl_share_pct ?? null,
      };
      // The auth store persists `admin` via Zustand persist middleware; write
      // it directly so the next render reflects the new session.
      useAdminAuthStore.setState({ admin: next });
      window.localStorage.setItem(
        "nb.admin.auth",
        JSON.stringify({ state: { admin: next }, version: 0 }),
      );
      qc.clear();
      toast.success(`Logged in as ${r.admin.user_code}`);
      router.push("/dashboard");
    } catch (e: any) {
      toast.error(e?.response?.data?.error?.message || e.message || "Failed");
    } finally {
      setLoggingIn(false);
    }
  }

  const sa = data?.sub_admin;
  const wallet = data?.wallet ?? {};
  const pnl = data?.pnl ?? {};
  const trades = data?.trades ?? {};

  const tradeCols: Column<any>[] = [
    {
      key: "executed_at",
      header: "When",
      render: (r) => new Date(r.executed_at).toLocaleString(),
    },
    { key: "user_code", header: "User", render: (r) => r.user_code ?? "—" },
    { key: "symbol", header: "Symbol" },
    { key: "exchange", header: "Exch" },
    { key: "action", header: "Side", render: (r) => r.action },
    { key: "quantity", header: "Qty", align: "right" },
    { key: "price", header: "Price", align: "right" },
    {
      key: "value",
      header: "Value",
      align: "right",
      render: (r) => inr(r.value),
    },
    {
      key: "brokerage",
      header: "Brokerage",
      align: "right",
      render: (r) => inr(r.brokerage),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader
        title={sa ? `${sa.full_name}` : "Sub-admin report"}
        description={
          sa
            ? `${sa.user_code} · ${sa.email} · ${sa.mobile} · PNL share ${sa.pnl_share_pct ?? "0"}%`
            : "Loading…"
        }
        actions={
          <div className="flex gap-2">
            <Button
              variant="outline"
              onClick={() => router.push("/management/sub-admins")}
            >
              <ArrowLeft className="size-4" /> Back
            </Button>
            <Button onClick={loginAsSubAdmin} disabled={loggingIn || !sa}>
              <LogIn className="size-4" />
              {loggingIn ? "Signing in…" : "Login as"}
            </Button>
          </div>
        }
      />

      {/* Top stats grid — Users (clients) and Brokers are separate tiles
          so the client count isn't inflated by broker/sub-broker logins. */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-5">
        <StatCard
          icon={<Users className="size-4" />}
          label="Users"
          value={String(data?.user_count ?? 0)}
          sub={`${data?.active_user_count ?? 0} active`}
          loading={isFetching && !data}
        />
        <StatCard
          icon={<Users className="size-4" />}
          label="Brokers / Sub-brokers"
          value={String(data?.broker_count ?? 0)}
          sub="login accounts"
          loading={isFetching && !data}
        />
        <StatCard
          icon={<Activity className="size-4" />}
          label="Open positions"
          value={String(data?.open_positions ?? 0)}
          sub={`Unrealised ${inr(pnl.open_unrealised)}`}
          loading={isFetching && !data}
        />
        <StatCard
          icon={<ClipboardList className="size-4" />}
          label="Trades today"
          value={String(trades.today ?? 0)}
          sub={`${trades.this_week ?? 0} this week · ${trades.all_time ?? 0} all-time`}
          loading={isFetching && !data}
        />
        <StatCard
          icon={<Wallet className="size-4" />}
          label="Wallet balance"
          value={inr(wallet.available_balance)}
          sub={`Margin ${inr(wallet.used_margin)}`}
          loading={isFetching && !data}
        />
      </div>

      {/* PNL row */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
        <PnlCard label="Today realised PNL" value={pnl.today_realised} />
        <PnlCard label="This week realised PNL" value={pnl.week_realised} />
        <PnlCard label="All-time realised PNL" value={pnl.all_time_realised} />
      </div>

      {/* Money flow */}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
        <StatCard
          icon={<TrendingDown className="size-4 text-emerald-500" />}
          label="Deposits (this week)"
          value={inr(data?.deposits_week)}
          loading={isFetching && !data}
        />
        <StatCard
          icon={<TrendingUp className="size-4 text-red-500" />}
          label="Withdrawals (this week)"
          value={inr(data?.withdrawals_week)}
          loading={isFetching && !data}
        />
        <StatCard
          icon={<Wallet className="size-4" />}
          label="Total brokerage (pool)"
          value={inr(wallet.total_brokerage)}
          sub={`Deposits ${inr(wallet.total_deposits)} · Withdrawals ${inr(wallet.total_withdrawals)}`}
          loading={isFetching && !data}
        />
      </div>

      {/* Recent trades */}
      <div className="space-y-2">
        <div className="text-sm font-medium">Recent trades (latest 10)</div>
        <DataTable
          columns={tradeCols}
          rows={data?.recent_trades}
          keyExtractor={(r) => r.id}
          loading={isFetching && !data}
        />
      </div>
    </div>
  );
}

function StatCard({
  icon,
  label,
  value,
  sub,
  loading,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  sub?: string;
  loading?: boolean;
}) {
  return (
    <div className="rounded-md border border-border bg-card p-4">
      <div className="flex items-center justify-between">
        <div className="text-xs uppercase tracking-wide text-muted-foreground">
          {label}
        </div>
        <div className="text-muted-foreground">{icon}</div>
      </div>
      <div className="mt-1 text-xl font-semibold">{loading ? "…" : value}</div>
      {sub && <div className="mt-0.5 text-xs text-muted-foreground">{sub}</div>}
    </div>
  );
}

function PnlCard({ label, value }: { label: string; value: string | undefined }) {
  const n = Number(value ?? 0);
  const tone = n > 0 ? "text-emerald-500" : n < 0 ? "text-red-500" : "text-foreground";
  return (
    <div className="rounded-md border border-border bg-card p-4">
      <div className="text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <div className={`mt-1 text-2xl font-semibold ${tone}`}>{inr(value)}</div>
    </div>
  );
}
