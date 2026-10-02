"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Plus, Trash2 } from "lucide-react";
import { PayinOutAPI } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { PageHeader } from "@/components/common/PageHeader";
import { WdRulesPanel } from "@/components/admin/payments/WdRulesPanel";
import { cn } from "@/lib/utils";

export default function AdminAccountsPage() {
  const qc = useQueryClient();
  const { data: banks } = useQuery({ queryKey: ["admin", "banks"], queryFn: () => PayinOutAPI.bankAccounts() });

  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState({
    bank_name: "",
    account_holder: "",
    account_number: "",
    ifsc_code: "",
    upi_id: "",
    is_active: true,
    is_default: false,
  });

  async function add() {
    try {
      await PayinOutAPI.createBank(form);
      toast.success("Added");
      setAdding(false);
      qc.invalidateQueries({ queryKey: ["admin", "banks"] });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function remove(id: string) {
    if (!confirm("Delete this company bank?")) return;
    try {
      await PayinOutAPI.deleteBank(id);
      toast.success("Deleted");
      qc.invalidateQueries({ queryKey: ["admin", "banks"] });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Company accounts & W/D rules"
        description="Bank accounts where users deposit + minimum/maximum/auto-approve rules."
        actions={
          <Dialog open={adding} onOpenChange={setAdding}>
            <DialogTrigger asChild>
              <Button>
                <Plus className="size-4" /> Add bank
              </Button>
            </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>Add company bank</DialogTitle>
              </DialogHeader>
              <div className="space-y-3">
                {(["bank_name", "account_holder", "account_number", "ifsc_code", "upi_id"] as const).map((k) => (
                  <div key={k} className="space-y-1.5">
                    <Label className="capitalize">{k.replace("_", " ")}</Label>
                    <Input
                      value={(form as any)[k]}
                      onChange={(e) => setForm((f) => ({ ...f, [k]: e.target.value }))}
                    />
                  </div>
                ))}
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={form.is_default}
                    onChange={(e) => setForm((f) => ({ ...f, is_default: e.target.checked }))}
                    className="size-4 accent-primary"
                  />
                  Default
                </label>
              </div>
              <DialogFooter>
                <Button variant="outline" onClick={() => setAdding(false)}>
                  Cancel
                </Button>
                <Button onClick={add}>Add</Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        }
      />

      <section className="space-y-2">
        <h2 className="text-sm uppercase tracking-wider text-muted-foreground">Company bank accounts</h2>
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          {banks?.map((b: any) => (
            // Subtle gradient + tinted ring per default/secondary tier
            // so default banks pop visually. Default first (sorted
            // server-side already) so admins see the primary at top.
            <Card
              key={b.id}
              className={cn(
                "relative overflow-hidden border-0 shadow-sm ring-1 transition-shadow hover:shadow-md",
                b.is_default
                  ? "bg-gradient-to-br from-emerald-50 via-card to-card ring-emerald-500/30 dark:from-emerald-500/10"
                  : "bg-card ring-border",
              )}
            >
              <CardHeader className="flex flex-row items-start justify-between gap-2 p-3 pb-2 sm:p-4">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <CardTitle className="truncate text-sm font-semibold sm:text-base" title={b.bank_name}>
                      {b.bank_name}
                    </CardTitle>
                    {b.is_default && (
                      <span className="inline-flex shrink-0 items-center rounded-full bg-emerald-500/15 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-emerald-600 dark:text-emerald-400">
                        Default
                      </span>
                    )}
                  </div>
                  <CardDescription className="mt-1 text-[11px] leading-snug">
                    <span className="truncate">{b.account_holder}</span>
                  </CardDescription>
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={() => remove(b.id)}
                  aria-label="Delete"
                  className="size-8 shrink-0"
                >
                  <Trash2 className="size-4 text-destructive" />
                </Button>
              </CardHeader>
              <CardContent className="space-y-1.5 p-3 pt-0 text-[11px] sm:p-4 sm:pt-0">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[9px] uppercase tracking-wider text-muted-foreground">A/C</span>
                  <span className="truncate font-mono" title={b.account_number}>{b.account_number || "—"}</span>
                </div>
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[9px] uppercase tracking-wider text-muted-foreground">IFSC</span>
                  <span className="truncate font-mono uppercase" title={b.ifsc_code}>{b.ifsc_code || "—"}</span>
                </div>
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[9px] uppercase tracking-wider text-muted-foreground">UPI</span>
                  <span className="truncate" title={b.upi_id || "—"}>{b.upi_id || "—"}</span>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      </section>

      <section className="space-y-2">
        <h2 className="text-sm uppercase tracking-wider text-muted-foreground">
          Deposit / withdrawal rules
        </h2>
        <WdRulesPanel />
      </section>
    </div>
  );
}
