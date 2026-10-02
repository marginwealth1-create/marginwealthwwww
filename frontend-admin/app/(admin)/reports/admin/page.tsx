"use client";

import { useQuery } from "@tanstack/react-query";
import { UserCheck, ListOrdered, CalendarRange, CalendarClock, Building2 } from "lucide-react";
import { PlatformReportsAPI } from "@/lib/api";
import { PageHeader } from "@/components/common/PageHeader";

function n(v: any): string {
  return Number(v ?? 0).toLocaleString("en-IN");
}

/** Small rectangular metric card — icon chip + label + big number. */
function MetricCard({
  icon: Icon,
  label,
  value,
  accent = "text-foreground",
  chip = "text-muted-foreground",
}: {
  icon: any;
  label: string;
  value: any;
  accent?: string;
  chip?: string;
}) {
  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <div className="flex items-center gap-2 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
        <Icon className={`size-4 ${chip}`} /> {label}
      </div>
      <div className={`mt-1.5 text-2xl font-bold ${accent}`}>{n(value)}</div>
    </div>
  );
}

export default function AdminReportsPage() {
  const { data, isLoading } = useQuery({
    queryKey: ["admin", "platform-reports"],
    queryFn: () => PlatformReportsAPI.get(),
    refetchInterval: 30_000,
  });

  if (isLoading || !data) {
    return (
      <div className="space-y-4">
        <PageHeader title="Admin Reports" description="Platform-wide order execution & login activity" />
        <div className="text-sm text-muted-foreground">Loading…</div>
      </div>
    );
  }

  const admins = data.admins ?? [];

  return (
    <div className="space-y-5">
      <PageHeader
        title="Admin Reports"
        description="Platform-wide order execution & login activity across every admin pool"
      />

      {/* ── Bold hero: total users logged in today (all admins combined) ── */}
      <div className="rounded-2xl border border-emerald-500/30 bg-gradient-to-br from-emerald-500/15 to-emerald-500/5 p-5">
        <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-emerald-500">
          <UserCheck className="size-4" /> Users logged in today
        </div>
        <div className="mt-1 text-4xl font-extrabold text-emerald-500">{n(data.logins.today)}</div>
        <div className="mt-1 text-xs text-muted-foreground">
          across all admins · platform-wide
        </div>
        <div className="mt-4 grid grid-cols-2 gap-3">
          <div className="rounded-lg border border-border bg-card/60 px-3 py-2">
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">This week active</div>
            <div className="text-lg font-bold">{n(data.logins.this_week)}</div>
          </div>
          <div className="rounded-lg border border-border bg-card/60 px-3 py-2">
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Last week active</div>
            <div className="text-lg font-bold">{n(data.logins.last_week)}</div>
          </div>
        </div>
      </div>

      {/* ── Orders executed platform-wide ── */}
      <div>
        <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          Orders executed · whole platform
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <MetricCard icon={ListOrdered} label="Today" value={data.orders.today} accent="text-primary" chip="text-primary" />
          <MetricCard icon={CalendarRange} label="This week" value={data.orders.this_week} />
          <MetricCard icon={CalendarClock} label="Last week" value={data.orders.last_week} />
        </div>
      </div>

      {/* ── Per-admin breakdown ── */}
      <div>
        <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          <Building2 className="size-4" /> Per-admin breakdown
        </div>

        {/* Desktop table */}
        <div className="hidden overflow-x-auto rounded-lg border border-border md:block">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-[11px] uppercase tracking-wider text-muted-foreground">
                <th className="px-4 py-2.5">Admin</th>
                <th className="px-4 py-2.5 text-right">Orders today</th>
                <th className="px-4 py-2.5 text-right">Active today</th>
                <th className="px-4 py-2.5 text-right">This week</th>
                <th className="px-4 py-2.5 text-right">Last week</th>
              </tr>
            </thead>
            <tbody>
              {admins.length === 0 ? (
                <tr>
                  <td colSpan={5} className="px-4 py-10 text-center text-muted-foreground">
                    No admins yet.
                  </td>
                </tr>
              ) : (
                admins.map((a) => (
                  <tr key={a.admin_id ?? a.admin_code} className="border-b border-border/60 last:border-b-0">
                    <td className="px-4 py-2.5">
                      <div className="font-medium">{a.admin_name}</div>
                      <div className="font-mono text-[10px] text-muted-foreground">{a.admin_code}</div>
                    </td>
                    <td className="px-4 py-2.5 text-right font-semibold text-primary">{n(a.orders_today)}</td>
                    <td className="px-4 py-2.5 text-right font-semibold text-emerald-500">{n(a.active_today)}</td>
                    <td className="px-4 py-2.5 text-right">{n(a.active_week)}</td>
                    <td className="px-4 py-2.5 text-right text-muted-foreground">{n(a.active_last_week)}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* Mobile cards */}
        <div className="space-y-2 md:hidden">
          {admins.length === 0 ? (
            <div className="py-10 text-center text-sm text-muted-foreground">No admins yet.</div>
          ) : (
            admins.map((a) => (
              <div key={a.admin_id ?? a.admin_code} className="rounded-lg border border-border bg-card p-3">
                <div className="flex items-center justify-between gap-2">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-semibold">{a.admin_name}</div>
                    <div className="font-mono text-[10px] text-muted-foreground">{a.admin_code}</div>
                  </div>
                  <div className="text-right">
                    <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Orders today</div>
                    <div className="text-lg font-bold text-primary">{n(a.orders_today)}</div>
                  </div>
                </div>
                <div className="mt-2 grid grid-cols-3 gap-2 text-center">
                  <div className="rounded-md border border-border bg-background/50 py-1.5">
                    <div className="text-[9px] uppercase tracking-wider text-muted-foreground">Today</div>
                    <div className="text-sm font-bold text-emerald-500">{n(a.active_today)}</div>
                  </div>
                  <div className="rounded-md border border-border bg-background/50 py-1.5">
                    <div className="text-[9px] uppercase tracking-wider text-muted-foreground">This wk</div>
                    <div className="text-sm font-bold">{n(a.active_week)}</div>
                  </div>
                  <div className="rounded-md border border-border bg-background/50 py-1.5">
                    <div className="text-[9px] uppercase tracking-wider text-muted-foreground">Last wk</div>
                    <div className="text-sm font-bold text-muted-foreground">{n(a.active_last_week)}</div>
                  </div>
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
