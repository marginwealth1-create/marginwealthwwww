"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Gift } from "lucide-react";
import { BonusesUserAPI } from "@/lib/api";
import { formatINR } from "@/lib/utils";
import { BonusDrawer } from "./BonusDrawer";

/**
 * Wallet tile showing the bonus credit pool. Reads the bonus `credit` from
 * the wallet summary (NOT credit_limit). Renders nothing when the user has no
 * bonus credit AND no active bonus — which is always the case while the
 * feature is off (summary.credit == "0", /bonuses 503s), so it's inert then.
 */
export function BonusCard({ credit }: { credit?: string | number }) {
  const { data } = useQuery({
    queryKey: ["my-bonuses"],
    queryFn: () => BonusesUserAPI.myBonuses(),
    retry: false, // 503 when the feature is off — don't hammer
    refetchInterval: 15000,
  });
  const [open, setOpen] = useState(false);

  const list = data ?? [];
  const active = list.filter((b: any) => b.status === "ACTIVE");
  const creditNum = Number(credit ?? 0);
  if (creditNum <= 0 && active.length === 0) return null;

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="flex flex-col items-start gap-1 rounded-xl border border-primary/30 bg-primary/5 p-3 text-left transition hover:bg-primary/10"
      >
        <span className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-primary">
          <Gift className="size-3.5" /> Bonus credit
        </span>
        <span className="text-lg font-bold">{formatINR(creditNum)}</span>
        <span className="text-[11px] text-muted-foreground">
          {active.length} active · tap for details
        </span>
      </button>
      <BonusDrawer open={open} onClose={() => setOpen(false)} bonuses={list} />
    </>
  );
}
