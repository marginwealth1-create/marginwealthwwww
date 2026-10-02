"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { ArrowDownRight, ArrowUpRight, Minus } from "lucide-react";

import { TradingAPI } from "@/lib/api";
import { PageHeader } from "@/components/common/PageHeader";
import { DataTable, type Column } from "@/components/common/DataTable";
import { Card, CardContent } from "@/components/ui/card";
import { cn, formatINR } from "@/lib/utils";

type ExposureRow = {
  token: string;
  symbol: string;
  exchange: string;
  segment: string;
  net_qty: number;
  long_qty: number;
  short_qty: number;
  long_positions: number;
  short_positions: number;
  users: number;
  positions: number;
  notional: number;
  margin_used: number;
  ltp: number;
};

type ExposurePayload = {
  rows: ExposureRow[];
  totals: {
    symbols: number;
    positions: number;
    long_positions: number;
    short_positions: number;
    users: number;
    margin_used: number;
  };
};

const num = (v: number, dp = 2) =>
  Number(v ?? 0).toLocaleString("en-IN", {
    minimumFractionDigits: dp,
    maximumFractionDigits: dp,
  });

export default function ExposurePage() {
  const [segment, setSegment] = useState("ALL");

  const { data, isFetching } = useQuery<ExposurePayload>({
    queryKey: ["admin", "exposure"],
    queryFn: () => TradingAPI.exposure(),
    refetchInterval: 5000,
  });

  const allRows = data?.rows ?? [];
  // Segments present in the book right now — no point offering a filter for
  // a segment nobody holds.
  const segments = useMemo(
    () => Array.from(new Set(allRows.map((r) => r.segment).filter(Boolean))).sort(),
    [allRows],
  );
  const rows = useMemo(
    () => (segment === "ALL" ? allRows : allRows.filter((r) => r.segment === segment)),
    [allRows, segment],
  );
  // Totals follow the filter, so "options only" answers how many option
  // trades are open on each side.
  const totals = useMemo(
    () => ({
      symbols: rows.length,
      positions: rows.reduce((n, r) => n + r.positions, 0),
      long_positions: rows.reduce((n, r) => n + r.long_positions, 0),
      short_positions: rows.reduce((n, r) => n + r.short_positions, 0),
      users: segment === "ALL" ? (data?.totals.users ?? 0) : 0,
      margin_used: rows.reduce((n, r) => n + r.margin_used, 0),
    }),
    [rows, segment, data],
  );

  const columns: Column<ExposureRow>[] = [
    {
      key: "symbol",
      header: "Symbol",
      render: (r) => (
        <Link
          href={`/positions?q=${encodeURIComponent(r.symbol)}`}
          className="font-medium hover:underline"
        >
          {r.symbol}
        </Link>
      ),
    },
    { key: "exchange", header: "Exch" },
    {
      key: "side",
      header: "House side",
      // Clients net long ⇒ the house carries the opposite position.
      render: (r) => <HouseSide netQty={r.net_qty} />,
    },
    {
      key: "net_qty",
      header: "Client net qty",
      align: "right" as const,
      render: (r) => (
        <span
          className={cn(
            "font-tabular",
            r.net_qty > 0 ? "text-buy" : r.net_qty < 0 ? "text-sell" : "text-muted-foreground",
          )}
        >
          {num(r.net_qty, 2)}
        </span>
      ),
    },
    {
      key: "open_trades",
      header: "Buy / Sell trades",
      align: "right" as const,
      // Counts, not quantity: how many open trades sit on each side.
      render: (r) => (
        <span className="font-tabular">
          <span className="text-buy">{r.long_positions}</span>
          <span className="text-muted-foreground"> / </span>
          <span className="text-sell">{r.short_positions}</span>
        </span>
      ),
    },
    {
      key: "long_qty",
      header: "Buy qty",
      align: "right" as const,
      render: (r) => <span className="font-tabular text-buy">{num(r.long_qty, 2)}</span>,
    },
    {
      key: "short_qty",
      header: "Sell qty",
      align: "right" as const,
      render: (r) => <span className="font-tabular text-sell">{num(r.short_qty, 2)}</span>,
    },
    {
      key: "users",
      header: "Clients",
      align: "right" as const,
      render: (r) => <span className="font-tabular">{r.users}</span>,
    },
    {
      key: "positions",
      header: "Positions",
      align: "right" as const,
      render: (r) => <span className="font-tabular text-muted-foreground">{r.positions}</span>,
    },
    {
      key: "ltp",
      header: "Last price",
      align: "right" as const,
      render: (r) => (
        <span className="font-tabular">{r.ltp > 0 ? num(r.ltp, 2) : "—"}</span>
      ),
    },
    {
      key: "notional",
      header: "Notional",
      align: "right" as const,
      // Instrument's own currency (USD for CFDs, INR for MCX/NSE) — never summed.
      render: (r) => <span className="font-tabular">{num(r.notional, 0)}</span>,
    },
    {
      key: "margin_used",
      header: "Margin (₹)",
      align: "right" as const,
      render: (r) => <span className="font-tabular">{formatINR(r.margin_used)}</span>,
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader
        title="Exposure"
        description="Every open client position folded together per symbol — the book the house is carrying. Refreshes every 5 seconds."
        actions={
          <select
            value={segment}
            onChange={(e) => setSegment(e.target.value)}
            className="h-9 rounded-md border border-border bg-background px-2 text-sm"
            aria-label="Filter by segment"
          >
            <option value="ALL">All segments</option>
            {segments.map((s) => (
              <option key={s} value={s}>
                {s.replaceAll("_", " ")}
              </option>
            ))}
          </select>
        }
      />

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <Stat label="Symbols in book" value={String(totals.symbols)} />
        <Stat label="Buy trades open" value={String(totals.long_positions)} tone="buy" />
        <Stat label="Sell trades open" value={String(totals.short_positions)} tone="sell" />
        <Stat label="Clients holding" value={segment === "ALL" ? String(totals.users) : "—"} />
        <Stat label="Margin used" value={formatINR(totals.margin_used)} />
      </div>

      <DataTable
        columns={columns}
        rows={rows}
        keyExtractor={(r) => `${r.token}-${r.segment}`}
        loading={isFetching && !data}
        empty="No open positions right now."
        tableClassName="w-max"
      />

      <p className="text-xs text-muted-foreground">
        Notional is in each instrument&apos;s own currency (USD for CFDs, INR for
        Indian contracts), so it is shown per symbol and never added up. Margin is
        in rupees everywhere, so that column totals above.
      </p>
    </div>
  );
}

function HouseSide({ netQty }: { netQty: number }) {
  if (!netQty) {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
        <Minus className="size-3" /> Flat
      </span>
    );
  }
  const houseShort = netQty > 0; // clients long ⇒ house short
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium",
        houseShort ? "bg-sell/10 text-sell" : "bg-buy/10 text-buy",
      )}
    >
      {houseShort ? <ArrowDownRight className="size-3" /> : <ArrowUpRight className="size-3" />}
      House {houseShort ? "short" : "long"}
    </span>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "buy" | "sell";
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-[11px] uppercase tracking-wider text-muted-foreground">
          {label}
        </div>
        <div
          className={cn(
            "mt-1 text-xl font-semibold tabular-nums",
            tone === "buy" && "text-buy",
            tone === "sell" && "text-sell",
          )}
        >
          {value}
        </div>
      </CardContent>
    </Card>
  );
}
