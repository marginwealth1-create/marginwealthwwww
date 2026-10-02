"use client";

import { useMemo } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowDownRight,
  ArrowUpRight,
  BarChart3,
  Banknote,
  Building2,
  ChevronRight,
  Landmark,
  Laptop,
  LineChart,
  TrendingUp,
  Zap,
} from "lucide-react";
import { InstrumentAPI } from "@/lib/api";
import { useMarketStream } from "@/lib/useMarketStream";
import { usePriceFlash } from "@/lib/usePriceFlash";
import { cn } from "@/lib/utils";

// ─────────────────────────────────────────────────────────────────────
// Dashboard "Market overview" — a compact, live, color-coded snapshot of
// the top indices + a few large-cap stocks. Reuses the exact data plumbing
// the InstrumentsPanel already relies on:
//   • `InstrumentAPI.search(segment)` → curated INDICES / STOCKS rows.
//   • `InstrumentAPI.quotesBatch(tokens)` → seeds LTP/change on first paint
//     so the rows never flash "—".
//   • `useMarketStream(tokens)` → live ticks (Zerodha + Infoway overlay),
//     so prices update at the WS cadence with a brief green/red flash.
// Mobile-first: the dashboard renders this in place of the old stat tiles
// on phones; desktop keeps the original tiles.
// ─────────────────────────────────────────────────────────────────────

// Rotating icon + accent palette so each row gets a distinct, calm tile —
// matches the reference design (green / blue / violet / amber / cyan).
const PALETTE: { bg: string; fg: string }[] = [
  { bg: "bg-emerald-500/10", fg: "text-emerald-600 dark:text-emerald-400" },
  { bg: "bg-blue-500/10", fg: "text-blue-600 dark:text-blue-400" },
  { bg: "bg-violet-500/10", fg: "text-violet-600 dark:text-violet-400" },
  { bg: "bg-amber-500/10", fg: "text-amber-600 dark:text-amber-400" },
  { bg: "bg-cyan-500/10", fg: "text-cyan-600 dark:text-cyan-400" },
  { bg: "bg-rose-500/10", fg: "text-rose-600 dark:text-rose-400" },
];

const ICONS = [TrendingUp, Building2, Banknote, BarChart3, Zap, Laptop, LineChart, Landmark];

