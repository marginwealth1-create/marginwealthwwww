"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Check, Copy, X } from "lucide-react";
import { PayinOutAPI } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { DataTable, type Column } from "@/components/common/DataTable";
import { StatusPill } from "@/components/common/StatusPill";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn, formatINR } from "@/lib/utils";
import { OwnerBadge } from "@/components/admin/OwnerBadge";
import { LedgerSheet } from "@/components/admin/LedgerSheet";
import { useAdminAuthStore } from "@/stores/authStore";
import { canEdit } from "@/lib/permissions";

/**
 * One bank/UPI value + an inline copy button. Used in the
 * Withdrawals destination columns so admins can lift the holder /
 * account / IFSC / UPI into their payout tool in one click. Empty
 * values render as a muted "—" so the column stays visually aligned
 * across rows.
 */
function CopyableField({
  value,
  label,
  mono = true,
  uppercase = false,
}: {
  value?: string | null;
  label: string;
  mono?: boolean;
  uppercase?: boolean;
}) {
  if (!value) {
    return <span className="text-xs text-muted-foreground/60">—</span>;
  }
  async function doCopy() {
    try {
      await navigator.clipboard.writeText(value!);
      toast.success(`${label} copied`);
    } catch {
      // Clipboard API can fail on non-HTTPS / older browsers. Fall back
      // to selecting the text so the operator can copy manually.
      toast.error("Copy failed — long-press to select");
    }
  }
  return (
    <span className="inline-flex max-w-full items-center gap-1.5">
      <span
        className={cn(
          "truncate text-xs",
          mono && "font-mono",
          uppercase && "uppercase",
        )}
        title={value}
      >
        {value}
      </span>
      <button
        type="button"
        onClick={doCopy}
        aria-label={`Copy ${label}`}
        title={`Copy ${label}`}
        className="grid size-5 shrink-0 place-items-center rounded text-muted-foreground hover:bg-muted hover:text-foreground"
      >
        <Copy className="size-3" />
      </button>
    </span>
  );
}

