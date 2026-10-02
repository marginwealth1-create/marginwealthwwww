import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

const inrFmt = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 2,
  minimumFractionDigits: 2,
});

const numFmt = new Intl.NumberFormat("en-IN", {
  maximumFractionDigits: 2,
  minimumFractionDigits: 2,
});

export function formatINR(value: number | string | null | undefined, opts?: { withSymbol?: boolean }) {
  if (value === null || value === undefined || value === "") return opts?.withSymbol === false ? "0.00" : "₹ 0.00";
  const n = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(n)) return opts?.withSymbol === false ? "0.00" : "₹ 0.00";
  return opts?.withSymbol === false ? numFmt.format(n) : inrFmt.format(n).replace("₹", "₹ ");
}

export function formatNumber(value: number | string | null | undefined, fractionDigits = 2) {
  if (value === null || value === undefined || value === "") return "0";
  const n = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(n)) return "0";
  return new Intl.NumberFormat("en-IN", {
    maximumFractionDigits: fractionDigits,
    minimumFractionDigits: fractionDigits,
  }).format(n);
}

export function formatPercent(value: number | string | null | undefined, fractionDigits = 2) {
  if (value === null || value === undefined || value === "") return "0.00%";
  const n = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(n)) return "0.00%";
  return `${n >= 0 ? "+" : ""}${n.toFixed(fractionDigits)}%`;
}

export function pnlColor(value: number | string | null | undefined): string {
  const n = typeof value === "string" ? Number(value) : (value ?? 0);
  if (!Number.isFinite(n) || n === 0) return "text-muted-foreground";
  return n > 0 ? "text-profit" : "text-loss";
}

/** Per the broker's spec we now treat every feed price as INR — there is
 *  no live USD→INR conversion anywhere, and prices for Infoway-fed
 *  instruments (forex / crypto / metals / energy / international equities
 *  & indices) render with ₹ instead of $. So this classifier is hard-
 *  coded to false; every existing caller (`formatPrice`, M2M reducers,
 *  order-panel notional, trade-detail summary) sees "not USD-quoted" and
 *  skips the multiplication / currency-prefix change automatically.
 *
 *  Kept as a typed function with the original signature so call sites
 *  don't need to be rewritten. The parameter is intentionally unused. */
export function isUsdSegment(segmentOrExchange?: string | null): boolean {
  void segmentOrExchange;
  return false;
}

/** Format a market price — bare grouped number, no currency prefix.
 *  Per the broker's spec, all instrument prices render without ₹ or $.
 *  Decimal places: 2 for Indian / equity-style rows (the common case),
 *  4 for forex pairs (EURUSD-style fine-grained pips), so 1.0823 keeps
 *  its precision instead of collapsing to 1.08. Segment / exchange are
 *  still accepted on the call signature so existing callers compile. */
export function formatPrice(
  value: number | string | null | undefined,
  segment?: string | null,
  exchange?: string | null,
): string {
  const n = typeof value === "string" ? Number(value) : (value ?? 0);
  if (!Number.isFinite(n)) return "—";
  const s = `${(segment ?? "").toUpperCase()} ${(exchange ?? "").toUpperCase()}`;
  const decimals = /FOREX|FX|CDS/.test(s) ? 4 : 2;
  return new Intl.NumberFormat("en-IN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(n);
}

/** Parse a backend datetime string. If no timezone is present, treat it as UTC.
 * Backend stores UTC but historically serialised as `2026-05-09T09:41:18` with
 * no offset, which JS otherwise interprets as the browser's local timezone. */
export function parseBackendDate(v: string | Date | null | undefined): Date | null {
  if (!v) return null;
  if (v instanceof Date) return isNaN(v.getTime()) ? null : v;
  const hasTz = /[zZ]|[+-]\d{2}:?\d{2}$/.test(v);
  const d = new Date(hasTz ? v : v + "Z");
  return isNaN(d.getTime()) ? null : d;
}

/** Format a backend datetime as Indian Standard Time (e.g. `09 May, 09:41 am IST`). */
export function formatIST(
  v: string | Date | null | undefined,
  opts?: { withSeconds?: boolean }
): string {
  const d = parseBackendDate(v);
  if (!d) return "—";
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    second: opts?.withSeconds ? "2-digit" : undefined,
    hour12: true,
  }).format(d) + " IST";
}

/**
 * Compact exact buy/sell timestamp for the positions / trade blotter
 * tables — "14 May 15:25:11". 24-hour clock, no IST suffix (every user
 * trades from India), date-first so same-day rows line up visually.
 * Replaces the "1d ago / 3h ago" relative format in the TIME column:
 * traders want to see the precise exchange-side execution time, not a
 * rounded approximation.
 */
export function exactTimestamp(v: string | Date | null | undefined): string {
  const d = parseBackendDate(v);
  if (!d) return "—";
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  })
    .format(d)
    // Intl emits "14 May, 15:25:11" — drop the comma so the cell is a
    // single tight token, matching how the desktop Zerodha blotter shows
    // it (no comma between date and clock).
    .replace(",", "");
}

export function relativeTime(date: string | Date): string {
  const d = parseBackendDate(date);
  if (!d) return "—";
  const diff = Date.now() - d.getTime();
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  if (day < 7) return `${day}d ago`;
  return d.toLocaleDateString("en-IN");
}
