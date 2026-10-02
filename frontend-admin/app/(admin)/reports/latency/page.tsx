"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { AlertTriangle } from "lucide-react";

import { ReportsAdminAPI } from "@/lib/api";
import { PageHeader } from "@/components/common/PageHeader";
import { DataTable, type Column } from "@/components/common/DataTable";
import { cn, formatINR } from "@/lib/utils";

type Row = {
  user_id: string;
  user_code: string;
  label: string;
  fills: number;
  avg_fill_ms: number;
  max_fill_ms: number;
  avg_tick_age_ms: number | null;
  max_tick_age_ms: number | null;
  stale_fills: number;
  stale_pnl: number;
  pnl: number;
};

type Payload = {
  rows: Row[];
  totals: { fills: number; stale_fills: number; stale_pnl: number };
  days: number;
  stale_ms: number;
};

export default function LatencyReportPage() {
  const [days, setDays] = useState(7);
  const [staleMs, setStaleMs] = useState(1000);

  const { data, isFetching } = useQuery<Payload>({
    queryKey: ["admin", "latency", days, staleMs],
    queryFn: () => ReportsAdminAPI.latency({ days, stale_ms: staleMs }),
  });

  const rows = data?.rows ?? [];

  const columns: Column<Row>[] = [
    {
      key: "label",
      header: "Client",
      render: (r) => (
        <Link href={`/users/${r.user_id}`} className="font-medium hover:underline">
          {r.label}
        </Link>
      ),
    },
    {
      key: "fills",
      header: "Fills",
      align: "right" as const,
      render: (r) => <span className="font-tabular">{r.fills}</span>,
    },
    {
      key: "stale_fills",
      header: "On stale price",
      align: "right" as const,
      render: (r) => (
        <span
          className={cn(
            "font-tabular",
            r.stale_fills > 0 ? "font-semibold text-atm" : "text-muted-foreground",
          )}
        >
          {r.stale_fills}
        </span>
      ),
    },
    {
      key: "stale_pnl",
      header: "P&L on those",
      align: "right" as const,
      // Many stale fills AND money made on them is the pattern worth chasing.
      render: (r) => (
        <span className={cn("font-tabular", r.stale_pnl > 0 ? "text-sell" : "text-muted-foreground")}>
          {formatINR(r.stale_pnl)}
        </span>
      ),
    },
    {
      key: "avg_tick_age_ms",
      header: "Avg tick age",
      align: "right" as const,
      render: (r) => (
        <span className="font-tabular text-muted-foreground">
          {r.avg_tick_age_ms == null ? "—" : `${r.avg_tick_age_ms} ms`}
        </span>
      ),
    },
    {
      key: "max_tick_age_ms",
      header: "Worst tick age",
      align: "right" as const,
      render: (r) => (
        <span className="font-tabular text-muted-foreground">
          {r.max_tick_age_ms == null ? "—" : `${r.max_tick_age_ms} ms`}
        </span>
      ),
    },
    {
      key: "avg_fill_ms",
      header: "Our fill time",
      align: "right" as const,
      render: (r) => (
        <span className="font-tabular text-muted-foreground">{r.avg_fill_ms} ms</span>
      ),
    },
    {
      key: "pnl",
      header: "Total P&L",
      align: "right" as const,
      render: (r) => (
        <span className={cn("font-tabular", r.pnl >= 0 ? "text-buy" : "text-sell")}>
          {formatINR(r.pnl)}
        </span>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader
        title="Latency"
        description="How fresh the price was when each client's orders filled."
        actions={
          <div className="flex items-center gap-2">
            <select
              value={staleMs}
              onChange={(e) => setStaleMs(Number(e.target.value))}
              className="h-9 rounded-md border border-border bg-background px-2 text-sm"
              aria-label="Stale threshold"
            >
              <option value={500}>Stale over 0.5 s</option>
              <option value={1000}>Stale over 1 s</option>
              <option value={3000}>Stale over 3 s</option>
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

      {(data?.totals.stale_fills ?? 0) > 0 && (
        <div className="flex items-start gap-2 rounded-md border border-atm/30 bg-atm/10 px-3 py-2 text-sm">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-atm" />
          <span>
            {data?.totals.stale_fills} fill(s) landed on a price older than{" "}
            {staleMs} ms, booking {formatINR(data?.totals.stale_pnl ?? 0)} in P&L.
          </span>
        </div>
      )}

      <DataTable
        columns={columns}
        rows={rows}
        keyExtractor={(r) => r.user_id}
        loading={isFetching && !data}
        empty="No measured fills yet in this window."
        tableClassName="w-max"
      />

      <p className="text-xs text-muted-foreground">
        A client with many stale fills <em>and</em> profit on those fills is the
        latency-arbitrage pattern; a few stale fills on their own usually just
        mean a quiet symbol. Only fills placed after execution recording went
        live appear here.
      </p>
    </div>
  );
}
