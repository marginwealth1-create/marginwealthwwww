"use client";

import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { formatINR } from "@/lib/utils";

function daysUntil(iso?: string | null): number | null {
  if (!iso) return null;
  const d = new Date(iso).getTime() - Date.now();
  return Math.max(0, Math.ceil(d / 86400000));
}

const STATUS_STYLE: Record<string, string> = {
  ACTIVE: "bg-amber-500/15 text-amber-500",
  COMPLETED: "bg-emerald-500/15 text-emerald-500",
  CONSUMED: "bg-muted text-muted-foreground",
  EXPIRED: "bg-muted text-muted-foreground",
  CANCELLED: "bg-red-500/15 text-red-500",
};

export function BonusDrawer({
  open,
  onClose,
  bonuses,
}: {
  open: boolean;
  onClose: () => void;
  bonuses: any[];
}) {
  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Your bonuses</DialogTitle>
        </DialogHeader>
        <div className="max-h-[65vh] space-y-2 overflow-y-auto">
          {(bonuses ?? []).length === 0 && (
            <p className="py-6 text-center text-sm text-muted-foreground">No bonuses yet.</p>
          )}
          {(bonuses ?? []).map((b) => {
            const pct = Math.round((b.wager_progress_pct ?? 0) * 100);
            const hasWager = Number(b.wager_target_volume) > 0;
            const days = daysUntil(b.expires_at);
            return (
              <div key={b.id} className="rounded-xl border border-border bg-card p-3">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-semibold">{b.template_name}</span>
                  <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${STATUS_STYLE[b.status] || "bg-muted text-muted-foreground"}`}>
                    {b.status}
                  </span>
                </div>
                <div className="mt-1 text-xs text-muted-foreground">
                  Granted {formatINR(b.original_amount)} · Credit left{" "}
                  <span className="font-semibold text-foreground">{formatINR(b.current_credit)}</span>
                </div>
                {hasWager ? (
                  <div className="mt-2">
                    <div className="h-1.5 w-full rounded bg-muted">
                      <div className="h-1.5 rounded bg-primary" style={{ width: `${pct}%` }} />
                    </div>
                    <div className="mt-0.5 text-[10px] text-muted-foreground">
                      Wager {pct}% — trade {formatINR(b.wager_target_volume)} total to unlock withdrawal
                    </div>
                  </div>
                ) : (
                  <div className="mt-1 text-[10px] text-muted-foreground">No wager requirement</div>
                )}
                {b.status === "ACTIVE" && days !== null && (
                  <div className="mt-1 text-[10px] text-amber-500">Expires in {days} day{days === 1 ? "" : "s"}</div>
                )}
              </div>
            );
          })}
        </div>
      </DialogContent>
    </Dialog>
  );
}
