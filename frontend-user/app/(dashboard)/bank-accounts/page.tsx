"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  Building2,
  CreditCard,
  Loader2,
  Plus,
  Smartphone,
  Star,
  Trash2,
} from "lucide-react";
import { WalletAPI, type PayoutMethod } from "@/lib/api";
import { PageHeader } from "@/components/common/PageHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/**
 * Manage saved withdrawal destinations — multiple bank accounts + UPI ids,
 * one primary per channel. At withdrawal the user just picks a saved method
 * instead of re-typing. Reached from Profile → Bank accounts.
 */
export default function BankAccountsPage() {
  const qc = useQueryClient();
  const { data: methods, isLoading } = useQuery({
    queryKey: ["my-banks"],
    queryFn: () => WalletAPI.myBankAccounts(),
  });
  const { data: wdRules } = useQuery({
    queryKey: ["user", "wd-rules"],
    queryFn: () => WalletAPI.wdRules(),
  });
  const upiAllowed = wdRules?.withdrawal?.allow_upi_payout !== false;

  const [addOpen, setAddOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [form, setForm] = useState({
    type: "UPI" as "UPI" | "BANK",
    upi_id: "",
    bank_name: "",
    account_holder: "",
    account_number: "",
    ifsc_code: "",
  });

  const list = methods ?? [];

  async function save() {
    if (saving) return;
    const body: Record<string, string> = { method_type: form.type };
    if (form.type === "UPI") {
      const v = form.upi_id.trim();
      if (!v.includes("@")) return toast.error("Enter a valid UPI id (e.g. name@bank)");
      body.upi_id = v;
    } else {
      if (!form.account_holder.trim()) return toast.error("Account holder name required");
      if (!form.account_number.trim()) return toast.error("Account number required");
      if (!form.ifsc_code.trim()) return toast.error("IFSC required");
      body.bank_name = form.bank_name.trim();
      body.account_holder = form.account_holder.trim();
      body.account_number = form.account_number.trim();
      body.ifsc_code = form.ifsc_code.trim().toUpperCase();
    }
    setSaving(true);
    try {
      await WalletAPI.addBankAccount(body);
      toast.success(form.type === "UPI" ? "UPI added" : "Bank account added");
      setAddOpen(false);
      setForm({ type: upiAllowed ? "UPI" : "BANK", upi_id: "", bank_name: "", account_holder: "", account_number: "", ifsc_code: "" });
      qc.invalidateQueries({ queryKey: ["my-banks"] });
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setSaving(false);
    }
  }

  async function makePrimary(id: string) {
    setBusyId(id);
    try {
      await WalletAPI.setPrimaryBankAccount(id);
      await qc.invalidateQueries({ queryKey: ["my-banks"] });
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setBusyId(null);
    }
  }

  async function remove(id: string) {
    if (!window.confirm("Remove this payout method?")) return;
    setBusyId(id);
    try {
      await WalletAPI.deleteBankAccount(id);
      await qc.invalidateQueries({ queryKey: ["my-banks"] });
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="space-y-4 pb-4">
      <PageHeader
        back
        backHref="/profile"
        title="Bank accounts"
        description="Your saved withdrawal destinations"
      />

      {!upiAllowed && (
        <p className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-600 dark:text-amber-400">
          UPI withdrawals are disabled by your broker — withdrawals go to a bank account.
        </p>
      )}

      {!addOpen ? (
        <Button
          onClick={() => {
            setForm((f) => ({ ...f, type: upiAllowed ? "UPI" : "BANK" }));
            setAddOpen(true);
          }}
          className="w-full"
        >
          <Plus className="size-4" /> Add {upiAllowed ? "bank / UPI" : "bank account"}
        </Button>
      ) : (
        <div className="space-y-3 rounded-xl border border-border bg-card p-4">
          <div className="flex items-center justify-between">
            <span className="text-sm font-semibold">Add payout method</span>
            <button
              onClick={() => setAddOpen(false)}
              className="text-xs text-muted-foreground hover:text-foreground"
            >
              Cancel
            </button>
          </div>
          {upiAllowed && (
            <div className="flex gap-2">
              {(["UPI", "BANK"] as const).map((t) => (
                <button
                  key={t}
                  onClick={() => setForm((f) => ({ ...f, type: t }))}
                  className={`flex-1 rounded-md border px-2 py-2 text-xs font-semibold ${
                    form.type === t
                      ? "border-primary bg-primary/10 text-primary"
                      : "border-border text-muted-foreground"
                  }`}
                >
                  {t === "UPI" ? "UPI ID" : "Bank account"}
                </button>
              ))}
            </div>
          )}
          {form.type === "UPI" ? (
            <Input
              placeholder="name@bank"
              value={form.upi_id}
              onChange={(e) => setForm((f) => ({ ...f, upi_id: e.target.value }))}
            />
          ) : (
            <div className="space-y-2">
              <Input placeholder="Account holder name" value={form.account_holder} onChange={(e) => setForm((f) => ({ ...f, account_holder: e.target.value }))} />
              <Input placeholder="Account number" value={form.account_number} onChange={(e) => setForm((f) => ({ ...f, account_number: e.target.value }))} />
              <Input placeholder="IFSC code" className="uppercase" maxLength={11} value={form.ifsc_code} onChange={(e) => setForm((f) => ({ ...f, ifsc_code: e.target.value.toUpperCase() }))} />
              <Input placeholder="Bank name (optional)" value={form.bank_name} onChange={(e) => setForm((f) => ({ ...f, bank_name: e.target.value }))} />
            </div>
          )}
          <Button onClick={save} disabled={saving} className="w-full">
            {saving ? "Saving…" : "Save"}
          </Button>
        </div>
      )}

      {isLoading ? (
        <div className="rounded-xl border border-border bg-card p-6 text-center text-sm text-muted-foreground">
          Loading…
        </div>
      ) : list.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border bg-card p-8 text-center">
          <CreditCard className="mx-auto size-8 text-muted-foreground/50" />
          <p className="mt-2 text-sm font-medium">No saved accounts yet</p>
          <p className="text-xs text-muted-foreground">
            Add a bank account or UPI to withdraw your funds.
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {list.map((m) => (
            <MethodCard
              key={m.id}
              m={m}
              busy={busyId === m.id}
              onPrimary={() => makePrimary(m.id)}
              onRemove={() => remove(m.id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function MethodCard({
  m,
  busy,
  onPrimary,
  onRemove,
}: {
  m: PayoutMethod;
  busy: boolean;
  onPrimary: () => void;
  onRemove: () => void;
}) {
  const isUpi = m.method_type === "UPI";
  const title = isUpi
    ? m.upi_id || "UPI"
    : `${m.bank_name || "Bank"} ••${(m.account_number || "").slice(-4)}`;
  const sub = isUpi
    ? m.account_holder || "UPI"
    : [m.account_holder, m.ifsc_code].filter(Boolean).join(" · ");
  return (
    <div className="flex items-center gap-3 rounded-xl border border-border bg-card p-3.5">
      <div
        className={`grid size-10 shrink-0 place-items-center rounded-lg ${
          isUpi
            ? "bg-primary/10 text-primary"
            : "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
        }`}
      >
        {isUpi ? <Smartphone className="size-5" /> : <Building2 className="size-5" />}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="truncate text-sm font-semibold">{title}</span>
          <span className="rounded bg-muted px-1 text-[9px] uppercase text-muted-foreground">
            {m.method_type}
          </span>
          {m.is_default && (
            <span className="inline-flex items-center gap-0.5 rounded bg-primary/10 px-1 text-[9px] font-semibold uppercase text-primary">
              <Star className="size-2.5 fill-primary" /> Primary
            </span>
          )}
        </div>
        <p className="truncate text-xs text-muted-foreground">{sub}</p>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        {!m.is_default && (
          <button
            onClick={onPrimary}
            disabled={busy}
            title="Set as primary"
            className="rounded-md border border-border px-2 py-1 text-[11px] font-medium text-muted-foreground hover:border-primary hover:text-primary disabled:opacity-50"
          >
            Set primary
          </button>
        )}
        <button
          onClick={onRemove}
          disabled={busy}
          title="Remove"
          className="rounded-md p-1.5 text-muted-foreground hover:bg-red-500/10 hover:text-red-500 disabled:opacity-50"
        >
          {busy ? <Loader2 className="size-4 animate-spin" /> : <Trash2 className="size-4" />}
        </button>
      </div>
    </div>
  );
}