export function WithdrawalsPanel() {
  const qc = useQueryClient();
  const me = useAdminAuthStore((s) => s.admin);
  // VIEW-only sub-broker / admin shouldn't see clickable Approve / Reject.
  // Backend rejects too via require_perm("withdrawals","write"); UI just
  // matches so the user understands why nothing happens.
  const canMutate = canEdit(me, "withdrawals");
  // Default to "All" (same rationale as DepositsPanel — operator
  // flagged 21-May that landing on an empty PENDING list looked
  // like the queue was broken).
  const [status, setStatus] = useState("");
  // Client-wise search (#5) — debounced; resets to page 1 on change.
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  useEffect(() => {
    const t = setTimeout(() => {
      setSearch(searchInput.trim());
      setPage(1);
    }, 350);
    return () => clearTimeout(t);
  }, [searchInput]);
  const [page, setPage] = useState(1);
  const pageSize = 15;
  const [approving, setApproving] = useState<{ id: string; utr: string } | null>(null);
  const [rejecting, setRejecting] = useState<{ id: string; reason: string } | null>(null);
  // Ledger drawer target — pops the LedgerSheet from the per-row L
  // button so admin can review wallet timeline before approving.
  const [ledgerUser, setLedgerUser] = useState<{ id: string; user_code?: string; full_name?: string } | null>(null);

  function changeStatus(next: string) {
    setStatus(next);
    setPage(1);
  }

  const { data, isFetching } = useQuery({
    queryKey: ["admin", "withdrawals", status, search, page],
    queryFn: () =>
      PayinOutAPI.withdrawals({
        status: status || undefined,
        search: search || undefined,
        page,
        page_size: pageSize,
      }),
    placeholderData: (prev) => prev,
  });

  async function approve() {
    if (!approving) return;
    try {
      await PayinOutAPI.approveWithdrawal(approving.id, { utr_number: approving.utr || undefined });
      toast.success("Approved + wallet debited");
      setApproving(null);
      qc.invalidateQueries({ queryKey: ["admin", "withdrawals"] });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function reject() {
    if (!rejecting?.reason.trim()) {
      toast.error("Reason required");
      return;
    }
    try {
      await PayinOutAPI.rejectWithdrawal(rejecting.id, rejecting.reason);
      toast.success("Rejected");
      setRejecting(null);
      qc.invalidateQueries({ queryKey: ["admin", "withdrawals"] });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  const cols: Column<any>[] = [
    { key: "created_at", header: "When", render: (r) => new Date(r.created_at).toLocaleString() },
    {
      key: "user",
      header: "User",
      render: (r) => (
        <div className="flex flex-col leading-tight">
          <span className="text-sm">{r.user_name || "—"}</span>
          <span className="font-mono text-[10px] text-muted-foreground">
            {r.user_code || r.user_id?.slice(-8)}
          </span>
        </div>
      ),
    },
    { key: "owner", header: "Owner", render: (r) => <OwnerBadge row={r} me={me} /> },
    { key: "amount", header: "Amount", align: "right", render: (r) => formatINR(r.amount) },
    // ── Destination columns ─────────────────────────────────────
    // The single "Destination" cell used to cram holder · bank · IFSC
    // · UPI together, which was fine to read but painful to act on —
    // every admin tier (super-admin / admin / broker / sub-broker)
    // had to select each value and copy it by hand into their payout
    // tool. Now each field gets its own column with a Copy button
    // sitting inline next to the value. Empty fields render as "—"
    // so the table stays visually aligned across rows. Bank rows show
    // the holder / account / IFSC; UPI-only rows surface the VPA in
    // the UPI column and leave the bank columns blank.
    {
      key: "holder",
      header: "Holder",
      render: (r) => (
        <CopyableField
          value={(r.bank?.holder || r.bank?.name) ?? null}
          label="Holder name"
          mono={false}
        />
      ),
    },
    {
      key: "account_number",
      header: "Account no.",
      render: (r) => (
        <CopyableField
          value={r.bank?.account_number ?? null}
          label="Account number"
        />
      ),
    },
    {
      key: "ifsc",
      header: "IFSC",
      render: (r) => (
        <CopyableField value={r.bank?.ifsc ?? null} label="IFSC" uppercase />
      ),
    },
    {
      key: "upi_id",
      header: "UPI",
      render: (r) => (
        <CopyableField value={r.bank?.upi_id ?? null} label="UPI ID" />
      ),
    },
    {
      key: "remarks",
      header: "Remarks",
      render: (r) => r.remarks || "—",
    },
    {
      key: "utr_number",
      header: "UTR",
      render: (r) => (
        <CopyableField value={r.utr_number ?? null} label="UTR" />
      ),
    },
    { key: "status", header: "Status", render: (r) => <StatusPill status={r.status} /> },
    {
      key: "ledger",
      header: "LEDGER",
      align: "center",
      render: (r: any) => (
        <Button
          size="sm"
          variant="outline"
          className="h-7 w-7 p-0 font-mono font-semibold border-primary/50 text-primary hover:bg-primary hover:text-primary-foreground"
          title="View ledger / Adjust wallet"
          onClick={(e) => {
            e.stopPropagation();
            setLedgerUser({
              id: r.user_id,
              user_code: r.user_code,
              full_name: r.user_name,
            });
          }}
        >
          L
        </Button>
      ),
    },
    {
      key: "positions",
      header: "POSITION",
      align: "center",
      render: (r: any) => (
        <Button
          asChild
          size="sm"
          variant="outline"
          className="h-7 w-7 p-0 font-mono font-semibold border-atm/50 text-atm hover:bg-atm hover:text-atm-foreground"
          title="View positions"
          onClick={(e) => e.stopPropagation()}
        >
          <Link href={`/positions?user_id=${r.user_id}`}>P</Link>
        </Button>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      render: (r) =>
        r.status === "PENDING" ? (
          <div className="flex justify-end gap-1">
            <Button
              variant="ghost"
              size="icon"
              aria-label="Approve"
              disabled={!canMutate}
              title={canMutate ? undefined : "View-only access"}
              onClick={() => canMutate && setApproving({ id: r.id, utr: "" })}
            >
              <Check className="size-4 text-primary" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              aria-label="Reject"
              disabled={!canMutate}
              title={canMutate ? undefined : "View-only access"}
              onClick={() => canMutate && setRejecting({ id: r.id, reason: "" })}
            >
              <X className="size-4 text-destructive" />
            </Button>
          </div>
        ) : null,
    },
  ];

  const items = data?.items ?? [];
  const meta = data?.meta;
  const totalPages = meta?.total_pages ?? 1;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-xs text-muted-foreground">
          {meta?.total ?? 0} {status.toLowerCase() || "all"}
          {meta?.total ? ` · page ${meta.page} of ${totalPages}` : ""}
        </div>
        <div className="flex items-center gap-2">
          <Input
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            placeholder="Search client (name / code / mobile)"
            className="h-9 w-44 text-sm sm:w-64"
          />
          <select
            value={status}
            onChange={(e) => changeStatus(e.target.value)}
            className="h-9 rounded-md border border-border bg-background px-3 text-sm"
          >
            <option value="">All</option>
            <option value="PENDING">Pending</option>
            <option value="COMPLETED">Completed</option>
            <option value="REJECTED">Rejected</option>
          </select>
        </div>
      </div>
      {/* Desktop: full table with destination columns */}
      <div className="hidden md:block">
        <DataTable columns={cols} rows={items} keyExtractor={(r) => r.id} loading={isFetching && !data} />
      </div>

      {/* Mobile: stacked withdrawal cards */}
      <div className="space-y-2 md:hidden">
        {isFetching && !data && (
          <div className="rounded-lg border border-border bg-card p-6 text-center text-sm text-muted-foreground">
            Loading…
          </div>
        )}
        {!isFetching && items.length === 0 && (
          <div className="rounded-lg border border-dashed border-border bg-card p-6 text-center text-sm text-muted-foreground">
            No data
          </div>
        )}
        {items.map((r: any) => (
          <WithdrawalMobileCard
            key={r.id}
            r={r}
            me={me}
            canMutate={canMutate}
            onApprove={() => setApproving({ id: r.id, utr: "" })}
            onReject={() => setRejecting({ id: r.id, reason: "" })}
          />
        ))}
      </div>

      {totalPages > 1 && (
        <div className="flex items-center justify-end gap-2 text-xs">
          <Button
            variant="outline"
            size="sm"
            disabled={page <= 1}
            onClick={() => setPage(1)}
          >
            First
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={page <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
          >
            Prev
          </Button>
          <span className="self-center text-muted-foreground">
            {page} / {totalPages}
          </span>
          <Button
            variant="outline"
            size="sm"
            disabled={page >= totalPages}
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
          >
            Next
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={page >= totalPages}
            onClick={() => setPage(totalPages)}
          >
            Last
          </Button>
        </div>
      )}

      <Dialog open={!!approving} onOpenChange={(v) => !v && setApproving(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Approve withdrawal</DialogTitle>
          </DialogHeader>
          <Input
            placeholder="UTR / payment reference (optional)"
            value={approving?.utr ?? ""}
            onChange={(e) => setApproving((r) => (r ? { ...r, utr: e.target.value } : r))}
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setApproving(null)}>
              Cancel
            </Button>
            <Button onClick={approve}>Approve & debit</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!!rejecting} onOpenChange={(v) => !v && setRejecting(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Reject withdrawal</DialogTitle>
          </DialogHeader>
          <Input
            placeholder="Reason (mandatory)"
            value={rejecting?.reason ?? ""}
            onChange={(e) => setRejecting((r) => (r ? { ...r, reason: e.target.value } : r))}
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setRejecting(null)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={reject}>
              Reject
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* User ledger drawer — same component as the Users list / Deposits
          panel so admin can review wallet history before approving a
          payout. */}
      <LedgerSheet
        open={!!ledgerUser}
        user={ledgerUser}
        onClose={() => setLedgerUser(null)}
      />
    </div>
  );
}

/* ────────────────────────────────────────────────────────────────── */
/* Mobile withdrawal card                                               */
/* ────────────────────────────────────────────────────────────────── */

/**
 * Phone-friendly card for a withdrawal row. Bank / UPI destination
 * fields each keep their inline copy button so the payout-operator
 * workflow (copy holder → account → IFSC into their payment tool)
 * still works on mobile. Only renders Approve / Reject for PENDING +
 * when the caller has write permission — same gate the table uses.
 */
function WithdrawalMobileCard({
  r,
  me,
  canMutate,
  onApprove,
  onReject,
}: {
  r: any;
  me: any;
  canMutate: boolean;
  onApprove: () => void;
  onReject: () => void;
}) {
  const isPending = r.status === "PENDING";
  const isCompleted = r.status === "COMPLETED";
  const isRejected = r.status === "REJECTED";
  const accent = isPending
    ? "before:bg-amber-500"
    : isCompleted
      ? "before:bg-emerald-500"
      : isRejected
        ? "before:bg-destructive"
        : "before:bg-muted-foreground/30";

  // Toggle which destination block we show — some users have only UPI,
  // some only bank. Bank takes priority when both exist (matches the
  // desktop column ordering: holder → account → IFSC → UPI).
  const hasBank = !!(r.bank?.account_number || r.bank?.ifsc);
  const hasUpi = !!r.bank?.upi_id;

  return (
    <div
      className={`relative overflow-hidden rounded-xl border border-border bg-gradient-to-br from-card to-card/60 p-3 pl-4 shadow-sm before:absolute before:inset-y-2 before:left-1 before:w-1 before:rounded-full ${accent}`}
    >
      {/* Header */}
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <StatusPill status={r.status} />
            <span className="text-[11px] text-muted-foreground">
              {new Date(r.created_at).toLocaleString("en-IN", {
                day: "2-digit",
                month: "short",
                hour: "2-digit",
                minute: "2-digit",
                hour12: true,
              })}
            </span>
          </div>
          <div className="mt-1 text-sm font-semibold leading-tight">
            {r.user_name || "—"}
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-muted-foreground">
            <span className="font-mono">{r.user_code || r.user_id?.slice(-8)}</span>
            <OwnerBadge row={r} me={me} />
          </div>
        </div>
        <div className="shrink-0 text-right">
          <div className="font-tabular text-base font-bold text-rose-600 dark:text-rose-400">
            {formatINR(r.amount)}
          </div>
        </div>
      </div>

      {/* Destination block */}
      <div className="mt-2.5 space-y-1.5 rounded-lg bg-muted/30 p-2 text-[11px]">
        {hasBank && (
          <>
            <Row label="Holder">
              <CopyableField value={(r.bank?.holder || r.bank?.name) ?? null} label="Holder name" mono={false} />
            </Row>
            <Row label="Account">
              <CopyableField value={r.bank?.account_number ?? null} label="Account number" />
            </Row>
            <Row label="IFSC">
              <CopyableField value={r.bank?.ifsc ?? null} label="IFSC" uppercase />
            </Row>
          </>
        )}
        {hasUpi && (
          <Row label="UPI">
            <CopyableField value={r.bank?.upi_id ?? null} label="UPI ID" />
          </Row>
        )}
        {r.utr_number && (
          <Row label="UTR">
            <CopyableField value={r.utr_number} label="UTR" />
          </Row>
        )}
        {r.remarks && (
          <Row label="Remarks">
            <span className="truncate" title={r.remarks}>{r.remarks}</span>
          </Row>
        )}
      </div>

      {/* Footer actions */}
      {isPending && (
        <div className="mt-3 flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            className="h-9 flex-1 border-emerald-500/40 text-emerald-600 hover:bg-emerald-500 hover:text-white dark:text-emerald-400"
            disabled={!canMutate}
            onClick={onApprove}
            title={canMutate ? "Approve withdrawal" : "View-only access"}
          >
            <Check className="size-4" /> Approve
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-9 flex-1 border-destructive/40 text-destructive hover:bg-destructive hover:text-destructive-foreground"
            disabled={!canMutate}
            onClick={onReject}
            title={canMutate ? "Reject with reason" : "View-only access"}
          >
            <X className="size-4" /> Reject
          </Button>
        </div>
      )}
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-2">
      <span className="shrink-0 text-[9px] uppercase tracking-wider text-muted-foreground">{label}</span>
      <span className="min-w-0 max-w-[65%] text-right">{children}</span>
    </div>
  );
}
