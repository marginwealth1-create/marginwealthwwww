import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  PnlSharingAPI,
  type SettlementCadence,
  type AgreementStatus,
  type AgreementType,
  type SharingSettlementStatus,
} from "@/lib/api/pnl-sharing";

const KEYS = {
  agreements: (filters?: Record<string, unknown>) =>
    ["pnl-sharing", "agreements", filters] as const,
  agreement: (id: string) => ["pnl-sharing", "agreement", id] as const,
  settlements: (filters?: Record<string, unknown>) =>
    ["pnl-sharing", "settlements", filters] as const,
  report: (id: string, period: SettlementCadence, from: string, to: string) =>
    ["pnl-sharing", "report", id, period, from, to] as const,
  myAgreement: () => ["pnl-sharing", "me", "agreement"] as const,
};

export function useAgreements(filters?: {
  status?: AgreementStatus;
  agreement_type?: AgreementType;
  admin_id?: string;
  broker_id?: string;
  skip?: number;
  limit?: number;
}) {
  return useQuery({
    queryKey: KEYS.agreements(filters),
    queryFn: () => PnlSharingAPI.listAgreements(filters),
  });
}

export function useAgreement(id: string | undefined) {
  return useQuery({
    queryKey: id ? KEYS.agreement(id) : ["pnl-sharing", "agreement", "none"],
    queryFn: () => PnlSharingAPI.getAgreement(id!),
    enabled: !!id,
  });
}

export function useReport(
  id: string | undefined,
  period: SettlementCadence,
  from: string,
  to: string,
) {
  return useQuery({
    queryKey: id
      ? KEYS.report(id, period, from, to)
      : ["pnl-sharing", "report", "none"],
    queryFn: () => PnlSharingAPI.getReport(id!, { period, from, to }),
    enabled: !!id,
  });
}

export function useMyAgreement() {
  return useQuery({
    queryKey: KEYS.myAgreement(),
    queryFn: PnlSharingAPI.getMyAgreement,
    retry: false, // 404 is meaningful (no agreement)
  });
}

export function useSettlements(filters: {
  agreement_id?: string;
  status?: SharingSettlementStatus;
  from_date?: string;
  to_date?: string;
  skip?: number;
  limit?: number;
}) {
  return useQuery({
    queryKey: KEYS.settlements(filters),
    queryFn: () => PnlSharingAPI.listSettlements(filters),
  });
}

export function useCreateAgreement() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: PnlSharingAPI.createAgreement,
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["pnl-sharing", "agreements"] }),
  });
}

export function useUpdateAgreement() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      body,
    }: {
      id: string;
      body: Parameters<typeof PnlSharingAPI.updateAgreement>[1];
    }) => PnlSharingAPI.updateAgreement(id, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["pnl-sharing"] }),
  });
}

export function useAgreementAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({
      id,
      action,
    }: {
      id: string;
      action: "pause" | "resume" | "end";
    }) => {
      if (action === "pause") return PnlSharingAPI.pauseAgreement(id);
      if (action === "resume") return PnlSharingAPI.resumeAgreement(id);
      return PnlSharingAPI.endAgreement(id);
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["pnl-sharing"] }),
  });
}

export function useManualSettle() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: PnlSharingAPI.manualSettle,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["pnl-sharing"] }),
  });
}

export function useRetrySettlement() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: PnlSharingAPI.retrySettlement,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["pnl-sharing"] }),
  });
}
