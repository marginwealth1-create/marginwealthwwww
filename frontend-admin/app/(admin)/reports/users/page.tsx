"use client";

import { useQuery } from "@tanstack/react-query";
import { ReportsAdminAPI } from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { PageHeader } from "@/components/common/PageHeader";

export default function UsersReportPage() {
  const { data } = useQuery({ queryKey: ["admin", "reports", "users"], queryFn: () => ReportsAdminAPI.users() });
  return (
    <div className="space-y-4">
      <PageHeader title="Users report" />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Total" value={data?.total ?? "—"} />
        <Stat label="Active" value={data?.active ?? "—"} />
        <Stat label="Blocked" value={data?.blocked ?? "—"} />
        <Stat label="Last 24h signups" value={data?.last_24h_signups ?? "—"} />
      </div>
      <Card>
        <CardHeader>
          <CardTitle>By role</CardTitle>
        </CardHeader>
        <CardContent className="grid grid-cols-2 gap-2 md:grid-cols-5">
          {data?.by_role &&
            Object.entries(data.by_role).map(([k, v]) => (
              <div key={k} className="rounded-md border border-border bg-muted/20 p-3">
                <div className="text-xs uppercase tracking-wider text-muted-foreground">{k}</div>
                <div className="font-tabular text-xl font-semibold">{String(v)}</div>
              </div>
            ))}
        </CardContent>
      </Card>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: any }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardDescription>{label}</CardDescription>
        <CardTitle className="font-tabular text-2xl">{String(value)}</CardTitle>
      </CardHeader>
    </Card>
  );
}
