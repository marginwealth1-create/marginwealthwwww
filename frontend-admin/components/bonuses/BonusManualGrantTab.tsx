"use client";

import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { BonusesAdminAPI } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

export function BonusManualGrantTab() {
  const { data: templates } = useQuery({ queryKey: ["bonus-templates"], queryFn: () => BonusesAdminAPI.listTemplates() });
  const [mode, setMode] = useState<"template" | "custom">("custom");
  const [userId, setUserId] = useState("");
  const [templateId, setTemplateId] = useState("");
  const [depositAmount, setDepositAmount] = useState("");
  const [amount, setAmount] = useState("");
  const [notes, setNotes] = useState("");

  const grant = useMutation({
    mutationFn: () => {
      if (!userId.trim()) throw new Error("User ObjectId required");
      if (mode === "template") {
        if (!templateId) throw new Error("Pick a template");
        return BonusesAdminAPI.grant({ user_id: userId.trim(), template_id: templateId, deposit_amount: Number(depositAmount || 0) });
      }
      return BonusesAdminAPI.grant({ user_id: userId.trim(), amount: Number(amount || 0), notes });
    },
    onSuccess: () => {
      toast.success("Bonus granted");
      setDepositAmount(""); setAmount(""); setNotes("");
    },
    onError: (e: any) => toast.error(e?.message || "Grant failed"),
  });

  return (
    <Card className="max-w-lg space-y-4 p-4">
      <div className="flex gap-2">
        {(["custom", "template"] as const).map((m) => (
          <button key={m} onClick={() => setMode(m)}
            className={cn("flex-1 rounded-md border px-3 py-2 text-sm font-semibold",
              mode === m ? "border-primary bg-primary/10 text-primary" : "border-border text-muted-foreground")}>
            {m === "custom" ? "Custom amount" : "From template"}
          </button>
        ))}
      </div>

      <div>
        <Label>User ObjectId</Label>
        <Input value={userId} onChange={(e) => setUserId(e.target.value)} placeholder="Mongo _id of the user" />
      </div>

      {mode === "template" ? (
        <>
          <div>
            <Label>Template</Label>
            <select className="h-9 w-full rounded-md border border-border bg-background px-2 text-sm" value={templateId} onChange={(e) => setTemplateId(e.target.value)}>
              <option value="">— select —</option>
              {(templates ?? []).map((t: any) => (
                <option key={t.id} value={t.id}>{t.name} ({t.type})</option>
              ))}
            </select>
          </div>
          <div>
            <Label>Deposit amount (₹) — bonus is computed from this</Label>
            <Input type="number" value={depositAmount} onChange={(e) => setDepositAmount(e.target.value)} />
          </div>
        </>
      ) : (
        <>
          <div>
            <Label>Bonus amount (₹)</Label>
            <Input type="number" value={amount} onChange={(e) => setAmount(e.target.value)} />
          </div>
          <div>
            <Label>Notes (optional)</Label>
            <Input value={notes} onChange={(e) => setNotes(e.target.value)} />
          </div>
        </>
      )}

      <Button className="w-full" onClick={() => grant.mutate()} disabled={grant.isPending}>
        {grant.isPending ? "Granting…" : "Grant bonus"}
      </Button>
    </Card>
  );
}
