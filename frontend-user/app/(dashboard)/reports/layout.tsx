"use client";

import { ReactNode } from "react";

// Reports was a five-then-four-tab section (P&L / Tradebook / Brokerage /
// Margin). The operator cut it down to a SINGLE deliverable — a date-range
// tradebook download — so the sub-tab navigation is gone: there is nothing to
// switch between. `/reports/tradebook` is the only page; the sidebar and the
// profile "Reports" row point straight at it. This layout is now just a thin
// wrapper so the route group keeps working.
export default function ReportsLayout({ children }: { children: ReactNode }) {
  return <div className="space-y-4">{children}</div>;
}
