"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { useAdminAuthStore } from "@/stores/authStore";
import {
  useAgreements,
  useAgreementAction,
  useCreateAgreement,
  useMyAgreement,
  useReport,
  useUpdateAgreement,
} from "@/hooks/usePnlSharing";
import { AgreementTable } from "@/components/pnl-sharing/AgreementTable";
import { AgreementFormModal } from "@/components/pnl-sharing/AgreementFormModal";
import { SharingCard } from "@/components/pnl-sharing/SharingCard";
import { PeriodToggle } from "@/components/pnl-sharing/PeriodToggle";
import { SettlementHistoryTable } from "@/components/pnl-sharing/SettlementHistoryTable";
import { Button } from "@/components/ui/button";
import { api, ManagementAPI } from "@/lib/api";
import {
  PnlSharingAPI,
  type AgreementDTO,
  type AgreementStatus,
  type AgreementType,
  type SettlementCadence,
} from "@/lib/api/pnl-sharing";

function periodBounds(cadence: SettlementCadence): { from: string; to: string } {
  const now = new Date();
  if (cadence === "DAILY") {
    const start = new Date(now);
    start.setHours(0, 0, 0, 0);
    const end = new Date(now);
    end.setHours(23, 59, 59, 999);
    return { from: start.toISOString(), to: end.toISOString() };
  }
  if (cadence === "WEEKLY") {
    const day = now.getDay() || 7; // Sun=0 → 7 so Mon=1
    const start = new Date(now);
    start.setDate(now.getDate() - day + 1);
    start.setHours(0, 0, 0, 0);
    const end = new Date(start);
    end.setDate(start.getDate() + 6);
    end.setHours(23, 59, 59, 999);
    return { from: start.toISOString(), to: end.toISOString() };
  }
  // MONTHLY
  const start = new Date(now.getFullYear(), now.getMonth(), 1, 0, 0, 0, 0);
  const end = new Date(
    now.getFullYear(),
    now.getMonth() + 1,
    0,
    23,
    59,
    59,
    999
  );
  return { from: start.toISOString(), to: end.toISOString() };
}

export default function PnlSharingListPage() {
  const me = useAdminAuthStore((s) => s.admin);
  const isSuperAdmin = me?.role === "SUPER_ADMIN";
  const isAdmin = me?.role === "ADMIN";
  const isBroker = me?.role === "BROKER";

  if (isBroker) return <BrokerView />;

  return <AdminListView isSuperAdmin={!!isSuperAdmin} isAdmin={!!isAdmin} />;
}

