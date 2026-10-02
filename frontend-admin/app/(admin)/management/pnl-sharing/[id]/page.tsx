"use client";

import { useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  useAgreement,
  useAgreementAction,
  useManualSettle,
  useReport,
  useRetrySettlement,
} from "@/hooks/usePnlSharing";
import { SharingCard } from "@/components/pnl-sharing/SharingCard";
import { PeriodToggle } from "@/components/pnl-sharing/PeriodToggle";
import { SettlementHistoryTable } from "@/components/pnl-sharing/SettlementHistoryTable";
import { Button } from "@/components/ui/button";
import { PnlSharingAPI, type SettlementCadence } from "@/lib/api/pnl-sharing";

function triggerDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

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
    const day = now.getDay() || 7;
    const start = new Date(now);
    start.setDate(now.getDate() - day + 1);
    start.setHours(0, 0, 0, 0);
    const end = new Date(start);
    end.setDate(start.getDate() + 6);
    end.setHours(23, 59, 59, 999);
    return { from: start.toISOString(), to: end.toISOString() };
  }
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

export default function PnlSharingDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const [cadence, setCadence] = useState<SettlementCadence>("MONTHLY");

  const { data: agreement } = useAgreement(id);
  const { from, to } = periodBounds(cadence);
  const { data: report } = useReport(id, cadence, from, to);

  const { data: settlements = [] } = useQuery({
    queryKey: ["pnl-sharing", "settlements", id],
    queryFn: () =>
      PnlSharingAPI.listSettlements({ agreement_id: id, limit: 50 }),
    enabled: !!id,
  });

  const settleMut = useManualSettle();
  const retryMut = useRetrySettlement();
  const actionMut = useAgreementAction();

  if (!agreement || !report) {
    return <div className="p-6">Loading...</div>;
  }

  const currentRow = report.rows[report.rows.length - 1] ?? null;

  return (
    <div className="space-y-6 p-6">
      <Link
        href="/management/pnl-sharing"
        className="text-sm text-muted-foreground hover:underline"
      >
        ← Back to list
      </Link>

      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold">
            {agreement.admin_name || agreement.admin_user_code} ⇄{" "}
            {agreement.broker_name || agreement.broker_user_code}
          </h1>
          <div className="text-sm text-muted-foreground mt-1">
            {agreement.share_pct}% ·{" "}
            {agreement.agreement_type === "BROKERAGE_ONLY"
              ? "Brokerage only"
              : "PNL + Brokerage"}
            {" · "}{agreement.settlement_mode}
            {agreement.settlement_cadence &&
              ` · ${agreement.settlement_cadence}`}
            {" · "}
            {agreement.status}
          </div>
        </div>
        <div className="flex gap-2">
          {agreement.status === "ACTIVE" && (
            <Button
              variant="outline"
              onClick={async () => {
                await actionMut.mutateAsync({
                  id: agreement.id,
                  action: "pause",
                });
                toast.success("Paused");
              }}
            >
              Pause
            </Button>
          )}
          {agreement.status === "PAUSED" && (
            <Button
              variant="outline"
              onClick={async () => {
                await actionMut.mutateAsync({
                  id: agreement.id,
                  action: "resume",
                });
                toast.success("Resumed");
              }}
            >
              Resume
            </Button>
          )}
          {agreement.status !== "ENDED" && (
            <Button
              variant="outline"
              onClick={async () => {
                if (!confirm("End this agreement?")) return;
                await actionMut.mutateAsync({
                  id: agreement.id,
                  action: "end",
                });
                toast.success("Ended");
              }}
            >
              End
            </Button>
          )}
        </div>
      </div>

      <div className="flex items-center justify-between">
        <PeriodToggle value={cadence} onChange={setCadence} />
        <Button
          onClick={async () => {
            try {
              await settleMut.mutateAsync({
                agreement_id: agreement.id,
                cadence,
              });
              toast.success("Settled");
            } catch (e: unknown) {
              const err = e as { message?: string };
              toast.error(err?.message ?? "Settlement failed");
            }
          }}
        >
          Settle now
        </Button>
      </div>

      {currentRow && (
        <SharingCard
          agreement={agreement}
          row={currentRow}
          showDownloads
          onDownloadPdf={async () => {
            try {
              const { blob, filename } = await PnlSharingAPI.downloadReport(
                agreement.id,
                { period: cadence, from, to, format: "pdf" },
              );
              triggerDownload(blob, filename);
            } catch (e) {
              const err = e as { message?: string };
              toast.error(err?.message ?? "PDF download failed");
            }
          }}
          onDownloadExcel={async () => {
            try {
              const { blob, filename } = await PnlSharingAPI.downloadReport(
                agreement.id,
                { period: cadence, from, to, format: "excel" },
              );
              triggerDownload(blob, filename);
            } catch (e) {
              const err = e as { message?: string };
              toast.error(err?.message ?? "Excel download failed");
            }
          }}
        />
      )}

      <div>
        <h2 className="text-lg font-semibold mb-2">Settlement History</h2>
        <SettlementHistoryTable
          settlements={settlements}
          canRetry
          onRetry={async (s) => {
            await retryMut.mutateAsync(s.id);
            toast.success("Retried");
          }}
        />
      </div>
    </div>
  );
}
