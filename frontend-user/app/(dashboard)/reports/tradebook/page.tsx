"use client";

import { useState } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { ReportPdfButton } from "@/components/common/ReportPdfButton";
import { DateRangeBar, type DateRange } from "@/components/common/DateRangeBar";
import { Card } from "@/components/ui/card";
import { istPeriods } from "@/lib/time";
import { cn } from "@/lib/utils";

// Two documents for one date range. The Billing statement is the FIFO-paired
// statement, the same one the admin downloads (one backend assembly); its P&L
// is what the wallet booked. The Trade Book lists every fill as executed. The
// dates go as plain YYYY-MM-DD: the backend reads them as whole IST days.
// Opens on this IST week (Monday start), the period the ledger settles.
export default function TradebookPage() {
  const [periods] = useState(() => istPeriods());
  const [range, setRange] = useState<DateRange>(() => ({ from: periods[0].from, to: periods[0].to }));

  return (
    <div className="space-y-4">
      <PageHeader
        back
        title="Tradebook"
        description="Choose a date range and download your trade book."
      />

      <div className="flex flex-wrap items-center gap-2">
        <div className="flex flex-wrap gap-1 rounded-md border border-border bg-card p-1">
          {periods.map((p) => (
            <button
              key={p.label}
              type="button"
              onClick={() => setRange({ from: p.from, to: p.to })}
              className={cn(
                "rounded px-2.5 py-1 text-xs font-medium transition-colors",
                p.from === range.from && p.to === range.to
                  ? "bg-primary/15 text-primary"
                  : "text-muted-foreground hover:bg-muted/40 hover:text-foreground",
              )}
            >
              {p.label}
            </button>
          ))}
        </div>
        <DateRangeBar simple value={range} onChange={setRange} />
      </div>

      <Card className="p-4">
        <div className="text-sm font-medium">Download</div>
        <div className="mt-1 text-xs text-muted-foreground">
          For the selected period ({range.from} → {range.to}).
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          <ReportPdfButton
            kind="tradebook/full"
            params={{ from_date: range.from, to_date: range.to }}
            label="Billing statement"
          />
          <ReportPdfButton
            kind="tradebook"
            params={{ from_date: range.from, to_date: range.to }}
            label="Trade Book"
          />
        </div>
      </Card>
    </div>
  );
}
