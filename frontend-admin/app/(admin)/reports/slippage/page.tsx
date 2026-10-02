"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { ReportsAdminAPI } from "@/lib/api";
import { PageHeader } from "@/components/common/PageHeader";
import { DataTable, type Column } from "@/components/common/DataTable";
import { Card, CardContent } from "@/components/ui/card";
import { cn, formatINR } from "@/lib/utils";

type Row = {
  label: string;
  user_id?: string;
  fills: number;
  avg_slip: number;
  worst_slip: number;
  best_slip: number;
  markup_value: number;
};

type Payload = {
  rows: Row[];
  totals: { fills: number; markup_value: number };
  days: number;
  group_by: string;
};

export default function SlippageReportPage() {
  const [days, setDays] = useState(7);
  const [groupBy, setGroupBy] = useState<"user" | "symbol">("user");

  const { data, isFetching } = useQuery<Payload>({
    queryKey: ["admin", "slippage", days, groupBy],
    queryFn: () => ReportsAdminAPI.slippage({ days, group_by: groupBy }),
  });

  const rows = data?.rows ?? [];

  const columns: Column<Row>[] = [
    {
      key: "label",
      header: groupBy === "user" ? "Client" : "Symbol",
      render: (r) =>
        r.user_id ? (
          <Link href={`/users/${r.user_id}`} className="font-medium hover:underline">
            {r.label}
          </Link>
        ) : (
          <span className="font-medium">{r.label}</span>
        ),
    },
    {
      key: "fills",
      header: "Fills",
      align: "right" as const,
      render: (r) => <span className="font-tabular">{r.fills}</span>,
    },
    {
      key: "avg_slip",
      header: "Avg slippage",
      align: "right" as const,
      // Positive = the client got a worse price than their screen quoted.
      render: (r) => (
        <span className={cn("font-tabular", r.avg_slip > 0 ? "text-sell" : "text-buy")}>
          {r.avg_slip > 0 ? "+" : ""}
          {r.avg_slip}
        </span>
      ),
    },
    {
      key: "worst_slip",
      header: "Worst",
      align: "right" as const,
      render: (r) => <span className="font-tabular text-muted-foreground">{r.worst_slip}</span>,
    },
    {
      key: "best_slip",
      header: "Best",
      align: "right" as const,
      render: (r) => <span className="font-tabular text-muted-foreground">{r.best_slip}</span>,
    },
    {
      key: "markup_value",
      header: "Spread taken (₹)",
      align: "right" as const,
      render: (r) => <span className="font-tabular">{formatINR(r.markup_value)}</span>,
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader
        title="Slippage"
        description="Where fills landed against the price the client was shown on their order panel."
        actions={
          <div className="flex items-center gap-2">
            <select
              value={groupBy}
              onChange={(e) => setGroupBy(e.target.value as "user" | "symbol")}
              className="h-9 rounded-md border border-border bg-background px-2 text-sm"
              aria-label="Group by"
            >
              <option value="user">By client</option>
              <option value="symbol">By symbol</option>
            </select>
            <select
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
              className="h-9 rounded-md border border-border bg-background px-2 text-sm"
              aria-label="Period"
            >
              <option value={1}>Today</option>
              <option value={7}>7 days</option>
              <option value={30}>30 days</option>
            </select>
          </div>
        }
      />

      <div className="grid gap-3 sm:grid-cols-2">
        <Card>
          <CardContent className="p-4">
            <div className="text-[11px] uppercase tracking-wider text-muted-foreground">
              Fills measured
            </div>
            <div className="mt-1 text-xl font-semibold tabular-nums">
              {data?.totals.fills ?? 0}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <div className="text-[11px] uppercase tracking-wider text-muted-foreground">
              Spread taken
            </div>
            <div className="mt-1 text-xl font-semibold tabular-nums">
              {formatINR(data?.totals.markup_value ?? 0)}
            </div>
          </CardContent>
        </Card>
      </div>

      <DataTable
        columns={columns}
        rows={rows}
        keyExtractor={(r) => r.user_id ?? r.label}
        loading={isFetching && !data}
        empty="No measured fills yet in this window."
        tableClassName="w-max"
      />

      <p className="text-xs text-muted-foreground">
        Positive slippage means the client got a worse price than quoted: paid
        more buying, received less selling. Only fills placed after execution
        recording went live can be measured — older trades never stored what
        their price should be compared against, so they are left out rather than
        counted as zero.
      </p>
    </div>
  );
}
