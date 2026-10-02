"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Plus, Search, Trash2 } from "lucide-react";
import { ExpiryOverridesAPI, InstrumentAdminAPI, SettingsAPI } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { PageHeader } from "@/components/common/PageHeader";
import { useAdminAuthStore } from "@/stores/authStore";
import { cn } from "@/lib/utils";

interface UnderlyingCfg {
  label: string;
  symbol: string;
  color: string;
  max_expiries?: number | null;
}

const DEFAULT_COLOR = "emerald";
// Palette used to AUTO-assign a chip color per underlying. Color is purely
// a cosmetic accent on the user-side picker — it's not admin-managed, so we
// just cycle this palette on save to keep the chips visually distinct.
const COLOR_PALETTE = ["emerald", "violet", "rose", "amber", "sky", "fuchsia"];

const KEYS = {
  underlyings: "option_chain.underlyings",
  strikesAroundAtm: "option_chain.strikes_around_atm",
  strikesAroundAtmBySegment: "option_chain.strikes_around_atm_by_segment",
  maxExpiries: "option_chain.max_expiries",
  maxExpiriesByExchange: "option_chain.max_expiries_by_exchange",
};

const EXCHANGES = ["NSE", "BSE", "MCX"] as const;
type ExBucket = (typeof EXCHANGES)[number];
type MbeMap = Record<ExBucket, number>;

// Per-option-segment "strikes around ATM" — each option segment gets its own
// count so the operator can, e.g., show more Index-option strikes than Stock.
const OPT_SEGMENTS = [
  { key: "NSE_IDX_OPT", label: "Index Option", hint: "NIFTY / BANKNIFTY / FINNIFTY …" },
  { key: "NSE_STK_OPT", label: "Stock Option", hint: "RELIANCE / TCS / stock F&O" },
  { key: "MCX_OPT", label: "MCX Option", hint: "GOLD / SILVER / CRUDE …" },
  { key: "BSE_OPT", label: "BSE Option", hint: "SENSEX / BANKEX" },
] as const;
type SegKey = (typeof OPT_SEGMENTS)[number]["key"];
type SbsMap = Record<SegKey, number>;
const DEFAULT_SBS: SbsMap = { NSE_IDX_OPT: 10, NSE_STK_OPT: 10, MCX_OPT: 10, BSE_OPT: 10 };

