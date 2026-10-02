// frontend-admin/lib/api/pnl-sharing.ts
import { api, unwrap } from "@/lib/api";

export type AgreementStatus = "ACTIVE" | "PAUSED" | "ENDED";
export type SettlementMode = "AUTO" | "MANUAL";
export type SettlementCadence = "DAILY" | "WEEKLY" | "MONTHLY";
export type AgreementType = "PNL_AND_BROKERAGE" | "BROKERAGE_ONLY";
export type SharingSettlementStatus = "PENDING" | "SETTLED" | "FAILED";

export interface AgreementDTO {
  id: string;
  admin_id: string;
  admin_name: string | null;
  admin_user_code: string | null;
  broker_id: string;
  broker_name: string | null;
  broker_user_code: string | null;
  share_pct: string;
  settlement_mode: SettlementMode;
  settlement_cadence: SettlementCadence | null;
  status: AgreementStatus;
  agreement_type: AgreementType;
  effective_from: string;
  effective_until: string | null;
  created_at: string;
  updated_at: string;
}

export interface SettlementDTO {
  id: string;
  agreement_id: string;
  admin_id: string;
  broker_id: string;
  period_start: string;
  period_end: string;
  cadence: SettlementCadence;
  net_client_pnl_inr: string;
  net_client_bkg_inr: string;
  total_of_both_inr: string;
  actual_pnl_inr: string;
  sharing_pnl_inr: string;
  sharing_bkg_inr: string;
  sharing_total_inr: string;
  share_pct_snapshot: string;
  status: SharingSettlementStatus;
  settled_at: string | null;
  failure_reason: string | null;
  retry_count: number;
}

export interface ReportRow {
  period_start: string;
  period_end: string;
  net_client_pnl_inr: string;
  net_client_bkg_inr: string;
  total_of_both_inr: string;
  actual_pnl_inr: string;
  sharing_pnl_inr: string;
  sharing_bkg_inr: string;
  settlement_status: "SETTLED" | "PENDING" | "FAILED" | "UNSETTLED";
}

export interface ReportSummary {
  total_sharing_pnl_inr: string;
  total_sharing_bkg_inr: string;
  periods_settled: number;
  periods_pending: number;
  periods_failed: number;
  periods_unsettled: number;
}

export interface ReportResponse {
  agreement: AgreementDTO;
  rows: ReportRow[];
  summary: ReportSummary;
}

export const PnlSharingAPI = {
  // Agreements
  listAgreements: (params?: {
    status?: AgreementStatus;
    agreement_type?: AgreementType;
    admin_id?: string;
    broker_id?: string;
    skip?: number;
    limit?: number;
  }) =>
    unwrap<AgreementDTO[]>(
      api.get("/admin/pnl-sharing/agreements", { params })
    ),

  createAgreement: (body: {
    admin_id: string;
    broker_id: string;
    share_pct: string;
    settlement_mode: SettlementMode;
    settlement_cadence?: SettlementCadence | null;
    agreement_type?: AgreementType;
  }) =>
    unwrap<AgreementDTO>(api.post("/admin/pnl-sharing/agreements", body)),

  getAgreement: (id: string) =>
    unwrap<AgreementDTO>(api.get(`/admin/pnl-sharing/agreements/${id}`)),

  updateAgreement: (
    id: string,
    body: Partial<{
      share_pct: string;
      settlement_mode: SettlementMode;
      settlement_cadence: SettlementCadence | null;
    }>
  ) =>
    unwrap<AgreementDTO>(api.patch(`/admin/pnl-sharing/agreements/${id}`, body)),

  pauseAgreement: (id: string) =>
    unwrap<AgreementDTO>(
      api.post(`/admin/pnl-sharing/agreements/${id}/pause`)
    ),

  resumeAgreement: (id: string) =>
    unwrap<AgreementDTO>(
      api.post(`/admin/pnl-sharing/agreements/${id}/resume`)
    ),

  endAgreement: (id: string) =>
    unwrap<AgreementDTO>(api.post(`/admin/pnl-sharing/agreements/${id}/end`)),

  // Settlements
  listSettlements: (params: {
    agreement_id?: string;
    status?: SharingSettlementStatus;
    from_date?: string;
    to_date?: string;
    skip?: number;
    limit?: number;
  }) =>
    unwrap<SettlementDTO[]>(
      api.get("/admin/pnl-sharing/settlements", { params })
    ),

  manualSettle: (body: {
    agreement_id: string;
    cadence: SettlementCadence;
    period_start?: string;
  }) =>
    unwrap<SettlementDTO>(
      api.post("/admin/pnl-sharing/settlements/manual", body)
    ),

  retrySettlement: (id: string) =>
    unwrap<SettlementDTO>(
      api.post(`/admin/pnl-sharing/settlements/${id}/retry`)
    ),

  // Reports
  getReport: (
    agreementId: string,
    params: { period: SettlementCadence; from: string; to: string }
  ) =>
    unwrap<ReportResponse>(
      api.get(`/admin/pnl-sharing/reports/${agreementId}`, { params })
    ),

  downloadReport: async (
    agreementId: string,
    params: {
      period: SettlementCadence;
      from: string;
      to: string;
      format: "pdf" | "excel";
    },
  ) => {
    const resp = await api.get(
      `/admin/pnl-sharing/reports/${agreementId}/download`,
      { params, responseType: "blob" },
    );
    const blob = resp.data as Blob;
    // Try to read filename from Content-Disposition header
    const cd: string = (resp.headers["content-disposition"] as string) || "";
    const match = cd.match(/filename="([^"]+)"/);
    const filename =
      (match && match[1]) ||
      `pnl_sharing_report.${params.format === "pdf" ? "pdf" : "xlsx"}`;
    return { blob, filename };
  },

  // Broker self
  getMyAgreement: () =>
    unwrap<AgreementDTO>(api.get("/admin/pnl-sharing/me/agreement")),
};
