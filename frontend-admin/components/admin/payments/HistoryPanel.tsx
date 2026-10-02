"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowDownToLine, ArrowUpFromLine, Image as ImageIcon, Search } from "lucide-react";
import { PayinOutAPI } from "@/lib/api";
import { API_URL } from "@/lib/constants";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { DataTable, type Column } from "@/components/common/DataTable";
import { StatusPill } from "@/components/common/StatusPill";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { cn, formatINR, pnlColor } from "@/lib/utils";

// Unified row shape so deposits and withdrawals can share the same
// column layout. The original record is kept on `_raw` so the proof
// preview and bank-snapshot tooltip can dig out the type-specific
// fields without poking the merged structure with `any` everywhere.
type HistoryRow = {
  id: string;
  type: "DEPOSIT" | "WITHDRAWAL";
  user_id: string;
  amount: number;
  status: string;
  mode: string;            // payment_mode for deposits, "BANK" for withdrawals
  utr: string;             // utr_number on both
  remark: string;          // user_remark | remarks | rejection_reason | admin_remark — first non-empty
  bank_label: string;      // for withdrawals: "BankName · ••1234"; for deposits: payment_mode
  proof_url: string | null;
  created_at: string;
  _raw: any;
};

type TypeFilter = "ALL" | "DEPOSIT" | "WITHDRAWAL";

function normalizeDeposit(r: any): HistoryRow {
  return {
    id: r.id,
    type: "DEPOSIT",
    user_id: r.user_id,
    amount: Number(r.amount ?? 0),
    status: String(r.status ?? ""),
    mode: r.payment_mode || "—",
    utr: r.utr_number || "",
    remark: r.admin_remark || r.user_remark || "",
    bank_label: r.payment_mode || "—",
    proof_url: r.screenshot_url || null,
    created_at: r.created_at,
    _raw: r,
  };
}

function normalizeWithdrawal(r: any): HistoryRow {
  const bankName = r.bank?.name || r.bank?.bank_name || "—";
  const last4 = (r.bank?.account_number || "").slice(-4);
  return {
    id: r.id,
    type: "WITHDRAWAL",
    user_id: r.user_id,
    amount: Number(r.amount ?? 0),
    status: String(r.status ?? ""),
    mode: "BANK",
    utr: r.utr_number || "",
    remark: r.rejection_reason || r.remarks || r.admin_remark || "",
    bank_label: last4 ? `${bankName} · ••${last4}` : bankName,
    proof_url: null,
    created_at: r.created_at,
    _raw: r,
  };
}

