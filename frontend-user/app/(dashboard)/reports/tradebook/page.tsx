"use client";

import { useState } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { ReportPdfButton } from "@/components/common/ReportPdfButton";
import { DateRangeBar, type DateRange } from "@/components/common/DateRangeBar";
import { Card } from "@/components/ui/card";

// Two documents for one date range. The Billing statement is the FIFO-paired
// statement, the same one the admin downloads (one backend assembly); its P&L
// is what the wallet booked. The Trade Book lists every fill as executed. The
// dates go as plain YYYY-MM-DD: the backend reads them as whole IST days.
export default function TradebookPage() {
  const [range, setRange] = useState<DateRange>(() => {
    const to = new Date();
    const from = new Date();
    from.setDate(from.getDate() - 30);
    const iso = (d: Date) =>
      `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    return { from: iso(from), to: iso(to) };
  });

  return (
    <div className="space-y-4">
      <PageHeader
        back
        title="Tradebook"
        description="Choose a date range and download your trade book."
      />

      <DateRangeBar simple value={range} onChange={setRange} />

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
