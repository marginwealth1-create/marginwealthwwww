"use client";

import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Download, FileText, X } from "lucide-react";
import type { ReportRow, AgreementDTO } from "@/lib/api/pnl-sharing";

function fmt(n: string): string {
  const num = Number(n);
  if (Number.isNaN(num)) return n;
  return new Intl.NumberFormat("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(num);
}

function colorFor(n: string): string {
  const num = Number(n);
  if (num > 0) return "text-profit";
  if (num < 0) return "text-loss";
  return "text-muted-foreground";
}

interface Props {
  agreement: AgreementDTO;
  row: ReportRow; // single period's data (the "current open period" or selected period)
  showDownloads?: boolean; // false in Phase A (no download endpoint yet)
  onClose?: () => void;
  onDownloadExcel?: () => void;
  onDownloadPdf?: () => void;
}

export function SharingCard({
  agreement,
  row,
  showDownloads = false,
  onClose,
  onDownloadExcel,
  onDownloadPdf,
}: Props) {
  return (
    <Card className="p-6 max-w-md">
      <div className="flex items-start gap-3 mb-4">
        <div className="flex-1">
          <div className="text-sm text-muted-foreground">Account ID</div>
          <div className="text-2xl font-bold tracking-wide text-foreground">
            {agreement.admin_user_code || "—"}
          </div>
        </div>
        {onClose && (
          <Button size="sm" variant="ghost" onClick={onClose} aria-label="Close">
            <X className="w-4 h-4" />
          </Button>
        )}
        {showDownloads && (
          <>
            <Button
              size="sm"
              variant="outline"
              onClick={onDownloadExcel}
              disabled={!onDownloadExcel}
              title={onDownloadExcel ? "Excel" : "Available in Phase C"}
            >
              <Download className="w-4 h-4 mr-1" /> Excel
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={onDownloadPdf}
              disabled={!onDownloadPdf}
              title={onDownloadPdf ? "PDF" : "Available in Phase C"}
            >
              <FileText className="w-4 h-4 mr-1" /> PDF
            </Button>
          </>
        )}
      </div>

      <div className="space-y-3 font-mono text-sm">
        <Row label="NET CLIENT PNL" value={row.net_client_pnl_inr} />
        <Row label="NET CLIENT BKG" value={row.net_client_bkg_inr} positive />

        <hr className="border-border" />

        <Row label="TOTAL OF BOTH" value={row.total_of_both_inr} />

        <hr className="border-border" />

        <div className="bg-accent -mx-2 px-2 py-2 rounded">
          <Row label="= ACTUAL PNL" value={row.actual_pnl_inr} bold />
        </div>

        {agreement.agreement_type !== "BROKERAGE_ONLY" && (
          <Row label="SHARING PNL" value={row.sharing_pnl_inr} />
        )}
        <Row label="SHARING BKG" value={row.sharing_bkg_inr} />
      </div>
    </Card>
  );
}

function Row({
  label,
  value,
  bold,
  positive,
}: {
  label: string;
  value: string;
  bold?: boolean;
  positive?: boolean;
}) {
  const cls = positive ? "text-info" : colorFor(value);
  return (
    <div className={`flex justify-between ${bold ? "font-bold" : ""}`}>
      <span className="text-muted-foreground">{label}</span>
      <span className={cls}>{fmt(value)}</span>
    </div>
  );
}
