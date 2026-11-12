"use client";

import { cn } from "@/lib/utils";
import type { FieldDef } from "@/lib/nettingMatrixConfig";

// 0 on a brokerage field is a real "free"; 0 on these margin fields means
// "inherit" (backend ZERO_INHERITS_FIELDS) — never "no margin".
const COMMISSION_FIELDS = ["commission", "optionBuyCommission", "optionSellCommission"];
const ZERO_INHERITS_FIELDS = new Set([
  "intradayMargin", "overnightMargin",
  "optionBuyIntraday", "optionBuyOvernight", "optionSellIntraday", "optionSellOvernight",
  "expiryDayIntradayMargin", "expiryDayOptionBuyMargin", "expiryDayOptionSellMargin",
]);
const TIER_LABEL: Record<string, string> = {
  BROKER: "broker",
  SUB_ADMIN: "admin",
  SUPER_ADMIN: "super-admin",
  SEED: "platform default",
};

/** Ask before a save stages a ₹0 brokerage anywhere in `edits`
 *  ({ rowKey: { field: value } }). Returns false when the admin cancels. */
export function confirmZeroBrokerage(edits: Record<string, Record<string, any>>): boolean {
  const zero = Object.values(edits).some((e) => COMMISSION_FIELDS.some((k) => e[k] === 0));
  return !zero || window.confirm("Brokerage ₹0 — trades under this setting will be charged nothing. Save?");
}

function fmtValue(field: FieldDef, v: any): string {
  if (v === 0 && COMMISSION_FIELDS.includes(field.key)) return "₹0 — free";
  return field.options?.find((o) => String(o.v) === String(v))?.l ?? String(v);
}

export function Cell({
  field,
  value,
  dirty,
  na,
  onChange,
  inheritPlaceholder,
  inheritValue,
  inheritTier,
}: {
  field: FieldDef;
  value: any;
  dirty: boolean;
  na: boolean;
  onChange: (v: any) => void;
  inheritPlaceholder?: boolean;
  // The value this cell currently INHERITS (pool cascade). When present we
  // show it as the placeholder instead of the bare word "inherit", so the
  // admin sees what's in effect before they override it.
  inheritValue?: any;
  // Tier the inherited value comes from (segment matrix). When set, an empty
  // cell reads "Not set · <value> (<tier>)".
  inheritTier?: string;
}) {
  const hasInherit = inheritValue !== undefined && inheritValue !== null && inheritValue !== "";
  const shown = hasInherit ? fmtValue(field, inheritValue) : null;
  const inheritText = inheritTier
    ? `Not set · ${shown ?? "—"} (${TIER_LABEL[inheritTier] ?? inheritTier})`
    : shown ?? "inherit";
  const zeroHint =
    value === 0
      ? COMMISSION_FIELDS.includes(field.key)
        ? "₹0 — free"
        : ZERO_INHERITS_FIELDS.has(field.key)
        ? "0 = inherit"
        : null
      : null;
  if (na) {
    return <div className="px-2 py-1 text-center text-[10px] text-muted-foreground/50">—</div>;
  }
  const cls = cn(
    "h-7 w-full min-w-[100px] rounded border px-1 text-[11px]",
    dirty ? "border-amber-500/60 bg-amber-500/10" : "border-border bg-background/50"
  );
  if (field.type === "select") {
    return (
      <select
        value={value === undefined || value === null ? "" : String(value)}
        onChange={(e) => {
          const raw = e.target.value;
          const opt = field.options?.find((o) => String(o.v) === raw);
          // Empty string from a select always means "no value set" — emit
          // null so the backend Pydantic Literal accepts it (writes null on
          // the override doc and the resolver inherits segment-level).
          if (raw === "") {
            onChange(null);
            return;
          }
          onChange(opt ? opt.v : raw);
        }}
        className={cls}
      >
        {inheritPlaceholder && (
          <option value="">
            {inheritTier ? inheritText : hasInherit ? `inherit (${inheritText})` : "— inherit —"}
          </option>
        )}
        {/* The inherit row above already stands for a config "" option. */}
        {field.options?.filter((o) => !(inheritPlaceholder && o.v === "")).map((o) => (
          <option key={String(o.v)} value={String(o.v)}>
            {o.l}
          </option>
        ))}
      </select>
    );
  }
  const input = (
    <input
      type={field.type === "time" ? "time" : "number"}
      step={field.type === "number" ? "0.01" : undefined}
      value={value === undefined || value === null ? "" : value}
      onChange={(e) =>
        onChange(
          e.target.value === ""
            ? null
            : field.type === "number"
            ? Number(e.target.value)
            : e.target.value
        )
      }
      placeholder={inheritPlaceholder ? inheritText : ""}
      title={inheritPlaceholder && (value === undefined || value === null || value === "") ? inheritText : undefined}
      className={cls}
    />
  );
  if (!zeroHint) return input;
  return (
    <div className="relative">
      {input}
      <span className="pointer-events-none absolute right-5 top-1/2 -translate-y-1/2 text-[10px] text-amber-500">
        {zeroHint}
      </span>
    </div>
  );
}
