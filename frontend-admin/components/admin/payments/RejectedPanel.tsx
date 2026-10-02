"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Image as ImageIcon } from "lucide-react";
import { PayinOutAPI } from "@/lib/api";
import { API_URL } from "@/lib/constants";
import { Button } from "@/components/ui/button";
import { DataTable, type Column } from "@/components/common/DataTable";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { formatINR, cn } from "@/lib/utils";

type SubTab = "deposits" | "withdrawals";

export function RejectedPanel() {
  const [sub, setSub] = useState<SubTab>("deposits");
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);

  // The deposits + withdrawals endpoints are paginated and return
  // `{ items, meta }` now. Rejected sub-tab fetches a large page (100)
  // so the admin can scan everything in one screen without needing
  // a pager here — rejected lists are typically short.
  const { data: depositsResp, isFetching: depLoading } = useQuery({
    queryKey: ["admin", "deposits", "REJECTED"],
    queryFn: () => PayinOutAPI.deposits({ status: "REJECTED", page: 1, page_size: 100 }),
  });
  const { data: withdrawalsResp, isFetching: wdLoading } = useQuery({
    queryKey: ["admin", "withdrawals", "REJECTED"],
    queryFn: () => PayinOutAPI.withdrawals({ status: "REJECTED", page: 1, page_size: 100 }),
  });
  const deposits = depositsResp?.items ?? [];
  const withdrawals = withdrawalsResp?.items ?? [];

  const depositCols: Column<any>[] = [
    { key: "created_at", header: "When", render: (r) => new Date(r.created_at).toLocaleString() },
    { key: "user_id", header: "User", render: (r) => <span className="font-mono text-[11px]">{r.user_id.slice(-8)}</span> },
    { key: "amount", header: "Amount", align: "right", render: (r) => formatINR(r.amount) },
    { key: "payment_mode", header: "Mode" },
    { key: "utr_number", header: "UTR", render: (r) => r.utr_number || "—" },
    { key: "user_remark", header: "User remark", render: (r) => r.user_remark || "—", className: "max-w-[180px] truncate" },
    {
      key: "admin_remark",
      header: "Reason",
      render: (r) => <span className="text-destructive">{r.admin_remark || "—"}</span>,
      className: "max-w-[200px] truncate",
    },
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
  ];

  const withdrawalCols: Column<any>[] = [
    { key: "created_at", header: "When", render: (r) => new Date(r.created_at).toLocaleString() },
    { key: "user_id", header: "User", render: (r) => <span className="font-mono text-[11px]">{r.user_id.slice(-8)}</span> },
    { key: "amount", header: "Amount", align: "right", render: (r) => formatINR(r.amount) },
    { key: "bank_account_id", header: "To bank", render: (r) => <span className="font-mono text-[11px]">{(r.bank_account_id || "").slice(-8)}</span> },
    {
      key: "rejection_reason",
      header: "Reason",
      render: (r) => <span className="text-destructive">{r.rejection_reason || r.admin_remark || "—"}</span>,
      className: "max-w-[260px] truncate",
    },
  ];

  const isLoading = sub === "deposits" ? depLoading : wdLoading;
  const rows = sub === "deposits" ? deposits : withdrawals;
  const cols = sub === "deposits" ? depositCols : withdrawalCols;

  return (
    <div className="space-y-3">
      <div className="inline-flex rounded-md border border-border bg-muted/30 p-1 text-sm">
        {(["deposits", "withdrawals"] as SubTab[]).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setSub(t)}
            className={cn(
              "rounded px-3 py-1.5 capitalize transition-colors",
              sub === t ? "bg-primary/15 text-primary" : "text-muted-foreground hover:text-foreground"
            )}
          >
            Rejected {t} ({(t === "deposits" ? deposits : withdrawals).length})
          </button>
        ))}
      </div>

      <DataTable columns={cols} rows={rows} keyExtractor={(r) => r.id} loading={isLoading && !rows} />

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
