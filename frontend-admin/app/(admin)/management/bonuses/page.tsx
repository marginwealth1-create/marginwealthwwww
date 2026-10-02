"use client";

import { useState } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { cn } from "@/lib/utils";
import { BonusTemplatesTab } from "@/components/bonuses/BonusTemplatesTab";
import { BonusGrantsTab } from "@/components/bonuses/BonusGrantsTab";
import { BonusManualGrantTab } from "@/components/bonuses/BonusManualGrantTab";

const TABS = [
  { id: "templates", label: "Templates" },
  { id: "grants", label: "Grants" },
  { id: "manual", label: "Manual grant" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function BonusesPage() {
  const [tab, setTab] = useState<TabId>("templates");
  return (
    <div className="space-y-4">
      <PageHeader
        title="Bonuses"
        description="Deposit-bonus templates, grants, and manual credit. Requires BONUSES_ENABLED on the server."
      />
      <div className="flex gap-1 border-b border-border">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={cn(
              "-mb-px border-b-2 px-4 py-2 text-sm font-medium",
              tab === t.id
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>
      {tab === "templates" && <BonusTemplatesTab />}
      {tab === "grants" && <BonusGrantsTab />}
      {tab === "manual" && <BonusManualGrantTab />}
    </div>
  );
}
