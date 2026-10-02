"use client";

import { useState } from "react";
import { useSearchParams } from "next/navigation";
import { PageHeader } from "@/components/common/PageHeader";
import { CategoryChips } from "@/components/admin/netting/CategoryChips";
import { SegmentMatrix } from "@/components/admin/netting/SegmentMatrix";
import { ScriptOverrides } from "@/components/admin/netting/ScriptOverrides";
import { UserOverrides } from "@/components/admin/netting/UserOverrides";
import { cn } from "@/lib/utils";

// Four clearly-separated settings: segment (global + per-user) and script
// (global + per-user). Same cascade both sides — user-wise overrides the
// global for that one user; anything not set there keeps the global.
type Tab = "seg-global" | "seg-user" | "script-global" | "script-user";

const TABS: { id: Tab; label: string; description: string }[] = [
  {
    id: "seg-global",
    label: "Segment — Global",
    description: "Global per-segment defaults — each row applies to every instrument in that segment.",
  },
  {
    id: "seg-user",
    label: "Segment — User-wise",
    description: "Pick a user and override segment values just for them. Blank fields keep the global segment setting.",
  },
  {
    id: "script-global",
    label: "Script — Global",
    description: "Global per-symbol overrides within a segment. Blank = inherits the segment default.",
  },
  {
    id: "script-user",
    label: "Script — User-wise",
    description: "Pick a user and override a specific symbol just for them. Symbols not set here keep the global script / segment setting.",
  },
];

export default function SegmentSettingsPage() {
  const sp = useSearchParams();
  const raw = sp.get("tab");
  const initialTab: Tab = TABS.some((t) => t.id === raw) ? (raw as Tab) : "seg-global";
  const [tab, setTab] = useState<Tab>(initialTab);
  const [category, setCategory] = useState("lot");

  const meta = TABS.find((t) => t.id === tab) ?? TABS[0];
  // The category chips (Lot / Quantity / …) only drive the GLOBAL matrices.
  // Both user-wise tabs carry their own category chips inside UserOverrides.
  const showChips = tab === "seg-global" || tab === "script-global";

  return (
    <div className="space-y-4">
      <PageHeader title="Segment Settings" description={meta.description} />

      {/* Tabs: Segment (Global / User-wise) · Script (Global / User-wise).
          Horizontal-scrolls on small screens so all four stay reachable. */}
      <div className="sticky top-0 z-20 -mx-4 overflow-x-auto border-b border-border bg-background/95 px-4 py-2 backdrop-blur supports-[backdrop-filter]:bg-background/60 scrollbar-thin">
        <div className="inline-flex min-w-full gap-1">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => setTab(t.id)}
              className={cn(
                "whitespace-nowrap rounded-md px-3 py-2 text-xs font-medium transition-colors sm:px-4 sm:text-sm",
                tab === t.id
                  ? "bg-primary/15 text-primary"
                  : "text-muted-foreground hover:bg-muted/50 hover:text-foreground"
              )}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {showChips && <CategoryChips value={category} onChange={setCategory} />}

      {tab === "seg-global" && <SegmentMatrix categoryId={category} />}
      {tab === "seg-user" && <UserOverrides mode="segment" />}
      {tab === "script-global" && <ScriptOverrides categoryId={category} />}
      {tab === "script-user" && <UserOverrides mode="script" />}
    </div>
  );
}
