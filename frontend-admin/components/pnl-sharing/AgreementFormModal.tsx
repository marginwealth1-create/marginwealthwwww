"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import type {
  AgreementDTO,
  AgreementType,
  SettlementCadence,
  SettlementMode,
} from "@/lib/api/pnl-sharing";

interface AdminLite {
  id: string;
  name: string;
  user_code: string;
}

interface BrokerLite {
  id: string;
  name: string;
  user_code: string;
  admin_id: string;
}

interface Props {
  open: boolean;
  onClose: () => void;
  // Edit mode: pre-fill from this
  existing?: AgreementDTO;
  // For super-admin: list of admins to pick from. For admin: omit (auto-fill self).
  admins?: AdminLite[];
  selfAdminId?: string;
  loadBrokersForAdmin: (adminId: string) => Promise<BrokerLite[]>;
  onSubmit: (body: {
    admin_id: string;
    broker_id: string;
    share_pct: string;
    settlement_mode: SettlementMode;
    settlement_cadence?: SettlementCadence;
    agreement_type?: AgreementType;
  }) => Promise<void>;
}

export function AgreementFormModal({
  open,
  onClose,
  existing,
  admins,
  selfAdminId,
  loadBrokersForAdmin,
  onSubmit,
}: Props) {
  const [adminId, setAdminId] = useState(
    existing?.admin_id ?? selfAdminId ?? ""
  );
  const [brokerId, setBrokerId] = useState(existing?.broker_id ?? "");
  const [sharePct, setSharePct] = useState(existing?.share_pct ?? "30");
  const [mode, setMode] = useState<SettlementMode>(
    existing?.settlement_mode ?? "MANUAL"
  );
  const [cadence, setCadence] = useState<SettlementCadence>(
    existing?.settlement_cadence ?? "MONTHLY"
  );
  const [agreementType, setAgreementType] = useState<AgreementType>(
    existing?.agreement_type ?? "PNL_AND_BROKERAGE"
  );
  const [brokers, setBrokers] = useState<BrokerLite[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!adminId) {
      setBrokers([]);
      return;
    }
    loadBrokersForAdmin(adminId).then(setBrokers).catch(() => setBrokers([]));
  }, [adminId, loadBrokersForAdmin]);

  const isEdit = !!existing;

  const submit = async () => {
    setError(null);
    if (!adminId || !brokerId) {
      setError("Select admin and broker");
      return;
    }
    const n = Number(sharePct);
    if (Number.isNaN(n) || n < 0 || n > 100) {
      setError("Share % must be between 0 and 100");
      return;
    }
    setSubmitting(true);
    try {
      await onSubmit({
        admin_id: adminId,
        broker_id: brokerId,
        share_pct: sharePct,
        settlement_mode: mode,
        settlement_cadence: mode === "AUTO" ? cadence : undefined,
        agreement_type: agreementType,
      });
      onClose();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      setError(err?.response?.data?.detail ?? err?.message ?? "Failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {isEdit ? "Edit Agreement" : "New P&L Sharing Agreement"}
          </DialogTitle>
        </DialogHeader>
        <div className="space-y-4">
          {admins && !isEdit && (
            <div>
              <Label>Admin</Label>
              <select
                value={adminId}
                onChange={(e) => {
                  setAdminId(e.target.value);
                  setBrokerId("");
                }}
                className="w-full bg-background text-foreground border border-input rounded px-3 py-2"
              >
                <option value="">Select admin...</option>
                {admins.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name} ({a.user_code})
                  </option>
                ))}
              </select>
            </div>
          )}
          <div>
            <Label>Broker</Label>
            <select
              value={brokerId}
              onChange={(e) => setBrokerId(e.target.value)}
              disabled={isEdit}
              className="w-full bg-background text-foreground border border-input rounded px-3 py-2 disabled:opacity-50"
            >
              <option value="">Select broker...</option>
              {brokers.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.name} ({b.user_code})
                </option>
              ))}
            </select>
          </div>
          <div>
            <Label>Share % (0-100)</Label>
            <Input
              value={sharePct}
              onChange={(e) => setSharePct(e.target.value)}
            />
          </div>
          <div>
            <Label>Sharing Type</Label>
            <div className="flex gap-4 mt-1">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="radio"
                  name="agreement_type"
                  value="PNL_AND_BROKERAGE"
                  checked={agreementType === "PNL_AND_BROKERAGE"}
                  onChange={() => setAgreementType("PNL_AND_BROKERAGE")}
                  disabled={isEdit}
                />
                PNL + Brokerage
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="radio"
                  name="agreement_type"
                  value="BROKERAGE_ONLY"
                  checked={agreementType === "BROKERAGE_ONLY"}
                  onChange={() => setAgreementType("BROKERAGE_ONLY")}
                  disabled={isEdit}
                />
                Brokerage only
              </label>
            </div>
            {isEdit && (
              <p className="text-xs text-muted-foreground mt-1">
                Type is immutable after creation.
              </p>
            )}
          </div>
          <div>
            <Label>Settlement Mode</Label>
            <div className="flex gap-4 mt-1">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="radio"
                  name="mode"
                  value="AUTO"
                  checked={mode === "AUTO"}
                  onChange={() => setMode("AUTO")}
                />
                AUTO
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="radio"
                  name="mode"
                  value="MANUAL"
                  checked={mode === "MANUAL"}
                  onChange={() => setMode("MANUAL")}
                />
                MANUAL
              </label>
            </div>
          </div>
          {mode === "AUTO" && (
            <div>
              <Label>Cadence</Label>
              <select
                value={cadence}
                onChange={(e) => setCadence(e.target.value as SettlementCadence)}
                className="w-full bg-background text-foreground border border-input rounded px-3 py-2"
              >
                <option value="DAILY">Daily</option>
                <option value="WEEKLY">Weekly</option>
                <option value="MONTHLY">Monthly</option>
              </select>
            </div>
          )}
          {error && <div className="text-red-400 text-sm">{error}</div>}
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={submitting}>
            {submitting ? "Saving..." : isEdit ? "Save" : "Create"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