function AdminListView({
  isSuperAdmin,
  isAdmin,
}: {
  isSuperAdmin: boolean;
  isAdmin: boolean;
}) {
  const me = useAdminAuthStore((s) => s.admin);
  const [statusFilter, setStatusFilter] = useState<AgreementStatus | undefined>(
    undefined
  );
  const [typeFilter, setTypeFilter] = useState<AgreementType | undefined>(
    undefined
  );
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<AgreementDTO | null>(null);

  const { data: agreements = [], isLoading } = useAgreements({
    status: statusFilter,
    agreement_type: typeFilter,
  });
  const createMut = useCreateAgreement();
  const updateMut = useUpdateAgreement();
  const actionMut = useAgreementAction();

  // For super-admin "New Agreement" modal: list of admins.
  const { data: superAdminsList = [] } = useQuery({
    queryKey: ["pnl-sharing", "admins-for-form"],
    queryFn: async () => {
      const { items } = await ManagementAPI.listSubAdmins({ page_size: 200 });
      return items.map((a: { id: string; full_name: string; user_code: string }) => ({
        id: a.id,
        name: a.full_name,
        user_code: a.user_code,
      }));
    },
    enabled: isSuperAdmin,
  });

  const loadBrokersForAdmin = async (adminId: string) => {
    // Use the existing brokers endpoint, scoped to that admin
    const resp = await api.get("/admin/management/brokers", {
      params: { admin_id: adminId, page_size: 200 },
    });
    const items: Array<{ id: string; full_name: string; user_code: string }> =
      resp.data?.data?.items ?? resp.data?.data ?? [];
    return items.map((b) => ({
      id: b.id,
      name: b.full_name,
      user_code: b.user_code,
      admin_id: adminId,
    }));
  };

  const handleSubmit = async (
    body: Parameters<typeof createMut.mutateAsync>[0]
  ) => {
    if (editing) {
      // EDIT mode: only patch supported fields
      await updateMut.mutateAsync({
        id: editing.id,
        body: {
          share_pct: body.share_pct,
          settlement_mode: body.settlement_mode,
          settlement_cadence: body.settlement_cadence ?? null,
        },
      });
      toast.success("Agreement updated");
    } else {
      await createMut.mutateAsync(body);
      toast.success("Agreement created");
    }
  };

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">P&amp;L Sharing</h1>
        <Button
          onClick={() => {
            setEditing(null);
            setModalOpen(true);
          }}
        >
          + New Agreement
        </Button>
      </div>

      <div className="flex gap-2">
        {(["ACTIVE", "PAUSED", "ENDED"] as const).map((s) => (
          <Button
            key={s}
            size="sm"
            variant={statusFilter === s ? "default" : "outline"}
            onClick={() =>
              setStatusFilter(statusFilter === s ? undefined : s)
            }
          >
            {s}
          </Button>
        ))}
      </div>

      <div className="flex gap-2 mt-2">
        {(["PNL_AND_BROKERAGE", "BROKERAGE_ONLY"] as const).map((t) => (
          <Button
            key={t}
            size="sm"
            variant={typeFilter === t ? "default" : "outline"}
            onClick={() => setTypeFilter(typeFilter === t ? undefined : t)}
          >
            {t === "PNL_AND_BROKERAGE" ? "PNL + Brokerage" : "Brokerage only"}
          </Button>
        ))}
      </div>

      {isLoading ? (
        <div className="text-muted-foreground">Loading...</div>
      ) : (
        <AgreementTable
          agreements={agreements}
          showAdminColumn={isSuperAdmin}
          onEdit={(a) => {
            setEditing(a);
            setModalOpen(true);
          }}
          onPauseResume={async (a) => {
            await actionMut.mutateAsync({
              id: a.id,
              action: a.status === "ACTIVE" ? "pause" : "resume",
            });
            toast.success(
              `Agreement ${a.status === "ACTIVE" ? "paused" : "resumed"}`
            );
          }}
          onEnd={async (a) => {
            if (!confirm("End this agreement? Cannot be undone.")) return;
            await actionMut.mutateAsync({ id: a.id, action: "end" });
            toast.success("Agreement ended");
          }}
        />
      )}

      <AgreementFormModal
        open={modalOpen}
        onClose={() => {
          setModalOpen(false);
          setEditing(null);
        }}
        existing={editing ?? undefined}
        admins={isSuperAdmin ? superAdminsList : undefined}
        selfAdminId={isAdmin && me ? me.id : undefined}
        loadBrokersForAdmin={loadBrokersForAdmin}
        onSubmit={handleSubmit}
      />
    </div>
  );
}

function BrokerView() {
  const [cadence, setCadence] = useState<SettlementCadence>("MONTHLY");
  const { data: agreement, isLoading, error } = useMyAgreement();
  const { from, to } = periodBounds(cadence);
  const { data: report } = useReport(agreement?.id, cadence, from, to);

  const { data: settlements = [] } = useQuery({
    queryKey: ["pnl-sharing", "settlements", agreement?.id],
    queryFn: () =>
      PnlSharingAPI.listSettlements({
        agreement_id: agreement!.id,
        limit: 50,
      }),
    enabled: !!agreement,
  });

  if (isLoading) return <div className="p-6">Loading...</div>;
  if (error || !agreement) {
    return (
      <div className="p-6 max-w-md">
        <h1 className="text-2xl font-bold">P&amp;L Sharing</h1>
        <p className="text-muted-foreground mt-4">No P&amp;L sharing agreement active.</p>
      </div>
    );
  }

  const currentRow = report?.rows[report.rows.length - 1] ?? null;

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-bold">
          P&amp;L Sharing — Agreement with{" "}
          {agreement.admin_name || agreement.admin_user_code}
        </h1>
        <div className="text-sm text-muted-foreground">
          {agreement.share_pct}% ·{" "}
          {agreement.agreement_type === "BROKERAGE_ONLY"
            ? "Brokerage only"
            : "PNL + Brokerage"}
          {" · "}{agreement.settlement_mode}
          {agreement.settlement_cadence &&
            ` · ${agreement.settlement_cadence}`}
          {" · "}
          {agreement.status}
          <span className="ml-2 italic">(read-only)</span>
        </div>
      </div>

      <PeriodToggle value={cadence} onChange={setCadence} />

      {currentRow && <SharingCard agreement={agreement} row={currentRow} />}

      <div>
        <h2 className="text-lg font-semibold mb-2">Settlement History</h2>
        <SettlementHistoryTable settlements={settlements} />
      </div>
    </div>
  );
}