export default function OptionChainAdminPage() {
  const qc = useQueryClient();
  const admin = useAdminAuthStore((s) => s.admin);

  // Scope is derived from WHO is logged in — no manual picker.
  //   SUPER_ADMIN → edits the platform-wide default (PlatformSetting).
  //   ADMIN       → edits an override that applies to ITS OWN pool users.
  //   BROKER      → edits an override that applies to ITS OWN sub-tree users.
  const role = admin?.role ?? "";
  const isSuper = role === "SUPER_ADMIN";
  const myKind: "ADMIN" | "BROKER" | null =
    role === "ADMIN" ? "ADMIN" : role === "BROKER" ? "BROKER" : null;
  const myId = admin?.id ?? "";
  const canOverride = !isSuper && !!myKind && !!myId;

  const { data: rows } = useQuery({
    queryKey: ["admin", "settings", "platform", "option_chain"],
    queryFn: () => SettingsAPI.platformList("option_chain"),
  });

  const { data: overrideRow, refetch: refetchOverride } = useQuery({
    queryKey: ["admin", "expiry-override", myKind, myId],
    queryFn: () => ExpiryOverridesAPI.get(myKind as any, myId),
    enabled: canOverride,
  });

  const [underlyings, setUnderlyings] = useState<UnderlyingCfg[]>([]);
  const [strikesAroundAtm, setStrikesAroundAtm] = useState<number>(15);
  const [sbs, setSbs] = useState<SbsMap>({ ...DEFAULT_SBS });
  const [mbe, setMbe] = useState<MbeMap>({ NSE: 6, BSE: 6, MCX: 6 });
  const [overrideUnderlyings, setOverrideUnderlyings] = useState<boolean>(true);
  const [overrideMaxExpiries, setOverrideMaxExpiries] = useState<boolean>(true);
  const [saving, setSaving] = useState(false);

  // Hydrate from global rows (super-admin) OR from my override row,
  // inheriting the global values for any field I don't shadow.
  useEffect(() => {
    if (!rows) return;
    const globalUnd =
      (rows.find((r: any) => r.key === KEYS.underlyings)?.value as UnderlyingCfg[] | undefined) ?? [];
    const globalStrikes = Number(rows.find((r: any) => r.key === KEYS.strikesAroundAtm)?.value) || 15;
    const globalMax = Number(rows.find((r: any) => r.key === KEYS.maxExpiries)?.value) || 6;
    const gmbeRaw = (rows.find((r: any) => r.key === KEYS.maxExpiriesByExchange)?.value || {}) as Record<string, any>;
    const globalMbe: MbeMap = {
      NSE: Number(gmbeRaw?.NSE) || globalMax || 6,
      BSE: Number(gmbeRaw?.BSE) || globalMax || 6,
      MCX: Number(gmbeRaw?.MCX) || globalMax || 6,
    };
    const gsbsRaw = (rows.find((r: any) => r.key === KEYS.strikesAroundAtmBySegment)?.value || {}) as Record<string, any>;
    const globalSbs = OPT_SEGMENTS.reduce((acc, s) => {
      acc[s.key] = Number(gsbsRaw?.[s.key]) || globalStrikes || 10;
      return acc;
    }, {} as SbsMap);
    setStrikesAroundAtm(globalStrikes);

    if (isSuper) {
      setUnderlyings(globalUnd);
      setMbe(globalMbe);
      setSbs(globalSbs);
      return;
    }
    if (!canOverride || !overrideRow) return;
    if (overrideRow.underlyings !== null && overrideRow.underlyings !== undefined) {
      setUnderlyings(overrideRow.underlyings as UnderlyingCfg[]);
      setOverrideUnderlyings(true);
    } else {
      setUnderlyings(globalUnd);
      setOverrideUnderlyings(false);
    }
    const ovMbe = (overrideRow as any).max_expiries_by_exchange;
    if (ovMbe !== null && ovMbe !== undefined) {
      setMbe({
        NSE: Number(ovMbe?.NSE) || globalMbe.NSE,
        BSE: Number(ovMbe?.BSE) || globalMbe.BSE,
        MCX: Number(ovMbe?.MCX) || globalMbe.MCX,
      });
      setOverrideMaxExpiries(true);
    } else if (overrideRow.max_expiries_fallback !== null && overrideRow.max_expiries_fallback !== undefined) {
      const f = Number(overrideRow.max_expiries_fallback);
      setMbe({ NSE: f, BSE: f, MCX: f });
      setOverrideMaxExpiries(true);
    } else {
      setMbe(globalMbe);
      setOverrideMaxExpiries(false);
    }
    const ovSbs = (overrideRow as any).strikes_around_atm_by_segment;
    if (ovSbs !== null && ovSbs !== undefined) {
      setSbs(
        OPT_SEGMENTS.reduce((acc, s) => {
          acc[s.key] = Number(ovSbs?.[s.key]) || globalSbs[s.key];
          return acc;
        }, {} as SbsMap),
      );
    } else {
      setSbs(globalSbs);
    }
  }, [rows, overrideRow, isSuper, canOverride]);

  const dirty = useMemo(() => {
    if (isSuper) {
      if (!rows) return false;
      const u = rows.find((r: any) => r.key === KEYS.underlyings)?.value;
      const s = Number(rows.find((r: any) => r.key === KEYS.strikesAroundAtm)?.value);
      const gmbe = (rows.find((r: any) => r.key === KEYS.maxExpiriesByExchange)?.value || {}) as Record<string, any>;
      if (s !== strikesAroundAtm) return true;
      for (const ex of EXCHANGES) {
        if ((Number(gmbe?.[ex]) || 0) !== mbe[ex]) return true;
      }
      const gsbs = (rows.find((r: any) => r.key === KEYS.strikesAroundAtmBySegment)?.value || {}) as Record<string, any>;
      for (const seg of OPT_SEGMENTS) {
        if ((Number(gsbs?.[seg.key]) || s || 15) !== sbs[seg.key]) return true;
      }
      return JSON.stringify(u ?? []) !== JSON.stringify(underlyings);
    }
    return canOverride;
  }, [rows, underlyings, strikesAroundAtm, sbs, mbe, isSuper, canOverride]);

  function addUnderlying() {
    setUnderlyings((p) => [...p, { label: "", symbol: "", color: DEFAULT_COLOR, max_expiries: null }]);
  }
  function updateUnderlying(idx: number, patch: Partial<UnderlyingCfg>) {
    setUnderlyings((p) => p.map((u, i) => (i === idx ? { ...u, ...patch } : u)));
  }
  function removeUnderlying(idx: number) {
    setUnderlyings((p) => p.filter((_, i) => i !== idx));
  }

  async function save() {
    if (!isSuper && !canOverride) {
      toast.error("Your account can't edit expiry settings");
      return;
    }
    for (const u of underlyings) {
      if (!u.label.trim() || !u.symbol.trim()) {
        toast.error("Every underlying needs a label and a symbol");
        return;
      }
    }
    for (const seg of OPT_SEGMENTS) {
      const n = sbs[seg.key];
      if (!Number.isFinite(n) || n < 1 || n > 100) {
        toast.error(`${seg.label}: strikes around ATM must be 1–100`);
        return;
      }
    }
    for (const ex of EXCHANGES) {
      if (mbe[ex] < 1 || mbe[ex] > 24) {
        toast.error(`${ex} max expiries must be between 1 and 24`);
        return;
      }
    }
    setSaving(true);
    try {
      const cleaned = underlyings.map((u, idx) => {
        const base: Record<string, any> = {
          label: u.label.trim(),
          symbol: u.symbol.trim().toUpperCase().replace(/\s+/g, ""),
          // Auto-assigned (not admin-managed) — keeps the picker chips distinct.
          color: u.color || COLOR_PALETTE[idx % COLOR_PALETTE.length],
        };
        const me = Number(u.max_expiries);
        if (Number.isFinite(me) && me > 0) base.max_expiries = me;
        return base;
      });

      if (isSuper) {
        await Promise.all([
          SettingsAPI.updatePlatform(KEYS.underlyings, cleaned),
          // Per-option-segment strike counts + a flat fallback (= the
          // Index-option value) for any legacy / unsegmented consumer and the
          // resolver's ultimate fallback.
          SettingsAPI.updatePlatform(KEYS.strikesAroundAtmBySegment, sbs),
          SettingsAPI.updatePlatform(KEYS.strikesAroundAtm, sbs.NSE_IDX_OPT),
          SettingsAPI.updatePlatform(KEYS.maxExpiriesByExchange, mbe),
          // Legacy single fallback mirrors NSE so old single-int consumers
          // + the resolver's ultimate fallback stay sane.
          SettingsAPI.updatePlatform(KEYS.maxExpiries, mbe.NSE),
        ]);
        toast.success("Platform default option-chain settings saved");
        qc.invalidateQueries({ queryKey: ["admin", "settings", "platform", "option_chain"] });
      } else {
        await ExpiryOverridesAPI.upsert(myKind as any, myId, {
          underlyings: overrideUnderlyings ? cleaned : null,
          max_expiries_fallback: overrideMaxExpiries ? mbe.NSE : null,
          max_expiries_by_exchange: overrideMaxExpiries ? mbe : null,
          strikes_around_atm_by_segment: overrideMaxExpiries ? sbs : null,
        });
        toast.success("Saved — applies to your users");
        refetchOverride();
      }
    } catch (e: any) {
      toast.error(e.message || "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function resetToDefault() {
    if (!canOverride) return;
    if (!confirm("Reset to the platform default? Your users will use the default expiry settings.")) return;
    try {
      await ExpiryOverridesAPI.remove(myKind as any, myId);
      toast.success("Reset to platform default");
      refetchOverride();
    } catch (e: any) {
      toast.error(e.message || "Failed to reset");
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Expiry / option-chain settings"
        description={
          isSuper
            ? "Applies to YOUR own clients (those not under any admin). The underlyings/stock list is shared platform-wide, but the expiry counts set here do NOT touch any admin's users — each admin / broker sets expiries for their own pool."
            : "These settings apply to YOUR users only. Leave a section on 'use default' to inherit the platform default; turn it on to customise it for your pool."
        }
        actions={
          <div className="flex flex-wrap items-center gap-2">
            {canOverride && overrideRow?.exists && (
              <Button variant="outline" onClick={resetToDefault}>Reset to default</Button>
            )}
            <Button onClick={save} disabled={!dirty} loading={saving}>Save changes</Button>
          </div>
        }
      />

      {canOverride && (
        <div className="rounded-md border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-[11px] text-emerald-700 dark:text-emerald-300">
          {overrideRow?.exists
            ? "Custom expiry settings are active for your users."
            : "Your users currently use the platform default. Save below to customise it for them."}
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Underlyings</CardTitle>
            <CardDescription>Chips shown across the top of the picker.</CardDescription>
            {canOverride && (
              <ToggleStrip
                enabled={overrideUnderlyings}
                onChange={setOverrideUnderlyings}
                label="Customise the underlyings list for my users"
                hint="When OFF, your users see the platform default list."
              />
            )}
          </CardHeader>
          <CardContent className={cn("space-y-2", canOverride && !overrideUnderlyings && "pointer-events-none opacity-50")}>
            {underlyings.length === 0 && (
              <div className="rounded-md border border-dashed border-border px-4 py-6 text-center text-xs text-muted-foreground">
                No underlyings configured. Add at least one.
              </div>
            )}
            {underlyings.map((u, idx) => (
              <div key={idx} className="grid grid-cols-[1fr_1.5fr_130px_auto] items-end gap-2 rounded-md border border-border bg-muted/10 p-2">
                <div className="space-y-1">
                  <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">Label</Label>
                  <Input value={u.label} onChange={(e) => updateUnderlying(idx, { label: e.target.value })} placeholder="Nifty" />
                </div>
                <UnderlyingSymbolField
                  value={u.symbol}
                  onChange={(symbol, suggestedLabel) => {
                    const patch: Partial<UnderlyingCfg> = { symbol };
                    if (!u.label.trim() && suggestedLabel) patch.label = suggestedLabel;
                    updateUnderlying(idx, patch);
                  }}
                />
                <div className="space-y-1">
                  <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">Show expiry month</Label>
                  <Input
                    type="number" min={1} max={24}
                    value={u.max_expiries ?? ""}
                    onChange={(e) => {
                      const raw = e.target.value;
                      updateUnderlying(idx, { max_expiries: raw === "" ? null : Number(raw) });
                    }}
                    placeholder="inherit"
                  />
                </div>
                <Button type="button" variant="ghost" size="icon" onClick={() => removeUnderlying(idx)} aria-label="Remove">
                  <Trash2 className="size-4 text-muted-foreground" />
                </Button>
              </div>
            ))}
            <Button type="button" variant="outline" size="sm" onClick={addUnderlying}>
              <Plus className="size-4" /> Add underlying
            </Button>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Display window</CardTitle>
            <CardDescription>How many strikes and expiries the picker exposes.</CardDescription>
            {canOverride && (
              <ToggleStrip
                enabled={overrideMaxExpiries}
                onChange={setOverrideMaxExpiries}
                label="Customise the expiry fallback for my users"
                hint="When OFF, your users use the platform default fallback."
              />
            )}
          </CardHeader>
          <CardContent className="space-y-4">
            {/* Per-option-segment "strikes around ATM" — Index / Stock / MCX /
                BSE option each get their own count. The chain renders 2N+1
                strikes centred on the LIVE ATM and auto-recentres as the
                underlying moves. Shown to super-admin (platform default) and
                to admin/broker (their own pool, gated by the toggle above). */}
            <div className={cn("space-y-2", canOverride && !overrideMaxExpiries && "pointer-events-none opacity-50")}>
              <div>
                <Label>Strikes around ATM — per option segment</Label>
                <p className="text-[11px] text-muted-foreground">
                  Each renders 2N+1 strikes centred on the live ATM, auto-recentring as price moves.
                  {canOverride ? " Applies to your users." : " Platform-wide default."}
                </p>
              </div>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                {OPT_SEGMENTS.map((seg) => (
                  <div key={seg.key} className="space-y-1">
                    <Label className="text-xs">{seg.label}</Label>
                    <Input
                      type="number" min={1} max={100}
                      value={sbs[seg.key]}
                      onChange={(e) =>
                        setSbs((p) => ({ ...p, [seg.key]: Number(e.target.value || 1) }))
                      }
                    />
                    <p className="text-[10px] leading-tight text-muted-foreground">
                      {seg.hint} · <span className="font-tabular">{sbs[seg.key] * 2 + 1}</span> strikes (ATM ± {sbs[seg.key]})
                    </p>
                  </div>
                ))}
              </div>
            </div>
            <div className={cn("space-y-3", canOverride && !overrideMaxExpiries && "pointer-events-none opacity-50")}>
              <p className="text-[11px] text-muted-foreground">
                Per-exchange fallback — applies to every instrument of that exchange (option chain + futures search) whose per-script "Show expiry month" is blank.
              </p>
              {EXCHANGES.map((ex) => (
                <div key={ex} className="space-y-1">
                  <Label>{ex} — max expiries</Label>
                  <Input
                    type="number" min={1} max={24}
                    value={mbe[ex]}
                    onChange={(e) => setMbe((p) => ({ ...p, [ex]: Number(e.target.value || 1) }))}
                  />
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function UnderlyingSymbolField({
  value, onChange,
}: { value: string; onChange: (symbol: string, suggestedLabel?: string) => void }) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const q = (value || "").trim();

  const { data: nfo } = useQuery({
    queryKey: ["admin", "underlyings", "NFO", q],
    queryFn: () => InstrumentAdminAPI.underlyings({ exchange: "NFO", q: q || undefined, limit: 15 }),
    enabled: open, staleTime: 60_000,
  });
  const { data: mcx } = useQuery({
    queryKey: ["admin", "underlyings", "MCX", q],
    queryFn: () => InstrumentAdminAPI.underlyings({ exchange: "MCX", q: q || undefined, limit: 15 }),
    enabled: open, staleTime: 60_000,
  });

  const suggestions = useMemo(() => {
    const seen = new Set<string>();
    const out: { symbol: string; exchange: "NFO" | "MCX" }[] = [];
    for (const s of nfo ?? []) {
      const up = String(s).toUpperCase();
      if (seen.has(up)) continue;
      seen.add(up); out.push({ symbol: up, exchange: "NFO" });
    }
    for (const s of mcx ?? []) {
      const up = String(s).toUpperCase();
      if (seen.has(up)) continue;
      seen.add(up); out.push({ symbol: up, exchange: "MCX" });
    }
    return out.slice(0, 20);
  }, [nfo, mcx]);

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (!wrapRef.current) return;
      if (!wrapRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  function pick(s: { symbol: string; exchange: string }) {
    const titleCase = s.symbol.length > 0 ? s.symbol.charAt(0) + s.symbol.slice(1).toLowerCase() : s.symbol;
    onChange(s.symbol, titleCase);
    setOpen(false);
  }

  return (
    <div className="relative space-y-1" ref={wrapRef}>
      <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">Symbol</Label>
      <div className="relative">
        <Search className="pointer-events-none absolute left-3 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={value}
          onChange={(e) => { onChange(e.target.value.toUpperCase()); setOpen(true); setActive(0); }}
          onFocus={() => setOpen(true)}
          onKeyDown={(e) => {
            if (!open || suggestions.length === 0) return;
            if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(suggestions.length - 1, a + 1)); }
            else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(0, a - 1)); }
            else if (e.key === "Enter") { e.preventDefault(); pick(suggestions[active]); }
            else if (e.key === "Escape") setOpen(false);
          }}
          placeholder="Search NIFTY / GOLD / RELIANCE…"
          className="pl-9 font-mono"
        />
      </div>
      {open && suggestions.length > 0 && (
        <div className="absolute left-0 right-0 top-full z-20 mt-1 max-h-56 overflow-y-auto rounded-md border border-border bg-background shadow-lg scrollbar-thin">
          {suggestions.map((s, i) => (
            <button
              key={`${s.exchange}|${s.symbol}`} type="button"
              onMouseEnter={() => setActive(i)} onClick={() => pick(s)}
              className={"flex w-full items-center justify-between gap-3 px-3 py-1.5 text-left text-xs transition-colors " + (i === active ? "bg-muted/50" : "hover:bg-muted/30")}
            >
              <span className="font-mono">{s.symbol}</span>
              <span className="rounded bg-muted/40 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-muted-foreground">{s.exchange}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function ToggleStrip({
  enabled, onChange, label, hint,
}: { enabled: boolean; onChange: (v: boolean) => void; label: string; hint?: string }) {
  return (
    <div className="mt-2 flex items-center gap-2 rounded-md border border-border bg-muted/10 px-2 py-1.5 text-[11px]">
      <input type="checkbox" checked={enabled} onChange={(e) => onChange(e.target.checked)} className="size-3.5 accent-primary" />
      <span className="font-semibold">{label}</span>
      {hint && <span className="text-muted-foreground">{hint}</span>}
    </div>
  );
}
