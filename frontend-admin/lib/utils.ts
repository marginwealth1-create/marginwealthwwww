import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

const inrFmt = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 2,
});

export function formatINR(value: number | string | null | undefined) {
  if (value === null || value === undefined || value === "") return "₹ 0.00";
  const n = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(n)) return "₹ 0.00";
  return inrFmt.format(n).replace("₹", "₹ ");
}

/** Compact INR using the Indian numbering scale — K (thousand), L (lakh =
 *  1,00,000), Cr (crore = 1,00,00,000). Use on tiles where the full digit
 *  string overflows the card (e.g. ₹7,42,50,67,910.40 in a 200 px box).
 *
 *  Values < 1,000 still render with two decimals so a wallet at ₹278.88
 *  doesn't get rounded to "₹0K". Above 1K we round to 1 dp; above 1Cr to 2 dp
 *  so traders still see the bulk of the precision on big totals. Negative
 *  values flip the sign in front of the symbol the same way `formatINR` does
 *  via Intl. */
export function formatINRCompact(value: number | string | null | undefined): string {
  // No more K / L / Cr abbreviations — user wants the exact amount with
  // standard Indian grouping (2,12,34,567.89) everywhere across user
  // app, admin panel, and web. Callers used to import this for compact
  // KPI tiles; switching to the full formatter is a no-op semantically
  // since admin reports never display below-paisa precision anyway.
  if (value === null || value === undefined || value === "") return "₹0.00";
  const n = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(n)) return "₹0.00";
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(n);
}

export function formatNumber(value: number | string | null | undefined, fractionDigits = 0) {
  if (value === null || value === undefined || value === "") return "0";
  const n = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(n)) return "0";
  return new Intl.NumberFormat("en-IN", {
    maximumFractionDigits: fractionDigits,
    minimumFractionDigits: fractionDigits,
  }).format(n);
}

export function pnlColor(value: number | string | null | undefined): string {
  const n = typeof value === "string" ? Number(value) : (value ?? 0);
  if (!Number.isFinite(n) || n === 0) return "text-muted-foreground";
  return n > 0 ? "text-profit" : "text-loss";
}

export function formatPercent(
  value: number | string | null | undefined,
  fractionDigits = 2,
): string {
  if (value === null || value === undefined || value === "") return "0.00%";
  const n = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(n)) return "0.00%";
  return `${n >= 0 ? "+" : ""}${n.toFixed(fractionDigits)}%`;
}
