"use client";

import { useQuery } from "@tanstack/react-query";
import { ReportsAdminAPI } from "@/lib/api";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { PageHeader } from "@/components/common/PageHeader";
import { formatINR } from "@/lib/utils";

export default function FinancialReportPage() {
  const { data } = useQuery({ queryKey: ["admin", "reports", "financial"], queryFn: () => ReportsAdminAPI.financial() });
  if (!data) return <div className="text-sm text-muted-foreground">Loading…</div>;
  return (
    <div className="space-y-4">
      <PageHeader title="Financial report" description="Aggregate wallet figures across all users" />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
        <Stat label="Wallet balance" value={formatINR(data.wallet_balance)} />
        <Stat label="Margin used" value={formatINR(data.margin_used)} />
        <Stat label="Credit limit" value={formatINR(data.credit_limit)} />
        <Stat label="Total deposits" value={formatINR(data.total_deposits)} />
        <Stat label="Total withdrawals" value={formatINR(data.total_withdrawals)} />
        <Stat label="Total brokerage" value={formatINR(data.total_brokerage)} />
        <Stat label="Pending deposits" value={data.pending_deposits} />
        <Stat label="Pending withdrawals" value={data.pending_withdrawals} />
      </div>
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
