"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Search, Star, X } from "lucide-react";
import { InstrumentAPI, MarketwatchAPI, SegmentSettingsAPI } from "@/lib/api";
import { useMarketStream } from "@/lib/useMarketStream";
import { cn, formatPrice, pnlColor } from "@/lib/utils";
import { MobileOptionChain } from "@/components/trading/MobileOptionChain";
import { TickFlash } from "@/components/trading/TickFlash";

interface Props {
  activeToken: string | null;
  /** `seed` carries the row's last-known ltp/bid/ask + identity so the trade
   *  card paints a price AND a usable instrument instantly, instead of
   *  waiting ~5-7 s for its own fresh WS connection + REST fetches on first
   *  open (which left it at "0.00" / "Instrument not loaded"). */
  onSelect: (
    token: string,
    seed?: {
      ltp?: number | null;
      bid?: number | null;
      ask?: number | null;
      symbol?: string | null;
      exchange?: string | null;
      segment?: string | null;
    },
  ) => void;
}

/** A freshly (re)subscribed token's very first tick/snapshot can legitimately
 *  be the backend's zero-valued skeleton (`_empty_quote` in
 *  market_data_service.py) — sent before the real Zerodha/Infoway overlay
 *  fills in, which happens on the NEXT tick cycle (up to a few seconds for a
 *  cold/rarely-viewed symbol). `useMarketStream`'s own sticky-merge already
 *  guards against this WITHIN one hook lifetime, but every page navigation
 *  tears down the old WS + its sticky cache and mounts a brand new one, so
 *  the first live overlay after a remount has no `prev` to fall back to.
 *  Using `??` to merge that overlay onto the REST-cached price is therefore
 *  wrong — `0 ?? cached` evaluates to `0`, clobbering a perfectly good
 *  cached price with the placeholder zero for that brief window. Treating
 *  non-positive/non-finite as "no live data yet" (same semantics as
 *  `isPositive` in useMarketStream) keeps the cached price on screen until
 *  a REAL tick arrives — this is the fix for "market page/app reopen par
 *  price 2-3 sec ke liye 0 dikhta hai" (root cause: 0 is a valid exchange
 *  price to `??` but not a valid price for FX/equity/commodity symbols). */
function livePrice(v: number | null | undefined): number | null {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n : null;
}

const _EXPIRY_MONTHS = [
  "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
  "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
] as const;

/** Compact expiry for a mobile row — `19 AUG` (the broker-app convention the
 *  operator asked for). The year is appended ONLY when the contract expires
 *  in a different calendar year (`19 JAN 27`), so the common near-month case
 *  stays short while a far month can never be misread. Server sends
 *  `YYYY-MM-DD`; equities have no expiry and render nothing. */
function formatExpiryShort(raw: string | null | undefined): string {
  if (!raw) return "";
  const s = String(raw).slice(0, 10);
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  if (!m) return "";
  const [, y, mo, d] = m;
  const mon = _EXPIRY_MONTHS[Number(mo) - 1];
  if (!mon) return "";
  const label = `${Number(d)} ${mon}`;
  return Number(y) === new Date().getFullYear() ? label : `${label} ${y.slice(2)}`;
}

type Bucket = {
  key: string;
  label: string;
  mode: "watchlist" | "filter";
  segments?: string[];
  adminRows?: string[];
  // Indian-segment chips are user-managed: list shows only what the
  // user has explicitly added (via search + "+"). Mirrors the desktop
  // InstrumentsPanel behaviour. Infoway-fed chips (Forex/Crypto/etc.)
  // stay non-managed — the entire small feed is shown.
  managed?: boolean;
};

