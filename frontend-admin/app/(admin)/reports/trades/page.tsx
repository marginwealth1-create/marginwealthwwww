"use client";

import { useQuery } from "@tanstack/react-query";
import { ReportsAdminAPI } from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { PageHeader } from "@/components/common/PageHeader";
import { formatINR } from "@/lib/utils";

export default function TradesReportPage() {
  const { data } = useQuery({ queryKey: ["admin", "reports", "trades"], queryFn: () => ReportsAdminAPI.trades() });
  if (!data) return <div className="text-sm text-muted-foreground">Loading…</div>;

  return (
    <div className="space-y-4">
      <PageHeader title="Trades report" />
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {(["today", "week"] as const).map((period) => (
          <Card key={period}>
            <CardHeader>
              <CardTitle className="capitalize">{period}</CardTitle>
              <CardDescription>{data[period]?.count ?? 0} trades</CardDescription>
            </CardHeader>
            <CardContent className="space-y-1 text-sm">
              <Row label="Volume" value={formatINR(data[period]?.volume)} />
              <Row label="Brokerage" value={formatINR(data[period]?.brokerage)} />
              <Row label="Total charges" value={formatINR(data[period]?.charges)} />
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: any }) {
  return (
    <div className="flex justify-between border-b border-border/50 py-1.5 last:border-b-0">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-tabular">{String(value)}</span>
    </div>
  );
}
