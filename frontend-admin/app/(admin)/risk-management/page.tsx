"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { AlertTriangle, ClipboardCopy, LockKeyhole, RotateCcw, Save, Search, ShieldAlert, Timer, X } from "lucide-react";
import { RiskAPI, UsersAPI } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { PageHeader } from "@/components/common/PageHeader";
import { useAdminAuthStore } from "@/stores/authStore";
import { canEdit } from "@/lib/permissions";

type FieldType = "percent" | "int" | "boolean";

interface Field {
  key: string;
  label: string;
  type: FieldType;
  suffix?: string;
  help?: string;
}

const FIELDS: Field[] = [
  { key: "stopOutWarningPercent", label: "Stop-out warning", type: "percent", suffix: "%", help: "Notify the user when floating loss reaches this % of total wallet balance (available + used margin + credit limit). 0 = no warning" },
  { key: "stopOutPercent", label: "Stop-out", type: "percent", suffix: "%", help: "Force-close every open position when floating loss reaches this % of total wallet balance (available + used margin + credit limit). 0 = no auto-flatten" },
  { key: "exitOnlyMode", label: "Exit-only mode (no new entries)", type: "boolean", help: "When ON, validator rejects every new-entry order. Existing positions can still be closed" },
  { key: "profitTradeHoldMinSeconds", label: "Profit trade hold minimum", type: "int", suffix: "sec", help: "Minimum seconds a profitable trade must be held before user-initiated close is allowed. 0 = no hold" },
  { key: "lossTradeHoldMinSeconds", label: "Loss trade hold minimum", type: "int", suffix: "sec", help: "Minimum seconds a losing trade must be held before user-initiated close is allowed. 0 = no hold" },
  { key: "staleFeedBlockSec", label: "Block trading on a stale price", type: "int", suffix: "sec", help: "Reject new orders when an instrument's exchange price has not moved for this long; trading resumes on the next price update. Skipped for 60s after the feed reconnects. 0 = off" },
];

/** Coerce a draft value to the right type before POST so we never send the
 *  fractional float that Pydantic 2.13 strict-mode rejects on the int fields. */
function coerce(field: Field, v: any): any {
  if (v === "" || v === undefined || v === null) return null;
  if (field.type === "int") return Math.round(Number(v));
  if (field.type === "percent") return Number(v);
  if (field.type === "boolean") return Boolean(v);
  return v;
}

export default function RiskManagementPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Risk Management"
        description="Set automatic protection levels for every user. Global rules apply by default — override individuals when needed."
      />

      {/* Friendly explainer strip — three plain-language tiles so admins
          who don't speak in stop-out percentages still understand what
          the three editable groups below actually do. */}
      <section className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <ExplainerTile
          icon={<ShieldAlert className="size-4" />}
          tone="emerald"
          title="Stop-out"
          desc="When a user's losses cross this %, all open positions auto-square-off to protect the wallet."
        />
        <ExplainerTile
          icon={<LockKeyhole className="size-4" />}
          tone="amber"
          title="Exit-only mode"
          desc="User can close existing positions but cannot open new trades. Useful during market panic."
        />
        <ExplainerTile
          icon={<Timer className="size-4" />}
          tone="sky"
          title="Hold timers"
          desc="Minimum / maximum time a trade must stay open. Prevents scalping abuse on illiquid scrips."
        />
      </section>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <GlobalCard />
        <UserCard />
      </div>
    </div>
  );
}

/**
 * Compact explainer tile used at the top of /risk-management. Pure
 * presentation — describes one risk concept in plain language so a
 * new admin understands what they're editing in the cards below.
 */