// Bucket order: Favorites first, then every Indian-exchange-backed
// segment (NSE EQ / FUT / OPT, MCX FUT), then the international /
// Infoway-backed ones (Indices, Stocks, Commodities, Forex, Crypto) so
// a user reaching for "NIFTY FUT" doesn't have to scroll past five
// foreign-market chips first. Matches the user's request: Indian
// markets pehle, crypto/forex last.
const BUCKETS: Bucket[] = [
  { key: "favorites", label: "Favorites", mode: "watchlist" },
  // Indian segments — managed (user explicitly adds instruments)
  { key: "nse_eq", label: "NSE EQ", mode: "filter", segments: ["NSE_EQUITY"], adminRows: ["NSE_EQ"], managed: true },
  { key: "nse_fut", label: "NSE FUT", mode: "filter", segments: ["NSE_FUTURE", "NSE_INDEX_FUTURE"], adminRows: ["NSE_FUT"], managed: true },
  { key: "nse_opt", label: "NSE OPT", mode: "filter", segments: ["NSE_INDEX_OPTION_BUY", "NSE_INDEX_OPTION_SELL", "NSE_STOCK_OPTION_BUY", "NSE_STOCK_OPTION_SELL"], adminRows: ["NSE_OPT"], managed: true },
  { key: "bse_eq", label: "BSE EQ", mode: "filter", segments: ["BSE_EQUITY"], adminRows: ["BSE_EQ"], managed: true },
  { key: "bse_fut", label: "BSE FUT", mode: "filter", segments: ["BSE_FUTURE", "BSE_INDEX_FUTURE"], adminRows: ["BSE_FUT"], managed: true },
  { key: "bse_opt", label: "BSE OPT", mode: "filter", segments: ["BSE_OPTION_BUY", "BSE_OPTION_SELL"], adminRows: ["BSE_OPT"], managed: true },
  { key: "mcx_fut", label: "MCX FUT", mode: "filter", segments: ["MCX_FUTURE"], adminRows: ["MCX_FUT"], managed: true },
  // MCX OPT — was missing from this list (admin enables MCX_OPT but the
  // mobile chip strip never surfaced it, so users on phones couldn't
  // see commodity options at all). Same admin row + segment-set as the
  // desktop InstrumentsPanel.
  { key: "mcx_opt", label: "MCX OPT", mode: "filter", segments: ["MCX_OPTION_BUY", "MCX_OPTION_SELL"], adminRows: ["MCX_OPT"], managed: true },
  // Infoway-fed chips — non-managed (entire small feed visible)
  { key: "indices", label: "Indices", mode: "filter", segments: ["INDICES"], adminRows: ["INDICES"] },
  { key: "stocks", label: "Stocks", mode: "filter", segments: ["STOCKS"], adminRows: ["STOCKS"] },
  { key: "commodities", label: "Commodities", mode: "filter", segments: ["COMMODITIES"], adminRows: ["COMMODITIES"] },
  { key: "forex", label: "Forex", mode: "filter", segments: ["FOREX"], adminRows: ["FOREX"] },
  { key: "crypto", label: "Crypto", mode: "filter", segments: ["CRYPTO_PERPETUAL", "CRYPTO_SPOT", "CRYPTO_FUTURE"], adminRows: ["CRYPTO"] },
];

/**
 * Mobile-only instruments bar that sits at the top of the terminal page.
 * Mirrors the InstrumentsPanel's search + bucket model but in a flatter
 * horizontal layout — chips strip + search row + scrollable picks. Tapping
 * a row fires `onSelect(token)` which the terminal page maps onto the
 * existing `selectedToken` state — no navigation, the chart and order panel
 * below just swap.
 */
