"use client";

import { useQuery } from "@tanstack/react-query";
import { Info } from "lucide-react";
import { WalletAPI, type WdRule } from "@/lib/api";
import { cn, formatINR } from "@/lib/utils";

interface Props {
  kind: "deposit" | "withdrawal";
  className?: string;
}

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

// Inline info banner the deposit / withdrawal dialogs render at the top.
// Resolves rules via the user-side cascade endpoint (broker → admin →
// super-admin → global) so the values shown here are guaranteed to match
// the server-side validator that runs on submit — no chance of the user
// seeing one set of rules and the API rejecting on a different set.
//
// Operator spec 22-May: "user ko dikhe full rules — min, max, daily, allowed
// days, time — withdraw form ke upar inline banner". Keep this dense but
// readable on phone (4-up grid on sm+, 2-up on phone).
export function WdRulesBanner({ kind, className }: Props) {
  const { data, isLoading } = useQuery({
    queryKey: ["user", "wd-rules"],
    queryFn: () => WalletAPI.wdRules(),
    staleTime: 60_000,
  });

  if (isLoading || !data) {
    return (
      <div className={cn("rounded-lg border border-border bg-card px-3 py-2 text-xs text-muted-foreground", className)}>
        Loading rules…
      </div>
    );
  }

  const rule: WdRule = kind === "deposit" ? data.deposit : data.withdrawal;
  const isAllDays =
    !rule.allowed_days || rule.allowed_days.length === 0 || rule.allowed_days.length === 7;
  const daysLabel = isAllDays
    ? "All days"
    : rule.allowed_days!.map((d) => WEEKDAYS[d]).filter(Boolean).join(", ");

  return (
    <div
      className={cn(
        "rounded-lg border border-primary/30 bg-primary/5 px-3 py-2.5 text-xs",
        className,
      )}
    >
      <div className="flex items-center gap-1.5 pb-2">
        <Info className="size-3.5 text-primary" />
        <span className="font-semibold text-primary">
          {kind === "deposit" ? "Deposit rules" : "Withdrawal rules"}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-1.5">
        <RuleRow label="Min amount" value={formatINR(rule.min_amount)} />
        <RuleRow label="Max amount" value={formatINR(rule.max_amount)} />
        <RuleRow label="Allowed days" value={daysLabel} wide />
      </div>
      {/* Remark is optional on both deposit + withdrawal now (server no
          longer enforces mandatory_remark), so the old "required" warning
          is intentionally not shown. */}
    </div>
  );
}

function RuleRow({ label, value, wide = false }: { label: string; value: string; wide?: boolean }) {
  return (
    <div className={cn("min-w-0", wide && "col-span-2")}>
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className="truncate font-medium text-foreground" title={value}>
        {value}
      </div>
    </div>
  );
}
