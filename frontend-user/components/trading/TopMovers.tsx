"use client";

import { useMemo } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { TrendingDown, TrendingUp } from "lucide-react";
import { InstrumentAPI } from "@/lib/api";
import { useMarketStream } from "@/lib/useMarketStream";
import { usePriceFlash } from "@/lib/usePriceFlash";
import { cn } from "@/lib/utils";

// ─────────────────────────────────────────────────────────────────────
// Dashboard "Top movers" — ranks a curated basket of NIFTY large-caps by
// live % change and shows the top gainers + top losers side by side.
// Reuses the exact same data plumbing as MarketOverview:
//   • per-symbol search → live token
//   • quotesBatch seed → first paint
//   • useMarketStream → live ticks (Zerodha) with a green/red flash
// Mobile-first; rendered on the dashboard in place of the removed
// open-positions / recent-orders panels.
// ─────────────────────────────────────────────────────────────────────

// Curated NSE large-cap basket (NIFTY heavyweights) — a big enough pool
// that the ranking is meaningful, small enough to stay well under the WS
// subscription cap.
const BASKET = [
  "RELIANCE",
  "TCS",
  "HDFCBANK",
  "ICICIBANK",
  "INFY",
  "SBIN",
  "BHARTIARTL",
  "ITC",
  "LT",
  "AXISBANK",
  "KOTAKBANK",
  "TATAMOTORS",
  "MARUTI",
  "SUNPHARMA",
  "BAJFINANCE",
  "HINDUNILVR",
];

const TOP_N = 3;

function fmtPrice(n: number): string {
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

// Pick the cash/equity row for a symbol: exact symbol match, no expiry,
// preferring NSE.
function pickEquity(hits: any[], want: string): any | null {
  if (!hits.length) return null;
  const W = want.toUpperCase();
  const sym = (h: any) => String(h?.symbol ?? "").toUpperCase();
  return (
    hits.find((h) => sym(h) === W && !h.expiry && String(h.exchange).toUpperCase() === "NSE") ??
    hits.find((h) => sym(h) === W && !h.expiry) ??
    hits.find((h) => sym(h) === W) ??
    null
  );
}

export function TopMovers({ className }: { className?: string }) {
  // Resolve each basket symbol to a live token.
  const { data: items = [], isLoading } = useQuery<any[]>({
    queryKey: ["top-movers", "resolve"],
    queryFn: async () => {
      const resolved = await Promise.all(
        BASKET.map(async (q) => {
          try {
            const hits = await InstrumentAPI.search(q, "NSE", undefined, 6);
            const pick = pickEquity(hits ?? [], q);
            return pick ? { token: String(pick.token), symbol: pick.symbol, name: pick.name } : null;
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

  const { data: seed } = useQuery<any[]>({
    queryKey: ["top-movers-seed", tokensKey],
    queryFn: () => InstrumentAPI.quotesBatch(tokens),
    enabled: tokens.length > 0,
    staleTime: 30_000,
    refetchInterval: false,
  });

  const stream = useMarketStream(tokens);

  // Merge seed + live, attach change_pct/ltp to each basket item.
  const enriched = useMemo(() => {
    const m = new Map<string, any>();
    for (const q of seed ?? []) m.set(String(q.token), q);
    stream.forEach((q, t) => m.set(t, q));
    return items
      .map((it) => {
        const q = m.get(String(it.token));
        return {
          ...it,
          ltp: Number(q?.ltp ?? 0),
          pct: Number(q?.change_pct ?? 0),
        };
      })
      .filter((it) => it.ltp > 0);
  }, [items, seed, stream]);

  const { gainers, losers } = useMemo(() => {
    const sorted = [...enriched].sort((a, b) => b.pct - a.pct);
    return {
      gainers: sorted.slice(0, TOP_N),
      losers: sorted.slice(-TOP_N).reverse(),
    };
  }, [enriched]);

  const loading = isLoading || (enriched.length === 0 && tokens.length > 0);

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
          <h3 className="text-sm font-bold tracking-tight">Top movers</h3>
        </div>
        <span className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          NSE
        </span>
      </div>

      {loading ? (
        <div className="grid grid-cols-2 divide-x divide-border">
          {[0, 1].map((col) => (
            <div key={col} className="space-y-3 p-4">
              <div className="h-3 w-16 animate-pulse rounded bg-muted/50" />
              {Array.from({ length: TOP_N }).map((_, i) => (
                <div key={i} className="space-y-1.5">
                  <div className="h-3 w-20 animate-pulse rounded bg-muted/50" />
                  <div className="h-2.5 w-12 animate-pulse rounded bg-muted/40" />
                </div>
              ))}
            </div>
          ))}
        </div>
      ) : enriched.length === 0 ? (
        <div className="px-4 py-8 text-center text-xs text-muted-foreground">
          Live prices unavailable right now.
        </div>
      ) : (
        <div className="grid grid-cols-2 divide-x divide-border">
          <MoverColumn title="Gainers" rows={gainers} positive />
          <MoverColumn title="Losers" rows={losers} positive={false} />
        </div>
      )}
    </section>
  );
}

function MoverColumn({
  title,
  rows,
  positive,
}: {
  title: string;
  rows: any[];
  positive: boolean;
}) {
  return (
    <div className="p-3">
      <div
        className={cn(
          "mb-1 flex items-center gap-1 px-1 text-[11px] font-bold uppercase tracking-wider",
          positive ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400",
        )}
      >
        {positive ? <TrendingUp className="size-3.5" /> : <TrendingDown className="size-3.5" />}
        {title}
      </div>
      <ul>
        {rows.map((r) => (
          <MoverRow key={r.token} row={r} />
        ))}
      </ul>
    </div>
  );
}

function MoverRow({ row }: { row: any }) {
  const flash = usePriceFlash(row.ltp);
  const up = row.pct >= 0;
  return (
    <li>
      <Link
        href={`/terminal?token=${row.token}`}
        className="flex items-center justify-between gap-2 rounded-lg px-1 py-1.5 transition-colors hover:bg-muted/40 active:bg-muted/60"
      >
        <div className="min-w-0">
          <div className="truncate text-xs font-bold tracking-tight">{row.symbol}</div>
          <div
            className={cn(
              "font-tabular text-[11px] tabular-nums transition-colors duration-300",
              flash === "up"
                ? "text-emerald-500"
                : flash === "down"
                  ? "text-red-500"
                  : "text-muted-foreground",
            )}
          >
            {"\u20B9"}
            {fmtPrice(row.ltp)}
          </div>
        </div>
        <span
          className={cn(
            "shrink-0 font-tabular text-xs font-semibold tabular-nums",
            up ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400",
          )}
        >
          {up ? "+" : ""}
          {row.pct.toFixed(2)}%
        </span>
      </Link>
    </li>
  );
}
