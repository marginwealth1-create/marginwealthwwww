"use client";

import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Plus, Save, Trash2 } from "lucide-react";
import { InstrumentAdminAPI, NettingAPI } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { CATEGORY_FIELDS, isFieldNA } from "@/lib/nettingMatrixConfig";
import { Cell } from "./Cell";


export function ScriptOverrides({ categoryId }: { categoryId: string }) {
  const qc = useQueryClient();
  const fields = CATEGORY_FIELDS[categoryId] || [];
  const { data: segments } = useQuery({
    queryKey: ["admin", "netting", "segments"],
    queryFn: () => NettingAPI.segments(),
  });
  const [segmentName, setSegmentName] = useState<string>("");
  const { data: scripts, isLoading } = useQuery({
    queryKey: ["admin", "netting", "scripts", segmentName],
    queryFn: () => NettingAPI.scripts(segmentName || undefined),
    enabled: !!segments,
  });

  const [edits, setEdits] = useState<Record<string, Record<string, any>>>({});
  const [saving, setSaving] = useState(false);
  const [newSym, setNewSym] = useState("");
  const [newSymDebounced, setNewSymDebounced] = useState("");
  const [newSegId, setNewSegId] = useState("");
  const [adding, setAdding] = useState(false);
  const [selectingAll, setSelectingAll] = useState(false);
  const [clearing, setClearing] = useState(false);
  // Cap on how many symbols "Select all" will add as individual rows. Beyond
  // this a per-symbol approach is the wrong tool — the Segments tab sets one
  // value for the whole segment in a single row. Kept at the instruments
  // endpoint's own max page_size (200) so the whole set fetches in one call.
  const SELECT_ALL_CAP = 200;
  // True only when `newSym` came from clicking a typeahead suggestion (a real
  // instrument / valid pattern). Any manual keystroke clears it, so free-typed
  // junk like "VIBHOOTI" can't be Added — the Add button stays disabled until
  // a suggestion is picked. Backend `create_script` enforces the same rule.
  const [picked, setPicked] = useState(false);

  // Debounce the symbol input so the typeahead doesn't hammer the admin
  // instruments endpoint on every keystroke. 200 ms matches the user-side
  // panel's debounce — same "feels instant" feel without thrashing Mongo.
  useEffect(() => {
    const t = setTimeout(() => setNewSymDebounced(newSym), 200);
    return () => clearTimeout(t);
  }, [newSym]);

  // Decode the picked segment into the bits the picker needs:
  //   • exchange  — Kite business channel (NSE / NFO / BSE / BFO / MCX)
  //   • mode      — "eq" (cash stocks, exact match) | "fut" (futures
  //                 pattern) | "opt" (option pattern, expanded to two
  //                 rows per underlying for CE + PE)
  const newSeg = segments?.find((s: any) => s.id === newSegId);
  const segName = (newSeg?.name || "").toUpperCase();
  // Picker mode per segment:
  //  • "fut" / "opt" → derivative segments: search deduped underlyings from
  //    the Zerodha CSV (exchange = NFO / BFO / MCX); a pick fills the
  //    <UND>FUT / <UND>CE / <UND>PE pattern that covers every expiry/strike.
  //  • "eq" → everything else: cash stocks (NSE_EQ/BSE_EQ), CRYPTO, and the
  //    Infoway-fed FOREX / STOCKS / INDICES / COMMODITIES. These are searched
  //    as real instrument symbols scoped to the segment via `netting_segment`
  //    (the backend resolves the admin segment row → its instrument segments),
  //    which is what previously left those segments with NO typeahead at all.
  const { exchange: exchangeForSeg, mode: pickerMode } = (() => {
    if (segName === "NSE_STK_FUT" || segName === "NSE_IDX_FUT") return { exchange: "NFO", mode: "fut" as const };
    if (segName === "NSE_STK_OPT" || segName === "NSE_IDX_OPT") return { exchange: "NFO", mode: "opt" as const };
    if (segName === "NSE_FUT") return { exchange: "NFO", mode: "fut" as const };
    if (segName === "NSE_OPT") return { exchange: "NFO", mode: "opt" as const };
    if (segName === "BSE_FUT") return { exchange: "BFO", mode: "fut" as const };
    if (segName === "BSE_OPT") return { exchange: "BFO", mode: "opt" as const };
    if (segName === "MCX_FUT") return { exchange: "MCX", mode: "fut" as const };
    if (segName === "MCX_OPT") return { exchange: "MCX", mode: "opt" as const };
    if (segName) return { exchange: undefined, mode: "eq" as const };
    return { exchange: undefined, mode: undefined };
  })();

  // EQ-style segments: search real instrument symbols scoped to the picked
  // segment (works for cash, crypto AND Infoway markets).
  const { data: eqHits } = useQuery({
    queryKey: ["admin", "script-eq-hits", segName, newSymDebounced],
    queryFn: () =>
      InstrumentAdminAPI.list({
        q: newSymDebounced.trim(),
        netting_segment: segName,
        page_size: 12,
      }),
    enabled: pickerMode === "eq" && !!segName && newSymDebounced.trim().length >= 1,
    staleTime: 30_000,
    placeholderData: (prev) => prev,
  });

  // F&O segments: search deduped underlyings (one row per NIFTY /
  // BANKNIFTY / SBIN / …). For OPT segments we fetch CE underlyings
  // because the universe is the same as PE — the picker just renders
  // each underlying twice (once as "<UND> (CE)", once as "<UND> (PE)").
  const futExchanges = pickerMode === "fut" || pickerMode === "opt" ? exchangeForSeg : undefined;
  const { data: undHits } = useQuery({
    queryKey: ["admin", "script-und-hits", futExchanges, pickerMode, newSymDebounced],
    queryFn: () =>
      InstrumentAdminAPI.underlyings({
        exchange: futExchanges!,
        contract_type: pickerMode === "fut" ? "FUT" : "CE",
        q: newSymDebounced.trim(),
        limit: 12,
      }),
    enabled: !!futExchanges && newSymDebounced.trim().length >= 1,
    staleTime: 60_000,
    placeholderData: (prev) => prev,
  });

  const [typeaheadOpen, setTypeaheadOpen] = useState(false);

  function setEdit(id: string, key: string, val: any) {
    setEdits((prev) => ({ ...prev, [id]: { ...(prev[id] || {}), [key]: val } }));
  }
  function getValue(s: any, key: string) {
    if (edits[s.id]?.[key] !== undefined) return edits[s.id][key];
    return s[key];
  }

  const dirtyCount = Object.values(edits).reduce((s, e) => s + Object.keys(e).length, 0);

  async function saveAll() {
    setSaving(true);
    try {
      for (const id of Object.keys(edits)) {
        await NettingAPI.updateScript(id, edits[id]);
      }
      toast.success(`Saved ${dirtyCount} change${dirtyCount === 1 ? "" : "s"}`);
      setEdits({});
      qc.invalidateQueries({ queryKey: ["admin", "netting", "scripts"] });
    } catch (e: any) {
      toast.error(e.message || "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function addScript() {
    if (!newSegId) return toast.error("Pick a segment");
    if (!newSym.trim()) return toast.error("Enter symbol");
    if (!picked)
      return toast.error("Pick a symbol from the suggestions list (free-typed symbols aren't allowed)");
    const seg = segments?.find((s: any) => s.id === newSegId);
    if (!seg) return toast.error("Invalid segment");
    setAdding(true);
    try {
      await NettingAPI.createScript({
        segment_id: newSegId,
        segment_name: seg.name,
        symbol: newSym.trim().toUpperCase(),
        tradingSymbol: newSym.trim().toUpperCase(),
      });
      toast.success(`Added ${newSym.trim().toUpperCase()} to ${seg.name}`);
      setNewSym("");
      setPicked(false);
      qc.invalidateQueries({ queryKey: ["admin", "netting", "scripts"] });
    } catch (e: any) {
      toast.error(e.message || "Add failed");
    } finally {
      setAdding(false);
    }
  }

  // "Select all" — add EVERY symbol of the picked segment as its own override
  // row in one shot, so the admin can then set values and prune the few they
  // don't want. For FUT/OPT we add the underlying patterns (each covers all
  // expiries/strikes); for everything else we add the real instrument symbols.
  async function selectAll() {
    if (!newSegId) return toast.error("Pick a segment in 'Add symbol to' first");
    const seg = segments?.find((s: any) => s.id === newSegId);
    if (!seg) return toast.error("Invalid segment");
    setSelectingAll(true);
    try {
      let symbols: string[] = [];
      if (pickerMode === "fut") {
        const u = await InstrumentAdminAPI.underlyings({
          exchange: exchangeForSeg!,
          contract_type: "FUT",
          limit: 100,
        });
        symbols = (u ?? []).map((x) => `${x}FUT`);
      } else if (pickerMode === "opt") {
        const u = await InstrumentAdminAPI.underlyings({
          exchange: exchangeForSeg!,
          contract_type: "CE",
          limit: 100,
        });
        symbols = (u ?? []).flatMap((x) => [`${x}CE`, `${x}PE`]);
      } else {
        const res = await InstrumentAdminAPI.list({
          netting_segment: seg.name,
          page_size: SELECT_ALL_CAP,
        });
        const total = res?.meta?.total ?? (res?.items?.length ?? 0);
        if (total > SELECT_ALL_CAP) {
          toast.error(
            `${seg.displayName} has ${total} instruments — too many to add one-by-one. ` +
              `Use the Segments tab to set one value for the whole segment.`,
          );
          return;
        }
        symbols = (res?.items ?? []).map((r: any) => r.symbol).filter(Boolean);
      }
      symbols = Array.from(new Set(symbols.filter(Boolean)));
      if (symbols.length === 0)
        return toast.error("No instruments found for this segment");
      if (symbols.length > SELECT_ALL_CAP)
        return toast.error(
          `${symbols.length} symbols is too many to add one-by-one — use the Segments tab for a whole-segment value.`,
        );
      if (
        !confirm(
          `Add all ${symbols.length} ${seg.displayName} symbols as override rows?\n` +
            `You can set values below and remove any you don't want afterwards.`,
        )
      )
        return;
      const r = await NettingAPI.createScriptsBulk({
        segment_id: newSegId,
        segment_name: seg.name,
        symbols,
      });
      toast.success(`Added ${r.created} symbols to ${seg.name}. Set values below and Save.`);
      setSegmentName(seg.name); // switch the view filter so the new rows show
      qc.invalidateQueries({ queryKey: ["admin", "netting", "scripts"] });
    } catch (e: any) {
      toast.error(e.message || "Select all failed");
    } finally {
      setSelectingAll(false);
    }
  }

  // Remove every override row currently shown (after a confirm). Rows the
  // caller's tier can't edit are skipped, not fatal.
  async function clearAllShown() {
    const rows = scripts ?? [];
    if (rows.length === 0) return;
    if (!confirm(`Remove all ${rows.length} override row(s) shown?`)) return;
    setClearing(true);
    let ok = 0;
    let skipped = 0;
    for (const r of rows) {
      try {
        await NettingAPI.deleteScript(r.id);
        ok++;
      } catch {
        skipped++;
      }
    }
    toast.success(`Removed ${ok}${skipped ? `, ${skipped} skipped` : ""}`);
    qc.invalidateQueries({ queryKey: ["admin", "netting", "scripts"] });
    setClearing(false);
  }

  async function delScript(id: string, sym: string) {
    if (!confirm(`Remove override for ${sym}?`)) return;
    try {
      await NettingAPI.deleteScript(id);
      toast.success("Deleted");
      qc.invalidateQueries({ queryKey: ["admin", "netting", "scripts"] });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-2 rounded-md border border-border bg-muted/10 p-3 text-sm">
        <div className="space-y-1">
          <Label>Segment</Label>
          {/* ONE segment selector — drives BOTH the row filter AND the
              "add symbol" target. Picking a segment filters the list and
              enables the symbol box; "All segments" only views (can't add). */}
          <select
            value={segmentName}
            onChange={(e) => {
              const name = e.target.value;
              setSegmentName(name);
              const seg = segments?.find((s: any) => s.name === name);
              setNewSegId(seg?.id || "");
              // Switching segment invalidates the previously-picked symbol.
              setNewSym("");
              setPicked(false);
            }}
            className="h-9 rounded-md border border-border bg-background px-2 text-xs"
          >
            <option value="">— All segments —</option>
            {segments?.map((s: any) => (
              <option key={s.id} value={s.name}>
                {s.displayName}
              </option>
            ))}
          </select>
        </div>
        <div className="ml-auto flex items-end gap-2">
          <div className="relative space-y-1">
            <Label>Symbol</Label>
            <Input
              value={newSym}
              onChange={(e) => {
                setNewSym(e.target.value);
                setPicked(false); // manual typing => must re-pick from list
                setTypeaheadOpen(true);
              }}
              onFocus={() => setTypeaheadOpen(true)}
              onBlur={() => {
                // Delay close so click on a suggestion lands first.
                setTimeout(() => setTypeaheadOpen(false), 150);
              }}
              disabled={!newSegId}
              placeholder={newSegId ? "SBIN  or  NIFTYFUT (all NIFTY futs)" : "Pick a segment first"}
              className="h-9 w-64 uppercase disabled:cursor-not-allowed disabled:opacity-50"
            />
            {/* Typeahead popover — content depends on the segment kind:
                • EQ segments  →  real stock symbols (SBIN, RELIANCE, …)
                • FUT segments →  deduped underlyings (NIFTY, BANKNIFTY, …)
                                  each pick fills `<UND>FUT`.
                • OPT segments →  each underlying rendered twice as
                                  "<UND> (CE)" and "<UND> (PE)" so admin
                                  picks the side; result fills `<UND>CE`
                                  or `<UND>PE`. */}
            {typeaheadOpen && pickerMode && newSymDebounced.trim().length >= 1 && (
              <div className="absolute top-full z-30 mt-1 w-80 overflow-hidden rounded-md border border-border bg-popover shadow-lg">
                <div className="max-h-64 divide-y divide-border overflow-y-auto scrollbar-thin">
                  {pickerMode === "eq" ? (
                    (eqHits?.items ?? []).length === 0 ? (
                      <div className="px-3 py-3 text-[11px] text-muted-foreground">
                        No matching instruments.
                      </div>
                    ) : (
                      (eqHits!.items as any[]).map((r) => (
                        <button
                          key={r.id}
                          type="button"
                          onMouseDown={(e) => {
                            e.preventDefault();
                            setNewSym(r.symbol);
                            setPicked(true);
                            setTypeaheadOpen(false);
                          }}
                          className="flex w-full items-center justify-between px-3 py-2 text-left text-xs hover:bg-accent/50"
                        >
                          <span className="font-mono">{r.symbol}</span>
                          <span className="truncate text-[10px] text-muted-foreground">
                            {r.exchange} · {r.segment ?? r.instrument_type ?? ""}
                          </span>
                        </button>
                      ))
                    )
                  ) : pickerMode === "fut" ? (
                    (undHits ?? []).length === 0 ? (
                      <div className="px-3 py-3 text-[11px] text-muted-foreground">
                        No matching underlyings.
                      </div>
                    ) : (
                      (undHits as string[]).map((u) => (
                        <button
                          key={u}
                          type="button"
                          onMouseDown={(e) => {
                            e.preventDefault();
                            // FUT pattern — applies to every expiry.
                            setNewSym(`${u}FUT`);
                            setPicked(true);
                            setTypeaheadOpen(false);
                          }}
                          className="flex w-full items-center justify-between px-3 py-2 text-left text-xs hover:bg-accent/50"
                        >
                          <span className="font-mono font-semibold">{u}</span>
                          <span className="text-[10px] text-muted-foreground">
                            saves as <span className="font-mono">{u}FUT</span> — every expiry
                          </span>
                        </button>
                      ))
                    )
                  ) : (
                    // OPT: render each underlying twice — once for CE, once for PE.
                    (undHits ?? []).length === 0 ? (
                      <div className="px-3 py-3 text-[11px] text-muted-foreground">
                        No matching underlyings.
                      </div>
                    ) : (
                      (undHits as string[]).flatMap((u) => (
                        (["CE", "PE"] as const).map((side) => (
                          <button
                            key={`${u}-${side}`}
                            type="button"
                            onMouseDown={(e) => {
                              e.preventDefault();
                              setNewSym(`${u}${side}`);
                              setPicked(true);
                              setTypeaheadOpen(false);
                            }}
                            className="flex w-full items-center justify-between px-3 py-2 text-left text-xs hover:bg-accent/50"
                          >
                            <span className="font-mono font-semibold">
                              {u} <span className="text-muted-foreground">({side})</span>
                            </span>
                            <span className="text-[10px] text-muted-foreground">
                              saves as <span className="font-mono">{u}{side}</span> — every strike + expiry
                            </span>
                          </button>
                        ))
                      ))
                    )
                  )}
                </div>
              </div>
            )}
          </div>
          <Button onClick={addScript} loading={adding} disabled={!picked || !newSegId}>
            <Plus className="size-4" /> Add
          </Button>
          <Button
            variant="outline"
            onClick={selectAll}
            loading={selectingAll}
            disabled={!newSegId}
            title="Add every symbol of this segment as its own row, then edit/remove"
          >
            Select all
          </Button>
          <Button onClick={saveAll} disabled={dirtyCount === 0} loading={saving}>
            <Save className="size-4" /> Save {dirtyCount > 0 ? `(${dirtyCount})` : ""}
          </Button>
        </div>
      </div>

      {isLoading ? (
        <div className="text-sm text-muted-foreground">Loading scripts…</div>
      ) : (scripts ?? []).length === 0 ? (
        <div className="rounded-md border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
          No script overrides yet. Pick a segment + add a symbol above to start.
        </div>
      ) : (
        <>
        <div className="flex items-center justify-between">
          <span className="text-xs text-muted-foreground">
            {(scripts ?? []).length} override row(s)
            {segmentName ? ` in ${segmentName}` : " (all segments)"}
          </span>
          <Button variant="ghost" size="sm" onClick={clearAllShown} loading={clearing}>
            <Trash2 className="size-3.5 text-destructive" /> Clear all shown
          </Button>
        </div>
        <div className="overflow-x-auto rounded-lg border border-border bg-card">
          <table className="min-w-full text-xs">
            <thead className="bg-card">
              <tr className="border-b border-border">
                <th className="sticky left-0 z-10 bg-card px-3 py-2 text-left text-muted-foreground">
                  Script
                </th>
                {fields.map((f) => (
                  <th
                    key={f.key}
                    className="whitespace-nowrap px-2 py-2 text-left text-muted-foreground"
                  >
                    {f.label}
                  </th>
                ))}
                <th className="px-2 py-2 text-right text-muted-foreground">Del</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {(scripts ?? []).map((s: any) => {
                const segRow = segments?.find((g: any) => g.name === s.segment_name);
                return (
                  <tr key={s.id} className="hover:bg-muted/30">
                    <td className="sticky left-0 z-0 whitespace-nowrap bg-card px-3 py-2">
                      <div className="font-mono text-[11px]">{s.symbol}</div>
                      <div className="text-[10px] text-muted-foreground">{s.segment_name}</div>
                    </td>
                    {fields.map((f) => (
                      <td key={f.key} className="px-1 py-1">
                        <Cell
                          field={f}
                          na={isFieldNA(segRow as any, categoryId, f)}
                          value={getValue(s, f.key)}
                          dirty={edits[s.id]?.[f.key] !== undefined}
                          inheritPlaceholder
                          // Show the segment (global) default this symbol
                          // inherits — "inherit (₹X)" instead of bare
                          // "inherit" — so the admin sees the value actually
                          // in effect before overriding it.
                          inheritValue={segRow ? (segRow as any)[f.key] : undefined}
                          onChange={(v) => setEdit(s.id, f.key, v)}
                        />
                      </td>
                    ))}
                    <td className="px-2 py-1 text-right">
                      <Button variant="ghost" size="icon" onClick={() => delScript(s.id, s.symbol)}>
                        <Trash2 className="size-3.5 text-destructive" />
                      </Button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        </>
      )}
    </div>
  );
}
