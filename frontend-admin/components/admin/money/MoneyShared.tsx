"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Search } from "lucide-react";
import { Input } from "@/components/ui/input";
import { cn, formatINR } from "@/lib/utils";
import type { MoneyFilterParams } from "@/lib/api";

// ── Period filter ──────────────────────────────────────────────────────────
export type Period = { preset: string; from: string; to: string };

export const PRESETS: { key: string; label: string }[] = [
  { key: "all_time", label: "All time" },
  { key: "this_week", label: "This week" },
  { key: "last_week", label: "Last week" },
  { key: "this_month", label: "This month" },
  { key: "last_month", label: "Last month" },
];

// Default to the FULL history (#2 — deposit/withdrawal history must be
// all-time, not a rolling weekly/monthly window). Operators can still narrow
// it with the preset dropdown / date pickers.
export const DEFAULT_PERIOD: Period = { preset: "all_time", from: "", to: "" };

/** Preset wins when set; custom dates otherwise (mutually exclusive). */
export function periodToParams(p: Period): MoneyFilterParams {
  if (p.preset) return { preset: p.preset };
  return { from_date: p.from || undefined, to_date: p.to || undefined };
}

export function periodKey(p: Period): string {
  return p.preset ? `preset:${p.preset}` : `range:${p.from}:${p.to}`;
}

// ── Tabs (two separate routes) ──────────────────────────────────────────────
export function MoneyTabs() {
  const path = usePathname();
  const tabs = [
    { href: "/money-transactions", label: "By User" },
    { href: "/broker-deposits", label: "By Broker" },
  ];
  return (
    <div className="inline-flex rounded-lg border border-border bg-card p-1">
      {tabs.map((t) => {
        const active = path === t.href;
        return (
          <Link
            key={t.href}
            href={t.href}
            className={cn(
              "rounded-md px-4 py-1.5 text-sm font-medium transition-colors",
              active ? "bg-primary text-primary-foreground shadow-sm" : "text-muted-foreground hover:text-foreground",
            )}
          >
            {t.label}
          </Link>
        );
      })}
    </div>
  );
}

// ── Filter bar (search + period) ────────────────────────────────────────────
export function MoneyFilterBar({
  search,
  onSearch,
  searchPlaceholder,
  period,
  onPeriod,
}: {
  search: string;
  onSearch: (v: string) => void;
  searchPlaceholder: string;
  period: Period;
  onPeriod: (p: Period) => void;
}) {
  return (
    <div className="flex flex-col gap-3 rounded-xl border border-border bg-card/40 p-3 sm:flex-row sm:flex-wrap sm:items-center">
      <div className="relative flex-1 min-w-[220px]">
        <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          placeholder={searchPlaceholder}
          className="h-10 pl-9"
        />
      </div>
      <select
        value={period.preset}
        onChange={(e) => onPeriod({ preset: e.target.value, from: "", to: "" })}
        className="h-10 rounded-md border border-border bg-background px-3 text-sm"
      >
        {PRESETS.map((p) => (
          <option key={p.key} value={p.key}>
            {p.label}
          </option>
        ))}
        {!period.preset && <option value="">Custom</option>}
      </select>
      <div className="flex items-center gap-1.5">
        <input
          type="date"
          value={period.from}
          onChange={(e) => onPeriod({ preset: "", from: e.target.value, to: period.to })}
          className="h-10 rounded-md border border-border bg-background px-2 text-sm"
        />
        <span className="text-xs text-muted-foreground">→</span>
        <input
          type="date"
          value={period.to}
          onChange={(e) => onPeriod({ preset: "", from: period.from, to: e.target.value })}
          className="h-10 rounded-md border border-border bg-background px-2 text-sm"
        />
      </div>
    </div>
  );
}

// ── KPI tile (auto-shrink + clip so crore-scale ₹ never overflows) ──────────
export type Tone = "in" | "out" | "settled" | "net" | "muted";

function toneClasses(tone: Tone, value: number): { ring: string; text: string } {
  switch (tone) {
    case "in":
      return { ring: "border-emerald-500/30 bg-emerald-500/5", text: "text-emerald-500" };
    case "out":
      return { ring: "border-orange-500/30 bg-orange-500/5", text: "text-orange-500" };
    case "settled":
      return { ring: "border-amber-500/30 bg-amber-500/5", text: "text-amber-500" };
    case "net":
      return value < 0
        ? { ring: "border-red-500/30 bg-red-500/5", text: "text-red-500" }
        : { ring: "border-emerald-500/30 bg-emerald-500/5", text: "text-emerald-500" };
    default:
      return { ring: "border-border bg-card/40", text: "text-foreground" };
  }
}

/** Font auto-shrinks as the string grows (crore-scale ₹ ≈ 17 chars). */
function fitFontClass(s: string): string {
  const n = s.length;
  if (n <= 10) return "text-[26px] leading-7";
  if (n <= 13) return "text-[22px] leading-7";
  if (n <= 16) return "text-[18px] leading-6";
  if (n <= 19) return "text-[15px] leading-5";
  return "text-[13px] leading-5";
}

export function KpiTile({
  label,
  value,
  tone = "muted",
  money = true,
}: {
  label: string;
  value: number;
  tone?: Tone;
  money?: boolean;
}) {
  const display = money ? formatINR(value) : String(value);
  const tc = toneClasses(tone, value);
  return (
    <div className={cn("overflow-hidden rounded-xl border p-3", tc.ring)}>
      <div className="truncate text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className={cn("mt-1 overflow-hidden truncate font-bold tabular-nums", fitFontClass(display), tc.text)}>
        {display}
      </div>
    </div>
  );
}

// ── Cell helper: zero renders as a muted dash ───────────────────────────────
export function moneyCell(value: number, tone?: "in" | "out" | "settled" | "net") {
  const n = Number(value || 0);
  if (!n) return <span className="text-muted-foreground">–</span>;
  const cls =
    tone === "in"
      ? "text-emerald-500"
      : tone === "out"
        ? "text-orange-500"
        : tone === "settled"
          ? "text-amber-500"
          : tone === "net"
            ? n < 0
              ? "text-red-500"
              : "text-emerald-500"
            : "";
  return <span className={cn("tabular-nums", cls)}>{formatINR(n)}</span>;
}

// ── CSV download ────────────────────────────────────────────────────────────
export function downloadCSV(filename: string, headers: string[], rows: (string | number)[][]) {
  const esc = (v: string | number) => {
    const s = String(v ?? "");
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const csv = [headers.map(esc).join(","), ...rows.map((r) => r.map(esc).join(","))].join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
