"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { BonusesAdminAPI } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const TYPES = ["FIRST_DEPOSIT", "REGULAR_DEPOSIT", "RELOAD", "SPECIAL"];
const CALC = ["PERCENTAGE", "FIXED"];

type Form = {
  id?: string;
  name: string;
  type: string;
  bonus_type: string;
  bonus_value: string;
  min_deposit: string;
  max_bonus: string;
  wager_requirement_multiple: string;
  duration_days: string;
  usage_limit: string;
  status: string;
  description: string;
};

const EMPTY: Form = {
  name: "", type: "FIRST_DEPOSIT", bonus_type: "PERCENTAGE", bonus_value: "",
  min_deposit: "0", max_bonus: "", wager_requirement_multiple: "0",
  duration_days: "30", usage_limit: "", status: "ACTIVE", description: "",
};

export function BonusTemplatesTab() {
  const qc = useQueryClient();
  const { data: rows } = useQuery({ queryKey: ["bonus-templates"], queryFn: () => BonusesAdminAPI.listTemplates() });
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<Form>(EMPTY);

  const save = useMutation({
    mutationFn: async () => {
      const body: any = {
        name: form.name.trim(),
        type: form.type,
        bonus_type: form.bonus_type,
        bonus_value: Number(form.bonus_value || 0),
        min_deposit: Number(form.min_deposit || 0),
        max_bonus: form.max_bonus === "" ? null : Number(form.max_bonus),
        wager_requirement_multiple: Number(form.wager_requirement_multiple || 0),
        duration_days: Number(form.duration_days || 0),
        usage_limit: form.usage_limit === "" ? null : Number(form.usage_limit),
        status: form.status,
        description: form.description,
      };
      return form.id ? BonusesAdminAPI.updateTemplate(form.id, body) : BonusesAdminAPI.createTemplate(body);
    },
    onSuccess: () => {
      toast.success(form.id ? "Template updated" : "Template created");
      setOpen(false); setForm(EMPTY);
      qc.invalidateQueries({ queryKey: ["bonus-templates"] });
    },
    onError: (e: any) => toast.error(e?.message || "Save failed"),
  });

  const del = useMutation({
    mutationFn: (id: string) => BonusesAdminAPI.deleteTemplate(id),
    onSuccess: () => { toast.success("Deleted"); qc.invalidateQueries({ queryKey: ["bonus-templates"] }); },
    onError: (e: any) => toast.error(e?.message || "Delete failed"),
  });

  function edit(t: any) {
    setForm({
      id: t.id, name: t.name, type: t.type, bonus_type: t.bonus_type,
      bonus_value: String(t.bonus_value ?? ""), min_deposit: String(t.min_deposit ?? "0"),
      max_bonus: t.max_bonus == null ? "" : String(t.max_bonus),
      wager_requirement_multiple: String(t.wager_requirement_multiple ?? 0),
      duration_days: String(t.duration_days ?? 30),
      usage_limit: t.usage_limit == null ? "" : String(t.usage_limit),
      status: t.status, description: t.description ?? "",
    });
    setOpen(true);
  }

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <Button onClick={() => { setForm(EMPTY); setOpen((o) => !o); }}>
          <Plus className="size-4" /> {open ? "Close" : "New template"}
        </Button>
      </div>

      {open && (
        <Card className="space-y-3 p-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div><Label>Name</Label><Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></div>
            <div>
              <Label>Type</Label>
              <select className="h-9 w-full rounded-md border border-border bg-background px-2 text-sm" value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value })}>
                {TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </div>
            <div>
              <Label>Calculation</Label>
              <select className="h-9 w-full rounded-md border border-border bg-background px-2 text-sm" value={form.bonus_type} onChange={(e) => setForm({ ...form, bonus_type: e.target.value })}>
                {CALC.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </div>
            <div><Label>{form.bonus_type === "PERCENTAGE" ? "Percent (%)" : "Amount (₹)"}</Label><Input type="number" value={form.bonus_value} onChange={(e) => setForm({ ...form, bonus_value: e.target.value })} /></div>
            <div><Label>Min deposit (₹)</Label><Input type="number" value={form.min_deposit} onChange={(e) => setForm({ ...form, min_deposit: e.target.value })} /></div>
            <div><Label>Max bonus (₹, blank = none)</Label><Input type="number" value={form.max_bonus} onChange={(e) => setForm({ ...form, max_bonus: e.target.value })} /></div>
            <div><Label>Wager × (0 = none)</Label><Input type="number" value={form.wager_requirement_multiple} onChange={(e) => setForm({ ...form, wager_requirement_multiple: e.target.value })} /></div>
            <div><Label>Duration (days)</Label><Input type="number" value={form.duration_days} onChange={(e) => setForm({ ...form, duration_days: e.target.value })} /></div>
            <div><Label>Usage limit (blank = ∞)</Label><Input type="number" value={form.usage_limit} onChange={(e) => setForm({ ...form, usage_limit: e.target.value })} /></div>
            <div>
              <Label>Status</Label>
              <select className="h-9 w-full rounded-md border border-border bg-background px-2 text-sm" value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value })}>
                <option value="ACTIVE">ACTIVE</option>
                <option value="INACTIVE">INACTIVE</option>
              </select>
            </div>
          </div>
          <div><Label>Description</Label><Input value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></div>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => { setOpen(false); setForm(EMPTY); }}>Cancel</Button>
            <Button onClick={() => save.mutate()} disabled={save.isPending || !form.name.trim()}>
              {save.isPending ? "Saving…" : form.id ? "Update" : "Create"}
            </Button>
          </div>
        </Card>
      )}

      <Card className="overflow-x-auto p-0">
        <table className="w-full text-sm">
          <thead className="border-b border-border text-left text-xs text-muted-foreground">
            <tr>
              <th className="p-2">Name</th><th className="p-2">Type</th><th className="p-2">Calc</th>
              <th className="p-2 text-right">Value</th><th className="p-2 text-right">Min dep</th>
              <th className="p-2 text-right">Max</th><th className="p-2 text-right">Wager</th>
              <th className="p-2 text-right">Days</th><th className="p-2 text-right">Used</th>
              <th className="p-2">Status</th><th className="p-2"></th>
            </tr>
          </thead>
          <tbody>
            {(rows ?? []).map((t: any) => (
              <tr key={t.id} className="border-b border-border/50">
                <td className="p-2 font-medium">{t.name}</td>
                <td className="p-2">{t.type}</td>
                <td className="p-2">{t.bonus_type}</td>
                <td className="p-2 text-right">{t.bonus_type === "PERCENTAGE" ? `${t.bonus_value}%` : `₹${t.bonus_value}`}</td>
                <td className="p-2 text-right">₹{t.min_deposit}</td>
                <td className="p-2 text-right">{t.max_bonus ? `₹${t.max_bonus}` : "—"}</td>
                <td className="p-2 text-right">{t.wager_requirement_multiple}×</td>
                <td className="p-2 text-right">{t.duration_days}</td>
                <td className="p-2 text-right">{t.used_count}{t.usage_limit ? `/${t.usage_limit}` : ""}</td>
                <td className="p-2">
                  <span className={t.status === "ACTIVE" ? "text-emerald-500" : "text-muted-foreground"}>{t.status}</span>
                </td>
                <td className="p-2">
                  <div className="flex justify-end gap-1">
                    <Button variant="ghost" size="icon" onClick={() => edit(t)}><Pencil className="size-4" /></Button>
                    <Button variant="ghost" size="icon" onClick={() => { if (confirm("Delete this template?")) del.mutate(t.id); }}>
                      <Trash2 className="size-4 text-destructive" />
                    </Button>
                  </div>
                </td>
              </tr>
            ))}
            {(rows ?? []).length === 0 && (
              <tr><td colSpan={11} className="p-6 text-center text-muted-foreground">No templates yet.</td></tr>
            )}
          </tbody>
        </table>
      </Card>
    </div>
  );
}
