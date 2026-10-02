"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { AlertTriangle, Check, ShieldCheck, X } from "lucide-react";
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
import { formatINR } from "@/lib/utils";
import { OwnerBadge } from "@/components/admin/OwnerBadge";
import { useAdminAuthStore } from "@/stores/authStore";
import { canEdit } from "@/lib/permissions";

/**
 * Settlement Requests panel — admin queue for users whose
 * `auto_settlement = OFF` and whose wallet has gone negative. The
 * pending row carries the live shortfall (refreshed by the backend on
 * every new debit). Approving runs the same floor-to-0 + book-to-
 * settlement_outstanding sequence the auto-mode flow would have done;
 * rejecting marks the row REJECTED and leaves the wallet negative
 * (the user stays blocked from opening new trades until a fresh
 * debit re-queues a request OR they deposit / win back into the
 * black).
 */
export function SettlementRequestsPanel() {
  const qc = useQueryClient();
  const me = useAdminAuthStore((s) => s.admin);
  // VIEW-only sub-broker / admin shouldn't see clickable buttons.
  // Backend rejects too (require_perm("deposits","write")) but the UI
  // must match so the user understands why nothing happens.
  const canMutate = canEdit(me, "deposits");

  const [status, setStatus] = useState<"PENDING" | "APPROVED" | "REJECTED" | "">(
    "PENDING",
  );
  const [rejecting, setRejecting] = useState<{ id: string; reason: string } | null>(
    null,
  );

  const { data, isFetching } = useQuery({
    queryKey: ["admin", "settlement-requests", status],
    queryFn: () => PayinOutAPI.settlementRequests(status || undefined),
    refetchInterval: 5000,
  });

  function removeLocally(id: string) {
    qc.setQueryData<any[]>(["admin", "settlement-requests", status], (prev) =>
      (prev ?? []).filter((r) => r.id !== id),
    );
  }

  async function approve(id: string, userCode: string, amount: string) {
    const ok = window.confirm(
      `Approve settlement request?\n\n` +
        `User: ${userCode}\n` +
        `Shortfall: ₹${Number(amount).toFixed(2)}\n\n` +
        `This will floor the user's available balance to ₹0 and book ` +
        `the shortfall into settlement_outstanding. The user can resume ` +
        `opening new trades immediately after.`,
    );
    if (!ok) return;
    if (status === "PENDING") removeLocally(id);
    try {
      await PayinOutAPI.approveSettlement(id);
      toast.success("Settlement approved + balance floored to ₹0");
      qc.invalidateQueries({ queryKey: ["admin", "settlement-requests"] });
      // User detail / wallet caches likely on screen elsewhere — refresh them too.
      qc.invalidateQueries({ queryKey: ["admin", "user"] });
    } catch (e: any) {
      toast.error(e.message);
      qc.invalidateQueries({ queryKey: ["admin", "settlement-requests"] });
    }
  }

  async function reject() {
    if (!rejecting) return;
    if (!rejecting.reason.trim()) {
      toast.error("Reason required");
      return;
    }
    const id = rejecting.id;
    if (status === "PENDING") removeLocally(id);
    try {
      await PayinOutAPI.rejectSettlement(id, rejecting.reason);
      toast.success("Settlement rejected — balance stays negative");
      setRejecting(null);
      qc.invalidateQueries({ queryKey: ["admin", "settlement-requests"] });
      qc.invalidateQueries({ queryKey: ["admin", "user"] });
    } catch (e: any) {
      toast.error(e.message);
      qc.invalidateQueries({ queryKey: ["admin", "settlement-requests"] });
    }
  }

  const cols: Column<any>[] = [
    {
      key: "created_at",
      header: "When",
      render: (r) => new Date(r.created_at).toLocaleString(),
    },
    {
      key: "user",
      header: "User",
      render: (r) => (
        <div className="flex flex-col leading-tight">
          <span className="font-medium">{r.user_name || r.user_id}</span>
          <span className="font-mono text-[10px] text-muted-foreground">
            {r.user_code}
          </span>
        </div>
      ),
    },
    { key: "owner", header: "Owner", render: (r) => <OwnerBadge row={r} me={me} /> },
    {
      key: "requested_amount",
      header: "Shortfall",
      align: "right",
      render: (r) => (
        <div className="flex flex-col items-end leading-tight">
          <span className="font-tabular font-bold text-amber-600 dark:text-amber-400">
            {formatINR(r.requested_amount)}
          </span>
          {r.current_available != null && (
            <span
              className="text-[10px] text-muted-foreground"
              title={`Live available_balance: ${r.current_available}`}
            >
              live: {formatINR(r.current_available)}
            </span>
          )}
        </div>
      ),
    },
    {
      key: "reason",
      header: "Reason",
      className: "max-w-[280px] truncate",
      render: (r) => (
        <span className="text-xs text-muted-foreground" title={r.narration}>
          {r.narration || "—"}
        </span>
      ),
    },
    {
      key: "status",
      header: "Status",
      render: (r) => <StatusPill status={r.status} />,
    },
    {
      key: "actions",
      header: "",
      align: "right",
      render: (r) => {
        if (r.status !== "PENDING" || !canMutate) {
          return r.rejected_reason ? (
            <span
              className="text-[10px] text-muted-foreground"
              title={r.rejected_reason}
            >
              {r.rejected_reason.length > 24
                ? `${r.rejected_reason.slice(0, 24)}…`
                : r.rejected_reason}
            </span>
          ) : (
            <span className="text-[10px] text-muted-foreground">—</span>
          );
        }
        return (
          <div className="flex justify-end gap-1.5">
            <Button
              size="sm"
              onClick={() => approve(r.id, r.user_code, r.requested_amount)}
              className="h-7 gap-1 rounded-md bg-emerald-600 px-2.5 text-xs font-semibold text-white hover:bg-emerald-700"
            >
              <Check className="size-3.5" /> Approve
            </Button>
            <Button
              size="sm"
              onClick={() => setRejecting({ id: r.id, reason: "" })}
              className="h-7 gap-1 rounded-md bg-destructive/15 px-2.5 text-xs font-semibold text-destructive ring-1 ring-inset ring-destructive/30 hover:bg-destructive hover:text-destructive-foreground"
            >
              <X className="size-3.5" /> Reject
            </Button>
          </div>
        );
      },
    },
  ];

  const rows = data ?? [];

  return (
    <div className="space-y-3">
      {/* Header strip: status filter + count */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">Status:</span>
          {(["PENDING", "APPROVED", "REJECTED", ""] as const).map((s) => (
            <button
              key={s || "ALL"}
              type="button"
              onClick={() => setStatus(s)}
              className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                status === s
                  ? "bg-primary/15 text-primary"
                  : "text-muted-foreground hover:bg-muted/50 hover:text-foreground"
              }`}
            >
              {s || "All"}
            </button>
          ))}
        </div>
        <span className="text-xs text-muted-foreground">
          {rows.length} request{rows.length === 1 ? "" : "s"}
        </span>
      </div>

      {/* Empty-state hint when nothing pending — reduces operator
          confusion about "is this tab working?" on quiet days. */}
      {!isFetching && rows.length === 0 && status === "PENDING" && (
        <div className="flex items-start gap-3 rounded-md border border-emerald-500/30 bg-emerald-500/10 p-3">
          <ShieldCheck className="size-5 shrink-0 text-emerald-600 dark:text-emerald-400" />
          <div className="text-xs">
            <div className="font-semibold text-emerald-700 dark:text-emerald-300">
              No pending settlements
            </div>
            <div className="text-muted-foreground">
              Settlement requests appear here when a user with
              auto-settlement OFF goes into negative balance. Toggle a
              user's auto-settlement from their detail page.
            </div>
          </div>
        </div>
      )}

      <DataTable
        columns={cols}
        rows={rows}
        keyExtractor={(r) => r.id}
        loading={isFetching && !data}
      />

      {/* Reject dialog — mirror DepositsPanel pattern */}
      <Dialog open={!!rejecting} onOpenChange={(o) => !o && setRejecting(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <AlertTriangle className="size-4 text-destructive" />
              Reject settlement request
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-3 text-sm">
            <p className="text-muted-foreground">
              Rejecting leaves the user's wallet at its current{" "}
              <span className="font-semibold text-destructive">negative</span>{" "}
              balance and keeps them blocked from opening new trades. The
              audit log will record your reason.
            </p>
            <Input
              autoFocus
              placeholder="Reason (required)"
              value={rejecting?.reason ?? ""}
              onChange={(e) =>
                setRejecting((p) => (p ? { ...p, reason: e.target.value } : p))
              }
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRejecting(null)}>
              Cancel
            </Button>
            <Button
              onClick={reject}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              <X className="size-4" /> Reject
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