function fmtPrice(n: number): string {
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

// Curated dashboard watchlist — Indian indices + one large-cap + crypto +
// gold. `q` is the search query, `match` the acceptable symbol(s) so we can
// pick the cash/spot row over any F&O contract that shares the name.
const WATCHLIST: { q: string; short: string; name: string; match: string[] }[] = [
  { q: "NIFTY 50", short: "NIFTY", name: "Nifty 50", match: ["NIFTY 50", "NIFTY"] },
  { q: "NIFTY BANK", short: "BANKNIFTY", name: "Bank Nifty", match: ["NIFTY BANK", "BANKNIFTY"] },
  { q: "SENSEX", short: "SENSEX", name: "BSE Sensex", match: ["SENSEX"] },
  { q: "HDFCBANK", short: "HDFCBANK", name: "HDFC Bank", match: ["HDFCBANK"] },
  { q: "BTCUSD", short: "BTCUSD", name: "Bitcoin", match: ["BTCUSD", "BTCUSDT"] },
  { q: "XAUUSD", short: "GOLD", name: "Gold (XAU/USD)", match: ["XAUUSD", "GOLD"] },
];

// Exchanges that quote in INR; everything else (crypto / forex / metals
// from the Infoway feed) is USD-quoted, so the row shows a $ prefix.
const INDIAN_EXCH = new Set(["NSE", "BSE", "NFO", "BFO", "MCX", "CDS", "NCO"]);

function currencyFor(item: any): string {
  const ex = String(item?.exchange ?? "").toUpperCase();
  return INDIAN_EXCH.has(ex) ? "\u20B9" : "$";
}

// Pick the best instrument from a search response: exact symbol match with
// no expiry (spot / cash / index) wins, then any exact symbol, then any
// non-derivative row, finally the first hit.
function pickBestMatch(hits: any[], wanted: string[]): any | null {
  if (!hits.length) return null;
  const W = wanted.map((s) => s.toUpperCase());
  const sym = (h: any) => String(h?.symbol ?? "").toUpperCase();
  return (
    hits.find((h) => W.includes(sym(h)) && !h.expiry) ??
    hits.find((h) => W.includes(sym(h))) ??
    hits.find((h) => !h.expiry) ??
    hits[0]
  );
}

export function MarketOverview({ className }: { className?: string }) {
  // Resolve each curated symbol to a live token via instrument search.
  const { data: items = [], isLoading } = useQuery<any[]>({
    queryKey: ["mkt-overview", "resolve"],
    queryFn: async () => {
      const resolved = await Promise.all(
        WATCHLIST.map(async (w) => {
          try {
            const hits = await InstrumentAPI.search(w.q, undefined, undefined, 12);
            const pick = pickBestMatch(hits ?? [], w.match);
            return pick ? { ...pick, _short: w.short, _name: w.name } : null;
          } catch {
            return null;
          }
        }),
      );
      return resolved.filter(Boolean) as any[];
    },
    staleTime: 5 * 60_000,
  });

  const tokens = useMemo(() => items.map((i) => String(i.token)), [items]);
  const tokensKey = tokens.join(",");

  // Seed quotes so rows show a price before the first WS tick arrives.
  const { data: seed } = useQuery<any[]>({
    queryKey: ["mkt-overview-seed", tokensKey],
    queryFn: () => InstrumentAPI.quotesBatch(tokens),
    enabled: tokens.length > 0,
    staleTime: 30_000,
    refetchInterval: false,
  });

  // Live stream — overwrites the seed per token as ticks arrive.
  const stream = useMarketStream(tokens);
  const quoteByToken = useMemo(() => {
    const m = new Map<string, any>();
    for (const q of seed ?? []) m.set(String(q.token), q);
    stream.forEach((q, t) => m.set(t, q));
    return m;
  }, [seed, stream]);

  const loading = isLoading;

  return (
    <section
      className={cn(
        "overflow-hidden rounded-2xl border border-border bg-card shadow-sm",
        className,
      )}
    >
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="relative flex size-2">
            <span className="absolute inline-flex size-full animate-ping rounded-full bg-emerald-500/60" />
            <span className="relative inline-flex size-2 rounded-full bg-emerald-500" />
          </span>
          <h3 className="text-sm font-bold tracking-tight">Market overview</h3>
        </div>
        <span className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          Live
        </span>
      </div>

      {/* Rows */}
      {loading ? (
        <ul className="divide-y divide-border">
          {Array.from({ length: 6 }).map((_, i) => (
            <li key={i} className="flex items-center gap-3 px-4 py-3">
              <div className="size-10 animate-pulse rounded-xl bg-muted/50" />
              <div className="flex-1 space-y-1.5">
                <div className="h-3 w-20 animate-pulse rounded bg-muted/50" />
                <div className="h-2.5 w-28 animate-pulse rounded bg-muted/40" />
              </div>
              <div className="space-y-1.5 text-right">
                <div className="ml-auto h-3 w-16 animate-pulse rounded bg-muted/50" />
                <div className="ml-auto h-2.5 w-12 animate-pulse rounded bg-muted/40" />
              </div>
            </li>
          ))}
        </ul>
      ) : items.length === 0 ? (
        <div className="px-4 py-8 text-center text-xs text-muted-foreground">
          Market data unavailable right now.
        </div>
      ) : (
        <ul className="divide-y divide-border">
          {items.map((item, i) => (
            <MarketRow
              key={item.token}
              item={item}
              quote={quoteByToken.get(String(item.token))}
              index={i}
            />
          ))}
        </ul>
      )}

      {/* Footer */}
      <Link
        href="/terminal"
        className="flex items-center justify-center gap-1 border-t border-border py-2.5 text-xs font-semibold text-primary transition-colors hover:bg-primary/5"
      >
        Open trading terminal <ChevronRight className="size-3.5" />
      </Link>
    </section>
  );
}

function MarketRow({
  item,
  quote,
  index,
}: {
  item: any;
  quote: any;
  index: number;
}) {
  const ltp = Number(quote?.ltp ?? 0);
  const pct = Number(quote?.change_pct ?? 0);
  const flash = usePriceFlash(ltp);
  const up = pct >= 0;
  const hasQuote = ltp > 0;

  const { bg, fg } = PALETTE[index % PALETTE.length];
  const Icon = ICONS[index % ICONS.length];

  return (
    <li>
      <Link
        href={`/terminal?token=${item.token}`}
        className="flex items-center gap-3 px-4 py-3 transition-colors hover:bg-muted/40 active:bg-muted/60"
      >
        {/* Accent icon tile */}
        <div className={cn("grid size-10 shrink-0 place-items-center rounded-xl", bg, fg)}>
          <Icon className="size-5" strokeWidth={2.25} />
        </div>

        {/* Symbol + name */}
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-bold tracking-tight">
            {item._short ?? item.symbol}
          </div>
          <div className="truncate text-[11px] text-muted-foreground">
            {item._name ?? item.name}
          </div>
        </div>

        {/* Price + change pill */}
        <div className="shrink-0 text-right">
          <div
            className={cn(
              "font-tabular text-sm font-bold tabular-nums transition-colors duration-300",
              flash === "up"
                ? "text-emerald-500"
                : flash === "down"
                  ? "text-red-500"
                  : "text-foreground",
            )}
          >
            {hasQuote ? `${currencyFor(item)}${fmtPrice(ltp)}` : "\u2014"}
          </div>
          <span
            className={cn(
              "mt-1 inline-flex items-center gap-0.5 rounded-full px-1.5 py-0.5 text-[11px] font-semibold",
              hasQuote
                ? up
                  ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                  : "bg-red-500/10 text-red-600 dark:text-red-400"
                : "bg-muted text-muted-foreground",
            )}
          >
            {hasQuote && (up ? <ArrowUpRight className="size-3" /> : <ArrowDownRight className="size-3" />)}
            {hasQuote ? `${up ? "+" : ""}${pct.toFixed(2)}%` : "--"}
          </span>
        </div>
      </Link>
    </li>
  );
}