export function MobileInstrumentsBar({ activeToken, onSelect }: Props) {
  const qc = useQueryClient();
  // Always expanded — the collapse chevron was removed (operator: "iski
  // zarurat nahi"). Kept as a const so the existing `expanded &&` query
  // gates below stay valid without a wider refactor.
  const [expanded] = useState(true);
  // Top-level view toggle: the existing watchlist (default, unchanged) and
  // the new Groww-style Options chain. The watchlist logic/queries below are
  // untouched — the Options branch is purely additive.
  const [view, setView] = useState<"watchlist" | "options">("watchlist");
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [bucketKey, setBucketKey] = useState<string>("favorites");
  // Optimistic favorite toggle — tracks tokens the user just starred /
  // unstarred so the star icon flips before the network round-trip lands.
  // Reset whenever the source watchlist refetches.
  const [pendingFav, setPendingFav] = useState<Map<string, boolean>>(new Map());

  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search), 180);
    return () => clearTimeout(t);
  }, [search]);

  const { data: inactiveRows } = useQuery({
    queryKey: ["segment-settings", "inactive"],
    queryFn: () => SegmentSettingsAPI.inactive(),
    staleTime: 30_000,
    refetchInterval: 60_000,
    placeholderData: (prev) => prev,
  });
  const inactiveSet = useMemo(() => new Set(inactiveRows ?? []), [inactiveRows]);
  const visibleBuckets = useMemo(
    () =>
      BUCKETS.filter((b) => {
        const rows = b.adminRows ?? [];
        if (rows.length === 0) return true;
        return rows.some((r) => !inactiveSet.has(r));
      }),
    [inactiveSet],
  );
  useEffect(() => {
    if (!visibleBuckets.find((b) => b.key === bucketKey)) setBucketKey("favorites");
  }, [visibleBuckets, bucketKey]);

  // Smooth-scroll the active chip into view when the bucket changes.
  // `block: nearest` keeps vertical position; `inline: center` slides the
  // chip into the middle of the strip so the user always sees adjacent
  // buckets on both sides — far better than the previous `snap-start`
  // jump that left the active chip pinned to the left edge.
  const chipsScrollerRef = useRef<HTMLDivElement | null>(null);
  const chipRefs = useRef<Record<string, HTMLButtonElement>>({});
  useEffect(() => {
    const el = chipRefs.current[bucketKey];
    if (el) el.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "center" });
  }, [bucketKey]);

  const bucket = visibleBuckets.find((b) => b.key === bucketKey) ?? visibleBuckets[0];

  const { data: watchlists } = useQuery({
    queryKey: ["watchlists"],
    queryFn: () => MarketwatchAPI.list(),
    staleTime: 30_000,
  });
  const activeWl = watchlists?.[0];

  // Token → item-id map for the active watchlist so a tap on the star can
  // resolve the item id needed for `removeItem` without a second round-trip.
  const favItemByToken = useMemo(() => {
    const map = new Map<string, string>();
    for (const it of activeWl?.items ?? []) {
      if (it?.instrument_token && it?.id) map.set(String(it.instrument_token), String(it.id));
    }
    return map;
  }, [activeWl]);

  // Clear the optimistic-flip overlay once the server-side list catches up
  // — otherwise repeated stars on the same token would stay "pending" forever.
  useEffect(() => {
    if (pendingFav.size === 0) return;
    setPendingFav((prev) => {
      const next = new Map(prev);
      for (const [tok, wantStarred] of prev) {
        const isStarred = favItemByToken.has(tok);
        if (isStarred === wantStarred) next.delete(tok);
      }
      return next;
    });
  }, [favItemByToken, pendingFav]);

  function isFav(token: string): boolean {
    const tok = String(token);
    if (pendingFav.has(tok)) return pendingFav.get(tok)!;
    return favItemByToken.has(tok);
  }

  async function toggleFavorite(token: string) {
    const tok = String(token);
    if (!activeWl?.id) {
      toast.error("No watchlist available");
      return;
    }
    const currentlyFav = isFav(tok);
    // Flip the star instantly — server reconciliation happens in the
    // invalidate below.
    setPendingFav((prev) => new Map(prev).set(tok, !currentlyFav));
    try {
      if (currentlyFav) {
        const itemId = favItemByToken.get(tok);
        if (!itemId) throw new Error("Item not found in watchlist");
        await MarketwatchAPI.removeItem(activeWl.id, itemId);
      } else {
        await MarketwatchAPI.addItem(activeWl.id, tok);
      }
      qc.invalidateQueries({ queryKey: ["watchlists"] });
      qc.invalidateQueries({ queryKey: ["watchlist-quotes"] });
    } catch (e: any) {
      // Roll back the optimistic flip on failure.
      setPendingFav((prev) => {
        const next = new Map(prev);
        next.delete(tok);
        return next;
      });
      toast.error(e?.message || (currentlyFav ? "Failed to remove" : "Failed to add"));
    }
  }
  const { data: wlQuotes } = useQuery({
    queryKey: ["watchlist-quotes", activeWl?.id],
    queryFn: () => MarketwatchAPI.quotes(activeWl!.id),
    // Stays enabled WHILE searching favorites too — the Favorites search is a
    // local filter over these rows (see `list` below), so we keep the live
    // quotes flowing instead of pausing them the moment the user types.
    enabled: !!activeWl?.id && bucketKey === "favorites" && expanded,
    refetchInterval: 3000,
    staleTime: 2000,
    refetchOnWindowFocus: false,
    placeholderData: (prev) => prev,
  });

  const browseSegments = bucket?.mode === "filter" ? bucket.segments?.join(",") : undefined;
  const searchScopeSegments = bucket?.mode === "filter" ? browseSegments : undefined;

  // Managed-segment marker — Indian chips show only what the user
  // explicitly added. The admin row name (e.g. "NSE_EQ") is the
  // backend's segment key. Same flow as the desktop InstrumentsPanel.
  const managedSegmentName =
    bucket?.managed && bucket?.adminRows?.[0] ? bucket.adminRows[0] : null;

  const { data: segmentItems } = useQuery<any[]>({
    queryKey: ["segment-items", managedSegmentName],
    queryFn: () => MarketwatchAPI.segmentItems(managedSegmentName!),
    enabled: !!managedSegmentName && expanded,
    staleTime: 30_000,
    placeholderData: (prev) => prev,
  });
  const addedTokenSet = useMemo(() => {
    const s = new Set<string>();
    for (const it of segmentItems ?? []) {
      if (it?.instrument_token) s.add(String(it.instrument_token));
    }
    return s;
  }, [segmentItems]);

  async function addToSegment(token: string, symbol: string) {
    if (!managedSegmentName) return;
    try {
      await MarketwatchAPI.addSegmentItem(managedSegmentName, token);
      qc.invalidateQueries({ queryKey: ["segment-items", managedSegmentName] });
      toast.success(`Added ${symbol} to ${bucket?.label}`, { duration: 1500 });
    } catch (e: any) {
      toast.error(e?.message || `Failed to add ${symbol}`);
    }
  }

  async function removeFromSegment(token: string, symbol: string) {
    if (!managedSegmentName) return;
    try {
      await MarketwatchAPI.removeSegmentItem(managedSegmentName, token);
      qc.invalidateQueries({ queryKey: ["segment-items", managedSegmentName] });
      toast.success(`Removed ${symbol}`, { duration: 1500 });
    } catch (e: any) {
      toast.error(e?.message || `Failed to remove ${symbol}`);
    }
  }

  const { data: searchHits } = useQuery({
    queryKey: ["mobile-instruments-search", debouncedSearch, searchScopeSegments],
    queryFn: () =>
      InstrumentAPI.search(debouncedSearch, undefined, searchScopeSegments, 30),
    // Favorites NEVER hits the global instrument search — searching the
    // Favorites tab filters the starred rows locally (user: "fav me sirf fav
    // vale search honge, all search nahi"). Only the segment/browse buckets
    // run this query, and managed Indian segments stay scoped to their own
    // segment via `searchScopeSegments`.
    enabled:
      debouncedSearch.trim().length > 0 &&
      expanded &&
      bucket?.mode !== "watchlist",
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    placeholderData: (prev) => prev,
  });
  // Browse the bucket — cap at 40 (was 60). A bigger result set bloats the
  // WS subscribe list and the per-row quote map without the user actually
  // seeing past the first ~10 rows on screen.
  // Skipped for managed segments — those render `segmentItems` (user's
  // explicit additions) instead of the full Kite cache.
  const { data: bucketHits } = useQuery({
    queryKey: ["mobile-instruments-bucket", bucketKey, browseSegments],
    queryFn: () => InstrumentAPI.search(undefined, undefined, browseSegments, 40),
    enabled:
      search.trim().length === 0 &&
      bucket?.mode !== "watchlist" &&
      !!browseSegments &&
      expanded &&
      !managedSegmentName,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    placeholderData: (prev) => prev,
  });

  // `tokensKey` (a stable string) is the dep — using the array itself
  // would invalidate the memo on every render even when the contents
  // matched, which in turn churned the WS subscribe / unsubscribe in
  // useMarketStream below.
  // Cap subscribed tokens to LIVE_TOKEN_CAP (top N of the filtered
  // list). Mirrors the same cap on the desktop InstrumentsPanel —
  // prevents the backend pump from refreshing 100+ overlays per cycle
  // when the user only sees 8-10 rows on a phone viewport. The
  // remaining rows render with the initial REST snapshot bid/ask which
  // is good enough until the user scrolls.
  const LIVE_TOKEN_CAP = 30;
  const tokensKey = useMemo<string>(() => {
    const all = (() => {
      if (debouncedSearch.trim().length > 0 && bucket?.mode !== "watchlist") {
        return (searchHits ?? []).map((s: any) => s.token);
      }
      if (bucket?.mode === "watchlist") {
        // Subscribe the FAVOURITE tokens to WS + batch quotes too. Without
        // this the Favorites tab got no live overlay — `quoteByToken` was
        // empty, so the enrich() below fell back to the /marketwatch/quotes
        // REST ltp, which is 0 for Infoway (crypto/forex/metals). Result:
        // those rows froze at 0.00 while every other bucket ticked live.
        // instrument_token == symbol for Infoway, so they stream fine.
        return (activeWl?.items ?? []).map((it: any) =>
          String(it.instrument_token ?? it.token),
        );
      }
      if (managedSegmentName) {
        return (segmentItems ?? []).map((it: any) => String(it.instrument_token));
      }
      return (bucketHits ?? []).map((s: any) => s.token);
    })();
    return all.slice(0, LIVE_TOKEN_CAP).join(",");
  }, [debouncedSearch, searchHits, bucketHits, bucket?.mode, managedSegmentName, segmentItems, activeWl?.items]);
  const visibleTokens = useMemo<string[]>(
    () => (tokensKey ? tokensKey.split(",") : []),
    [tokensKey],
  );

  const { data: liveQuotes } = useQuery<any[]>({
    queryKey: ["mobile-instruments-batch-seed", tokensKey],
    queryFn: () => InstrumentAPI.quotesBatch(visibleTokens),
    enabled: visibleTokens.length > 0 && expanded,
    staleTime: 60_000,
    refetchInterval: false,
    refetchOnWindowFocus: false,
  });
  // Skip WS subscription entirely when the bar is collapsed OR when the
  // Options tab is showing (that view runs its own per-strike stream) — the
  // user can't see the watchlist rows, no point burning sockets / handlers.
  const streamQuotes = useMarketStream(
    expanded && view === "watchlist" ? visibleTokens : [],
  );
  const quoteByToken = useMemo(() => {
    const map = new Map<string, any>();
    for (const q of liveQuotes ?? []) map.set(String(q.token), q);
    streamQuotes.forEach((q, tok) => map.set(tok, q));
    return map;
  }, [liveQuotes, streamQuotes]);

  const list = useMemo(() => {
    const enrich = (s: any) => {
      const live = quoteByToken.get(String(s.token));
      return {
        instrument_token: s.token,
        symbol: s.symbol,
        exchange: s.exchange,
        segment: s.segment ?? s.instrument_type,
        expiry: s.expiry ?? null,
        instrument_type: s.instrument_type ?? null,
        bid: livePrice(live?.bid) ?? livePrice(s.bid),
        ask: livePrice(live?.ask) ?? livePrice(s.ask),
        ltp:
          livePrice(live?.ltp) ??
          livePrice(s.ltp) ??
          livePrice(live?.last_ltp) ??
          livePrice(s.last_ltp),
        change_pct: live?.change_pct ?? s.change_pct ?? null,
      };
    };
    // ── Favorites (watchlist) ──────────────────────────────────────────
    // ALWAYS renders the user's starred rows. A search term filters them
    // LOCALLY by symbol — it must NEVER fall through to the global
    // all-instruments search (user complaint: typing in Favorites was
    // searching every instrument instead of just the favorites). Live ticks
    // are overlaid onto the REST snapshot so prices stay fresh.
    if (bucket?.mode === "watchlist") {
      const rows = (wlQuotes ?? []).map((q: any) => {
        const tok = String(q.instrument_token ?? q.token ?? "");
        const live = quoteByToken.get(tok);
        return {
          ...q,
          bid: livePrice(live?.bid) ?? livePrice(q.bid),
          ask: livePrice(live?.ask) ?? livePrice(q.ask),
          ltp:
            livePrice(live?.ltp) ??
            livePrice(q.ltp) ??
            livePrice(live?.last_ltp) ??
            livePrice(q.last_ltp),
          change_pct: live?.change_pct ?? q.change_pct ?? null,
        };
      });
      const needle = debouncedSearch.trim().toLowerCase();
      if (!needle) return rows;
      return rows.filter((r: any) =>
        String(r.symbol ?? "").toLowerCase().includes(needle),
      );
    }
    // ── Segment / browse buckets ────────────────────────────────────────
    // A search term shows segment-scoped search hits (managed Indian
    // segments stay scoped via `searchScopeSegments`); empty search browses.
    if (debouncedSearch.trim().length > 0) return (searchHits ?? []).map(enrich);
    if (managedSegmentName) {
      return (segmentItems ?? []).map((it: any) =>
        enrich({
          token: it.instrument_token,
          symbol: it.symbol,
          exchange: it.exchange,
          // The segment-items API now returns the contract metadata, so the
          // added row keeps the same expiry line it had in the search list.
          segment: it.segment ?? null,
          expiry: it.expiry ?? null,
          instrument_type: it.instrument_type ?? null,
        }),
      );
    }
    return (bucketHits ?? []).map(enrich);
  }, [debouncedSearch, searchHits, wlQuotes, bucketHits, bucket, quoteByToken, managedSegmentName, segmentItems]);

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-background md:rounded-lg md:border md:border-border md:bg-card">
      {/* Header — mirrors the desktop InstrumentsPanel ("INSTRUMENTS"
          uppercase label + close on the right). The collapse chevron is
          kept so the user can shrink the strip on phones that have less
          vertical room. */}
      <div className="flex shrink-0 items-center gap-4 border-b border-border px-3 py-2">
        {(["watchlist", "options"] as const).map((v) => (
          <button
            key={v}
            type="button"
            onClick={() => setView(v)}
            className="relative py-0.5"
          >
            <span
              className={cn(
                "text-sm font-bold transition-colors",
                view === v ? "text-foreground" : "text-muted-foreground hover:text-foreground",
              )}
            >
              {v === "watchlist" ? "Watchlist" : "Options"}
            </span>
            {view === v && (
              <span className="absolute -bottom-[9px] left-0 right-0 h-0.5 rounded-full bg-foreground" />
            )}
          </button>
        ))}
      </div>

      {expanded && view === "options" && <MobileOptionChain onSelect={onSelect} />}

      {expanded && view === "watchlist" && (
        <>
          <div className="shrink-0 space-y-2 border-b border-border px-3 py-2">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search symbols..."
                className="h-8 w-full rounded-md border border-border bg-background pl-7 pr-7 text-xs outline-none placeholder:text-muted-foreground focus:border-primary"
              />
              {search && (
                <button
                  type="button"
                  onClick={() => setSearch("")}
                  aria-label="Clear search"
                  className="absolute right-1 top-1/2 grid size-6 -translate-y-1/2 place-items-center rounded text-muted-foreground hover:bg-muted/40 hover:text-foreground"
                >
                  <X className="size-3.5" />
                </button>
              )}
            </div>

            {/* Segment chips — horizontally scrollable strip. Hides the
                scrollbar entirely (touch-friendly), uses snap-x so a swipe
                lands the next chip cleanly aligned at the leading edge,
                and a fade-mask on the right indicates more chips are
                available off-screen. Larger touch target (h-7, px-3)
                makes the chips comfortable to tap on phones. `scroll-smooth`
                + `scrollIntoView` on the active chip below means a tap on
                a partially-clipped chip smoothly centres it instead of
                jumping abruptly to the snap point. */}
            <div
              ref={chipsScrollerRef}
              className="scroll-smooth -mx-3 overflow-x-auto overscroll-x-contain px-3 [scrollbar-width:none] [-ms-overflow-style:none] [&::-webkit-scrollbar]:hidden"
              style={{
                maskImage:
                  "linear-gradient(to right, black 0%, black calc(100% - 24px), transparent 100%)",
                WebkitMaskImage:
                  "linear-gradient(to right, black 0%, black calc(100% - 24px), transparent 100%)",
                WebkitOverflowScrolling: "touch",
              }}
            >
              <div className="flex snap-x gap-1.5">
                {visibleBuckets.map((b) => (
                  <button
                    key={b.key}
                    type="button"
                    ref={(el) => {
                      if (el) chipRefs.current[b.key] = el;
                      else delete chipRefs.current[b.key];
                    }}
                    onClick={() => setBucketKey(b.key)}
                    className={cn(
                      // uppercase keeps Infoway chips ("Forex", "Stocks", …)
                      // visually consistent with Zerodha chips ("NSE EQ" etc).
                      "h-8 shrink-0 snap-center whitespace-nowrap rounded-full border px-3.5 text-[12px] font-bold uppercase tracking-wide transition-colors",
                      bucketKey === b.key
                        ? "border-primary bg-primary/15 text-primary"
                        : "border-border text-foreground/70 hover:bg-muted/40 hover:text-foreground",
                    )}
                  >
                    {b.label}
                  </button>
                ))}
              </div>
            </div>
          </div>

          <div
            className="min-h-0 flex-1 touch-pan-y overflow-y-auto overscroll-contain scrollbar-thin"
            style={{ WebkitOverflowScrolling: "touch" }}
          >
            {list.length === 0 && (
              <div className="grid h-24 place-items-center px-4 text-center text-xs text-muted-foreground">
                {search.trim()
                  ? "No instruments match"
                  : managedSegmentName
                    ? `No instruments yet. Search above to add to ${bucket?.label}.`
                    : "Add instruments to your watchlist to see them here."}
              </div>
            )}
            {list.map((q: any) => {
              const token = String(q.instrument_token);
              const isActive = token === String(activeToken);
              const starred = isFav(token);
              const liveOverlay = quoteByToken.get(token);
              const bid = livePrice(q.bid) ?? livePrice(liveOverlay?.bid);
              const ask = livePrice(q.ask) ?? livePrice(liveOverlay?.ask);
              // Display chain, in order of truthfulness: live tick → REST
              // snapshot → the backend's persisted `last_ltp` (7-day Redis
              // `mdlast`, DISPLAY-ONLY by design — the matching engine reads
              // `ltp` which stays 0, so a stale price can never fill an
              // order). This is what keeps a REAL price on screen instead of
              // "0.00" or "—" during a cold start / market-closed feed gap.
              const ltp =
                livePrice(q.ltp) ??
                livePrice(liveOverlay?.ltp) ??
                livePrice(q.last_ltp) ??
                livePrice(liveOverlay?.last_ltp);
              const changePct = q.change_pct ?? liveOverlay?.change_pct ?? null;
              const inSearchMode = debouncedSearch.trim().length > 0;
              const alreadyAdded = managedSegmentName ? addedTokenSet.has(token) : false;
              // Right-edge action button — see desktop InstrumentsPanel
              // for the same context rules. Keeps the mobile row tight:
              // ONE action on the right edge, no star+plus side-by-side.
              let rightAction: React.ReactNode = null;
              if (managedSegmentName) {
                if (inSearchMode && !alreadyAdded) {
                  rightAction = (
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        addToSegment(token, q.symbol);
                      }}
                      aria-label={`Add ${q.symbol}`}
                      title={`Add to ${bucket?.label}`}
                      className="shrink-0 rounded border border-primary/40 px-2 py-1 text-[11px] font-semibold text-primary hover:bg-primary/10"
                    >
                      Add
                    </button>
                  );
                } else if (inSearchMode && alreadyAdded) {
                  rightAction = (
                    <span
                      title="Already added"
                      className="shrink-0 rounded px-2 py-1 text-[11px] font-semibold text-emerald-500"
                    >
                      Added
                    </span>
                  );
                } else {
                  // Browse mode on a managed Indian segment: pair the
                  // favorites star with the segment-remove X so the user
                  // can favorite a stock and/or remove it from this
                  // segment list, matching the desktop panel.
                  rightAction = (
                    <div className="flex shrink-0 items-center gap-0.5">
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          toggleFavorite(token);
                        }}
                        aria-label={
                          starred
                            ? `Remove ${q.symbol} from favorites`
                            : `Add ${q.symbol} to favorites`
                        }
                        title={starred ? "Remove from favorites" : "Add to favorites"}
                        className="grid size-7 place-items-center rounded hover:bg-muted/40"
                      >
                        <Star
                          className={cn(
                            "size-4 transition-colors",
                            starred ? "fill-atm text-atm" : "text-muted-foreground",
                          )}
                        />
                      </button>
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          removeFromSegment(token, q.symbol);
                        }}
                        aria-label={`Remove ${q.symbol}`}
                        title={`Remove from ${bucket?.label}`}
                        className="shrink-0 rounded border border-border px-2 py-1 text-[11px] font-semibold text-muted-foreground hover:bg-muted/40 hover:text-foreground"
                      >
                        Remove
                      </button>
                    </div>
                  );
                }
              } else {
                // Favorites + any non-managed bucket: a single star toggle
                // (filled gold when favourited). Tapping a filled star removes
                // it from favourites — same as the APK. (No separate X.)
                rightAction = (
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      toggleFavorite(token);
                    }}
                    aria-label={starred ? `Remove ${q.symbol} from favorites` : `Add ${q.symbol} to favorites`}
                    title={starred ? "Remove from favorites" : "Add to favorites"}
                    className="grid size-7 shrink-0 place-items-center rounded hover:bg-muted/40"
                  >
                    <Star
                      className={cn(
                        "size-4 transition-colors",
                        starred ? "fill-atm text-atm" : "text-muted-foreground",
                      )}
                    />
                  </button>
                );
              }
              return (
                <InstrumentRow
                  key={token}
                  token={token}
                  symbol={q.symbol}
                  exchange={q.exchange}
                  segment={q.segment}
                  expiry={q.expiry ?? null}
                  bid={bid}
                  ask={ask}
                  ltp={ltp}
                  changePct={changePct}
                  isActive={isActive}
                  onSelect={() =>
                    onSelect(token, {
                      ltp,
                      bid,
                      ask,
                      symbol: q.symbol,
                      exchange: q.exchange,
                      segment: q.segment ?? q.instrument_type,
                    })
                  }
                  rightAction={rightAction}
                />
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}