export function HistoryPanel() {
  const [type, setType] = useState<TypeFilter>("ALL");
  const [status, setStatus] = useState<string>(""); // empty = all
  const [search, setSearch] = useState("");
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);

  // Both endpoints support a `status` query param. We fetch unfiltered
  // here (page_size=200 to cover the recent history without paginating
  // a unified merged view) and apply the status filter client-side so
  // the type chips can flip instantly without re-firing two requests
  // every time. 10 s poll matches the busier deposits panel without
  // doubling our request rate.
  const { data: depositsRaw, isFetching: depLoading } = useQuery({
    queryKey: ["admin", "payments", "history", "deposits"],
    queryFn: () => PayinOutAPI.deposits({ page: 1, page_size: 200 }),
    refetchInterval: 10000,
  });
  const { data: withdrawalsRaw, isFetching: wdLoading } = useQuery({
    queryKey: ["admin", "payments", "history", "withdrawals"],
    queryFn: () => PayinOutAPI.withdrawals({ page: 1, page_size: 200 }),
    refetchInterval: 10000,
  });

  const rows = useMemo<HistoryRow[]>(() => {
    const deps = (depositsRaw?.items ?? []).map(normalizeDeposit);
    const wds = (withdrawalsRaw?.items ?? []).map(normalizeWithdrawal);
    const all = [...deps, ...wds];
    // Newest-first — admins scan for "what just happened" far more
    // often than they look at the oldest record on the page.
    all.sort((a, b) => {
      const da = new Date(a.created_at).getTime() || 0;
      const db = new Date(b.created_at).getTime() || 0;
      return db - da;
    });
    return all;
  }, [depositsRaw, withdrawalsRaw]);

  // Client-side filter: type chip + status select + free-text search.
  // The search box matches the last 8 chars of user_id (which is what
  // the table already shows) plus the UTR — those are the two
  // identifiers an admin types when hunting for a specific request.
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return rows.filter((r) => {
      if (type !== "ALL" && r.type !== type) return false;
      if (status && r.status !== status) return false;
      if (!q) return true;
      return (
        r.user_id.toLowerCase().includes(q) ||
        r.utr.toLowerCase().includes(q) ||
        r.id.toLowerCase().includes(q)
      );
    });
  }, [rows, type, status, search]);

  // Summary counts shown above the table so admins see the split
  // without having to flip filters back and forth.
  const counts = useMemo(() => {
    let dep = 0;
    let wd = 0;
    let depAmt = 0;
    let wdAmt = 0;
    for (const r of rows) {
      if (r.type === "DEPOSIT") {
        dep += 1;
        if (r.status === "APPROVED") depAmt += r.amount;
      } else {
        wd += 1;
        if (r.status === "APPROVED" || r.status === "COMPLETED") wdAmt += r.amount;
      }
    }
    return { dep, wd, depAmt, wdAmt };
  }, [rows]);

  const cols: Column<HistoryRow>[] = [
    {
      key: "created_at",
      header: "When",
      render: (r) => (
        <span className="whitespace-nowrap font-tabular text-[12px]">
          {new Date(r.created_at).toLocaleString()}
        </span>
      ),
    },
    {
      key: "type",
      header: "Type",
      render: (r) =>
        r.type === "DEPOSIT" ? (
          <span className="inline-flex items-center gap-1 rounded-md bg-buy/10 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-buy ring-1 ring-inset ring-buy/30">
            <ArrowDownToLine className="size-3" /> Deposit
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 rounded-md bg-amber-500/10 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-amber-400 ring-1 ring-inset ring-amber-500/30">
            <ArrowUpFromLine className="size-3" /> Withdraw
          </span>
        ),
    },
    {
      key: "user_id",
      header: "User",
      render: (r) => <span className="font-mono text-[11px]">{r.user_id.slice(-8)}</span>,
    },
    {
      key: "amount",
      header: "Amount",
      align: "right",
      render: (r) => (
        <span
          className={cn(
            "font-tabular font-semibold tabular-nums",
            pnlColor(r.type === "DEPOSIT" ? r.amount : -r.amount),
          )}
        >
          {r.type === "DEPOSIT" ? "+" : "−"}
          {formatINR(r.amount)}
        </span>
      ),
    },
    {
      key: "bank_label",
      header: "Mode / Bank",
      render: (r) => <span className="text-[12px]">{r.bank_label}</span>,
    },
    {
      key: "utr",
      header: "UTR",
      render: (r) =>
        r.utr ? (
          <span className="font-mono text-[11px]">{r.utr}</span>
        ) : (
          <span className="text-muted-foreground/60">—</span>
        ),
    },
    {
      key: "remark",
      header: "Remark / Reason",
      className: "max-w-[220px] truncate",
      render: (r) =>
        r.remark ? (
          <span
            className={
              r.status === "REJECTED" ? "text-destructive" : "text-muted-foreground"
            }
            title={r.remark}
          >
            {r.remark}
          </span>
        ) : (
          <span className="text-muted-foreground/60">—</span>
        ),
    },
    {
      key: "status",
      header: "Status",
      render: (r) => <StatusPill status={r.status} />,
    },
    {
      key: "proof",
      header: "",
      align: "right",
      render: (r) =>
        r.proof_url ? (
          <Button
            variant="ghost"
            size="icon"
            aria-label="View proof"
            onClick={() => setPreviewUrl(r.proof_url)}
          >
            <ImageIcon className="size-4" />
          </Button>
        ) : null,
    },
  ];

  const isLoading = depLoading || wdLoading;

  return (
    <div className="space-y-3">
      {/* Stat strip: net deposit / withdrawal totals across approved
          rows. Glanceable answer to "how much have we processed?" */}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <StatTile
          label="Total Records"
          value={String(rows.length)}
          hint={`${counts.dep} deposits · ${counts.wd} withdrawals`}
        />
        <StatTile
          label="Approved Deposits"
          value={formatINR(counts.depAmt)}
          valueClass="text-buy"
        />
        <StatTile
          label="Processed Withdrawals"
          value={formatINR(counts.wdAmt)}
          valueClass="text-amber-400"
        />
        <StatTile
          label="Net In/Out"
          value={formatINR(counts.depAmt - counts.wdAmt)}
          valueClass={pnlColor(counts.depAmt - counts.wdAmt)}
        />
      </div>

      {/* Filter bar */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="inline-flex rounded-md border border-border bg-muted/30 p-1 text-sm">
          {(
            [
              { id: "ALL", label: "All" },
              { id: "DEPOSIT", label: "Deposits" },
              { id: "WITHDRAWAL", label: "Withdrawals" },
            ] as { id: TypeFilter; label: string }[]
          ).map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => setType(t.id)}
              className={cn(
                "rounded px-3 py-1.5 transition-colors",
                type === t.id
                  ? "bg-primary/15 text-primary"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {t.label}
            </button>
          ))}
        </div>

        <select
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          className="h-9 rounded-md border border-border bg-background px-3 text-sm"
        >
          <option value="">All statuses</option>
          <option value="PENDING">Pending</option>
          <option value="APPROVED">Approved</option>
          <option value="COMPLETED">Completed</option>
          <option value="REJECTED">Rejected</option>
          <option value="CANCELLED">Cancelled</option>
          <option value="PROCESSING">Processing</option>
        </select>

        <div className="relative ml-auto w-full sm:w-64">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search user / UTR / id"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-8"
          />
        </div>
      </div>

      <div className="text-xs text-muted-foreground">
        Showing {filtered.length} of {rows.length}
        {(type !== "ALL" || status || search) && " · filters active"}
      </div>

      <DataTable
        columns={cols}
        rows={filtered}
        keyExtractor={(r) => `${r.type}-${r.id}`}
        loading={isLoading && rows.length === 0}
      />

      <Dialog open={!!previewUrl} onOpenChange={(v) => !v && setPreviewUrl(null)}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>Payment proof</DialogTitle>
          </DialogHeader>
          {previewUrl && (
            <img
              src={previewUrl.startsWith("http") ? previewUrl : `${API_URL}${previewUrl}`}
              alt="Proof"
              className="max-h-[70vh] w-full rounded-md object-contain"
            />
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

function StatTile({
  label,
  value,
  hint,
  valueClass,
}: {
  label: string;
  value: string;
  hint?: string;
  valueClass?: string;
}) {
  return (
    <div className="rounded-md border border-border bg-card px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className={cn("mt-0.5 font-tabular text-sm font-semibold tabular-nums", valueClass)}>
        {value}
      </div>
      {hint && <div className="mt-0.5 text-[10px] text-muted-foreground">{hint}</div>}
    </div>
  );
}
