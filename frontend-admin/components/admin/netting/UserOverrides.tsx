"use client";

import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ClipboardCopy, EraserIcon, Plus, RotateCcw, Save, Search, Trash2, X } from "lucide-react";
import { InstrumentAdminAPI, NettingAPI, UsersAPI } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { CATEGORY_FIELDS, isFieldNA, type SegmentRow } from "@/lib/nettingMatrixConfig";
import { Cell } from "./Cell";
import { CategoryChips } from "./CategoryChips";

export function UserOverrides({
  mode = "both",
}: { mode?: "segment" | "script" | "both" } = {}) {
  const qc = useQueryClient();
  const sp = useSearchParams();
  const deepLinkUser = sp.get("user");
  const [userQuery, setUserQuery] = useState("");
  const [user, setUser] = useState<any | null>(null);
  // Clear-all-overrides confirm dialog target. Stores the pill the
  // admin clicked the X on so the modal can show their user_code +
  // override_count for confirmation.
  const [clearTarget, setClearTarget] = useState<any | null>(null);
  const [clearing, setClearing] = useState(false);

  useEffect(() => {
    if (deepLinkUser && !user) {
      UsersAPI.detail(deepLinkUser).then(setUser).catch(() => {});
    }
  }, [deepLinkUser]);

  const { data: search } = useQuery({
    queryKey: ["admin", "users", "netting-search", userQuery],
    queryFn: () => UsersAPI.list({ q: userQuery, page_size: 8 }),
    enabled: userQuery.trim().length >= 2,
  });

  // Quick-pick: every user who already has at least one segment override.
  // Refetches after every save / reset / copy so the count stays current.
  // Only list users relevant to THIS tab: segment mode → users with a
  // segment-wide override; script mode → users with a per-symbol override.
  const overrideKind =
    mode === "script" ? "script" : mode === "segment" ? "segment" : undefined;
  const { data: usersWithOverrides } = useQuery({
    queryKey: ["admin", "netting", "users-with-overrides", overrideKind ?? "all"],
    queryFn: () => NettingAPI.usersWithOverrides(overrideKind),
    refetchOnWindowFocus: false,
  });

  const { data: segments } = useQuery({
    queryKey: ["admin", "netting", "segments"],
    queryFn: () => NettingAPI.segments(),
  });
  const { data: overrides } = useQuery({
    queryKey: ["admin", "netting", "user", user?.id],
    queryFn: () => NettingAPI.userOverrides(user.id),
    enabled: !!user,
  });
  // Per-segment values this user INHERITS (pool cascade below their own
  // override) — used to show the in-effect value as the cell placeholder
  // instead of the bare word "inherit".
  const { data: inherited } = useQuery({
    queryKey: ["admin", "netting", "user-inherited", user?.id],
    queryFn: () => NettingAPI.userInherited(user.id),
    enabled: !!user,
  });

  // ── Copy-from-another-user picker ──────────────────────────────
  const [copyOpen, setCopyOpen] = useState(false);
  const [copyQuery, setCopyQuery] = useState("");
  const [copying, setCopying] = useState(false);
  const { data: copySearch } = useQuery({
    queryKey: ["admin", "users", "netting-copy-search", copyQuery],
    queryFn: () => UsersAPI.list({ q: copyQuery, page_size: 8 }),
    enabled: copyQuery.trim().length >= 2,
  });

  async function copyFrom(source: any) {
    if (!user) return;
    if (source.id === user.id) {
      toast.error("Source and destination users must be different");
      return;
    }
    if (!confirm(`Copy ${source.user_code}'s segment overrides onto ${user.user_code}? Overwrites the existing override docs.`)) return;
    setCopying(true);
    try {
      await NettingAPI.copy({ source_user_id: source.id, target_user_ids: [user.id], overwrite: true });
      toast.success(`Copied segment overrides from ${source.user_code}`);
      setCopyOpen(false);
      setCopyQuery("");
      qc.invalidateQueries({ queryKey: ["admin", "netting", "user", user.id] });
      qc.invalidateQueries({ queryKey: ["admin", "netting", "users-with-overrides"] });
    } catch (e: any) {
      toast.error(e.message || "Copy failed");
    } finally {
      setCopying(false);
    }
  }

  const [category, setCategory] = useState("lot");
  const fields = CATEGORY_FIELDS[category] || [];
  const [edits, setEdits] = useState<Record<string, Record<string, any>>>({});
  const [saving, setSaving] = useState(false);

  function getOverride(segName: string, key: string) {
    const row = overrides?.find((r: any) => r.segment_name === segName && !r.symbol);
    return row?.[key];
  }
  function getValue(segName: string, key: string) {
    if (edits[segName]?.[key] !== undefined) return edits[segName][key];
    return getOverride(segName, key) ?? "";
  }
  function setEdit(segName: string, key: string, val: any) {
    setEdits((prev) => ({ ...prev, [segName]: { ...(prev[segName] || {}), [key]: val } }));
  }

  const dirtyCount = Object.values(edits).reduce((s, e) => s + Object.keys(e).length, 0);

  async function saveAll() {
    if (!user) return;
    setSaving(true);
    try {
      for (const segName of Object.keys(edits)) {
        await NettingAPI.upsertUserOverride(user.id, segName, edits[segName]);
      }
      toast.success(`Saved ${dirtyCount} change${dirtyCount === 1 ? "" : "s"}`);
      setEdits({});
      qc.invalidateQueries({ queryKey: ["admin", "netting", "user", user.id] });
      qc.invalidateQueries({ queryKey: ["admin", "netting", "users-with-overrides"] });
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setSaving(false);
    }
  }

  async function reset(segName: string) {
    if (!user) return;
    if (!confirm(`Remove ${user.user_code}'s override for ${segName}?`)) return;
    try {
      await NettingAPI.deleteUserOverride(user.id, segName);
      toast.success("Reset");
      qc.invalidateQueries({ queryKey: ["admin", "netting", "user", user.id] });
      qc.invalidateQueries({ queryKey: ["admin", "netting", "users-with-overrides"] });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  // Initials helper
  const initials = (name: string, code: string) =>
    (name?.trim()
      ? name.trim().split(/\s+/).map((n) => n[0]).join("").slice(0, 2)
      : code.slice(2, 4)
    ).toUpperCase();

  return (
    <>
      {/* ── Confirm reset dialog ──────────────────────────────────── */}
      <Dialog open={!!clearTarget} onOpenChange={(o) => { if (!o) setClearTarget(null); }}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>Reset overrides?</DialogTitle>
          </DialogHeader>
          {clearTarget && (
            <div className="space-y-2">
              <div className="flex items-center gap-3 rounded-lg border border-border bg-muted/30 p-3">
                <div className="size-10 rounded-full bg-red-500/15 flex items-center justify-center text-sm font-bold text-red-500">
                  {initials(clearTarget.full_name, clearTarget.user_code)}
                </div>
                <div>
                  <div className="text-sm font-semibold">{clearTarget.full_name || clearTarget.user_code}</div>
                  <div className="text-[11px] font-mono text-muted-foreground">{clearTarget.user_code}</div>
                </div>
              </div>
              <p className="text-xs text-muted-foreground">
                Remove all{" "}
                <span className="font-semibold text-foreground">{clearTarget.override_count}</span>{" "}
                segment / script override{clearTarget.override_count === 1 ? "" : "s"} for this user?
                They will fall back to your tier&apos;s default settings. Open positions are NOT affected.
              </p>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setClearTarget(null)} disabled={clearing}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              loading={clearing}
              disabled={clearing}
              onClick={async () => {
                if (!clearTarget) return;
                setClearing(true);
                try {
                  const r = await NettingAPI.clearAllUserOverrides(clearTarget.id);
                  toast.success(`Reset ${clearTarget.user_code} — removed ${r?.deleted ?? 0} override(s)`);
                  setClearTarget(null);
                  if (user?.id === clearTarget.id) setEdits({});
                  qc.invalidateQueries({ queryKey: ["admin", "netting", "user", clearTarget.id] });
                  qc.invalidateQueries({ queryKey: ["admin", "netting", "users-with-overrides"] });
                } catch (e: any) {
                  toast.error(e?.message ?? "Reset failed");
                } finally {
                  setClearing(false);
                }
              }}
            >
              <EraserIcon className="size-4" /> Reset overrides
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── Two-column layout: left = user list, right = settings ── */}
      <div className="flex flex-col gap-4 lg:grid lg:grid-cols-[300px_1fr] lg:items-start">

        {/* ─── LEFT PANEL: search + user list ───────────────────── */}
        <div className="space-y-3">

          {/* Search box */}
          <div className="rounded-lg border border-border bg-card p-3 space-y-2">
            <Label className="text-xs font-semibold text-foreground">Search User</Label>
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={userQuery}
                onChange={(e) => { setUserQuery(e.target.value); setUser(null); }}
                placeholder="code / email / name (min 2 chars)"
                className="pl-9"
              />
            </div>
            {/* Search dropdown */}
            {userQuery.trim().length >= 2 && !user && (
              <div className="max-h-52 overflow-y-auto rounded-md border border-border bg-background scrollbar-thin">
                {(search?.items ?? []).length === 0 ? (
                  <div className="px-3 py-3 text-xs text-muted-foreground">No matches.</div>
                ) : (
                  search?.items.map((u: any) => (
                    <button
                      key={u.id}
                      type="button"
                      onClick={() => { setUser(u); setUserQuery(""); }}
                      className="flex w-full items-center gap-2.5 border-b border-border/40 px-3 py-2 text-left text-xs last:border-b-0 hover:bg-muted/30"
                    >
                      <div className="size-7 rounded-full bg-muted flex items-center justify-center text-[10px] font-bold shrink-0">
                        {initials(u.full_name, u.user_code)}
                      </div>
                      <div className="min-w-0">
                        <div className="truncate font-medium">{u.full_name || "—"}</div>
                        <div className="font-mono text-[10px] text-muted-foreground">{u.user_code}</div>
                      </div>
                    </button>
                  ))
                )}
              </div>
            )}
          </div>

          {/* Users with overrides — card list */}
          {(usersWithOverrides?.length ?? 0) > 0 && (
            <div className="rounded-lg border border-amber-500/30 overflow-hidden">
              <div className="flex items-center justify-between border-b border-amber-500/20 bg-amber-500/5 px-3 py-2">
                <span className="text-xs font-semibold text-amber-700 dark:text-amber-300">
                  Overridden Users
                </span>
                <span className="rounded-full bg-amber-500/20 px-2 py-0.5 text-[10px] font-bold text-amber-700 dark:text-amber-300">
                  {usersWithOverrides?.length}
                </span>
              </div>
              <div className="divide-y divide-border max-h-[55vh] overflow-y-auto">
                {usersWithOverrides?.map((u: any) => {
                  const active = user?.id === u.id;
                  const ini = initials(u.full_name, u.user_code);
                  return (
                    <div
                      key={u.id}
                      role="button"
                      tabIndex={0}
                      onClick={() => { setUser(u); setUserQuery(""); }}
                      onKeyDown={(e) => e.key === "Enter" && (setUser(u), setUserQuery(""))}
                      className={
                        "group flex cursor-pointer items-center gap-2.5 px-3 py-2.5 transition-colors outline-none " +
                        (active
                          ? "bg-primary/10 border-l-2 border-l-primary"
                          : "hover:bg-amber-500/5 border-l-2 border-l-transparent")
                      }
                    >
                      {/* Avatar */}
                      <div
                        className={
                          "size-9 rounded-full flex items-center justify-center text-[11px] font-bold shrink-0 " +
                          (active
                            ? "bg-primary text-primary-foreground"
                            : "bg-amber-500/15 text-amber-700 dark:text-amber-300")
                        }
                      >
                        {ini}
                      </div>
                      {/* Name + code */}
                      <div className="min-w-0 flex-1">
                        <div className={
                          "truncate text-sm font-bold " +
                          (active ? "text-primary" : "text-foreground")
                        }>
                          {u.full_name || u.user_code}
                        </div>
                        <div className="font-mono text-[11px] font-semibold text-foreground/70">{u.user_code}</div>
                      </div>
                      {/* Count badge + Remove text — stacked on right */}
                      <div className="flex shrink-0 flex-col items-end gap-0.5">
                        <span
                          className={
                            "rounded-full px-2 py-0.5 text-[10px] font-bold tabular-nums " +
                            (active
                              ? "bg-primary/20 text-primary"
                              : "bg-amber-500/15 text-amber-600 dark:text-amber-400")
                          }
                        >
                          {u.override_count}
                        </span>
                        <button
                          type="button"
                          onClick={(e) => { e.stopPropagation(); setClearTarget(u); }}
                          className="text-[10px] font-medium text-red-500/70 opacity-0 transition-opacity hover:text-red-500 group-hover:opacity-100 focus:opacity-100"
                        >
                          Remove
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>

        {/* ─── RIGHT PANEL: settings for selected user ──────────── */}
        <div className="space-y-3 min-w-0">
          {!user ? (
            <div className="flex h-56 flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border text-center">
              <div className="size-12 rounded-full bg-muted/50 flex items-center justify-center">
                <Search className="size-5 text-muted-foreground" />
              </div>
              <div className="text-sm font-medium text-muted-foreground">No user selected</div>
              <div className="text-xs text-muted-foreground">
                Pick a user from the list or search above
              </div>
            </div>
          ) : (
            <>
              {/* Selected user header */}
              <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-primary/30 bg-primary/5 p-3">
                <div className="flex items-center gap-3">
                  <div className="size-10 rounded-full bg-primary/20 flex items-center justify-center text-sm font-bold text-primary shrink-0">
                    {initials(user.full_name, user.user_code)}
                  </div>
                  <div>
                    <div className="text-sm font-semibold">{user.full_name || "—"}</div>
                    <div className="text-[11px] font-mono text-muted-foreground">{user.user_code}</div>
                  </div>
                </div>
                <div className="flex items-center gap-1">
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-7 gap-1 text-[11px]"
                    onClick={() => setCopyOpen((o) => !o)}
                    title="Copy another user's segment overrides onto this user"
                  >
                    <ClipboardCopy className="size-3" /> Copy from…
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => { setUser(null); setUserQuery(""); setCopyOpen(false); }}
                  >
                    <X className="size-3" />
                  </Button>
                </div>
              </div>

              {/* Copy-from picker */}
              {copyOpen && (
                <div className="space-y-2 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
                  <Label className="text-[11px] text-amber-700 dark:text-amber-300">
                    Copy segment overrides from another user
                  </Label>
                  <div className="relative">
                    <Search className="pointer-events-none absolute left-3 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
                    <Input
                      value={copyQuery}
                      onChange={(e) => setCopyQuery(e.target.value)}
                      placeholder="code / email / name (min 2 chars)"
                      className="h-8 pl-9 text-xs"
                    />
                  </div>
                  {copyQuery.trim().length >= 2 && (
                    <div className="max-h-40 overflow-y-auto rounded-md border border-border bg-background scrollbar-thin">
                      {(copySearch?.items ?? []).filter((u: any) => u.id !== user.id).length === 0 ? (
                        <div className="px-3 py-3 text-xs text-muted-foreground">No matches.</div>
                      ) : (
                        copySearch?.items
                          .filter((u: any) => u.id !== user.id)
                          .map((u: any) => (
                            <button
                              key={u.id}
                              type="button"
                              disabled={copying}
                              onClick={() => copyFrom(u)}
                              className="flex w-full items-center gap-2.5 border-b border-border/40 px-3 py-2 text-left text-xs last:border-b-0 hover:bg-amber-500/10 disabled:opacity-50"
                            >
                              <div className="size-6 rounded-full bg-muted flex items-center justify-center text-[10px] font-bold shrink-0">
                                {initials(u.full_name, u.user_code)}
                              </div>
                              <div className="min-w-0 flex-1">
                                <div className="truncate font-medium">{u.full_name || "—"}</div>
                                <div className="font-mono text-[10px] text-muted-foreground">{u.user_code}</div>
                              </div>
                              <span className="text-[10px] text-amber-600 shrink-0">copy →</span>
                            </button>
                          ))
                      )}
                    </div>
                  )}
                  <p className="text-[10px] text-muted-foreground">
                    Copies every per-segment override from the source user onto {user.user_code}. Overwrites existing overrides.
                  </p>
                </div>
              )}

              {/* Category chips + Save */}
              <div className="flex flex-wrap items-center gap-2">
                <CategoryChips value={category} onChange={setCategory} />
                <Button
                  className="ml-auto"
                  onClick={saveAll}
                  disabled={dirtyCount === 0}
                  loading={saving}
                >
                  <Save className="size-4" /> Save {dirtyCount > 0 ? `(${dirtyCount})` : ""}
                </Button>
              </div>

              {mode !== "script" && (
              <>
              <div className="flex items-center gap-2 px-1 pt-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                <span>Segment-wide overrides</span>
                <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] normal-case tracking-normal text-foreground/70">
                  applies to every symbol in the segment
                </span>
              </div>

              <div className="overflow-x-auto rounded-lg border border-border bg-card">
                <table className="min-w-full text-xs">
                  <thead className="bg-card">
                    <tr className="border-b border-border">
                      <th className="sticky left-0 z-10 bg-card px-3 py-2 text-left text-muted-foreground">
                        Segment
                      </th>
                      {fields.map((f) => (
                        <th key={f.key} className="whitespace-nowrap px-2 py-2 text-left text-muted-foreground">
                          {f.label}
                        </th>
                      ))}
                      <th className="px-2 py-2 text-right text-muted-foreground">Reset</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {(segments ?? []).map((seg: any) => {
                      const segRow: SegmentRow = {
                        code: seg.name,
                        name: seg.displayName,
                        lotApplies: seg.lotApplies,
                        qtyApplies: seg.qtyApplies,
                        optionApplies: seg.optionApplies,
                        expiryHoldApplies: seg.expiryHoldApplies,
                        futureApplies: seg.futureApplies,
                      };
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
                                na={isFieldNA(segRow, category, f)}
                                value={getValue(seg.name, f.key)}
                                dirty={edits[seg.name]?.[f.key] !== undefined}
                                inheritPlaceholder
                                inheritValue={inherited?.[seg.name]?.[f.key]}
                                onChange={(v) => setEdit(seg.name, f.key, v)}
                              />
                            </td>
                          ))}
                          <td className="px-2 py-1 text-right">
                            <Button variant="ghost" size="icon" onClick={() => reset(seg.name)}>
                              <RotateCcw className="size-3.5" />
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

              {mode !== "segment" && (
              <UserScriptOverrides
                user={user}
                segments={segments ?? []}
                category={category}
                fields={fields}
                overrides={overrides ?? []}
                inherited={inherited}
              />
              )}
            </>
          )}
        </div>
      </div>
    </>
  );
}

// ─────────────────────────────────────────────────────────────────
// Per-symbol section
// ─────────────────────────────────────────────────────────────────
function UserScriptOverrides({
  user,
  segments,
  category,
  fields,
  overrides,
  inherited,
}: {
  user: any;
  segments: any[];
  category: string;
  fields: { key: string; label: string; type: string; options?: { v: any; l: string }[] }[];
  overrides: any[];
  inherited?: Record<string, Record<string, any>>;
}) {
  const qc = useQueryClient();

  // Rows already saved server-side (symbol non-null).
  const symbolRows = useMemo(
    () => (overrides ?? []).filter((r: any) => !!r.symbol),
    [overrides],
  );

  // Add-symbol form state.
  const [addOpen, setAddOpen] = useState(false);
  const [addSegment, setAddSegment] = useState<string>(segments[0]?.name ?? "");
  const [addSymbol, setAddSymbol] = useState("");
  // Symbol typeahead — search real instruments scoped to the picked segment
  // (same source the Script · Global tab uses), so admins don't have to know
  // exact symbols. Debounced so it doesn't hammer the endpoint per keystroke.
  const [symOpen, setSymOpen] = useState(false);
  const [addSymbolDebounced, setAddSymbolDebounced] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setAddSymbolDebounced(addSymbol), 250);
    return () => clearTimeout(t);
  }, [addSymbol]);
  // IDENTICAL picker to Script · Global: FUT/OPT segments search deduped
  // UNDERLYINGS and a pick fills the every-expiry pattern (SILVERFUT / <UND>CE),
  // NOT a single expiry contract; everything else searches real symbols.
  const _segU = (addSegment || "").toUpperCase();
  const { exchange: exchangeForSeg, mode: pickerMode } = (() => {
    if (_segU === "NSE_STK_FUT" || _segU === "NSE_IDX_FUT" || _segU === "NSE_FUT")
      return { exchange: "NFO", mode: "fut" as const };
    if (_segU === "NSE_STK_OPT" || _segU === "NSE_IDX_OPT" || _segU === "NSE_OPT")
      return { exchange: "NFO", mode: "opt" as const };
    if (_segU === "BSE_FUT") return { exchange: "BFO", mode: "fut" as const };
    if (_segU === "BSE_OPT") return { exchange: "BFO", mode: "opt" as const };
    if (_segU === "MCX_FUT") return { exchange: "MCX", mode: "fut" as const };
    if (_segU === "MCX_OPT") return { exchange: "MCX", mode: "opt" as const };
    if (_segU) return { exchange: undefined, mode: "eq" as const };
    return { exchange: undefined, mode: undefined };
  })();
  const { data: eqHits } = useQuery({
    queryKey: ["admin", "netting", "usw-eq", _segU, addSymbolDebounced],
    queryFn: () =>
      InstrumentAdminAPI.list({ q: addSymbolDebounced.trim(), netting_segment: _segU, page_size: 12 }),
    enabled: pickerMode === "eq" && !!_segU && addSymbolDebounced.trim().length >= 1,
    staleTime: 30_000,
    placeholderData: (prev: any) => prev,
  });
  const _futEx = pickerMode === "fut" || pickerMode === "opt" ? exchangeForSeg : undefined;
  const { data: undHits } = useQuery({
    queryKey: ["admin", "netting", "usw-und", _futEx, pickerMode, addSymbolDebounced],
    queryFn: () =>
      InstrumentAdminAPI.underlyings({
        exchange: _futEx!,
        contract_type: pickerMode === "fut" ? "FUT" : "CE",
        q: addSymbolDebounced.trim(),
        limit: 12,
      }),
    enabled: !!_futEx && addSymbolDebounced.trim().length >= 1,
    staleTime: 60_000,
    placeholderData: (prev: any) => prev,
  });

  // Per-(segment, symbol) edit drafts — same shape as the segment
  // table's `edits`, but the OUTER key includes the symbol so two
  // rows for the same segment but different symbols don't collide.
  const [edits, setEdits] = useState<Record<string, Record<string, any>>>({});
  const [saving, setSaving] = useState(false);

  function rowKey(seg: string, sym: string) {
    return `${seg}::${sym.toUpperCase()}`;
  }
  function getOverride(seg: string, sym: string, field: string) {
    const row = symbolRows.find(
      (r: any) => r.segment_name === seg && (r.symbol ?? "") === sym,
    );
    return row?.[field];
  }
  function getValue(seg: string, sym: string, field: string) {
    const key = rowKey(seg, sym);
    if (edits[key]?.[field] !== undefined) return edits[key][field];
    return getOverride(seg, sym, field) ?? "";
  }
  function setEdit(seg: string, sym: string, field: string, val: any) {
    const key = rowKey(seg, sym);
    setEdits((prev) => ({
      ...prev,
      [key]: { ...(prev[key] || {}), [field]: val },
    }));
  }

  const dirtyCount = Object.values(edits).reduce(
    (s, e) => s + Object.keys(e).length,
    0,
  );

  async function saveAll() {
    if (!user) return;
    setSaving(true);
    try {
      for (const key of Object.keys(edits)) {
        const [seg, sym] = key.split("::");
        if (!seg || !sym) continue;
        await NettingAPI.upsertUserOverride(user.id, seg, edits[key], sym);
      }
      toast.success(`Saved ${dirtyCount} script override change${dirtyCount === 1 ? "" : "s"}`);
      setEdits({});
      qc.invalidateQueries({ queryKey: ["admin", "netting", "user", user.id] });
      qc.invalidateQueries({ queryKey: ["admin", "netting", "users-with-overrides"] });
    } catch (e: any) {
      toast.error(e?.message ?? "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function deleteRow(seg: string, sym: string) {
    if (!user) return;
    if (!confirm(`Remove ${user.user_code}'s override for ${sym} in ${seg}?`)) return;
    try {
      await NettingAPI.deleteUserOverride(user.id, seg, sym);
      toast.success("Script override removed");
      qc.invalidateQueries({ queryKey: ["admin", "netting", "user", user.id] });
      qc.invalidateQueries({ queryKey: ["admin", "netting", "users-with-overrides"] });
    } catch (e: any) {
      toast.error(e?.message ?? "Remove failed");
    }
  }

  function addRow() {
    const sym = addSymbol.trim().toUpperCase();
    if (!addSegment) {
      toast.error("Pick a segment");
      return;
    }
    if (!sym) {
      toast.error("Enter a symbol");
      return;
    }
    const exists =
      symbolRows.some(
        (r: any) => r.segment_name === addSegment && (r.symbol ?? "") === sym,
      ) || edits[rowKey(addSegment, sym)] !== undefined;
    if (exists) {
      toast.info("That (segment, symbol) is already in the list — scroll up to edit it");
      return;
    }
    // Seed an empty draft so the row appears with inheritable
    // placeholders; admin then fills cells normally.
    setEdits((prev) => ({ ...prev, [rowKey(addSegment, sym)]: {} }));
    setAddSymbol("");
    setAddOpen(false);
  }

  // Union of saved rows + currently-drafted-but-unsaved rows.
  const allKeys = useMemo(() => {
    const set = new Set<string>();
    for (const r of symbolRows) {
      set.add(rowKey(r.segment_name, r.symbol));
    }
    for (const k of Object.keys(edits)) set.add(k);
    return [...set].sort();
  }, [symbolRows, edits]);

  return (
    <div className="space-y-2 pt-3">
      <div className="flex flex-wrap items-center gap-2 px-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
        <span>Script (per-symbol) overrides</span>
        <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] normal-case tracking-normal text-foreground/70">
          applies only to the picked symbol — most-specific layer in the resolver cascade
        </span>
        <div className="ml-auto flex items-center gap-2">
          {dirtyCount > 0 && (
            <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-[10px] font-bold normal-case tracking-normal text-amber-600 dark:text-amber-400">
              {dirtyCount} unsaved
            </span>
          )}
          <Button
            size="sm"
            variant="outline"
            onClick={() => setAddOpen((v) => !v)}
            className="h-7 gap-1 text-[11px] normal-case"
          >
            <Plus className="size-3" /> Add symbol
          </Button>
          <Button
            size="sm"
            onClick={saveAll}
            disabled={dirtyCount === 0}
            loading={saving}
            className="h-7 gap-1 text-[11px] normal-case"
          >
            <Save className="size-3" /> Save {dirtyCount > 0 ? `(${dirtyCount})` : ""}
          </Button>
        </div>
      </div>

      {addOpen && (
        <div className="grid grid-cols-1 gap-2 rounded-md border border-dashed border-border bg-muted/10 p-3 sm:grid-cols-[1fr_1fr_auto]">
          <div>
            <Label className="text-[11px]">Segment</Label>
            <select
              value={addSegment}
              onChange={(e) => setAddSegment(e.target.value)}
              className="mt-1 h-9 w-full rounded-md border border-border bg-background px-2 text-sm"
            >
              {segments.map((s: any) => (
                <option key={s.name} value={s.name}>
                  {s.displayName} ({s.name})
                </option>
              ))}
            </select>
          </div>
          <div className="relative">
            <Label className="text-[11px]">Symbol</Label>
            <Input
              value={addSymbol}
              onChange={(e) => {
                setAddSymbol(e.target.value);
                setSymOpen(true);
              }}
              onFocus={() => setSymOpen(true)}
              onBlur={() => setTimeout(() => setSymOpen(false), 150)}
              placeholder="Type to search — e.g. silver, nifty, btc"
              className="mt-1"
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  addRow();
                }
              }}
            />
            {symOpen && pickerMode && addSymbolDebounced.trim().length >= 1 && (
              <div className="absolute z-30 mt-1 max-h-64 w-full overflow-y-auto rounded-md border border-border bg-popover shadow-lg scrollbar-thin">
                {pickerMode === "eq" ? (
                  (eqHits?.items ?? []).length === 0 ? (
                    <div className="px-3 py-3 text-[11px] text-muted-foreground">No matching instruments.</div>
                  ) : (
                    (eqHits!.items as any[]).map((r) => {
                      const sym = r.trading_symbol || r.symbol;
                      return (
                        <button
                          key={r.id || sym}
                          type="button"
                          onMouseDown={(e) => { e.preventDefault(); setAddSymbol(sym); setSymOpen(false); }}
                          className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-xs hover:bg-muted/60"
                        >
                          <span className="truncate font-semibold">{sym}</span>
                          <span className="shrink-0 font-mono text-[10px] text-muted-foreground">{r.exchange || ""}</span>
                        </button>
                      );
                    })
                  )
                ) : pickerMode === "fut" ? (
                  (undHits ?? []).length === 0 ? (
                    <div className="px-3 py-3 text-[11px] text-muted-foreground">No matching underlyings.</div>
                  ) : (
                    (undHits as string[]).map((u) => (
                      <button
                        key={u}
                        type="button"
                        onMouseDown={(e) => { e.preventDefault(); setAddSymbol(`${u}FUT`); setSymOpen(false); }}
                        className="flex w-full items-center justify-between px-3 py-2 text-left text-xs hover:bg-muted/60"
                      >
                        <span className="font-mono font-semibold">{u}</span>
                        <span className="text-[10px] text-muted-foreground">
                          saves as <span className="font-mono">{u}FUT</span> — every expiry
                        </span>
                      </button>
                    ))
                  )
                ) : (undHits ?? []).length === 0 ? (
                  <div className="px-3 py-3 text-[11px] text-muted-foreground">No matching underlyings.</div>
                ) : (
                  (undHits as string[]).flatMap((u) =>
                    (["CE", "PE"] as const).map((side) => (
                      <button
                        key={`${u}-${side}`}
                        type="button"
                        onMouseDown={(e) => { e.preventDefault(); setAddSymbol(`${u}${side}`); setSymOpen(false); }}
                        className="flex w-full items-center justify-between px-3 py-2 text-left text-xs hover:bg-muted/60"
                      >
                        <span className="font-mono font-semibold">{u} <span className="text-muted-foreground">({side})</span></span>
                        <span className="text-[10px] text-muted-foreground">
                          saves as <span className="font-mono">{u}{side}</span> — every strike + expiry
                        </span>
                      </button>
                    ))
                  )
                )}
              </div>
            )}
          </div>
          <div className="flex items-end">
            <Button onClick={addRow} className="h-9">
              Add row
            </Button>
          </div>
        </div>
      )}

      <div className="overflow-x-auto rounded-lg border border-border bg-card">
        <table className="min-w-full text-xs">
          <thead className="bg-card">
            <tr className="border-b border-border">
              <th className="sticky left-0 z-10 bg-card px-3 py-2 text-left text-muted-foreground">
                Segment / Symbol
              </th>
              {fields.map((f) => (
                <th
                  key={f.key}
                  className="whitespace-nowrap px-2 py-2 text-left text-muted-foreground"
                >
                  {f.label}
                </th>
              ))}
              <th className="px-2 py-2 text-right text-muted-foreground">Remove</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {allKeys.length === 0 && (
              <tr>
                <td
                  colSpan={fields.length + 2}
                  className="px-3 py-6 text-center text-muted-foreground"
                >
                  No per-symbol overrides yet. Click <b>Add symbol</b> to create one for{" "}
                  {user?.user_code ?? "this user"}.
                </td>
              </tr>
            )}
            {allKeys.map((key) => {
              const [seg, sym] = key.split("::");
              const segMeta = segments.find((s: any) => s.name === seg);
              if (!segMeta) return null;
              const segRow: SegmentRow = {
                code: segMeta.name,
                name: segMeta.displayName,
                lotApplies: segMeta.lotApplies,
                qtyApplies: segMeta.qtyApplies,
                optionApplies: segMeta.optionApplies,
                expiryHoldApplies: segMeta.expiryHoldApplies,
                futureApplies: segMeta.futureApplies,
              };
              return (
                <tr key={key} className="hover:bg-muted/30">
                  <td className="sticky left-0 z-0 whitespace-nowrap bg-card px-3 py-2">
                    <div className="font-medium">{sym}</div>
                    <div className="text-[10px] font-mono text-muted-foreground">
                      {segMeta.name}
                    </div>
                  </td>
                  {fields.map((f) => (
                    <td key={f.key} className="px-1 py-1">
                      <Cell
                        field={f as any}
                        na={isFieldNA(segRow, category, f as any)}
                        value={getValue(seg, sym, f.key)}
                        dirty={edits[key]?.[f.key] !== undefined}
                        inheritPlaceholder
                        inheritValue={inherited?.[seg]?.[f.key]}
                        onChange={(v) => setEdit(seg, sym, f.key, v)}
                      />
                    </td>
                  ))}
                  <td className="px-2 py-1 text-right">
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() => deleteRow(seg, sym)}
                      title="Remove this per-symbol override"
                    >
                      <Trash2 className="size-3.5 text-destructive" />
                    </Button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
