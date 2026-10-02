"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { Users } from "lucide-react";

import { ReportsAdminAPI } from "@/lib/api";
import { PageHeader } from "@/components/common/PageHeader";
import { DataTable, type Column } from "@/components/common/DataTable";
import { cn } from "@/lib/utils";

type ClusterUser = { user_id: string; user_code: string; name: string };

type Cluster = {
  symbol: string;
  action: string;
  users: ClusterUser[];
  user_count: number;
  trades: number;
  quantity: number;
  first_at: string | null;
  last_at: string | null;
};

type Payload = {
  clusters: Cluster[];
  window_sec: number;
  days: number;
  min_users: number;
};

function fmtTime(iso: string | null) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("en-IN", {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: true,
    });
  } catch {
    return iso;
  }
}

export default function SimilarActivityPage() {
  const [days, setDays] = useState(1);
  const [windowSec, setWindowSec] = useState(60);
  const [minUsers, setMinUsers] = useState(3);

  const { data, isFetching } = useQuery<Payload>({
    queryKey: ["admin", "similar-activity", days, windowSec, minUsers],
    queryFn: () =>
      ReportsAdminAPI.similarActivity({
        days,
        window_sec: windowSec,
        min_users: minUsers,
      }),
    refetchInterval: 30_000,
  });

  const clusters = data?.clusters ?? [];

  const columns: Column<Cluster>[] = [
    {
      key: "when",
      header: "When",
      render: (c) => (
        <span className="whitespace-nowrap text-muted-foreground">{fmtTime(c.first_at)}</span>
      ),
    },
    { key: "symbol", header: "Symbol", render: (c) => <span className="font-medium">{c.symbol}</span> },
    {
      key: "action",
      header: "Side",
      render: (c) => (
        <span
          className={cn(
            "rounded px-1.5 py-0.5 text-[11px] font-medium",
            c.action === "BUY" ? "bg-buy/10 text-buy" : "bg-sell/10 text-sell",
          )}
        >
          {c.action}
        </span>
      ),
    },
    {
      key: "user_count",
      header: "Clients",
      align: "right" as const,
      render: (c) => <span className="font-tabular font-semibold">{c.user_count}</span>,
    },
    {
      key: "trades",
      header: "Trades",
      align: "right" as const,
      render: (c) => <span className="font-tabular text-muted-foreground">{c.trades}</span>,
    },
    {
      key: "quantity",
      header: "Qty",
      align: "right" as const,
      render: (c) => <span className="font-tabular text-muted-foreground">{c.quantity}</span>,
    },
    {
      key: "who",
      header: "Who",
      render: (c) => (
        <div className="flex flex-wrap gap-1">
          {c.users.map((u) => (
            <Link
              key={u.user_id}
              href={`/users/${u.user_id}`}
              className="rounded-full border border-border px-2 py-0.5 text-[11px] hover:bg-muted"
              title={u.name || undefined}
            >
              {u.user_code || u.name || u.user_id.slice(-6)}
            </Link>
          ))}
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader
        title="Similar activity"
        description="Clients trading the same symbol, the same way, at the same moment — copy trading, a shared signal group, or one person running several accounts."
        actions={
          <div className="flex flex-wrap items-center gap-2">
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
            <select
              value={windowSec}
              onChange={(e) => setWindowSec(Number(e.target.value))}
              className="h-9 rounded-md border border-border bg-background px-2 text-sm"
              aria-label="Time window"
            >
              <option value={10}>Within 10 s</option>
              <option value={60}>Within 1 min</option>
              <option value={300}>Within 5 min</option>
            </select>
            <select
              value={minUsers}
              onChange={(e) => setMinUsers(Number(e.target.value))}
              className="h-9 rounded-md border border-border bg-background px-2 text-sm"
              aria-label="Minimum clients"
            >
              <option value={2}>2+ clients</option>
              <option value={3}>3+ clients</option>
              <option value={5}>5+ clients</option>
            </select>
          </div>
        }
      />

      <div className="flex items-center gap-2 rounded-md border border-border bg-card/40 px-3 py-2 text-sm">
        <Users className="size-4 text-muted-foreground" />
        <span>
          {clusters.length} group{clusters.length === 1 ? "" : "s"} found
        </span>
        <span className="text-muted-foreground">
          · {minUsers}+ clients on the same symbol and side within {windowSec}s
        </span>
      </div>

      <DataTable
        columns={columns}
        rows={clusters}
        keyExtractor={(c) => `${c.symbol}-${c.action}-${c.first_at}`}
        loading={isFetching && !data}
        empty="No groups matched — nobody traded together this closely."
        tableClassName="w-max"
      />

      <p className="text-xs text-muted-foreground">
        Agreement is normal on a sharp move, so treat a group as something to
        check rather than proof. Tighten the window or raise the client count to
        cut the noise.
      </p>
    </div>
  );
}
