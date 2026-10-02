"use client";

import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ChevronsDown, Save } from "lucide-react";
import { NettingAPI } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { CATEGORY_FIELDS, isFieldNA, type FieldDef, type SegmentRow } from "@/lib/nettingMatrixConfig";
import { Cell } from "./Cell";
import { useAdminAuthStore } from "@/stores/authStore";
import { canEdit } from "@/lib/permissions";

export function SegmentMatrix({ categoryId }: { categoryId: string }) {
  const qc = useQueryClient();
  const me = useAdminAuthStore((s) => s.admin);
  const canMutate = canEdit(me, "segment_settings");
  const fields = CATEGORY_FIELDS[categoryId] || [];
  const { data: segments, isLoading } = useQuery({
    queryKey: ["admin", "netting", "segments"],
    queryFn: () => NettingAPI.segments(),
  });

  const [edits, setEdits] = useState<Record<string, Record<string, any>>>({});
  const [saving, setSaving] = useState(false);

  function setEdit(segId: string, key: string, val: any) {
    setEdits((prev) => ({ ...prev, [segId]: { ...(prev[segId] || {}), [key]: val } }));
  }
  function getValue(seg: any, key: string) {
    if (edits[seg.id]?.[key] !== undefined) return edits[seg.id][key];
    return seg[key];
  }
  function rowOf(seg: any): SegmentRow {
    return {
      code: seg.name,
      name: seg.displayName,
      lotApplies: seg.lotApplies,
      qtyApplies: seg.qtyApplies,
      optionApplies: seg.optionApplies,
      expiryHoldApplies: seg.expiryHoldApplies,
      futureApplies: seg.futureApplies,
    };
  }

  /** Copy the top row's value for one column down to every other segment
   *  the field applies to. Sixteen segments is a lot of typing for a value
   *  that is usually meant to be the same everywhere. Stages edits only —
   *  nothing is written until Save. */
  function fillDown(f: FieldDef) {
    const rows = ((segments ?? []) as any[]).filter(
      (seg) => !isFieldNA(rowOf(seg), categoryId, f)
    );
    const src = rows[0];
    if (!src) return;
    const val = getValue(src, f.key) ?? null;
    setEdits((prev) => {
      const next = { ...prev };
      let n = 0;
      for (const seg of rows.slice(1)) {
        if ((getValue(seg, f.key) ?? null) === val) continue;
        next[seg.id] = { ...(next[seg.id] || {}), [f.key]: val };
        n++;
      }
      if (n === 0) toast.info(`Every segment already has this ${f.label}`);
      else toast.success(`${f.label} copied to ${n} segment(s) — press Save`);
      return next;
    });
  }

  // Self-heal stored select values that are no longer a valid option
  // (legacy enum like `marginCalcMode: "percent"` after we retired that
  // mode). Stage a dirty edit so the next Save normalises the row to a
  // current option.
  //
  // Critically: do NOT stage for null/undefined values. The backend
  // resolver has a defensive inference path (sniff intradayMargin > 100
  // → Times, else Fixed) that handles unset rows correctly. If we
  // pre-stage "fixed" here, an admin who edits `intradayMargin` from
  // default 100 → 700 expecting Times leverage will silently get
  // "Fixed · ₹100/lot" because Save commits both the staged
  // "marginCalcMode=fixed" AND keeps intradayMargin at 100 (the change
  // they thought they made never got typed in). Leave null alone — the
  // admin's explicit dropdown click is the only way to commit a mode.
  useEffect(() => {
    if (!segments || !fields.length) return;
    setEdits((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const seg of segments as any[]) {
        for (const f of fields) {
          if (f.type !== "select") continue;
          const stored = seg[f.key];
          // Leave unset values alone — backend resolver infers.
          if (stored === null || stored === undefined || stored === "") continue;
          const valid = (f.options ?? []).some((o: any) => String(o.v) === String(stored));
          if (valid) continue;
          const fallback = f.options?.[0]?.v;
          if (fallback === undefined) continue;
          if (next[seg.id]?.[f.key] === fallback) continue;
          next[seg.id] = { ...(next[seg.id] || {}), [f.key]: fallback };
          changed = true;
        }
      }
      return changed ? next : prev;
    });
    // intentional: rerun whenever the data refetches or the category changes
  }, [segments, fields]);

  const dirtyCount = Object.values(edits).reduce((s, e) => s + Object.keys(e).length, 0);

  async function saveAll() {
    setSaving(true);
    try {
      // Parallelise the PUTs. The old sequential loop made saving 14 dirty
      // segments take ~14× longer than necessary because every backend
      // request also does an O(N) Redis SCAN to invalidate the per-user
      // effective-settings cache. With Promise.all the round-trips overlap
      // and total wall time drops to ~one slow request, not the sum of all.
      const ids = Object.keys(edits);
      await Promise.all(ids.map((id) => NettingAPI.updateSegment(id, edits[id])));
      toast.success(`Saved ${dirtyCount} change${dirtyCount === 1 ? "" : "s"}`);
      setEdits({});
      qc.invalidateQueries({ queryKey: ["admin", "netting", "segments"] });
      // Also evict the user-side effective-settings cache key so any tab the
      // admin has open (terminal preview, etc.) refetches the new numbers on
      // its next 30 s window instead of holding stale values.
      qc.invalidateQueries({ queryKey: ["segment-settings"] });
    } catch (e: any) {
      toast.error(e.message || "Save failed");
    } finally {
      setSaving(false);
    }
  }

  if (isLoading) return <div className="text-sm text-muted-foreground">Loading segments…</div>;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-end">
        <Button
          onClick={saveAll}
          disabled={dirtyCount === 0 || !canMutate}
          title={canMutate ? undefined : "View-only access"}
          loading={saving}
        >
          <Save className="size-4" /> Save {dirtyCount > 0 ? `(${dirtyCount})` : ""}
        </Button>
      </div>
      {/* ── Mobile: card per segment ─────────────────────────────── */}
      <div className="md:hidden space-y-2">
        {(segments ?? []).map((seg: any) => {
          const segRow = rowOf(seg);
          const activeFields = fields.filter((f) => !isFieldNA(segRow, categoryId, f));
          if (activeFields.length === 0) return null;
          return (
            <div key={seg.id} className="rounded-lg border border-border bg-card">
              {/* Segment header */}
              <div className="flex items-center gap-2 border-b border-border px-3 py-2.5">
                <div className="size-7 rounded-md bg-muted flex items-center justify-center text-[10px] font-bold text-muted-foreground shrink-0">
                  {seg.displayName.slice(0, 2)}
                </div>
                <div>
                  <div className="text-xs font-semibold">{seg.displayName}</div>
                  <div className="text-[10px] font-mono text-muted-foreground">{seg.name}</div>
                </div>
                {Object.keys(edits[seg.id] ?? {}).length > 0 && (
                  <span className="ml-auto rounded-full bg-primary/15 px-1.5 py-0.5 text-[10px] font-semibold text-primary">
                    {Object.keys(edits[seg.id]).length} edited
                  </span>
                )}
              </div>
              {/* Fields grid */}
              <div className="grid grid-cols-2 gap-x-3 gap-y-3 p-3">
                {activeFields.map((f) => (
                  <div key={f.key} className="space-y-1">
                    <Label className="text-[10px] text-muted-foreground leading-none">
                      {f.label}
                    </Label>
                    <Cell
                      field={f}
                      na={false}
                      value={getValue(seg, f.key)}
                      dirty={edits[seg.id]?.[f.key] !== undefined}
                      onChange={(v) => setEdit(seg.id, f.key, v)}
                    />
                  </div>
                ))}
              </div>
            </div>
          );
        })}
      </div>

      {/* ── Desktop: horizontal scroll table ─────────────────────── */}
      <div className="hidden md:block overflow-x-auto rounded-lg border border-border bg-card">
        <table className="min-w-full text-xs">
          <thead className="bg-card">
            <tr className="border-b border-border">
              <th className="sticky left-0 z-10 bg-card px-3 py-2 text-left text-muted-foreground">
                Segment
              </th>
              {fields.map((f) => (
                <th key={f.key} className="whitespace-nowrap px-2 py-2 text-left text-muted-foreground">
                  <span className="inline-flex items-center gap-1">
                    {f.label}
                    {canMutate && (
                      <button
                        type="button"
                        onClick={() => fillDown(f)}
                        title="Copy the first segment's value to every segment"
                        className="rounded p-0.5 text-muted-foreground/60 hover:bg-muted hover:text-foreground"
                      >
                        <ChevronsDown className="size-3.5" />
                      </button>
                    )}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {(segments ?? []).map((seg: any) => {
              const segRow = rowOf(seg);
              return (
                <tr key={seg.id} className="hover:bg-muted/30">
                  <td className="sticky left-0 z-0 whitespace-nowrap bg-card px-3 py-2">
                    <div className="font-medium">{seg.displayName}</div>
                    <div className="text-[10px] font-mono text-muted-foreground">{seg.name}</div>
                  </td>
                  {fields.map((f) => (
                    <td key={f.key} className="px-1 py-1">
                      <Cell
                        field={f}
                        na={isFieldNA(segRow, categoryId, f)}
                        value={getValue(seg, f.key)}
                        dirty={edits[seg.id]?.[f.key] !== undefined}
                        onChange={(v) => setEdit(seg.id, f.key, v)}
                      />
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
