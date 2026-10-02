"use client";

import { useQuery } from "@tanstack/react-query";
import { TradingAPI } from "@/lib/api";
import { PageHeader } from "@/components/common/PageHeader";
import { DataTable, type Column } from "@/components/common/DataTable";
import { formatINR, formatPercent, pnlColor } from "@/lib/utils";

export default function AdminHoldingsPage() {
  const { data, isFetching } = useQuery({ queryKey: ["admin", "holdings"], queryFn: () => TradingAPI.holdings() });

  const cols: Column<any>[] = [
    { key: "user_code", header: "User" },
    { key: "symbol", header: "Symbol" },
    { key: "quantity", header: "Qty", align: "right" },
    { key: "avg_price", header: "Avg", align: "right", render: (r) => formatINR(r.avg_price) },
    { key: "ltp", header: "LTP", align: "right", render: (r) => formatINR(r.ltp) },
    { key: "invested_value", header: "Invested", align: "right", render: (r) => formatINR(r.invested_value) },
    { key: "current_value", header: "Current", align: "right", render: (r) => formatINR(r.current_value) },
    {
      key: "pnl",
      header: "P&L",
      align: "right",
      render: (r) => <span className={pnlColor(r.pnl)}>{formatINR(r.pnl)}</span>,
    },
    {
      key: "pnl_percentage",
      header: "%",
      align: "right",
      render: (r) => <span className={pnlColor(r.pnl_percentage)}>{formatPercent(r.pnl_percentage)}</span>,
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader title="Holdings (all users)" description={`${data?.length ?? 0} holdings`} />
      <DataTable columns={cols} rows={data} keyExtractor={(r) => r.id} loading={isFetching && !data} />
    </div>
  );
}
