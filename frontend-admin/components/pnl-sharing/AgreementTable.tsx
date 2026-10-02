"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { AlertCircle } from "lucide-react";
import type { AgreementDTO } from "@/lib/api/pnl-sharing";

interface Props {
  agreements: AgreementDTO[];
  showAdminColumn?: boolean; // false for ADMIN role (their own data only)
  onEdit?: (a: AgreementDTO) => void;
  onPauseResume?: (a: AgreementDTO) => void;
  onEnd?: (a: AgreementDTO) => void;
  failedAgreementIds?: Set<string>; // for ⚠ marker
}

export function AgreementTable({
  agreements,
  showAdminColumn = true,
  onEdit,
  onPauseResume,
  onEnd,
  failedAgreementIds,
}: Props) {
  const router = useRouter();
  const hasActions = !!(onEdit || onPauseResume || onEnd);
  return (
    <div className="overflow-x-auto border border-border rounded-lg">
      <table className="w-full text-sm">
        <thead className="bg-muted text-muted-foreground">
          <tr>
            {showAdminColumn && <th className="px-4 py-2 text-left">Admin</th>}
            <th className="px-4 py-2 text-left">Broker</th>
            <th className="px-4 py-2 text-right">Share %</th>
            <th className="px-4 py-2 text-left">Mode</th>
            <th className="px-4 py-2 text-left">Cadence</th>
            <th className="px-4 py-2 text-left">Status</th>
            <th className="px-4 py-2 text-left">Type</th>
            {hasActions && <th className="px-4 py-2 text-right">Actions</th>}
          </tr>
        </thead>
        <tbody>
          {agreements.length === 0 && (
            <tr>
              <td
                colSpan={showAdminColumn ? (hasActions ? 8 : 7) : hasActions ? 7 : 6}
                className="px-4 py-8 text-center text-muted-foreground"
              >
                No agreements
              </td>
            </tr>
          )}
          {agreements.map((a) => (
            <tr
              key={a.id}
              onClick={() => router.push(`/management/pnl-sharing/${a.id}`)}
              className="border-t border-border hover:bg-muted/50 cursor-pointer"
            >
              {showAdminColumn && (
                <td className="px-4 py-2">
                  <Link
                    href={`/management/pnl-sharing/${a.id}`}
                    className="hover:underline"
                  >
                    {failedAgreementIds?.has(a.id) && (
                      <AlertCircle className="inline w-4 h-4 mr-1 text-amber-400" />
                    )}
                    {a.admin_name ?? a.admin_user_code ?? a.admin_id}
                  </Link>
                </td>
              )}
              <td className="px-4 py-2">
                <Link
                  href={`/management/pnl-sharing/${a.id}`}
                  className="hover:underline"
                >
                  {a.broker_name ?? a.broker_user_code ?? a.broker_id}
                </Link>
              </td>
              <td className="px-4 py-2 text-right font-mono">{a.share_pct}</td>
              <td className="px-4 py-2">{a.settlement_mode}</td>
              <td className="px-4 py-2">{a.settlement_cadence ?? "—"}</td>
              <td className="px-4 py-2">
                <span
                  className={
                    a.status === "ACTIVE"
                      ? "text-profit"
                      : a.status === "PAUSED"
                        ? "text-atm"
                        : "text-muted-foreground"
                  }
                >
                  {a.status}
                </span>
              </td>
              <td className="px-4 py-2">
                <span
                  className={
                    a.agreement_type === "BROKERAGE_ONLY"
                      ? "text-info text-xs"
                      : "text-xs text-muted-foreground"
                  }
                >
                  {a.agreement_type === "BROKERAGE_ONLY"
                    ? "BKG only"
                    : "PNL + BKG"}
                </span>
              </td>
              {hasActions && (
                <td
                  className="px-4 py-2 text-right space-x-1"
                  onClick={(e) => e.stopPropagation()}
                >
                  {onEdit && (
                    <Button size="sm" variant="ghost" onClick={() => onEdit(a)}>
                      Edit
                    </Button>
                  )}
                  {onPauseResume && a.status !== "ENDED" && (
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => onPauseResume(a)}
                    >
                      {a.status === "ACTIVE" ? "Pause" : "Resume"}
                    </Button>
                  )}
                  {onEnd && a.status !== "ENDED" && (
                    <Button size="sm" variant="ghost" onClick={() => onEnd(a)}>
                      End
                    </Button>
                  )}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
