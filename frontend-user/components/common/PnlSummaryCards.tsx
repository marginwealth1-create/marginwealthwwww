"use client";

import { useQuery } from "@tanstack/react-query";
import { CalendarDays, TrendingDown, TrendingUp } from "lucide-react";
import { Card } from "@/components/ui/card";
import { PositionAPI } from "@/lib/api";
import { cn, formatINR, pnlColor } from "@/lib/utils";

export function PnlSummaryCards() {
  const { data: pnl } = useQuery({
    queryKey: ["positions", "pnl-summary"],
    queryFn: () => PositionAPI.pnlSummary(),
    refetchInterval: 10000,
  });

  // Three small cards in a row at every breakpoint — phone view used to
  // stack them full-width (`grid-cols-1 sm:grid-cols-3`), which made the
  // P&L section take up half the screen on mobile. Mobile traders care
  // about glanceable numbers, so the layout is now 3-up with compact
  // typography and the verbose hint hidden below `sm`.
  return (
    <section className="grid grid-cols-3 gap-2">
      <PnlCard
        label="Today"
        sublabel="Today's PNL"
        value={pnl?.today_pnl ?? 0}
        hint={`Realised ${formatINR(pnl?.today_realised ?? 0)} + Unrealised ${formatINR(pnl?.open_unrealised ?? 0)}`}
        icon={(pnl?.today_pnl ?? 0) >= 0 ? TrendingUp : TrendingDown}
      />
      <PnlCard
        label="This Week"
        sublabel="This Week's PNL"
        value={pnl?.week_pnl ?? 0}
        hint="Sun → today (IST)"
        icon={(pnl?.week_pnl ?? 0) >= 0 ? TrendingUp : TrendingDown}
      />
      <PnlCard
        label="Last Week"
        sublabel="Last Week's PNL"
        value={pnl?.last_week_pnl ?? 0}
        hint="Previous Sun → Sat — realised only"
        icon={CalendarDays}
      />
    </section>
  );
}

function PnlCard({
  label,
  sublabel,
  value,
  hint,
  icon: Icon,
}: {
  /** Short label shown on mobile (≤ ~10 chars to fit a 3-col grid). */
  label: string;
  /** Full label revealed on `sm:` and up where there's room. */
  sublabel?: string;
  value: number | string;
  hint?: string;
  icon?: any;
}) {
  const n = Number(value ?? 0);
  return (
    <Card className="p-2.5 sm:p-4">
      <div className="flex items-start justify-between gap-1.5">
        {/* Mobile shows the short label; sm+ swaps in the full one. */}
        <p className="truncate text-[10px] uppercase tracking-wider text-muted-foreground sm:text-sm sm:normal-case sm:tracking-normal">
          <span className="sm:hidden">{label}</span>
          <span className="hidden sm:inline">{sublabel ?? label}</span>
        </p>
        {Icon && <Icon className={cn("size-3.5 shrink-0 sm:size-4", pnlColor(n))} />}
      </div>
      <div
        className={cn(
          "mt-1 text-sm font-semibold sm:mt-2 sm:text-2xl",
          pnlColor(n),
        )}
      >
        {formatINR(n)}
      </div>
      {/* Hint hidden on phone — there isn't room and the headline number
          is what the user is checking at a glance. */}
      {hint && <div className="mt-0.5 hidden text-[11px] text-muted-foreground sm:block">{hint}</div>}
    </Card>
  );
}
