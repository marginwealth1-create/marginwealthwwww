"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CircleSlash, Pause, Play, Search } from "lucide-react";
import { InstrumentAdminAPI } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PageHeader } from "@/components/common/PageHeader";
import { DataTable, type Column } from "@/components/common/DataTable";
import { StatusPill } from "@/components/common/StatusPill";

export default function AdminInstrumentsPage() {
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [exchange, setExchange] = useState("");
  const [page, setPage] = useState(1);

  const { data, isFetching } = useQuery({
    queryKey: ["admin", "instruments", { q, exchange, page }],
    queryFn: () =>
      InstrumentAdminAPI.list({ q: q || undefined, exchange: exchange || undefined, page, page_size: 50 }),
  });

  async function toggleHalt(row: any) {
    try {
      if (row.is_halted) {
        await InstrumentAdminAPI.resume(row.id);
        toast.success(`${row.symbol} resumed`);
      } else {
        const reason = prompt("Halt reason?") || "";
        await InstrumentAdminAPI.halt(row.id, reason);
        toast.success(`${row.symbol} halted`);
      }
      qc.invalidateQueries({ queryKey: ["admin", "instruments"] });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  const cols: Column<any>[] = [
    { key: "token", header: "Token", render: (r) => <span className="font-mono text-[11px]">{r.token}</span> },
    { key: "symbol", header: "Symbol" },
    { key: "name", header: "Name", className: "max-w-[260px] truncate" },
    { key: "exchange", header: "Exch" },
    { key: "segment", header: "Segment" },
    { key: "instrument_type", header: "Type" },
    { key: "lot_size", header: "Lot", align: "right" },
    {
      key: "expiry",
      header: "Expiry",
      render: (r) =>
        r.expiry ? (
          <span className="whitespace-nowrap text-[11px]">
            {new Date(r.expiry).toLocaleDateString("en-IN", {
              day: "2-digit",
              month: "short",
              year: "numeric",
            })}
          </span>
        ) : (
          <span className="text-muted-foreground">—</span>
        ),
    },
    {
      key: "status",
      header: "Status",
      render: (r) => (
        <div className="flex gap-1">
          <StatusPill status={r.is_active ? "ACTIVE" : "CLOSED"} />
          {r.is_halted && <StatusPill status="REJECTED" className="!bg-amber-500/15 !text-amber-400" />}
        </div>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      render: (r) => (
        <Button variant="ghost" size="icon" aria-label="Halt/Resume" onClick={() => toggleHalt(r)}>
          {r.is_halted ? <Play className="size-4 text-primary" /> : <Pause className="size-4 text-amber-400" />}
        </Button>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader
        title="Instruments"
        description={`${data?.meta?.total ?? 0} instruments`}
      />

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[220px]">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={q}
            onChange={(e) => {
              setPage(1);
              setQ(e.target.value);
            }}
            placeholder="Search symbol or name"
            className="pl-9"
          />
        </div>
        <select
          value={exchange}
          onChange={(e) => {
            setPage(1);
            setExchange(e.target.value);
          }}
          className="h-10 rounded-md border border-border bg-background px-3 text-sm"
        >
          <option value="">All exchanges</option>
          <option value="NSE">NSE (cash)</option>
          <option value="NFO">NFO (NSE F&O)</option>
          <option value="BSE">BSE (cash)</option>
          <option value="BFO">BFO (BSE F&O)</option>
          <option value="MCX">MCX</option>
          <option value="CRYPTO">Crypto</option>
        </select>
      </div>

      <DataTable columns={cols} rows={data?.items} keyExtractor={(r) => r.id} loading={isFetching && !data} />

      {(data?.meta?.total_pages ?? 1) > 1 && (
        <div className="flex justify-end gap-2 text-xs">
          <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
            Prev
          </Button>
          <span className="self-center text-muted-foreground">
            {page} / {data?.meta?.total_pages}
          </span>
          <Button
            variant="outline"
            size="sm"
            disabled={page >= (data?.meta?.total_pages ?? 1)}
            onClick={() => setPage((p) => p + 1)}
          >
            Next
          </Button>
        </div>
      )}
    </div>
  );
}
