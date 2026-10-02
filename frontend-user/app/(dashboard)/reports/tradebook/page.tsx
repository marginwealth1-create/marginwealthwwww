"use client";

import { useState } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { ReportPdfButton } from "@/components/common/ReportPdfButton";
import { DateRangeBar, toIsoFrom, toIsoTo, type DateRange } from "@/components/common/DateRangeBar";
import { Card } from "@/components/ui/card";

// Reports = ONE deliverable: pick a date range and download the tradebook.
// A SINGLE "Full Tradebook" download. Its net P&L is the canonical
// Σ Trade.pnl_inr — byte-for-byte the same figure as the /pnl report AND the
// admin-side download (both call build_full_tradebook_pdf). The old
// "Simple PDF" was removed on purpose: it listed raw trades and derived its
// total from sell−buy value, so its P&L never matched the canonical number —
// the "tradebook aur pdf alag P&L" bug.
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
        <div className="text-sm font-medium">Download tradebook</div>
        <div className="mt-1 text-xs text-muted-foreground">
          For the selected period ({range.from} → {range.to}).
        </div>
        <div className="mt-3">
          <ReportPdfButton
            kind="tradebook/full"
            params={{ from_date: toIsoFrom(range.from), to_date: toIsoTo(range.to) }}
            label="Full Tradebook"
          />
        </div>
      </Card>
    </div>
  );
}