/**
 * Single row in the instruments list. Extracted as its own component so the
 * sticky-display hook for change% has a stable hook-call site — earlier
 * inlining it inside the `list.map()` callback violated the rules-of-hooks.
 * Receives every value pre-resolved by the parent so this stays a pure
 * presentational node. Card-level memoisation is intentionally NOT added:
 * each row depends on its own live ticks via the sticky hooks inside
 * FlashCell + useStickyNumber, so we want the row to repaint on every
 * change. Memoising on prop equality would defeat that.
 */
function InstrumentRow({
  token,
  symbol,
  exchange,
  segment,
  expiry,
  bid,
  ask,
  ltp,
  changePct,
  isActive,
  onSelect,
  rightAction,
}: {
  token: string;
  symbol: string;
  exchange?: string;
  segment?: string;
  expiry?: string | null;
  bid: number | null;
  ask: number | null;
  ltp: number | null;
  changePct: number | null;
  isActive: boolean;
  onSelect: () => void;
  rightAction: React.ReactNode;
}) {
  const stickyChange = useStickyNumber(changePct);
  // Two-price watchlist row (operator-approved layout): change% sits under
  // the symbol on the left, and the SELL (bid, red) + BUY (ask, green)
  // prices stack on the right so the trader sees both sides of the spread
  // at a glance — the same shape as the broker app. Falls back to LTP for
  // either side when only the last-traded price is available (e.g. a feed
  // that publishes LTP but no book), so the row never collapses to "—"
  // when there is a live price.
  const expiryLabel = formatExpiryShort(expiry);
  const rawBid = bid ?? ltp ?? null;
  const rawAsk = ask ?? ltp ?? null;
  const stickyBid = useStickyNumber(rawBid);
  const stickyAsk = useStickyNumber(rawAsk);
  const changeColor =
    stickyChange == null || stickyChange === 0
      ? "text-muted-foreground"
      : stickyChange > 0
        ? "text-emerald-500"
        : "text-red-500";
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onSelect}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
      className={cn(
        // Right column `auto` so Indian-segment rows fit both star + X
        // without clipping; single-button rows still sit flush on the
        // right edge.
        "grid w-full cursor-pointer grid-cols-[1fr_auto_auto] items-center gap-3 border-b border-border/40 px-3 py-2.5 text-xs transition-colors",
        isActive ? "bg-primary/10" : "hover:bg-muted/30",
      )}
    >
      {/* Bold symbol + change% (left, stacked) */}
      <div className="flex min-w-0 flex-col items-start leading-tight">
        <span
          className={cn(
            "truncate text-[15px] font-bold tracking-tight",
            isActive && "text-primary",
          )}
        >
          {symbol}
        </span>
        <div className="mt-0.5 flex min-w-0 items-baseline gap-1.5">
          <span
            className={cn(
              "font-tabular tabular-nums text-[11px] font-semibold",
              changeColor,
            )}
          >
            {stickyChange != null
              ? `${stickyChange >= 0 ? "+" : ""}${stickyChange.toFixed(2)}%`
              : "—"}
          </span>
          {/* Contract month — F&O only, blank for equity/index rows. */}
          {expiryLabel && (
            <span className="truncate text-[11px] font-medium uppercase text-muted-foreground">
              {expiryLabel}
            </span>
          )}
        </div>
      </div>

      {/* Bid (sell, red) on top + Ask (buy, green) below — both prices
          shown so the trader reads the full spread at a glance. */}
      <div className="flex flex-col items-end leading-tight">
        <TickFlash
          value={stickyBid}
          className="whitespace-nowrap px-0.5 font-tabular tabular-nums text-sm font-bold text-red-500"
        >
          {stickyBid != null ? formatPrice(stickyBid, segment, exchange) : "—"}
        </TickFlash>
        <TickFlash
          value={stickyAsk}
          className="mt-0.5 whitespace-nowrap px-0.5 font-tabular tabular-nums text-sm font-bold text-emerald-500"
        >
          {stickyAsk != null ? formatPrice(stickyAsk, segment, exchange) : "—"}
        </TickFlash>
      </div>

      {rightAction}
    </div>
  );
}

/** Returns the latest non-null/non-zero value the cell has ever held.
 *  Use for the change% chip — same flicker reason as the price cells:
 *  during REST/WS hand-off it briefly becomes null and the percentage
 *  pill disappears. Sticky version keeps the last good % on screen. */
function useStickyNumber(value: number | null | undefined): number | null {
  const lastGoodRef = useRef<number | null>(null);
  if (value != null && Number.isFinite(value)) {
    lastGoodRef.current = value as number;
  }
  return lastGoodRef.current;
}
