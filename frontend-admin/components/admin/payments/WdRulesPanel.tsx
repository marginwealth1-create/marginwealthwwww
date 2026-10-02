"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { PayinOutAPI } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

// Tier-aware deposit / withdrawal rules editor. Operator's 22-May spec:
// "super admin apne user ke liye, admin apne user ke liye, broker apne
// user ke liye". This panel reads from `GET /admin/wd-rules` which
// returns the CALLER'S OWN tier override (sparse — blanks mean "inherit
// from the layer below") plus the resolved effective values + per-field
// source labels. Form state seeds from the own row; saves go through
// `PUT /admin/wd-rules/{type}` which auto-targets the caller's tier.

type RuleFields = {
  min_amount: string | null;
  max_amount: string | null;
  daily_limit: string | null;
  allowed_days: number[] | null;
  allowed_times: { start: string; end: string }[] | null;
  charges_flat: string | null;
  charges_percent: number | null;
  auto_approve_under: string | null;
  mandatory_remark: boolean | null;
  block_withdrawal_with_open_positions: boolean | null;
  block_duplicate_pending: boolean | null;
  max_requests_per_day: number | null;
  require_bank_details: boolean | null;
  allow_upi_payout: boolean | null;
};

const WEEKDAYS = [
  { i: 0, label: "Mon" },
  { i: 1, label: "Tue" },
  { i: 2, label: "Wed" },
  { i: 3, label: "Thu" },
  { i: 4, label: "Fri" },
  { i: 5, label: "Sat" },
  { i: 6, label: "Sun" },
];

export function WdRulesPanel() {
  const { data: rules } = useQuery({
    queryKey: ["admin", "wd-rules"],
    queryFn: () => PayinOutAPI.wdRules(),
  });

  if (!rules) {
    return (
      <div className="rounded-md border border-border bg-card p-6 text-sm text-muted-foreground">
        Loading rules…
      </div>
    );
  }

  return (
    <section className="space-y-3">
      <div className="rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-xs">
        <p className="font-semibold text-primary">
          {rules.tier === "super_admin"
            ? "Super-admin pool"
            : rules.tier === "admin"
              ? "Your admin pool"
              : "Your broker pool"}
        </p>
        <p className="mt-0.5 text-muted-foreground">
          {rules.tier === "super_admin"
            ? "Applies to your direct users. Cascades into admin / broker pools as the fallback when they leave fields blank."
            : rules.tier === "admin"
              ? "Applies to users you own. Field left blank → inherits from super-admin."
              : "Applies to users in your sub-tree. Field left blank → inherits from your admin / super-admin."}
        </p>
      </div>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {rules.rules.map((r) => (
          <WdRuleCard key={r.rule_type} rule={r} />
        ))}
      </div>
    </section>
  );
}

