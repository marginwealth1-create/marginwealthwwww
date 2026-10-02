"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { BonusesAdminAPI } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

const STATUSES = ["", "ACTIVE", "COMPLETED", "CONSUMED", "EXPIRED", "CANCELLED"];

export function BonusGrantsTab() {
  const qc = useQueryClient();
  const [status, setStatus] = useState("");
  const [userInput, setUserInput] = useState("");
  const [userFilter, setUserFilter] = useState("");
  const { data } = useQuery({
    queryKey: ["bonus-grants", status, userFilter],
    queryFn: () =>
      BonusesAdminAPI.listGrants({
        status: status || undefined,
        user_id: userFilter || undefined,
        page: 1,
        limit: 100,
      }),
  });
  const [ledgerFor, setLedgerFor] = useState<any | null>(null);
  const { data: ledger } = useQuery({
    queryKey: ["bonus-ledger", ledgerFor?.id],
    queryFn: () => BonusesAdminAPI.ledger(ledgerFor.id),
    enabled: !!ledgerFor,
  });

  const cancel = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) => BonusesAdminAPI.cancel(id, reason),
    onSuccess: () => { toast.success("Bonus cancelled"); qc.invalidateQueries({ queryKey: ["bonus-grants"] }); },
    onError: (e: any) => toast.error(e?.message || "Cancel failed"),
  });
  const recompute = useMutation({
    mutationFn: (id: string) => BonusesAdminAPI.recompute(id),
    onSuccess: () => { toast.success("Recomputed from ledger"); qc.invalidateQueries({ queryKey: ["bonus-grants"] }); },
    onError: (e: any) => toast.error(e?.message || "Recompute failed"),
  });

  const rows = data?.bonuses ?? [];

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <select className="h-9 rounded-md border border-border bg-background px-2 text-sm" value={status} onChange={(e) => setStatus(e.target.value)}>
          {STATUSES.map((s) => <option key={s} value={s}>{s || "All statuses"}</option>)}
        </select>
        <Input className="h-9 w-56" placeholder="Filter by user ObjectId" value={userInput} onChange={(e) => setUserInput(e.target.value)} />
        <Button variant="outline" onClick={() => setUserFilter(userInput.trim())}>Apply</Button>
        {userFilter && <Button variant="ghost" onClick={() => { setUserInput(""); setUserFilter(""); }}>Clear</Button>}
      </div>

      <Card className="overflow-x-auto p-0">
        <table className="w-full text-sm">
          <thead className="border-b border-border text-left text-xs text-muted-foreground">
            <tr>
              <th className="p-2">User</th><th className="p-2">Bonus</th>
              <th className="p-2 text-right">Amount → Credit</th><th className="p-2">Wager</th>
              <th className="p-2">Status</th><th className="p-2"></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((b: any) => (
              <tr key={b.id} className="border-b border-border/50">
                <td className="p-2">
                  <div className="font-medium">{b.user_name || "—"}</div>
                  <div className="font-mono text-[10px] text-muted-foreground">{b.user_code || b.user_id?.slice(-8)}</div>
                </td>
                <td className="p-2">{b.template_name} <span className="text-[10px] text-muted-foreground">({b.type})</span></td>
                <td className="p-2 text-right">₹{b.original_amount} → <span className="font-semibold">₹{b.current_credit}</span></td>
                <td className="p-2">
                  {Number(b.wager_target_volume) > 0 ? (
                    <div className="min-w-[110px]">
                      <div className="h-1.5 w-full rounded bg-muted">
                        <div className="h-1.5 rounded bg-primary" style={{ width: `${Math.round((b.wager_progress_pct ?? 0) * 100)}%` }} />
                      </div>
                      <div className="mt-0.5 text-[10px] text-muted-foreground">{Math.round((b.wager_progress_pct ?? 0) * 100)}% of ₹{b.wager_target_volume}</div>
                    </div>
                  ) : <span className="text-[11px] text-muted-foreground">No wager</span>}
                </td>
                <td className="p-2">
                  <span className={
                    b.status === "ACTIVE" ? "text-amber-500" :
                    b.status === "COMPLETED" ? "text-emerald-500" : "text-muted-foreground"
                  }>{b.status}</span>
                </td>
                <td className="p-2">
                  <div className="flex justify-end gap-1">
                    <Button variant="ghost" size="sm" onClick={() => setLedgerFor(b)}>Ledger</Button>
                    <Button variant="ghost" size="sm" onClick={() => recompute.mutate(b.id)}>Recompute</Button>
                    {b.status === "ACTIVE" && (
                      <Button variant="ghost" size="sm" className="text-destructive"
                        onClick={() => { const r = prompt("Cancel reason?"); if (r) cancel.mutate({ id: b.id, reason: r }); }}>
                        Cancel
                      </Button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={6} className="p-6 text-center text-muted-foreground">No bonuses.</td></tr>}
          </tbody>
        </table>
      </Card>

      <Dialog open={!!ledgerFor} onOpenChange={(v) => !v && setLedgerFor(null)}>
        <DialogContent>
          <DialogHeader><DialogTitle>Bonus ledger — {ledgerFor?.template_name}</DialogTitle></DialogHeader>
          <div className="max-h-[60vh] space-y-1 overflow-y-auto text-sm">
            {(ledger ?? []).map((r: any) => (
              <div key={r.id} className="flex items-center justify-between border-b border-border/50 py-1">
                <span>{r.action}</span>
                <span className={Number(r.credit_delta) >= 0 ? "text-emerald-500" : "text-destructive"}>₹{r.credit_delta}</span>
              </div>
            ))}
            {(ledger ?? []).length === 0 && <div className="py-4 text-center text-muted-foreground">No entries.</div>}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
