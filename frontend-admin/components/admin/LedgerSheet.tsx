"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Loader2, ChevronDown } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { LedgerAdminAPI, UsersAPI, ApiError } from "@/lib/api";

interface Props {
  open: boolean;
  onClose: () => void;
  user:
    | {
        id: string;
        user_code?: string;
        full_name?: string;
        wallet?: { available_balance?: string | number };
      }
    | null;
}

function formatINR(v: unknown): string {
  const n = Number(v ?? 0);
  return `₹${n.toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

const PAGE_SIZE = 25;

// A SETTLEMENT_OUTSTANDING_BOOKED row is really an UNPAID BROKERAGE charge
// (not a stop-out/close shortfall) when its narration is a "Charges for …"
// line — the ₹/trade brokerage that couldn't be debited because the wallet
// was at 0, so it was parked on settlement_outstanding. Close-loss and
// admin position-delete shortfalls read "Realized loss … — shortfall" /
// "Delete … — reverse close P&L" and stay labelled as settlement.
function isBrokerageShortfall(tt: string, narration?: string): boolean {
  return (
    tt === "SETTLEMENT_OUTSTANDING_BOOKED" &&
    String(narration ?? "").trim().toLowerCase().startsWith("charges for")
  );
}

// Human-readable label for transaction type
function txLabel(tt: string, amt: number, narration?: string): string {
  if (isBrokerageShortfall(tt, narration)) return "Brokerage charge (unpaid)";
  switch (tt) {
    case "DEPOSIT": return "Deposit";
    case "WITHDRAWAL": return "Withdrawal";
    case "ADJUSTMENT": return amt >= 0 ? "Deposit (Admin)" : "Withdrawal (Admin)";
    case "BONUS": return "Bonus (Admin)";
    case "PENALTY": return "Penalty (Admin)";
    case "PROMO": return "Promo Credit (Admin)";
    case "SETTLEMENT_OUTSTANDING_BOOKED": return "Settlement — Shortfall Booked";
    case "SETTLEMENT_OUTSTANDING_RECOVERY": return "Settlement — Shortfall Recovered";
    default: return tt;
  }
}

// Color pill for transaction type
function txColor(tt: string, amt: number, narration?: string): string {
  if (isBrokerageShortfall(tt, narration)) return "bg-rose-100 text-rose-700 border-rose-200";
  if (tt === "SETTLEMENT_OUTSTANDING_BOOKED") return "bg-orange-100 text-orange-700 border-orange-200";
  if (tt === "SETTLEMENT_OUTSTANDING_RECOVERY") return "bg-blue-100 text-blue-700 border-blue-200";
  if (amt >= 0) return "bg-green-100 text-green-700 border-green-200";
  return "bg-red-100 text-red-700 border-red-200";
}

export function LedgerSheet({ open, onClose, user }: Props) {
  const qc = useQueryClient();
  const [amount, setAmount] = useState("");
  const [narration, setNarration] = useState("");
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);

  const { data, isLoading } = useQuery({
    queryKey: ["admin", "ledger", "user", user?.id],
    queryFn: () =>
      LedgerAdminAPI.list({ user_id: user!.id, page: 1, page_size: 500 }),
    enabled: !!user && open,
  });

  const { data: liveUser } = useQuery({
    queryKey: ["admin", "user", user?.id],
    queryFn: () => UsersAPI.detail(user!.id),
    enabled: !!user && open,
  });

  const liveBalance =
    liveUser?.wallet?.available_balance ?? user?.wallet?.available_balance;

  const txns: any[] = (data?.items ?? []).filter((t: any) => {
    const tt = String(t?.transaction_type ?? "").toUpperCase();
    // Hide weekly-settlement churn + admin reopen/delete reversals — these
    // are bookkeeping rollovers, not real money movement. Genuine daily
    // SL/TP/stop-out shortfall bookings ("Realized loss … close — shortfall")
    // stay visible.
    const narration = String(t?.narration ?? "").toLowerCase();
    if (narration.includes("weekly settlement") || narration.includes("settlement unbooked")) {
      return false;
    }
    return (
      tt === "DEPOSIT" ||
      tt === "WITHDRAWAL" ||
      tt === "SETTLEMENT_OUTSTANDING_BOOKED" ||
      tt === "SETTLEMENT_OUTSTANDING_RECOVERY" ||
      tt === "ADJUSTMENT" ||
      tt === "BONUS" ||
      tt === "PENALTY" ||
      tt === "PROMO"
    );
  });

  // Totals — ADJUSTMENT positive = deposit, negative = withdrawal; settlement tracked separately
  let totalDeposits = 0;
  let totalWithdrawals = 0;
  for (const t of txns) {
    const tt = String(t?.transaction_type ?? "").toUpperCase();
    const amt = Number(t?.amount ?? 0);
    if (tt === "SETTLEMENT_OUTSTANDING_BOOKED" || tt === "SETTLEMENT_OUTSTANDING_RECOVERY") continue;
    if (amt >= 0) totalDeposits += amt;
    else totalWithdrawals += Math.abs(amt);
  }
  const net = totalDeposits - totalWithdrawals;

  const adjustMut = useMutation({
    mutationFn: ({ signedAmount, narration }: { signedAmount: number; narration: string }) =>
      UsersAPI.walletAdjust(user!.id, { amount: signedAmount, narration, transaction_type: "ADJUSTMENT" }),
    onSuccess: () => {
      toast.success("Wallet adjusted");
      setAmount("");
      setNarration("");
      qc.invalidateQueries({ queryKey: ["admin", "users"] });
      qc.invalidateQueries({ queryKey: ["admin", "ledger", "user", user!.id] });
      qc.invalidateQueries({ queryKey: ["admin", "user", user!.id] });
    },
    onError: (e: unknown) => {
      const msg = e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Adjustment failed";
      toast.error(msg);
    },
  });

  const submitAdjust = (direction: "add" | "deduct") => {
    const n = Number(amount);
    if (!Number.isFinite(n) || n <= 0) { toast.error("Amount must be a positive number"); return; }
    if (!narration.trim()) { toast.error("Narration is required"); return; }
    adjustMut.mutate({ signedAmount: direction === "add" ? n : -n, narration: narration.trim() });
  };

  const visibleTxns = txns.slice(0, visibleCount);
  const hasMore = txns.length > visibleCount;

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) { onClose(); setVisibleCount(PAGE_SIZE); } }}>
      <DialogContent className="w-[95vw] max-w-2xl max-h-[90vh] overflow-y-auto rounded-xl">
        <DialogHeader>
          <DialogTitle>Ledger — {user?.full_name || user?.user_code || ""}</DialogTitle>
          <DialogDescription>Wallet transactions and fund adjustments</DialogDescription>
        </DialogHeader>

        {/* Balance + adjust */}
        <div className="rounded-xl border border-border bg-card p-4 space-y-3">
          <div>
            <div className="text-xs text-muted-foreground uppercase tracking-wider">Available Balance</div>
            <div className="font-mono text-xl sm:text-2xl font-bold mt-1">{formatINR(liveBalance)}</div>
          </div>
          <div className="grid grid-cols-1 gap-2">
            <div>
              <Label htmlFor="adjust-amount">Amount (₹)</Label>
              <Input id="adjust-amount" type="number" step="0.01" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="0.00" />
            </div>
            <div>
              <Label htmlFor="adjust-narration">Narration</Label>
              <Input id="adjust-narration" value={narration} onChange={(e) => setNarration(e.target.value)} placeholder="Reason for adjustment" />
            </div>
            <div className="flex gap-2">
              <Button className="flex-1" onClick={() => submitAdjust("add")} disabled={adjustMut.isPending}>
                {adjustMut.isPending ? <Loader2 className="size-4 animate-spin" /> : "Add Fund"}
              </Button>
              <Button className="flex-1" variant="outline" onClick={() => submitAdjust("deduct")} disabled={adjustMut.isPending}>
                Deduct Fund
              </Button>
            </div>
          </div>
        </div>

        {/* Summary tiles */}
        <div className="mt-3 space-y-2">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-base">📊</span>
            <span className="font-semibold text-sm">Ledger Summary</span>
          </div>
          <div className="grid grid-cols-1 xs:grid-cols-3 sm:grid-cols-3 gap-2">
            <div className="rounded-lg border border-green-200 bg-green-50 p-2.5 sm:p-3 flex sm:flex-col items-center sm:justify-center justify-between">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Total Deposits</div>
              <div className="font-mono text-sm sm:text-base font-bold text-green-700 sm:mt-1">{formatINR(totalDeposits)}</div>
              <div className="hidden sm:block text-[9px] text-muted-foreground mt-0.5">incl. admin adds</div>
            </div>
            <div className="rounded-lg border border-red-200 bg-red-50 p-2.5 sm:p-3 flex sm:flex-col items-center sm:justify-center justify-between">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Total Withdrawals</div>
              <div className="font-mono text-sm sm:text-base font-bold text-red-600 sm:mt-1">{formatINR(totalWithdrawals)}</div>
              <div className="hidden sm:block text-[9px] text-muted-foreground mt-0.5">incl. admin deducts</div>
            </div>
            <div className="rounded-lg border border-blue-200 bg-blue-50 p-2.5 sm:p-3 flex sm:flex-col items-center sm:justify-center justify-between">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Net Cash Flow</div>
              <div className={`font-mono text-sm sm:text-base font-bold sm:mt-1 ${net >= 0 ? "text-green-700" : "text-red-600"}`}>{formatINR(net)}</div>
              <div className="hidden sm:block text-[9px] text-muted-foreground mt-0.5">deposits − withdrawals</div>
            </div>
          </div>

        </div>

        {/* Transactions list */}
        <div className="mt-4">
          <div className="flex items-center justify-between mb-2">
            <div className="text-xs text-muted-foreground uppercase tracking-wider">
              All Transactions ({txns.length})
            </div>
          </div>
          {isLoading ? (
            <div className="text-sm text-muted-foreground">Loading...</div>
          ) : txns.length === 0 ? (
            <div className="text-sm text-muted-foreground">No transactions yet.</div>
          ) : (
            <>
              <div className="space-y-1">
                {visibleTxns.map((t: any) => {
                  const amt = Number(t.amount ?? 0);
                  const tt = String(t?.transaction_type ?? "").toUpperCase();
                  return (
                    <div key={t.id} className="flex items-center justify-between rounded-md border border-border p-2.5 text-sm bg-card hover:bg-muted/30 transition-colors">
                      <div className="flex flex-col leading-tight min-w-0 gap-0.5">
                        <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border w-fit ${txColor(tt, amt, t.narration)}`}>
                          {txLabel(tt, amt, t.narration)}
                        </span>
                        <span className="text-xs text-muted-foreground truncate max-w-[200px] sm:max-w-[300px]">{t.narration || "—"}</span>
                        <span className="text-[10px] text-muted-foreground">{t.created_at ? new Date(t.created_at).toLocaleString() : "—"}</span>
                      </div>
                      <div className="flex flex-col items-end shrink-0 ml-3">
                        <span className={`font-mono font-semibold text-sm ${amt >= 0 ? "text-green-600" : "text-red-500"}`}>
                          {amt >= 0 ? "+" : ""}{formatINR(amt)}
                        </span>
                        {t.balance_after != null && (
                          <span className="text-[10px] text-muted-foreground">bal {formatINR(t.balance_after)}</span>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
              {hasMore && (
                <button
                  onClick={() => setVisibleCount((c) => c + PAGE_SIZE)}
                  className="mt-3 w-full flex items-center justify-center gap-1.5 rounded-md border border-border py-2 text-xs text-muted-foreground hover:bg-muted/40 hover:text-foreground transition-colors"
                >
                  <ChevronDown className="size-3.5" />
                  Load more {Math.min(PAGE_SIZE, txns.length - visibleCount)} entries
                  <span className="ml-1 text-[10px] opacity-60">({txns.length - visibleCount} remaining)</span>
                </button>
              )}
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
