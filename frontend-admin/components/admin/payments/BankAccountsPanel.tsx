"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Edit2, Plus, QrCode, Trash2 } from "lucide-react";
import { PayinOutAPI } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import { UpiQR } from "@/components/common/UpiQR";
import { useAdminAuthStore } from "@/stores/authStore";
import { canEdit } from "@/lib/permissions";

type Bank = {
  id?: string;
  bank_name: string;
  account_holder: string;
  account_number: string;
  ifsc_code: string;
  upi_id?: string;
  qr_code_url?: string;  // legacy: only used as fallback if UPI ID is missing
  is_active: boolean;
  is_default: boolean;
  // Broker-only: false ⇒ inherited from parent admin's pool. Frontend
  // renders an "Inherited" badge and disables edit/delete on those rows.
  // True / undefined (legacy responses) ⇒ owned by caller, fully editable.
  editable?: boolean;
};

const EMPTY: Bank = {
  bank_name: "",
  account_holder: "",
  account_number: "",
  ifsc_code: "",
  upi_id: "",
  is_active: true,
  is_default: false,
};

export function BankAccountsPanel() {
  const qc = useQueryClient();
  const admin = useAdminAuthStore((s) => s.admin);
  // Caller's write capability. SUPER_ADMIN / admin-with-banks: full edit.
  // Broker: only if banks==EDIT — VIEW renders the list with mutation
  // buttons disabled, EDIT enables them, OFF hides the whole tab via
  // payments page gating.
  const canWriteBanks = canEdit(admin, "banks");
  const { data: banks, isFetching } = useQuery({
    queryKey: ["admin", "bank-accounts"],
    queryFn: () => PayinOutAPI.bankAccounts(),
  });
  const [editing, setEditing] = useState<Bank | null>(null);
  const [previewUpi, setPreviewUpi] = useState<{ upiId: string; payee?: string } | null>(null);
  const [saving, setSaving] = useState(false);

  function open(b?: Bank) {
    setEditing(b ? { ...b } : { ...EMPTY });
  }

  async function save() {
    if (!editing) return;
    if (!editing.bank_name.trim() || !editing.account_holder.trim() || !editing.account_number.trim() || !editing.ifsc_code.trim()) {
      toast.error("Bank name, holder, account no. and IFSC are required");
      return;
    }
    setSaving(true);
    try {
      // Send ONLY the fields the backend's create/update schema
      // accepts. `editable` and `id` are response-only / not part of
      // the payload — leaving them in the body could trip strict
      // validation and surface as a generic "Network Error" toast on
      // some setups (the user-flagged symptom from the broker side).
      const payload = {
        bank_name: editing.bank_name.trim(),
        account_holder: editing.account_holder.trim(),
        account_number: editing.account_number.trim(),
        ifsc_code: editing.ifsc_code.trim(),
        upi_id: editing.upi_id?.trim() || undefined,
        qr_code_url: editing.qr_code_url || undefined,
        is_active: editing.is_active,
        is_default: editing.is_default,
      };
      if (editing.id) {
        await PayinOutAPI.updateBank(editing.id, payload);
        toast.success("Bank updated");
      } else {
        await PayinOutAPI.createBank(payload);
        toast.success("Bank added");
      }
      setEditing(null);
      qc.invalidateQueries({ queryKey: ["admin", "bank-accounts"] });
    } catch (e: any) {
      // Surface the REAL backend error so the user can see permission
      // / validation problems instead of a vague "Save failed". Axios
      // sometimes flattens 4xx into `e.message = "Network Error"` if
      // the response is small / lacks Content-Type — checking
      // `response.data.detail` first catches FastAPI HTTPException
      // payloads cleanly.
      const detail =
        e?.response?.data?.error?.message ??
        e?.response?.data?.detail ??
        e?.message ??
        "Save failed";
      toast.error(String(detail));
    } finally {
      setSaving(false);
    }
  }

  async function remove(b: Bank) {
    if (!b.id) return;
    if (!confirm(`Delete ${b.bank_name} (${b.account_number})? Users will no longer see it.`)) return;
    try {
      await PayinOutAPI.deleteBank(b.id);
      toast.success("Deleted");
      qc.invalidateQueries({ queryKey: ["admin", "bank-accounts"] });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-xs text-muted-foreground">
          {banks?.length ?? 0} payment method{(banks?.length ?? 0) === 1 ? "" : "s"} — visible to users on the deposit form.
        </div>
        <Button
          onClick={() => open()}
          disabled={!canWriteBanks}
          title={!canWriteBanks ? "View-only access — Edit permission required" : undefined}
        >
          <Plus className="size-4" /> Add bank / UPI
        </Button>
      </div>

      {isFetching && !banks ? (
        <div className="text-sm text-muted-foreground">Loading…</div>
      ) : (banks?.length ?? 0) === 0 ? (
        <div className="rounded-md border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
          No payment methods yet. Add a bank account, UPI ID, or QR so users can deposit.
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
          {banks!.map((b: Bank) => (
            <div
              key={b.id}
              className={cn(
                "relative space-y-2 rounded-lg border p-4",
                b.is_active ? "border-border bg-card" : "border-border/50 bg-muted/20 opacity-60"
              )}
            >
              <div className="flex items-start justify-between gap-2">
                <div>
                  <div className="font-medium">{b.bank_name}</div>
                  <div className="text-xs text-muted-foreground">{b.account_holder}</div>
                </div>
                <div className="flex flex-wrap justify-end gap-1">
                  {b.editable === false && (
                    <span
                      className="rounded-full bg-brand-500/15 px-2 py-0.5 text-[10px] text-brand-500"
                      title="From your admin's pool — read-only here. Add your own banks below to override for your users."
                    >
                      INHERITED
                    </span>
                  )}
                  {b.is_default && (
                    <span className="rounded-full bg-primary/15 px-2 py-0.5 text-[10px] text-primary">DEFAULT</span>
                  )}
                  {!b.is_active && (
                    <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">DISABLED</span>
                  )}
                </div>
              </div>
              <div className="space-y-0.5 text-xs">
                <div className="font-mono">A/C: {b.account_number}</div>
                <div className="font-mono">IFSC: {b.ifsc_code}</div>
                {b.upi_id && <div className="font-mono text-primary">UPI: {b.upi_id}</div>}
              </div>
              {b.upi_id && (
                <button
                  type="button"
                  onClick={() => setPreviewUpi({ upiId: b.upi_id!, payee: b.account_holder })}
                  className="inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground"
                >
                  <QrCode className="size-3" /> View QR
                </button>
              )}
              <div className="flex justify-end gap-1 pt-1">
                {(() => {
                  // Inherited admin bank → never editable here, regardless
                  // of broker's banks permission level. Broker must manage
                  // their own pool only; touching the admin's pool would
                  // affect siblings/other-broker users.
                  const rowEditable = b.editable !== false && canWriteBanks;
                  const tip = !rowEditable
                    ? b.editable === false
                      ? "Inherited from admin — edit from admin account"
                      : "View-only access"
                    : undefined;
                  return (
                    <>
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => open(b)}
                        aria-label="Edit"
                        disabled={!rowEditable}
                        title={tip}
                      >
                        <Edit2 className="size-3.5" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => remove(b)}
                        aria-label="Delete"
                        disabled={!rowEditable}
                        title={tip}
                      >
                        <Trash2 className="size-3.5 text-destructive" />
                      </Button>
                    </>
                  );
                })()}
              </div>
            </div>
          ))}
        </div>
      )}

      <Dialog open={!!editing} onOpenChange={(v) => !v && setEditing(null)}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{editing?.id ? "Edit payment method" : "Add payment method"}</DialogTitle>
          </DialogHeader>
          {editing && (
            <div className="grid grid-cols-2 gap-3 text-sm">
              <Field label="Bank name *">
                <Input value={editing.bank_name} onChange={(e) => setEditing({ ...editing, bank_name: e.target.value })} />
              </Field>
              <Field label="Account holder *">
                <Input
                  value={editing.account_holder}
                  onChange={(e) => setEditing({ ...editing, account_holder: e.target.value })}
                />
              </Field>
              <Field label="Account number *">
                <Input
                  value={editing.account_number}
                  onChange={(e) => setEditing({ ...editing, account_number: e.target.value })}
                />
              </Field>
              <Field label="IFSC code *">
                <Input
                  value={editing.ifsc_code}
                  onChange={(e) => setEditing({ ...editing, ifsc_code: e.target.value.toUpperCase() })}
                  className="uppercase"
                />
              </Field>
              <Field label="UPI ID">
                <Input
                  value={editing.upi_id ?? ""}
                  onChange={(e) => setEditing({ ...editing, upi_id: e.target.value })}
                  placeholder="merchant@bank"
                />
              </Field>
              <div className="space-y-1.5">
                <Label className="text-xs">Auto-generated QR</Label>
                <div className="flex items-center gap-3 rounded-md border border-border bg-muted/10 p-2">
                  <UpiQR upiId={editing.upi_id} payeeName={editing.account_holder} size={128} />
                  <div className="text-[10px] text-muted-foreground">
                    {editing.upi_id?.trim()
                      ? "QR is generated from the UPI ID. Users will see this exact image — no upload needed."
                      : "Enter a UPI ID — QR will appear here."}
                  </div>
                </div>
              </div>
              <label className="col-span-2 flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={editing.is_active}
                  onChange={(e) => setEditing({ ...editing, is_active: e.target.checked })}
                  className="size-4 accent-primary"
                />
                Active (visible to users)
              </label>
              <label className="col-span-2 flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={editing.is_default}
                  onChange={(e) => setEditing({ ...editing, is_default: e.target.checked })}
                  className="size-4 accent-primary"
                />
                Make this the default (shown first to users)
              </label>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditing(null)}>
              Cancel
            </Button>
            <Button onClick={save} loading={saving}>
              {editing?.id ? "Update" : "Add"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!!previewUpi} onOpenChange={(v) => !v && setPreviewUpi(null)}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>UPI QR Code</DialogTitle>
            <DialogDescription>{previewUpi?.upiId}</DialogDescription>
          </DialogHeader>
          {previewUpi && (
            <div className="flex justify-center pb-2">
              <UpiQR upiId={previewUpi.upiId} payeeName={previewUpi.payee} size={256} />
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <Label className="text-xs">{label}</Label>
      {children}
    </div>
  );
}