function WdRuleCard({
  rule,
}: {
  rule: {
    rule_type: "DEPOSIT" | "WITHDRAWAL";
    own: any;
    effective: any;
    sources: Record<string, string>;
  };
}) {
  const qc = useQueryClient();
  // Form seeds from the OWN row only. A null here means "this admin
  // didn't override this field" — we surface the inherited value as a
  // placeholder so the operator always sees what users will actually
  // get even when nothing is set at this tier.
  const [form, setForm] = useState<RuleFields>(() => normaliseOwn(rule.own));
  const [saving, setSaving] = useState(false);

  function normaliseOwn(o: any): RuleFields {
    return {
      min_amount: o?.min_amount ?? null,
      max_amount: o?.max_amount ?? null,
      daily_limit: o?.daily_limit ?? null,
      allowed_days: Array.isArray(o?.allowed_days) ? o.allowed_days : null,
      allowed_times: Array.isArray(o?.allowed_times) ? o.allowed_times : null,
      charges_flat: o?.charges_flat ?? null,
      charges_percent: o?.charges_percent ?? null,
      auto_approve_under: o?.auto_approve_under ?? null,
      mandatory_remark: o?.mandatory_remark ?? null,
      block_withdrawal_with_open_positions:
        o?.block_withdrawal_with_open_positions ?? null,
      block_duplicate_pending: o?.block_duplicate_pending ?? null,
      require_bank_details: o?.require_bank_details ?? null,
      allow_upi_payout: o?.allow_upi_payout ?? null,
      max_requests_per_day:
        o?.max_requests_per_day === null || o?.max_requests_per_day === undefined
          ? null
          : Number(o.max_requests_per_day),
    };
  }

  function toggleDay(i: number) {
    const current =
      form.allowed_days ?? rule.effective.allowed_days ?? [0, 1, 2, 3, 4, 5, 6];
    const next = current.includes(i)
      ? current.filter((d: number) => d !== i)
      : [...current, i].sort();
    setForm((f) => ({ ...f, allowed_days: next }));
  }

  async function save() {
    setSaving(true);
    try {
      const payload: Record<string, any> = {
        min_amount: form.min_amount === "" || form.min_amount === null ? null : String(form.min_amount),
        max_amount: form.max_amount === "" || form.max_amount === null ? null : String(form.max_amount),
        mandatory_remark: form.mandatory_remark,
        // WITHDRAWAL-only toggle; the DEPOSIT card never renders it so its
        // form value stays null (no-op override) for deposit rows.
        block_withdrawal_with_open_positions:
          rule.rule_type === "WITHDRAWAL"
            ? form.block_withdrawal_with_open_positions
            : null,
        // Applies to BOTH cards — a pending deposit blocks a new deposit,
        // a pending withdrawal blocks a new withdrawal.
        block_duplicate_pending: form.block_duplicate_pending,
        // WITHDRAWAL-only: make full bank details mandatory on withdrawals.
        require_bank_details:
          rule.rule_type === "WITHDRAWAL" ? form.require_bank_details : null,
        // WITHDRAWAL-only: allow UPI as a payout channel (default ON).
        allow_upi_payout:
          rule.rule_type === "WITHDRAWAL" ? form.allow_upi_payout : null,
        // Per-day request cap (both cards). null = inherit, 0 = no limit.
        max_requests_per_day: form.max_requests_per_day,
        allowed_days: form.allowed_days,
        allowed_times: form.allowed_times,
        daily_limit: null,
        auto_approve_under: null,
        charges_flat: null,
        charges_percent: null,
      };

      await PayinOutAPI.updateWdRule(rule.rule_type, payload);
      toast.success("Saved");
      qc.invalidateQueries({ queryKey: ["admin", "wd-rules"] });
    } catch (e: any) {
      toast.error(e?.message || "Save failed");
    } finally {
      setSaving(false);
    }
  }

  const effActiveDays: number[] =
    rule.effective.allowed_days ?? [0, 1, 2, 3, 4, 5, 6];
  const formActiveDays: number[] = form.allowed_days ?? effActiveDays;

  return (
    <Card>
      <CardHeader className="space-y-1">
        <CardTitle className="text-base">{rule.rule_type}</CardTitle>
        <CardDescription className="text-xs">
          Blanks inherit from the layer below — leave a field empty if you
          don't want to override it.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-xs">
        <div className="grid grid-cols-2 gap-2">
          <RuleInput
            label="Min amount (₹)"
            value={form.min_amount}
            hint={inheritHint(rule, "min_amount", `₹${rule.effective.min_amount ?? 0}`)}
            onChange={(v) => setForm((f) => ({ ...f, min_amount: v }))}
          />
          <RuleInput
            label="Max amount (₹)"
            value={form.max_amount}
            hint={inheritHint(rule, "max_amount", `₹${rule.effective.max_amount ?? 0}`)}
            onChange={(v) => setForm((f) => ({ ...f, max_amount: v }))}
          />
        </div>

        {/* Per-day request cap — how many requests one user may submit per
            IST day. 0 = no limit. Blank inherits from the layer below. */}
        <div className="space-y-1">
          <Label className="text-xs">
            Max {rule.rule_type.toLowerCase()} requests / day (per user)
          </Label>
          <Input
            type="number"
            min={0}
            step={1}
            inputMode="numeric"
            value={form.max_requests_per_day ?? ""}
            placeholder={inheritHint(
              rule,
              "max_requests_per_day",
              `${rule.effective.max_requests_per_day ?? 0}`,
            )}
            onChange={(e) =>
              setForm((f) => ({
                ...f,
                max_requests_per_day:
                  e.target.value === ""
                    ? null
                    : Math.max(0, Math.floor(Number(e.target.value) || 0)),
              }))
            }
            className="h-8 text-xs"
          />
          <p className="text-[10px] text-muted-foreground">
            {form.max_requests_per_day === null
              ? `Inheriting from ${rule.sources.max_requests_per_day || "default"} — currently ${rule.effective.max_requests_per_day ?? 0} (0 = no limit)`
              : form.max_requests_per_day === 0
                ? "Will save: no limit"
                : `Will save: ${form.max_requests_per_day} per day`}
          </p>
        </div>

        <div className="space-y-1.5">
          <Label className="text-xs">Allowed days</Label>
          <div className="flex flex-wrap gap-1.5">
            {WEEKDAYS.map((d) => {
              const active = formActiveDays.includes(d.i);
              return (
                <button
                  key={d.i}
                  type="button"
                  onClick={() => toggleDay(d.i)}
                  className={
                    "rounded px-2.5 py-1 text-[11px] font-semibold ring-1 ring-inset transition-colors " +
                    (active
                      ? "bg-primary/15 text-primary ring-primary/30"
                      : "bg-muted/30 text-muted-foreground ring-border")
                  }
                >
                  {d.label}
                </button>
              );
            })}
            {form.allowed_days !== null && (
              <button
                type="button"
                onClick={() => setForm((f) => ({ ...f, allowed_days: null }))}
                className="rounded px-2.5 py-1 text-[11px] text-muted-foreground hover:underline"
              >
                Reset to inherit
              </button>
            )}
          </div>
          <p className="text-[10px] text-muted-foreground">
            {form.allowed_days === null
              ? `Inheriting from ${rule.sources.allowed_days || "default"} — currently ${formatDays(effActiveDays)}`
              : `Will save: ${formatDays(formActiveDays)}`}
          </p>
        </div>

        {/* ── Allowed time windows ── */}
        <div className="space-y-1.5">
          <Label className="text-xs">Allowed times (IST)</Label>
          <div className="space-y-1.5">
            {(form.allowed_times ?? []).map((w, idx) => (
              <div key={idx} className="flex items-center gap-2">
                <input
                  type="time"
                  value={w.start}
                  onChange={(e) => {
                    const next = [...(form.allowed_times ?? [])];
                    next[idx] = { ...next[idx], start: e.target.value };
                    setForm((f) => ({ ...f, allowed_times: next }));
                  }}
                  className="h-8 rounded-md border border-input bg-background px-2 text-xs"
                />
                <span className="text-muted-foreground">→</span>
                <input
                  type="time"
                  value={w.end}
                  onChange={(e) => {
                    const next = [...(form.allowed_times ?? [])];
                    next[idx] = { ...next[idx], end: e.target.value };
                    setForm((f) => ({ ...f, allowed_times: next }));
                  }}
                  className="h-8 rounded-md border border-input bg-background px-2 text-xs"
                />
                <button
                  type="button"
                  onClick={() => {
                    const next = (form.allowed_times ?? []).filter((_, i) => i !== idx);
                    setForm((f) => ({ ...f, allowed_times: next.length ? next : null }));
                  }}
                  className="text-[11px] text-destructive hover:underline"
                >
                  Remove
                </button>
              </div>
            ))}
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() =>
                setForm((f) => ({
                  ...f,
                  allowed_times: [...(f.allowed_times ?? []), { start: "09:00", end: "17:00" }],
                }))
              }
              className="rounded px-2.5 py-1 text-[11px] font-semibold ring-1 ring-inset bg-primary/15 text-primary ring-primary/30 hover:bg-primary/25 transition-colors"
            >
              + Add window
            </button>
            {form.allowed_times !== null && (
              <button
                type="button"
                onClick={() => setForm((f) => ({ ...f, allowed_times: null }))}
                className="rounded px-2.5 py-1 text-[11px] text-muted-foreground hover:underline"
              >
                Reset to inherit
              </button>
            )}
          </div>
          <p className="text-[10px] text-muted-foreground">
            {form.allowed_times === null
              ? rule.effective.allowed_times?.length
                ? `Inheriting from ${rule.sources.allowed_times || "global"} — currently ${formatTimes(rule.effective.allowed_times)}`
                : "No restriction — all hours allowed"
              : form.allowed_times.length === 0
                ? "No windows set — all hours allowed"
                : `Will save: ${formatTimes(form.allowed_times)}`}
          </p>
        </div>

        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={
              form.mandatory_remark === null
                ? !!rule.effective.mandatory_remark
                : !!form.mandatory_remark
            }
            onChange={(e) =>
              setForm((f) => ({ ...f, mandatory_remark: e.target.checked }))
            }
            className="size-4 accent-primary"
          />
          <span>Mandatory remark from user</span>
          {form.mandatory_remark === null && (
            <span className="text-[10px] text-muted-foreground">
              (inherited from {rule.sources.mandatory_remark || "default"})
            </span>
          )}
        </label>

        {rule.rule_type === "WITHDRAWAL" &&
          (() => {
            const isOn =
              form.block_withdrawal_with_open_positions === null
                ? !!rule.effective.block_withdrawal_with_open_positions
                : !!form.block_withdrawal_with_open_positions;
            return (
              <div className="rounded-lg border border-border bg-muted/20 p-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-sm font-semibold">
                      Block withdrawal with open positions
                    </p>
                    <p className="mt-0.5 text-[11px] text-muted-foreground">
                      When ON, a user holding ANY open trade can&apos;t request a
                      withdrawal until they close all positions.
                    </p>
                  </div>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={isOn}
                    onClick={() =>
                      setForm((f) => ({
                        ...f,
                        block_withdrawal_with_open_positions: !isOn,
                      }))
                    }
                    className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors ${
                      isOn ? "bg-primary" : "bg-muted-foreground/30"
                    }`}
                  >
                    <span
                      className={`inline-block size-4 transform rounded-full bg-white shadow transition-transform ${
                        isOn ? "translate-x-6" : "translate-x-1"
                      }`}
                    />
                  </button>
                </div>
                {form.block_withdrawal_with_open_positions === null && (
                  <p className="mt-1 text-[10px] text-muted-foreground">
                    (inherited from{" "}
                    {rule.sources.block_withdrawal_with_open_positions ||
                      "default"}
                    )
                  </p>
                )}
              </div>
            );
          })()}

        {rule.rule_type === "WITHDRAWAL" &&
          (() => {
            const isOn =
              form.require_bank_details === null
                ? !!rule.effective.require_bank_details
                : !!form.require_bank_details;
            return (
              <div className="rounded-lg border border-border bg-muted/20 p-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-sm font-semibold">
                      Bank details required
                    </p>
                    <p className="mt-0.5 text-[11px] text-muted-foreground">
                      When ON, users MUST enter full bank details (holder,
                      account number, IFSC) to withdraw — UPI stays optional.
                      Default OFF (UPI or bank, either works).
                    </p>
                  </div>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={isOn}
                    onClick={() =>
                      setForm((f) => ({ ...f, require_bank_details: !isOn }))
                    }
                    className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors ${
                      isOn ? "bg-primary" : "bg-muted-foreground/30"
                    }`}
                  >
                    <span
                      className={`inline-block size-4 transform rounded-full bg-white shadow transition-transform ${
                        isOn ? "translate-x-6" : "translate-x-1"
                      }`}
                    />
                  </button>
                </div>
                {form.require_bank_details === null && (
                  <p className="mt-1 text-[10px] text-muted-foreground">
                    (inherited from {rule.sources.require_bank_details || "default"})
                  </p>
                )}
              </div>
            );
          })()}

        {rule.rule_type === "WITHDRAWAL" &&
          (() => {
            // Default TRUE (UPI allowed) when neither this tier nor the
            // effective value is set.
            const eff =
              rule.effective.allow_upi_payout === undefined
                ? true
                : !!rule.effective.allow_upi_payout;
            const isOn =
              form.allow_upi_payout === null ? eff : !!form.allow_upi_payout;
            return (
              <div className="rounded-lg border border-border bg-muted/20 p-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-sm font-semibold">UPI payout allowed</p>
                    <p className="mt-0.5 text-[11px] text-muted-foreground">
                      When ON, users can withdraw to a UPI id. Turn OFF to force
                      bank-only withdrawals — the UPI option is hidden for users
                      and rejected server-side. Default ON.
                    </p>
                  </div>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={isOn}
                    onClick={() =>
                      setForm((f) => ({ ...f, allow_upi_payout: !isOn }))
                    }
                    className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors ${
                      isOn ? "bg-primary" : "bg-muted-foreground/30"
                    }`}
                  >
                    <span
                      className={`inline-block size-4 transform rounded-full bg-white shadow transition-transform ${
                        isOn ? "translate-x-6" : "translate-x-1"
                      }`}
                    />
                  </button>
                </div>
                {form.allow_upi_payout === null && (
                  <p className="mt-1 text-[10px] text-muted-foreground">
                    (inherited from {rule.sources.allow_upi_payout || "default"})
                  </p>
                )}
              </div>
            );
          })()}

        {/* Duplicate-pending gate — renders on BOTH the DEPOSIT and the
            WITHDRAWAL card. When ON, a user with a pending request of this
            type can't submit another until it's processed. */}
        {(() => {
          const isOn =
            form.block_duplicate_pending === null
              ? !!rule.effective.block_duplicate_pending
              : !!form.block_duplicate_pending;
          return (
            <div className="rounded-lg border border-border bg-muted/20 p-3">
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-sm font-semibold">
                    Block duplicate pending request
                  </p>
                  <p className="mt-0.5 text-[11px] text-muted-foreground">
                    When ON, a user with a pending request can&apos;t submit
                    another one until it&apos;s processed.
                  </p>
                </div>
                <button
                  type="button"
                  role="switch"
                  aria-checked={isOn}
                  onClick={() =>
                    setForm((f) => ({
                      ...f,
                      block_duplicate_pending: !isOn,
                    }))
                  }
                  className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors ${
                    isOn ? "bg-primary" : "bg-muted-foreground/30"
                  }`}
                >
                  <span
                    className={`inline-block size-4 transform rounded-full bg-white shadow transition-transform ${
                      isOn ? "translate-x-6" : "translate-x-1"
                    }`}
                  />
                </button>
              </div>
              {form.block_duplicate_pending === null && (
                <p className="mt-1 text-[10px] text-muted-foreground">
                  (inherited from{" "}
                  {rule.sources.block_duplicate_pending || "default"})
                </p>
              )}
            </div>
          );
        })()}

        <div className="flex justify-end pt-1">
          <Button size="sm" onClick={save} loading={saving} disabled={saving}>
            Save {rule.rule_type.toLowerCase()} rules
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function RuleInput({
  label,
  value,
  hint,
  onChange,
}: {
  label: string;
  value: string | null;
  hint: string;
  onChange: (v: string | null) => void;
}) {
  return (
    <div className="space-y-1">
      <Label className="text-xs">{label}</Label>
      <Input
        type="text"
        inputMode="decimal"
        value={value ?? ""}
        placeholder={hint}
        onChange={(e) => onChange(e.target.value === "" ? null : e.target.value)}
        className="h-8 text-xs"
      />
    </div>
  );
}

function inheritHint(
  rule: { sources: Record<string, string> },
  field: string,
  effectiveLabel: string,
): string {
  const src = rule.sources[field] || "default";
  if (src === "broker" || src === "admin" || src === "super_admin" || src === "global") {
    return `inherits ${effectiveLabel} from ${src}`;
  }
  return effectiveLabel;
}

function formatDays(days: number[]): string {
  if (!days.length) return "none";
  if (days.length === 7) return "All days";
  return days
    .map((d) => WEEKDAYS[d]?.label)
    .filter(Boolean)
    .join(", ");
}

function formatTimes(windows: { start: string; end: string }[]): string {
  if (!windows.length) return "all hours";
  return windows.map((w) => `${w.start}–${w.end}`).join(", ");
}
