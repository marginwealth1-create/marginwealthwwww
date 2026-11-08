"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Check, Image as ImageIcon, X } from "lucide-react";
import { PayinOutAPI } from "@/lib/api";
import { API_URL } from "@/lib/constants";
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
import { formatINR } from "@/lib/utils";
import { OwnerBadge } from "@/components/admin/OwnerBadge";
import { LedgerSheet } from "@/components/admin/LedgerSheet";
import { useAdminAuthStore } from "@/stores/authStore";
import { canEdit } from "@/lib/permissions";

export function DepositsPanel() {
  const qc = useQueryClient();
  const me = useAdminAuthStore((s) => s.admin);
  // VIEW-only sub-broker / admin shouldn't see clickable Approve / Reject.
  // Backend rejects too (require_perm("deposits","write")) but the UI must
  // match so the user understands why nothing happens.
  const canMutate = canEdit(me, "deposits");
  // Default to "All" so admins land on a full view of recent deposits
  // (not just PENDING). Operator-flagged 21-May: the empty-Pending
  // state was confusing on quiet hours -- "No data" suggested the
  // queue was broken when actually all rows had been processed.
  const [status, setStatus] = useState("");
  // Client-wise search (#5) — debounced so a keystroke doesn't fire a request
  // each character. Resets to page 1 whenever the term changes.
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
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [rejecting, setRejecting] = useState<{ id: string; remark: string } | null>(null);
  // Per-row in-flight guard. A slow network made admins double-click
  // Approve before the first request resolved, which double-credited the
  // user's wallet. removeLocally() only hides the row on the PENDING
  // filter — on the default "All" view the row stays clickable — so we
  // also gate every action on this set: while an id is in here its
  // Approve/Reject are no-ops and rendered disabled. Backend now also
  // guards atomically, but this kills the second request before it even
  // leaves the browser.
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const markBusy = (id: string) => setBusy((s) => new Set(s).add(id));
  const clearBusy = (id: string) =>
    setBusy((s) => {
      const n = new Set(s);
      n.delete(id);
      return n;
    });
  // Ledger drawer target — pops the same LedgerSheet that's used on the
  // Users list so admin can review the user's wallet timeline straight
  // from a payment row without navigating away.
  const [ledgerUser, setLedgerUser] = useState<{ id: string; user_code?: string; full_name?: string } | null>(null);

  // Reset to page 1 whenever the status filter changes so an admin
  // switching from "All" page 3 back to "Pending" doesn't land on an
  // empty page out of bounds.
  function changeStatus(next: string) {
    setStatus(next);
    setPage(1);
  }

  const { data, isFetching } = useQuery({
    queryKey: ["admin", "deposits", status, search, page],
    queryFn: () =>
      PayinOutAPI.deposits({
        status: status || undefined,
        search: search || undefined,
        page,
        page_size: pageSize,
      }),
    // Poll so new pending deposits from users appear without a manual
    // refresh. 5 s is fast enough to feel live and slow enough to avoid
    // pummeling the API.
    refetchInterval: 5000,
    placeholderData: (prev) => prev, // keep the table during page flips
  });

  // Drop a row from the pending list immediately on action. Without this the
  // row sits on screen until the next poll resolves and admins double-click
  // thinking it didn't register.
  function removeLocally(id: string) {
    qc.setQueryData<any>(["admin", "deposits", status, page], (prev: any) => {
      if (!prev) return prev;
      return { ...prev, items: (prev.items ?? []).filter((r: any) => r.id !== id) };
    });
  }

  async function approve(id: string) {
    if (busy.has(id)) return; // second click while first is in flight
    markBusy(id);
    if (status === "PENDING") removeLocally(id);
    try {
      await PayinOutAPI.approveDeposit(id);
      toast.success("Approved + wallet credited");
      qc.invalidateQueries({ queryKey: ["admin", "deposits"] });
    } catch (e: any) {
      toast.error(e.message);
      qc.invalidateQueries({ queryKey: ["admin", "deposits"] });
    } finally {
      clearBusy(id);
    }
  }

  async function reject() {
    if (!rejecting) return;
    if (!rejecting.remark.trim()) {
      toast.error("Reason required");
      return;
    }
    const id = rejecting.id;
    if (busy.has(id)) return;
    markBusy(id);
    if (status === "PENDING") removeLocally(id);
    try {
      await PayinOutAPI.rejectDeposit(id, rejecting.remark);
      toast.success("Rejected");
      setRejecting(null);
      qc.invalidateQueries({ queryKey: ["admin", "deposits"] });
    } catch (e: any) {
      toast.error(e.message);
      qc.invalidateQueries({ queryKey: ["admin", "deposits"] });
    } finally {
      clearBusy(id);
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
    {
      key: "amount",
      header: "Amount",
      align: "right",
      render: (r) => {
        // Settlement is informational only now — the user is NOT
        // expected to top it up via future deposits (policy change
        // 21-May). Surface the dues figure as a passive informational
        // chip without the "will be recovered" tooltip that promised
        // auto-deduction the backend no longer does.
        const settlement = Number(r.user_settlement_outstanding ?? 0);
        if (settlement <= 0) return formatINR(r.amount);
        return (
          <div className="flex flex-col items-end leading-tight">
            <span>{formatINR(r.amount)}</span>
            <span
              className="text-[10px] text-amber-600 dark:text-amber-400"
              title={`User has ₹${settlement.toFixed(2)} settlement on record (informational — not auto-recovered).`}
            >
              ⓘ ₹{settlement.toFixed(0)} settlement
            </span>
          </div>
        );
      },
    },
    { key: "payment_mode", header: "Mode" },
    { key: "utr_number", header: "UTR / Tx", render: (r) => <TxnCell r={r} /> },
    { key: "user_remark", header: "Remark", render: (r) => r.user_remark || "—", className: "max-w-[200px] truncate" },
    {
      key: "screenshot",
      header: "Proof",
      render: (r) =>
        r.screenshot_url ? (
          <Button variant="ghost" size="icon" onClick={() => setPreviewUrl(r.screenshot_url)}>
            <ImageIcon className="size-4" />
          </Button>
        ) : (
          "—"
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
              disabled={!canMutate || busy.has(r.id)}
              title={canMutate ? undefined : "View-only access"}
              onClick={() => canMutate && approve(r.id)}
            >
              <Check className="size-4 text-primary" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              aria-label="Reject"
              disabled={!canMutate || busy.has(r.id)}
              title={canMutate ? undefined : "View-only access"}
              onClick={() => canMutate && setRejecting({ id: r.id, remark: "" })}
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
            <option value="APPROVED">Approved</option>
            <option value="REJECTED">Rejected</option>
          </select>
        </div>
      </div>
      {/* Desktop: full table */}
      <div className="hidden md:block">
        <DataTable columns={cols} rows={items} keyExtractor={(r) => r.id} loading={isFetching && !data} />
      </div>

      {/* Mobile: stacked cards — same data, tap-friendly approve/reject */}
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
          <DepositMobileCard
            key={r.id}
            r={r}
            canMutate={canMutate}
            busy={busy.has(r.id)}
            onApprove={() => approve(r.id)}
            onReject={() => setRejecting({ id: r.id, remark: "" })}
            onPreview={() => r.screenshot_url && setPreviewUrl(r.screenshot_url)}
            me={me}
          />
        ))}
      </div>

      {/* Pagination — 15 rows per page (matches backend page_size).
          Hidden when there's only one page so the panel stays clean. */}
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

      <Dialog open={!!rejecting} onOpenChange={(v) => !v && setRejecting(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Reject deposit</DialogTitle>
          </DialogHeader>
          <Input
            placeholder="Reason (mandatory)"
            value={rejecting?.remark ?? ""}
            onChange={(e) => setRejecting((r) => (r ? { ...r, remark: e.target.value } : r))}
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setRejecting(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={reject}
              disabled={!!rejecting && busy.has(rejecting.id)}
            >
              Reject
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* User ledger drawer — opened by the per-row L button so admins
          can drill into a user's wallet transactions without leaving
          the payments queue. */}
      <LedgerSheet
        open={!!ledgerUser}
        user={ledgerUser}
        onClose={() => setLedgerUser(null)}
      />
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────────── */
/* Transaction / UTR cell                                               */
/* ─────────────────────────────────────────────────────────────────── */

/**
 * Shows the payment's transaction reference: bank UTR for UPI/BANK, or the
 * on-chain tx hash (manual crypto) / oxapay track id (gateway) for CRYPTO,
 * with the asset · network underneath. Click to copy the full value — tx
 * hashes are long and admins paste them into a block explorer to verify.
 */
function TxnCell({ r }: { r: any }) {
  const isCrypto = r.payment_mode === "CRYPTO";
  const txn = r.utr_number || r.crypto_tx_hash || r.gateway_ref || "";
  const sub = isCrypto
    ? [r.crypto_asset, r.crypto_network].filter(Boolean).join(" · ")
    : "";
  if (!txn) return <span className="text-muted-foreground">—</span>;
  return (
    <button
      type="button"
      className="flex max-w-[170px] flex-col items-start leading-tight text-left"
      title={`${txn}\n(click to copy)`}
      onClick={(e) => {
        e.stopPropagation();
        navigator.clipboard?.writeText(txn).then(
          () => toast.success("Transaction id copied"),
          () => {},
        );
      }}
    >
      <span className="w-full truncate font-mono text-[11px] hover:text-primary">{txn}</span>
      {sub && <span className="text-[9px] uppercase tracking-wide text-muted-foreground">{sub}</span>}
    </button>
  );
}

/* ─────────────────────────────────────────────────────────────────── */
/* Mobile deposit card                                                  */
/* ─────────────────────────────────────────────────────────────────── */

/**
 * Tap-friendly card view of a deposit row used on phones (<md). Lays
 * out the same data the desktop table shows but in a 3-section card:
 *   1. Header strip with status accent + amount headline
 *   2. Meta rows (user / mode / UTR / remark)
 *   3. Action footer (Approve / Reject / Proof)
 *
 * Approve / Reject only render for PENDING + when the operator has
 * write permission, exactly matching the table's behaviour so there's
 * no business-logic drift between the two views.
 */
function DepositMobileCard({
  r,
  canMutate,
  busy,
  onApprove,
  onReject,
  onPreview,
  me,
}: {
  r: any;
  canMutate: boolean;
  busy: boolean;
  onApprove: () => void;
  onReject: () => void;
  onPreview: () => void;
  me: any;
}) {
  const isPending = r.status === "PENDING";
  const isApproved = r.status === "APPROVED";
  const isRejected = r.status === "REJECTED";
  const settlement = Number(r.user_settlement_outstanding ?? 0);

  // Color the left edge of the card by status so an operator can scan
  // a long list and instantly spot PENDING vs APPROVED vs REJECTED.
  const accent = isPending
    ? "before:bg-amber-500"
    : isApproved
      ? "before:bg-emerald-500"
      : isRejected
        ? "before:bg-destructive"
        : "before:bg-muted-foreground/30";

  return (
    <div
      className={`relative overflow-hidden rounded-xl border border-border bg-gradient-to-br from-card to-card/60 p-3 pl-4 shadow-sm before:absolute before:inset-y-2 before:left-1 before:w-1 before:rounded-full ${accent}`}
    >
      {/* Top: status + when + amount */}
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
          <div className="font-tabular text-base font-bold text-emerald-600 dark:text-emerald-400">
            {formatINR(r.amount)}
          </div>
          {settlement > 0 && (
            <div
              className="mt-0.5 text-[10px] text-amber-600 dark:text-amber-400"
              title={`User has ₹${settlement.toFixed(2)} settlement on record (informational).`}
            >
              ⓘ ₹{settlement.toFixed(0)} settle
            </div>
          )}
        </div>
      </div>

      {/* Meta strip */}
      <div className="mt-2.5 grid grid-cols-2 gap-2 rounded-lg bg-muted/30 p-2 text-[11px]">
        <div className="min-w-0">
          <div className="text-[9px] uppercase tracking-wider text-muted-foreground">
            Mode
          </div>
          <div className="truncate font-medium">{r.payment_mode || "—"}</div>
        </div>
        <div className="min-w-0">
          <div className="text-[9px] uppercase tracking-wider text-muted-foreground">
            {r.payment_mode === "CRYPTO" ? "Tx" : "UTR"}
          </div>
          <TxnCell r={r} />
        </div>
        {r.user_remark && (
          <div className="col-span-2 min-w-0">
            <div className="text-[9px] uppercase tracking-wider text-muted-foreground">
              Remark
            </div>
            <div className="truncate" title={r.user_remark}>
              {r.user_remark}
            </div>
          </div>
        )}
      </div>

      {/* Footer actions */}
      <div className="mt-3 flex items-center gap-2">
        {r.screenshot_url && (
          <Button
            size="sm"
            variant="outline"
            className="h-9 flex-1"
            onClick={onPreview}
          >
            <ImageIcon className="size-4" /> Proof
          </Button>
        )}
        {isPending && (
          <>
            <Button
              size="sm"
              variant="outline"
              className="h-9 flex-1 border-emerald-500/40 text-emerald-600 hover:bg-emerald-500 hover:text-white dark:text-emerald-400"
              disabled={!canMutate || busy}
              onClick={onApprove}
              title={canMutate ? "Approve deposit" : "View-only access"}
            >
              <Check className="size-4" /> Approve
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="h-9 flex-1 border-destructive/40 text-destructive hover:bg-destructive hover:text-destructive-foreground"
              disabled={!canMutate || busy}
              onClick={onReject}
              title={canMutate ? "Reject with reason" : "View-only access"}
            >
              <X className="size-4" /> Reject
            </Button>
          </>
        )}
      </div>
    </div>
  );
}
