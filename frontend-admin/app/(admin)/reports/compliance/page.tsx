"use client";

import { useQuery } from "@tanstack/react-query";
import { ReportsAdminAPI } from "@/lib/api";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { PageHeader } from "@/components/common/PageHeader";

export default function ComplianceReportPage() {
  const { data } = useQuery({ queryKey: ["admin", "reports", "compliance"], queryFn: () => ReportsAdminAPI.compliance() });

  return (
    <div className="space-y-4">
      <PageHeader title="Compliance report" />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>KYC verified</CardDescription>
            <CardTitle className="font-tabular text-2xl text-primary">{data?.kyc_verified ?? "—"}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>KYC pending</CardDescription>
            <CardTitle className="font-tabular text-2xl text-amber-400">{data?.kyc_pending ?? "—"}</CardTitle>
          </CardHeader>
        </Card>
      </div>
    </div>
  );
}
