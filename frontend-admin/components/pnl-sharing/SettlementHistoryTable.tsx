"use client";

import { Button } from "@/components/ui/button";
import { RotateCcw } from "lucide-react";
import type { SettlementDTO } from "@/lib/api/pnl-sharing";

interface Props {
  settlements: SettlementDTO[];
  canRetry?: boolean;
  onRetry?: (s: SettlementDTO) => void;
}

export function SettlementHistoryTable({
  settlements,
  canRetry,
  onRetry,
}: Props) {
  return (
    <div className="overflow-x-auto border border-border rounded-lg">
      <table className="w-full text-sm">
        <thead className="bg-muted text-muted-foreground">
          <tr>
            <th className="px-4 py-2 text-left">Period</th>
            <th className="px-4 py-2 text-right">Sharing PNL</th>
            <th className="px-4 py-2 text-right">Sharing BKG</th>
            <th className="px-4 py-2 text-right">Total</th>
            <th className="px-4 py-2 text-left">Status</th>
            {canRetry && <th className="px-4 py-2 text-right">Action</th>}
          </tr>
        </thead>
        <tbody>
          {settlements.length === 0 && (
            <tr>
              <td
                colSpan={canRetry ? 6 : 5}
                className="px-4 py-8 text-center text-muted-foreground"
              >
                No settlements yet
              </td>
            </tr>
          )}
          {settlements.map((s) => (
            <tr key={s.id} className="border-t border-border">
              <td className="px-4 py-2">
                {new Date(s.period_start).toLocaleDateString()} →{" "}
                {new Date(s.period_end).toLocaleDateString()}
              </td>
              <td className="px-4 py-2 text-right font-mono">
                {s.sharing_pnl_inr}
              </td>
              <td className="px-4 py-2 text-right font-mono">
                {s.sharing_bkg_inr}
              </td>
              <td className="px-4 py-2 text-right font-mono">
                {s.sharing_total_inr}
              </td>
              <td className="px-4 py-2">
                <span
                  className={
                    s.status === "SETTLED"
                      ? "text-profit"
                      : s.status === "FAILED"
                        ? "text-loss"
                        : "text-atm"
                  }
                >
                  {s.status}
                </span>
                {s.failure_reason && (
                  <div className="text-xs text-loss/70 mt-0.5">
                    {s.failure_reason}
                  </div>
                )}
              </td>
              {canRetry && (
                <td className="px-4 py-2 text-right">
                  {s.status === "FAILED" && onRetry && (
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => onRetry(s)}
                      aria-label="Retry"
                    >
                      <RotateCcw className="w-4 h-4" />
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