function ExplainerTile({
  icon,
  tone,
  title,
  desc,
}: {
  icon: React.ReactNode;
  tone: "emerald" | "amber" | "sky";
  title: string;
  desc: string;
}) {
  const tones = {
    emerald: {
      ring: "ring-emerald-500/30",
      badge: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
      gradient: "from-emerald-50 dark:from-emerald-500/10",
    },
    amber: {
      ring: "ring-amber-500/30",
      badge: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
      gradient: "from-amber-50 dark:from-amber-500/10",
    },
    sky: {
      ring: "ring-sky-500/30",
      badge: "bg-sky-500/15 text-sky-600 dark:text-sky-400",
      gradient: "from-sky-50 dark:from-sky-500/10",
    },
  }[tone];
  return (
    <div
      className={`rounded-xl border-0 bg-gradient-to-br ${tones.gradient} via-card to-card p-3 shadow-sm ring-1 ${tones.ring}`}
    >
      <div className="flex items-center gap-2">
        <span className={`inline-flex size-7 items-center justify-center rounded-lg ${tones.badge}`}>
          {icon}
        </span>
        <div className="text-sm font-semibold">{title}</div>
      </div>
      <p className="mt-2 text-[11px] leading-snug text-muted-foreground">{desc}</p>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// GLOBAL DEFAULT
// ─────────────────────────────────────────────────────────────────────
function GlobalCard() {
  const qc = useQueryClient();
  const me = useAdminAuthStore((s) => s.admin);
  const canMutate = canEdit(me, "risk");
  const { data: globalDoc, isFetching } = useQuery({
    queryKey: ["admin", "risk", "global"],
    queryFn: () => RiskAPI.getGlobal(),
  });

  // Hydrate the draft from server only when the user hasn't started editing.
  // Without this guard a background refetch every couple seconds would wipe
  // any in-progress edits — so the form would feel like it ignores typing.
  const [draft, setDraft] = useState<Record<string, any>>({});
  const dirtyRef = useRef(false);

  useEffect(() => {
    if (!globalDoc) return;
    if (dirtyRef.current) return; // user is mid-edit — don't clobber
    setDraft(globalDoc);
  }, [globalDoc]);

  function update(key: string, v: any) {
    dirtyRef.current = true;
    setDraft((d) => ({ ...d, [key]: v }));
  }

  const dirty = useMemo(() => {
    if (!globalDoc) return false;
    return FIELDS.some((f) => {
      const a = draft[f.key];
      const b = globalDoc[f.key];
      return a !== b && !(a === undefined && b === undefined);
    });
  }, [draft, globalDoc]);

  const [saving, setSaving] = useState(false);

  async function save() {
    setSaving(true);
    try {
      const patch: Record<string, any> = {};
      for (const f of FIELDS) {
        const v = coerce(f, draft[f.key]);
        // Global default carries a concrete value for every field — a
        // cleared/empty number box means 0 ("off"), not "inherit". (The
        // per-user override card keeps coerce()'s null = inherit semantics.)
        patch[f.key] = v === null ? 0 : v;
      }
      await RiskAPI.updateGlobal(patch);
      toast.success("Global risk settings saved");
      dirtyRef.current = false;
      await qc.invalidateQueries({ queryKey: ["admin", "risk", "global"] });
    } catch (e: any) {
      toast.error(e.message || "Save failed");
    } finally {
      setSaving(false);
    }
  }

  function discard() {
    if (!globalDoc) return;
    dirtyRef.current = false;
    setDraft(globalDoc);
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          Global default
          {isFetching && !globalDoc && <span className="text-xs text-muted-foreground">loading…</span>}
        </CardTitle>
        <CardDescription>Applies to every user unless an explicit override is set.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {FIELDS.map((f) => (
          <FieldRow key={f.key} field={f} value={draft[f.key]} onChange={(v) => update(f.key, v)} />
        ))}
        <div className="flex justify-end gap-2 pt-2">
          {dirty && (
            <Button variant="outline" onClick={discard} disabled={saving}>
              Discard
            </Button>
          )}
          <Button
            onClick={save}
            disabled={!dirty || !canMutate}
            title={canMutate ? undefined : "View-only access"}
            loading={saving}
          >
            <Save className="size-4" /> Save global default
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────────────
// PER-USER OVERRIDE
// ─────────────────────────────────────────────────────────────────────
function UserCard() {
  const qc = useQueryClient();
  const me = useAdminAuthStore((s) => s.admin);
  const canMutate = canEdit(me, "risk");
  const sp = useSearchParams();
  const deepLinkUserId = sp.get("user");

  const [query, setQuery] = useState("");
  const [user, setUser] = useState<any | null>(null);

  useEffect(() => {
    if (deepLinkUserId && !user) {
      UsersAPI.detail(deepLinkUserId).then((u) => setUser(u)).catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deepLinkUserId]);

  const { data: search } = useQuery({
    queryKey: ["admin", "users", "risk-search", query],
    queryFn: () => UsersAPI.list({ q: query, page_size: 8 }),
    enabled: query.trim().length >= 2,
  });

  // Quick-pick: every user who already has an override doc, with a count of
  // how many of the 8 fields they customised. Refetches after every save so
  // the count reflects the latest state.
  const { data: usersWithOverrides } = useQuery({
    queryKey: ["admin", "risk", "users-with-overrides"],
    queryFn: () => RiskAPI.usersWithOverrides(),
    refetchOnWindowFocus: false,
  });

  const { data: payload, isFetching: payloadLoading } = useQuery({
    queryKey: ["admin", "risk", "user", user?.id],
    queryFn: () => RiskAPI.getUser(user.id),
    enabled: !!user,
  });

  const [draft, setDraft] = useState<Record<string, any>>({});
  const dirtyRef = useRef(false);
  const [saving, setSaving] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [copying, setCopying] = useState(false);

  // Reset draft + dirty whenever a different user is selected, or when
  // the server payload arrives for the current user.
  useEffect(() => {
    dirtyRef.current = false;
    setDraft({});
  }, [user?.id]);

  useEffect(() => {
    if (!payload) return;
    if (dirtyRef.current) return;
    setDraft(payload.user_settings ?? {});
  }, [payload]);

  function update(key: string, v: any) {
    dirtyRef.current = true;
    setDraft((d) => ({ ...d, [key]: v }));
  }

  const dirty = useMemo(() => {
    const baseline = payload?.user_settings ?? {};
    return FIELDS.some((f) => {
      const a = draft[f.key] ?? null;
      const b = baseline[f.key] ?? null;
      return a !== b;
    });
  }, [draft, payload]);

  function inheritedFor(key: string) {
    return payload?.global_settings?.[key];
  }

  async function save() {
    if (!user) return;
    setSaving(true);
    try {
      const patch: Record<string, any> = {};
      for (const f of FIELDS) patch[f.key] = coerce(f, draft[f.key]);
      await RiskAPI.upsertUser(user.id, patch);
      toast.success(`Override saved for ${user.user_code}`);
      dirtyRef.current = false;
      await qc.invalidateQueries({ queryKey: ["admin", "risk", "user", user.id] });
      qc.invalidateQueries({ queryKey: ["admin", "risk", "users-with-overrides"] });
    } catch (e: any) {
      toast.error(e.message || "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function reset() {
    if (!user) return;
    if (!confirm(`Remove ${user.user_code}'s risk override (revert to global)?`)) return;
    setResetting(true);
    try {
      await RiskAPI.deleteUser(user.id);
      toast.success("Reset to global");
      dirtyRef.current = false;
      setDraft({});
      await qc.invalidateQueries({ queryKey: ["admin", "risk", "user", user.id] });
      qc.invalidateQueries({ queryKey: ["admin", "risk", "users-with-overrides"] });
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setResetting(false);
    }
  }

  // ── Copy-from-another-user picker ────────────────────────────────
  const [copyOpen, setCopyOpen] = useState(false);
  const [copyQuery, setCopyQuery] = useState("");
  const { data: copySearch } = useQuery({
    queryKey: ["admin", "users", "risk-copy-search", copyQuery],
    queryFn: () => UsersAPI.list({ q: copyQuery, page_size: 8 }),
    enabled: copyQuery.trim().length >= 2,
  });

  async function copyFrom(source: any) {
    if (!user) return;
    if (source.id === user.id) {
      toast.error("Source and destination users must be different");
      return;
    }
    if (!confirm(`Copy ${source.user_code}'s risk override onto ${user.user_code}? This overwrites any existing override.`)) return;
    setCopying(true);
    try {
      await RiskAPI.copyFromUser(user.id, source.id);
      toast.success(`Copied risk override from ${source.user_code}`);
      dirtyRef.current = false;
      setCopyOpen(false);
      setCopyQuery("");
      await qc.invalidateQueries({ queryKey: ["admin", "risk", "user", user.id] });
      qc.invalidateQueries({ queryKey: ["admin", "risk", "users-with-overrides"] });
    } catch (e: any) {
      toast.error(e.message || "Copy failed");
    } finally {
      setCopying(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Per-user override</CardTitle>
        <CardDescription>
          Pick a user and override any of the 5 fields. Empty = inherit global. You can also copy another user's override in one click.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* Quick-pick: users who already have an override doc */}
        {(usersWithOverrides?.length ?? 0) > 0 && (
          <div className="rounded-md border border-amber-500/30 bg-amber-500/5 p-2">
            <div className="mb-1.5 flex items-center justify-between">
              <Label className="text-[11px] text-amber-700 dark:text-amber-300">
                Users with custom risk override ({usersWithOverrides?.length})
              </Label>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {usersWithOverrides?.map((u: any) => {
                const active = user?.id === u.id;
                return (
                  <button
                    key={u.id}
                    type="button"
                    onClick={() => {
                      setUser(u);
                      setQuery("");
                    }}
                    className={
                      "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] transition-colors " +
                      (active
                        ? "border-primary bg-primary text-primary-foreground"
                        : "border-amber-500/40 bg-background text-foreground hover:bg-amber-500/10")
                    }
                    title={`${u.full_name} — ${u.override_count} field${u.override_count === 1 ? "" : "s"} overridden`}
                  >
                    <span className="font-mono">{u.user_code}</span>
                    <span className={active ? "text-primary-foreground/80" : "text-muted-foreground"}>
                      ({u.override_count})
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {/* User picker */}
        <div className="space-y-1.5">
          <Label>Search user</Label>
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setUser(null);
              }}
              placeholder="code / email / name (min 2 chars)"
              className="pl-9"
            />
          </div>
          {query.trim().length >= 2 && !user && (
            <div className="max-h-48 overflow-y-auto rounded-md border border-border bg-muted/10 scrollbar-thin">
              {(search?.items ?? []).length === 0 ? (
                <div className="px-3 py-3 text-xs text-muted-foreground">No matches.</div>
              ) : (
                search?.items.map((u: any) => (
                  <button
                    key={u.id}
                    type="button"
                    onClick={() => setUser(u)}
                    className="flex w-full items-center justify-between border-b border-border/40 px-3 py-2 text-left text-xs last:border-b-0 hover:bg-muted/30"
                  >
                    <span>
                      <span className="font-mono">{u.user_code}</span>
                      <span className="ml-2 text-muted-foreground">{u.full_name}</span>
                    </span>
                  </button>
                ))
              )}
            </div>
          )}
          {user && (
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-primary/30 bg-primary/5 p-2 text-xs">
              <div>
                <div className="font-medium">{user.user_code}</div>
                <div className="text-muted-foreground">{user.full_name}</div>
              </div>
              <div className="flex items-center gap-1">
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 gap-1 text-[11px]"
                  onClick={() => {
                    // Prefill the draft with the current global snapshot so the
                    // admin can tweak just a couple of fields. Until they hit
                    // Save the values aren't persisted — Discard reverts.
                    if (!payload?.global_settings) return;
                    const next: Record<string, any> = {};
                    for (const f of FIELDS) next[f.key] = payload.global_settings[f.key];
                    setDraft(next);
                    dirtyRef.current = true;
                  }}
                  title="Pre-fill all 5 override fields with the current global values so you can tweak just a few"
                  disabled={!payload?.global_settings}
                >
                  ⤓ Fill from global
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 gap-1 text-[11px]"
                  onClick={() => setCopyOpen((o) => !o)}
                  title="Copy another user's risk override onto this user"
                >
                  <ClipboardCopy className="size-3" /> Copy from…
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={() => {
                    setUser(null);
                    setQuery("");
                    setCopyOpen(false);
                  }}
                >
                  <X className="size-3" />
                </Button>
              </div>
            </div>
          )}

          {/* Copy-from picker */}
          {user && copyOpen && (
            <div className="mt-2 space-y-2 rounded-md border border-amber-500/30 bg-amber-500/5 p-2">
              <Label className="text-[11px] text-amber-700 dark:text-amber-300">
                Copy override from another user
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
                  {(copySearch?.items ?? []).length === 0 ? (
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
                          className="flex w-full items-center justify-between border-b border-border/40 px-3 py-2 text-left text-xs last:border-b-0 hover:bg-amber-500/10 disabled:opacity-50"
                        >
                          <span>
                            <span className="font-mono">{u.user_code}</span>
                            <span className="ml-2 text-muted-foreground">{u.full_name}</span>
                          </span>
                          <span className="text-[10px] text-amber-600">copy →</span>
                        </button>
                      ))
                  )}
                </div>
              )}
              <p className="text-[10px] text-muted-foreground">
                Copies the source user's per-user override onto {user.user_code}. Overwrites the existing override.
              </p>
            </div>
          )}
        </div>

        {/* Override form */}
        {user && (
          <div className="space-y-3 pt-2">
            {payloadLoading && !payload ? (
              <div className="py-6 text-center text-xs text-muted-foreground">Loading…</div>
            ) : (
              <>
                {FIELDS.map((f) => (
                  <FieldRow
                    key={f.key}
                    field={f}
                    value={draft[f.key]}
                    inheritValue={inheritedFor(f.key)}
                    onChange={(v) => update(f.key, v)}
                    allowInherit
                  />
                ))}
                <div className="flex flex-wrap justify-between gap-2 pt-2">
                  <Button
                    variant="outline"
                    onClick={reset}
                    loading={resetting}
                    disabled={!payload?.user_settings || !canMutate}
                    title={canMutate ? undefined : "View-only access"}
                  >
                    <RotateCcw className="size-4" /> Reset to global
                  </Button>
                  <Button
                    onClick={save}
                    loading={saving}
                    disabled={!dirty || !canMutate}
                    title={canMutate ? undefined : "View-only access"}
                  >
                    <Save className="size-4" /> Save override
                  </Button>
                </div>
              </>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────────────
// FIELD ROW
// ─────────────────────────────────────────────────────────────────────
function FieldRow({
  field,
  value,
  inheritValue,
  onChange,
  allowInherit,
}: {
  field: Field;
  value: any;
  inheritValue?: any;
  onChange: (v: any) => void;
  allowInherit?: boolean;
}) {
  const isEmpty = value === undefined || value === null || value === "";
  return (
    <div className="space-y-1.5">
      <Label className="flex items-center justify-between">
        <span>{field.label}</span>
        {allowInherit && isEmpty && inheritValue !== undefined && inheritValue !== null && (
          <span className="text-[10px] text-muted-foreground">
            inherits {String(inheritValue)}{field.suffix ? ` ${field.suffix}` : ""}
          </span>
        )}
      </Label>
      {field.type === "boolean" ? (
        <select
          value={isEmpty ? "" : value ? "true" : "false"}
          onChange={(e) => onChange(e.target.value === "" ? null : e.target.value === "true")}
          className="h-9 w-full rounded-md border border-border bg-background px-2 text-sm"
        >
          {allowInherit && <option value="">— inherit —</option>}
          <option value="true">Yes</option>
          <option value="false">No</option>
        </select>
      ) : (
        <div className="flex items-center gap-2">
          <Input
            type="number"
            // sec fields = whole numbers; percent fields = up to 2 decimals.
            step={field.type === "int" ? "1" : "0.01"}
            min={0}
            value={isEmpty ? "" : value}
            onChange={(e) => {
              const raw = e.target.value;
              if (raw === "") {
                // Empty box: an override means "inherit" (null); the global
                // default keeps it EMPTY ("") so the operator can actually
                // clear/retype it — backspacing used to snap it back to 0 and
                // it could never be emptied. save() coerces a left-empty
                // global field to 0 ("off").
                onChange(allowInherit ? null : "");
                return;
              }
              const num = Number(raw);
              if (Number.isNaN(num)) return;
              onChange(field.type === "int" ? Math.round(num) : num);
            }}
            placeholder={allowInherit ? "inherit" : ""}
          />
          {field.suffix && <span className="text-xs text-muted-foreground">{field.suffix}</span>}
        </div>
      )}
      {field.help && <p className="text-[10px] text-muted-foreground">{field.help}</p>}
    </div>
  );
}
