"use client";

import { Suspense, useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Plus, X as XIcon } from "lucide-react";
import { LedgerAdminAPI, UsersAPI } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { PageHeader } from "@/components/common/PageHeader";
import { DataTable, type Column } from "@/components/common/DataTable";
import { Pagination } from "@/components/common/Pagination";
import { StatusPill } from "@/components/common/StatusPill";
import { formatINR, pnlColor } from "@/lib/utils";

export default function MasterLedgerPage() {
  return (
    <Suspense fallback={null}>
      <MasterLedgerInner />
    </Suspense>
  );
}

function MasterLedgerInner() {
  const qc = useQueryClient();
  const searchParams = useSearchParams();
  const queryUserId = searchParams?.get("user_id") ?? null;
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [type, setType] = useState("");

  // Reset to page 1 when switching to a different user filter, otherwise an
  // admin landing here from a user link can hit an empty page if their
  // previous view was deep into another user's ledger.
  useEffect(() => {
    setPage(1);
  }, [queryUserId]);

  const { data: scopedUser } = useQuery({
    queryKey: ["admin", "user", queryUserId],
    queryFn: () => UsersAPI.detail(queryUserId!),
    enabled: !!queryUserId,
    staleTime: 5 * 60_000,
  });

  const { data, isFetching } = useQuery({
    queryKey: ["admin", "ledger", { type, page, pageSize, queryUserId }],
    queryFn: () =>
      LedgerAdminAPI.list({
        transaction_type: type || undefined,
        user_id: queryUserId || undefined,
        page,
        page_size: pageSize,
      }),
  });

  const total = data?.meta?.total ?? 0;

  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ user_id: "", amount: "", transaction_type: "ADJUSTMENT", narration: "" });

  async function create() {
    if (!form.user_id || !form.amount || !form.narration) {
      toast.error("All fields required");
      return;
    }
    try {
      await LedgerAdminAPI.manualEntry({ ...form, amount: Number(form.amount) });
      toast.success("Manual entry posted");
      setCreating(false);
      setForm({ user_id: "", amount: "", transaction_type: "ADJUSTMENT", narration: "" });
      qc.invalidateQueries({ queryKey: ["admin", "ledger"] });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  const cols: Column<any>[] = [
    { key: "created_at", header: "When", render: (r) => new Date(r.created_at).toLocaleString() },
    { key: "user_code", header: "User" },
    { key: "transaction_type", header: "Type", render: (r) => <StatusPill status={r.transaction_type} /> },
    { key: "narration", header: "Narration", className: "max-w-[300px] truncate" },
    {
      key: "amount",
      header: "Amount",
      align: "right",
      render: (r) => <span className={pnlColor(r.amount)}>{formatINR(r.amount)}</span>,
    },
    { key: "balance_after", header: "Balance", align: "right", render: (r) => formatINR(r.balance_after) },
  ];

  return (
    <div className="space-y-4">
      {queryUserId && (
        <div className="mb-3 inline-flex items-center gap-2 rounded-md border border-primary/40 bg-primary/10 px-3 py-1.5 text-xs">
          <span className="text-muted-foreground">Filtered by user:</span>
          <span className="font-semibold text-primary">
            {(scopedUser as any)?.user_code ?? queryUserId.slice(-8)}
            {(scopedUser as any)?.full_name ? ` · ${(scopedUser as any).full_name}` : ""}
          </span>
          <Link
            href="/ledger"
            className="grid size-5 place-items-center rounded text-muted-foreground hover:bg-muted/40 hover:text-foreground"
            aria-label="Clear user filter"
          >
            <XIcon className="size-3" />
          </Link>
        </div>
      )}

      <PageHeader
        title="Master ledger"
        description={`${data?.meta?.total ?? 0} ledger entries`}
        actions={
          <div className="flex gap-2">
            <select
              value={type}
              onChange={(e) => {
                setPage(1);
                setType(e.target.value);
              }}
              className="h-10 rounded-md border border-border bg-background px-3 text-sm"
            >
              <option value="">All types</option>
              <option value="DEPOSIT">Deposit</option>
              <option value="WITHDRAWAL">Withdrawal</option>
              <option value="TRADE">Trade</option>
              <option value="BROKERAGE">Brokerage</option>
              <option value="CHARGES">Charges</option>
              <option value="ADJUSTMENT">Adjustment</option>
              <option value="BONUS">Bonus</option>
              <option value="PENALTY">Penalty</option>
            </select>
            <Dialog open={creating} onOpenChange={setCreating}>
              <DialogTrigger asChild>
                <Button>
                  <Plus className="size-4" /> Manual entry
                </Button>
              </DialogTrigger>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>Manual ledger entry</DialogTitle>
                </DialogHeader>
                <div className="space-y-3">
                  <div className="space-y-1.5">
                    <Label>User ID</Label>
                    <Input
                      value={form.user_id}
                      onChange={(e) => setForm((f) => ({ ...f, user_id: e.target.value }))}
                      placeholder="User Mongo ObjectId"
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label>Amount (negative to debit)</Label>
                    <Input
                      type="number"
                      step="0.01"
                      value={form.amount}
                      onChange={(e) => setForm((f) => ({ ...f, amount: e.target.value }))}
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label>Type</Label>
                    <select
                      value={form.transaction_type}
                      onChange={(e) => setForm((f) => ({ ...f, transaction_type: e.target.value }))}
                      className="h-10 w-full rounded-md border border-border bg-background px-3 text-sm"
                    >
                      <option value="ADJUSTMENT">Adjustment</option>
                      <option value="BONUS">Bonus</option>
                      <option value="PENALTY">Penalty</option>
                      <option value="PROMO">Promo</option>
                      <option value="REVERSAL">Reversal</option>
                    </select>
                  </div>
                  <div className="space-y-1.5">
                    <Label>Reason</Label>
                    <Input
                      value={form.narration}
                      onChange={(e) => setForm((f) => ({ ...f, narration: e.target.value }))}
                      placeholder="Mandatory reason / audit trail"
                    />
                  </div>
                </div>
                <DialogFooter>
                  <Button variant="outline" onClick={() => setCreating(false)}>
                    Cancel
                  </Button>
                  <Button onClick={create}>Post entry</Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>
          </div>
        }
      />
      <DataTable columns={cols} rows={data?.items} keyExtractor={(r) => r.id} loading={isFetching && !data} />

      <Pagination
        page={page}
        pageSize={pageSize}
        total={total}
        onPageChange={setPage}
        onPageSizeChange={setPageSize}
        pageSizeOptions={[50, 100, 200]}
      />
    </div>
  );
}
