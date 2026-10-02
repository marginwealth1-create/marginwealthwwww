"use client";

import { Button } from "@/components/ui/button";
import type { SettlementCadence } from "@/lib/api/pnl-sharing";

interface Props {
  value: SettlementCadence;
  onChange: (next: SettlementCadence) => void;
}

const OPTIONS: { value: SettlementCadence; label: string }[] = [
  { value: "DAILY", label: "Daily" },
  { value: "WEEKLY", label: "Weekly" },
  { value: "MONTHLY", label: "Monthly" },
];

export function PeriodToggle({ value, onChange }: Props) {
  return (
    <div className="inline-flex bg-muted border border-border rounded-lg p-1 gap-1">
      {OPTIONS.map((opt) => (
        <Button
          key={opt.value}
          size="sm"
          variant={value === opt.value ? "default" : "ghost"}
          onClick={() => onChange(opt.value)}
        >
          {opt.label}
        </Button>
      ))}
    </div>
  );
}
