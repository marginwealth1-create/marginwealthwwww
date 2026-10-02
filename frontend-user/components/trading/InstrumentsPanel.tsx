"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { RefreshCw, Search, Star, X } from "lucide-react";
import { InstrumentAPI, MarketwatchAPI, SegmentSettingsAPI } from "@/lib/api";
import { cn, formatPrice } from "@/lib/utils";
import { useMarketStream } from "@/lib/useMarketStream";
import { usePriceFlash } from "@/lib/usePriceFlash";

interface Props {
  onClose: () => void;
}

/** Generic asset-class buckets that map onto the backend's segment / exchange
 *  / instrument_type fields. The side panel ships with two layers:
 *    • Top-level groups (Forex, Stocks, Indices, Commodities, Crypto) — wide
 *      filters that just narrow by exchange or instrument type.
 *    • Granular segment chips (NSE EQ, NSE FUT, NSE OPT, BSE …, MCX …,
 *      Crypto Perp / Call / Put) — match the SegmentType enum used by the
 *      backend's netting + brokerage stacks.
 *  `segments` is sent comma-separated so a single round-trip can match
 *  multiple SegmentType values (NSE OPT covers four index- and stock-option
 *  segments, etc.). `instrumentTypes` does the same for InstrumentType. */
type Bucket = {
  key: string;
  label: string;
  group: "core" | "asset" | "nse" | "bse" | "mcx";
  // Either a watchlist marker, a segment/exchange/type filter, or free-text.
  mode: "watchlist" | "filter" | "query";
  segments?: string[];
  instrumentTypes?: string[];
  exchange?: string;
  query?: string;
  // Admin matrix row name(s) that drive this chip. When admin toggles a row
  // to `isActive = false`, the bucket disappears entirely from the dropdown
  // and the chip strip — not just its results. A bucket disappears when
  // EVERY row it depends on is inactive (so e.g. a future cross-segment
  // chip won't vanish from one row going off).
  adminRows?: string[];
  // When true, the chip is USER-MANAGED — the list shows only instruments
  // the user has explicitly added (via search + "Add"), not every Kite
  // cached row. Indian segments only. Infoway-fed segments (Forex, Crypto,
  // Stocks, Indices, Commodities) stay non-managed: those feeds are
  // small and curated already.
  managed?: boolean;
};

const BUCKETS: Bucket[] = [
  // Core
  { key: "favorites", label: "Favorites", group: "core", mode: "watchlist" },

  // Asset-class groups — strictly Infoway-fed segments. Indian-market
  // equivalents (NSE EQ, BSE EQ, MCX FUT, …) get their own dedicated
  // chips below, so these top-level filters never mix the two. Each
  // segment string here matches the value `_classify_infoway_code` writes
  // to `Instrument.segment` when mirroring Infoway subscriptions.
  { key: "forex", label: "Forex", group: "asset", mode: "filter", segments: ["FOREX"], adminRows: ["FOREX"] },
  { key: "stocks", label: "Stocks", group: "asset", mode: "filter", segments: ["STOCKS"], adminRows: ["STOCKS"] },
  { key: "indices", label: "Indices", group: "asset", mode: "filter", segments: ["INDICES"], adminRows: ["INDICES"] },
  { key: "commodities", label: "Commodities", group: "asset", mode: "filter", segments: ["COMMODITIES"], adminRows: ["COMMODITIES"] },
  { key: "crypto", label: "Crypto", group: "asset", mode: "filter", segments: ["CRYPTO_PERPETUAL", "CRYPTO_SPOT", "CRYPTO_FUTURE"], adminRows: ["CRYPTO"] },

  // NSE granular — managed (user adds instruments explicitly)
  { key: "nse_eq", label: "NSE EQ", group: "nse", mode: "filter", segments: ["NSE_EQUITY"], adminRows: ["NSE_EQ"], managed: true },
  { key: "nse_fut", label: "NSE FUT", group: "nse", mode: "filter", segments: ["NSE_FUTURE", "NSE_INDEX_FUTURE"], adminRows: ["NSE_FUT"], managed: true },
  { key: "nse_opt", label: "NSE OPT", group: "nse", mode: "filter", segments: ["NSE_INDEX_OPTION_BUY", "NSE_INDEX_OPTION_SELL", "NSE_STOCK_OPTION_BUY", "NSE_STOCK_OPTION_SELL"], adminRows: ["NSE_OPT"], managed: true },

  // BSE granular — managed
  { key: "bse_eq", label: "BSE EQ", group: "bse", mode: "filter", segments: ["BSE_EQUITY"], adminRows: ["BSE_EQ"], managed: true },
  { key: "bse_fut", label: "BSE FUT", group: "bse", mode: "filter", segments: ["BSE_FUTURE", "BSE_INDEX_FUTURE"], adminRows: ["BSE_FUT"], managed: true },
  { key: "bse_opt", label: "BSE OPT", group: "bse", mode: "filter", segments: ["BSE_OPTION_BUY", "BSE_OPTION_SELL"], adminRows: ["BSE_OPT"], managed: true },

  // MCX granular — managed
  { key: "mcx_fut", label: "MCX FUT", group: "mcx", mode: "filter", segments: ["MCX_FUTURE"], adminRows: ["MCX_FUT"], managed: true },
  { key: "mcx_opt", label: "MCX OPT", group: "mcx", mode: "filter", segments: ["MCX_OPTION_BUY", "MCX_OPTION_SELL"], adminRows: ["MCX_OPT"], managed: true },
  // Crypto deliberately has no granular split — admin manages a single
  // CRYPTO segment row, the top-level "Crypto" asset chip above covers
  // spot / perpetual / futures with one filter.
];

// GROUP_LABELS removed along with the native <select>+<optgroup> dropdown
// — the new single-strip chip selector flattens every bucket into one
// horizontal scrollable row, so per-group section headings are gone.

const _EXPIRY_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"] as const;

/** Format an expiry date for the side-panel row. Server returns
 *  `YYYY-MM-DD`; the F&O blotter convention is `DD-MMM-YYYY` so traders
 *  can scan a column of expiries fast (e.g. `26-JUN-2026`). Returns "" for
 *  non-F&O rows (no expiry field). */
function formatExpiry(raw: string | null | undefined): string {
  if (!raw) return "";
  const s = String(raw).slice(0, 10);
  const [y, m, d] = s.split("-");
  if (!y || !m || !d) return s;
  const mi = Number(m) - 1;
  if (mi < 0 || mi > 11) return s;
  return `${d}-${_EXPIRY_MONTHS[mi]}-${y}`;
}

/**
 * Sliding instruments panel — search any tradeable symbol, see live bid/ask,
 * 1-day arrow, click to open it in the terminal. Drives off the existing
 * marketwatch + instrument-search APIs (no new backend work).
 */
export function InstrumentsPanel({ onClose }: Props) {
  const router = useRouter();
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [bucketKey, setBucketKey] = useState<string>("favorites");
  // Optimistic favorite toggle — flips the star instantly while the
  // add/remove request is in flight. Reconciled by the watchlist refetch.
  const [pendingFav, setPendingFav] = useState<Map<string, boolean>>(new Map());

  // Inactive admin rows (Block → isActive = false). Refetched every 60 s so
  // a broker toggling a segment off shows up within a minute on every open
  // terminal; the backend caches the resolution for 30 s anyway. Buckets
  // whose admin row is in this set are removed before render so the chip
  // / dropdown entry disappears entirely instead of just returning empty
  // results.
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
        // Core (Favorites) buckets have no `adminRows` — always visible.
        if (rows.length === 0) return true;
        // Hide only when EVERY admin row backing the bucket is inactive.
        // Future cross-segment chips (e.g. one chip backed by both NSE_FUT
        // and BSE_FUT) survive partial deactivation.
        return rows.some((r) => !inactiveSet.has(r));
      }),
    [inactiveSet],
  );

  // Fall back to Favorites if the user had a bucket selected that was just
  // turned off — otherwise the dropdown would render the empty selection.
  useEffect(() => {
    if (!visibleBuckets.find((b) => b.key === bucketKey)) {
      setBucketKey("favorites");
    }
  }, [visibleBuckets, bucketKey]);

  const bucket = visibleBuckets.find((b) => b.key === bucketKey) ?? visibleBuckets[0];

  // Debounce the search input so we don't hammer the API on every keystroke.
  // 180 ms is the sweet spot — feels instant but lets a typist finish a word.
  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search), 180);
    return () => clearTimeout(t);
  }, [search]);

  // Active watchlist (drives the "Favorites" bucket)
  const { data: watchlists } = useQuery({
    queryKey: ["watchlists"],
    queryFn: () => MarketwatchAPI.list(),
    staleTime: 30_000,
  });
  const activeWl = watchlists?.[0];
  const { data: wlQuotes } = useQuery({
    queryKey: ["watchlist-quotes", activeWl?.id],
    queryFn: () => MarketwatchAPI.quotes(activeWl!.id),
    enabled: !!activeWl?.id && bucketKey === "favorites" && search.trim().length === 0,
    refetchInterval: 2000,
    placeholderData: (prev) => prev,
  });

  // Token → item-id lookup so the star can flip a row in or out of the
  // active watchlist without a second round-trip to find the item.
  const favItemByToken = useMemo(() => {
    const map = new Map<string, string>();
    for (const it of activeWl?.items ?? []) {
      if (it?.instrument_token && it?.id) map.set(String(it.instrument_token), String(it.id));
    }
    return map;
  }, [activeWl]);

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
      setPendingFav((prev) => {
        const next = new Map(prev);
        next.delete(tok);
        return next;
      });
      toast.error(e?.message || (currentlyFav ? "Failed to remove" : "Failed to add"));
    }
  }

  // Managed-segment marker — Indian chips (NSE EQ / NSE FUT / NSE OPT /
  // BSE * / MCX *) show only what the user has explicitly added. The
  // admin row name (e.g. "NSE_EQ") is the segment key on the backend.
  const managedSegmentName =
    bucket.managed && bucket.adminRows?.[0] ? bucket.adminRows[0] : null;

  // Per-segment "added items" list. Drives both the empty-list render
  // when search is off AND the "✓ Added" badge on search results.
  const { data: segmentItems } = useQuery<any[]>({
    queryKey: ["segment-items", managedSegmentName],
    queryFn: () => MarketwatchAPI.segmentItems(managedSegmentName!),
    enabled: !!managedSegmentName,
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
      toast.success(`Added ${symbol} to ${bucket.label}`, { duration: 1500 });
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

  // Bucket-driven browse (when search is empty and bucket isn't Favorites).
  // The backend accepts comma-separated `segment` and `instrument_type` so a
  // single chip ("NSE OPT") can match all four option-segment values in one
  // round-trip. `placeholderData: keep previous` keeps the table rendered
  // when the user flips between chips — no blank flash mid-switch.
  const browseSegments = bucket.mode === "filter" ? bucket.segments?.join(",") : undefined;
  const browseTypes = bucket.mode === "filter" ? bucket.instrumentTypes?.join(",") : undefined;
  const browseExchange = bucket.mode === "filter" ? bucket.exchange : undefined;

  // Free-text search — wins over the bucket when the box has any text.
  // Scoped to the current bucket's filters so typing "BANK" inside NSE OPT
  // returns only NSE option contracts, not MCX or crypto. When the bucket
  // is Favorites or a free-text bucket we don't constrain — that's
  // the global search the user expects.
  const searchScopeSegments = bucket.mode === "filter" ? browseSegments : undefined;
  const searchScopeTypes = bucket.mode === "filter" ? browseTypes : undefined;
  const searchScopeExchange = bucket.mode === "filter" ? browseExchange : undefined;
  const { data: searchHits } = useQuery({
    queryKey: [
      "instruments-search-side",
      debouncedSearch,
      searchScopeSegments,
      searchScopeTypes,
      searchScopeExchange,
    ],
    queryFn: () =>
      InstrumentAPI.search(
        debouncedSearch,
        searchScopeExchange,
        searchScopeSegments,
        30,
        searchScopeTypes,
      ),
    enabled: debouncedSearch.trim().length > 0,
    staleTime: 30_000,
    placeholderData: (prev) => prev,
  });
  const { data: bucketHits } = useQuery({
    queryKey: ["instruments-bucket", bucketKey, browseSegments, browseTypes, browseExchange],
    queryFn: () =>
      InstrumentAPI.search(
        bucket.mode === "query" ? bucket.query : undefined,
        browseExchange,
        browseSegments,
        100,
        browseTypes,
      ),
    enabled:
      search.trim().length === 0 &&
      bucket.mode !== "watchlist" &&
      // Managed segments don't pre-browse the full Kite cache — they
      // only render the user's added items. The browse only fires for
      // Forex / Crypto / etc. (Infoway-fed) chips.
      !managedSegmentName &&
      (bucket.mode === "query" || !!browseSegments || !!browseTypes || !!browseExchange),
    staleTime: 30_000,
    placeholderData: (prev) => prev,
  });

  // Tokens currently visible — drives the live-quote pump. Watchlist quotes
  // already include bid/ask, so we only need to pump the search/bucket lists.
  // Keyed off `debouncedSearch` (not the raw input) so a half-typed query
  // doesn't blank the visible-tokens list mid-keystroke.
  //
  // Capped at LIVE_TOKEN_CAP. A search returns up to 100 results which we
  // render in a non-virtualised list (web has no FlashList equivalent
  // wired in yet) — but subscribing all 100 means the backend pump
  // refreshes every overlay every 250 ms, and the per-tick WS payload
  // balloons. ChatGPT / Linear / Zerodha "feel-instant" requires the
  // visible top of the list to update fast, not the 90 rows below the
  // fold. After the backend pump was parallelised the cap matters less
  // for latency, but it still keeps the steady-state load proportional
  // to what the user actually looks at.
  const LIVE_TOKEN_CAP = 30;
  const visibleTokens = useMemo<string[]>(() => {
    const all = (() => {
      if (debouncedSearch.trim().length > 0) return (searchHits ?? []).map((s: any) => s.token);
      if (bucket.mode === "watchlist") {
        // Subscribe the FAVOURITE tokens to WS + batch quotes. Without this
        // `quoteByToken` stayed empty for the Favorites tab, so the row's
        // overlay (pickPositive(q.bid, liveOverlay?.bid, …)) had nothing to
        // pick and fell back to the /marketwatch/quotes REST ltp — which is 0
        // for Infoway (crypto/forex/metals). Those rows froze at 0 while every
        // other bucket ticked live. instrument_token == symbol for Infoway.
        return (activeWl?.items ?? []).map((it: any) =>
          String(it.instrument_token ?? it.token),
        );
      }
      if (managedSegmentName) {
        return (segmentItems ?? []).map((it: any) => String(it.instrument_token));
      }
      return (bucketHits ?? []).map((s: any) => s.token);
    })();
    return all.slice(0, LIVE_TOKEN_CAP);
  }, [debouncedSearch, searchHits, bucketHits, bucket, managedSegmentName, segmentItems, activeWl?.items]);

  // Live quote pump — uses the `/ws/marketdata` stream so bid/ask/change tick
  // at the same 250 ms cadence as the order panel / positions, instead of a
  // 2 s REST poll. The server-side `_overlay_all` runs per-tick: Infoway
  // (forex/crypto/metals/energy) + Zerodha (Indian) + admin spread. The
  // initial REST snapshot below seeds rows with bid/ask immediately on first
  // render — without it, rows would render "—" until the first WS tick (up
  // to one heartbeat). After the first tick the stream takes over.
  const tokensKey = visibleTokens.join(",");
  const { data: liveQuotes } = useQuery<any[]>({
    queryKey: ["instruments-batch-quotes-seed", tokensKey],
    queryFn: () => InstrumentAPI.quotesBatch(visibleTokens),
    enabled: visibleTokens.length > 0,
    staleTime: 30_000,
    refetchInterval: false,
  });
  const streamQuotes = useMarketStream(visibleTokens);
  const quoteByToken = useMemo(() => {
    const map = new Map<string, any>();
    // Seed with the REST snapshot first so the row has bid/ask before the
    // first WS tick. Live ticks overwrite per token as they arrive.
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
        // Expiry date for F&O contracts — surfaced under the symbol so the
        // trader knows exactly which expiry they're about to click into
        // without having to open the contract first. Index/equity rows
        // have no expiry, so the field is null and the row renders symbol
        // only.
        expiry: s.expiry ?? null,
        instrument_type: s.instrument_type ?? null,
        bid: live?.bid ?? null,
        ask: live?.ask ?? null,
        // LTP surfaced too so the row can fall back to it when the order book
        // (bid/ask) hasn't been pushed yet — equity / index instruments with
        // a Zerodha subscription land LTP before their depth, and traders
        // saw "— —" for the first second on every refresh. With LTP available
        // both cells render instantly and update to the real bid/ask the
        // moment depth arrives.
        ltp: live?.ltp ?? null,
        change_pct: live?.change_pct ?? null,
      };
    };
    if (debouncedSearch.trim().length > 0) return (searchHits ?? []).map(enrich);
    if (bucket.mode === "watchlist") return wlQuotes ?? [];
    if (managedSegmentName) {
      // Segment items come back as {id, instrument_token, symbol, exchange}
      // PLUS the contract metadata (expiry / instrument_type / segment) the
      // API now attaches. Passing `expiry` through is what keeps the
      // "26-JUN-2026" line under the symbol AFTER the instrument is added —
      // it used to be hardcoded null here, so the expiry a trader saw while
      // searching disappeared the moment they added it to the chip.
      return (segmentItems ?? []).map((it: any) =>
        enrich({
          token: it.instrument_token,
          symbol: it.symbol,
          exchange: it.exchange,
          segment: it.segment ?? null,
          expiry: it.expiry ?? null,
          instrument_type: it.instrument_type ?? null,
        }),
      );
    }
    return (bucketHits ?? []).map(enrich);
  }, [
    debouncedSearch, searchHits, wlQuotes, bucketHits, bucket,
    quoteByToken, managedSegmentName, segmentItems,
  ]);

  // Auto-clear search when panel re-opens
  useEffect(() => {
    setSearch("");
  }, []);

  function pickToken(token: string) {
    router.push(`/terminal?token=${encodeURIComponent(token)}`);
    onClose();
  }

  return (
    <aside className="flex h-full w-[min(340px,92vw)] shrink-0 animate-in slide-in-from-left-4 fade-in-0 flex-col border-r border-border bg-card duration-200">
      {/* Header */}
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          Instruments
        </span>
        <button
          type="button"
          aria-label="Refresh"
          className="ml-auto grid size-7 place-items-center rounded text-muted-foreground hover:bg-muted/40 hover:text-foreground"
        >
          <RefreshCw className="size-3.5" />
        </button>
        <button
          type="button"
          aria-label="Close panel"
          onClick={onClose}
          className="grid size-7 place-items-center rounded text-muted-foreground hover:bg-muted/40 hover:text-foreground"
        >
          <X className="size-4" />
        </button>
      </div>

      {/* Search + favourites filter */}
      <div className="space-y-2 border-b border-border px-3 py-2">
        <div className="relative">
          <Search className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search"
            className="h-8 w-full rounded-md border border-border bg-background pl-7 pr-2 text-xs outline-none placeholder:text-muted-foreground focus:border-primary"
          />
        </div>
        {/* Bucket selector — single horizontal chip strip covering every
            bucket (Favorites + All + asset / NSE / BSE / MCX). Replaces
            the old native <select> + secondary chip-strip combo: that
            popped a Windows-style OS dropdown the user couldn't style,
            and on mobile the chip row below it was hidden whenever
            Favorites / All was active. One strip = one consistent
            tap-and-pick UX on every device. `visibleBuckets` already
            hides any bucket whose admin row is flagged inactive. */}
        <div
          className="scroll-smooth -mx-1 flex gap-1 overflow-x-auto px-1 [scrollbar-width:none] [-ms-overflow-style:none] [&::-webkit-scrollbar]:hidden"
          style={{
            WebkitOverflowScrolling: "touch",
            maskImage:
              "linear-gradient(to right, black 0%, black calc(100% - 16px), transparent 100%)",
            WebkitMaskImage:
              "linear-gradient(to right, black 0%, black calc(100% - 16px), transparent 100%)",
          }}
        >
          {visibleBuckets.map((b) => (
            <button
              key={b.key}
              type="button"
              onClick={() => setBucketKey(b.key)}
              className={cn(
                // `uppercase` so Infoway-feed buckets (Forex / Stocks / Indices
                // / Commodities / Crypto) render in the same all-caps style as
                // Zerodha-fed chips (NSE EQ / NSE FUT / NSE OPT / …). Without
                // this the two halves of the strip look like they came from
                // different apps.
                "shrink-0 snap-center whitespace-nowrap rounded-full border px-2.5 py-0.5 text-[10px] font-medium uppercase tracking-wide transition-colors",
                bucketKey === b.key
                  ? "border-primary/40 bg-primary/10 text-primary"
                  : "border-border text-muted-foreground hover:bg-muted/40 hover:text-foreground",
              )}
            >
              {b.label}
            </button>
          ))}
        </div>
      </div>

      {/* List — column header removed; each row carries its own labelling
          via the stacked bid (red, top) / ask (green, bottom) layout. */}
      <div className="flex-1 overflow-y-auto scrollbar-thin">
        {list.length === 0 && (
          <div className="grid h-32 place-items-center px-4 text-center text-xs text-muted-foreground">
            {search.trim()
              ? "No instruments match"
              : managedSegmentName
                ? `No instruments yet. Search above to add to ${bucket.label}.`
                : "Add instruments to your watchlist to see them here."}
          </div>
        )}
        {list.map((q: any) => {
          const token = String(q.instrument_token);
          const starred = isFav(token);
          const liveOverlay = quoteByToken.get(token);
          const pickPositive = (...values: any[]) => {
            for (const v of values) {
              const n = Number(v);
              if (Number.isFinite(n) && n > 0) return n;
            }
            return null;
          };
          const bidDisplay = pickPositive(q.bid, liveOverlay?.bid, q.ltp, liveOverlay?.ltp);
          const askDisplay = pickPositive(q.ask, liveOverlay?.ask, q.ltp, liveOverlay?.ltp);
          const changePct = q.change_pct ?? liveOverlay?.change_pct ?? null;
          const inSearchMode = debouncedSearch.trim().length > 0;
          const alreadyAdded = managedSegmentName ? addedTokenSet.has(token) : false;
          // Action button on the right edge — meaning shifts by context:
          //   • Search mode + managed + not added → "+" add to segment
          //   • Search mode + managed + added     → muted "Added" badge
          //   • Showing managed segment list      → ★ favorite + X remove
          //   • Favorites watchlist row           → "X" remove from favorites
          //   • Otherwise (search hit, non-managed bucket) → star toggle
          //
          // The Indian-segment browse mode pairs the star with the X so a
          // user can independently (a) mark a stock as favorite for the
          // global watchlist AND (b) remove it from this segment list,
          // matching the way Zerodha Kite's market-watch shows both
          // controls per row.
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
                  title={`Add to ${bucket.label}`}
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
                    className="grid size-6 place-items-center rounded hover:bg-muted/40"
                  >
                    <Star
                      className={cn(
                        "size-3.5 transition-colors",
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
                    title={`Remove from ${bucket.label}`}
                    className="shrink-0 rounded border border-border px-2 py-1 text-[11px] font-semibold text-muted-foreground hover:bg-muted/40 hover:text-foreground"
                  >
                    Remove
                  </button>
                </div>
              );
            }
          } else if (bucket.mode === "watchlist" && starred) {
            // Favorites tab — X removes from the watchlist.
            rightAction = (
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  toggleFavorite(token);
                }}
                aria-label={`Remove ${q.symbol} from favorites`}
                title="Remove from favorites"
                className="shrink-0 rounded border border-border px-2 py-1 text-[11px] font-semibold text-muted-foreground hover:bg-muted/40 hover:text-foreground"
              >
                Remove
              </button>
            );
          } else {
            // Non-managed bucket — star toggle as before.
            rightAction = (
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  toggleFavorite(token);
                }}
                aria-label={starred ? `Remove ${q.symbol} from favorites` : `Add ${q.symbol} to favorites`}
                title={starred ? "Remove from favorites" : "Add to favorites"}
                className="grid size-6 shrink-0 place-items-center rounded hover:bg-muted/40"
              >
                <Star
                  className={cn(
                    "size-3.5 transition-colors",
                    starred ? "fill-atm text-atm" : "text-muted-foreground",
                  )}
                />
              </button>
            );
          }
          return (
            <div
              key={token}
              role="button"
              tabIndex={0}
              onClick={() => pickToken(token)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  pickToken(token);
                }
              }}
              // Last column is `auto` so it grows when an Indian-segment row
              // renders both the favorites star and the segment-remove X.
              // Other contexts (single star, single X, "+" button, "✓"
              // badge) just take the natural 28-px slot.
              className="grid w-full cursor-pointer grid-cols-[1fr_auto_auto] items-center gap-3 border-b border-border/40 px-3 py-2.5 text-left text-xs transition-colors hover:bg-muted/30"
            >
              {/* Symbol + change% + expiry (left side, stacked) */}
              <div className="flex min-w-0 flex-col items-start leading-tight">
                <span className="break-all font-semibold text-sm leading-snug">
                  {q.symbol}
                </span>
                <div className="mt-0.5 flex items-baseline gap-1.5 text-[10px]">
                  {changePct != null ? (
                    <span
                      className={cn(
                        "font-medium tabular-nums",
                        Number(changePct) > 0
                          ? "text-emerald-500"
                          : Number(changePct) < 0
                            ? "text-red-500"
                            : "text-muted-foreground",
                      )}
                    >
                      {Number(changePct) >= 0 ? "+" : ""}
                      {Number(changePct).toFixed(2)}%
                    </span>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                  {q.expiry && (
                    <span className="truncate text-muted-foreground">
                      Exp: {formatExpiry(q.expiry)}
                    </span>
                  )}
                </div>
              </div>

              {/* Bid (red, top) / Ask (green, bottom) — stacked vertically */}
              <div className="flex flex-col items-end gap-0.5 leading-tight">
                <FlashPrice
                  value={bidDisplay}
                  segment={q.segment}
                  exchange={q.exchange}
                  side="bid"
                />
                <FlashPrice
                  value={askDisplay}
                  segment={q.segment}
                  exchange={q.exchange}
                  side="ask"
                />
              </div>

              {/* Right-edge action — context-dependent (see above) */}
              {rightAction}
            </div>
          );
        })}
      </div>
    </aside>
  );
}


/**
 * Bid / ask cell that flashes green when the price ticks up, red when
 * it ticks down, and decays back to neutral after ~700 ms. Mirrors the
 * tick-flash UX every Indian broker (Zerodha / Upstox / Dhan) uses on
 * their market-watch grid — the trader's eye tracks price movement
 * without having to compare two numbers.
 *
 * Wrapping in a per-cell component means each row's hook tracks its
 * own previous value; rendering the cell inline inside `.map()` would
 * be illegal (hooks at the top of components only).
 */
function FlashPrice({
  value,
  segment,
  exchange,
  side,
}: {
  value: number | null;
  segment?: string;
  exchange?: string;
  side: "bid" | "ask";
}) {
  const dir = usePriceFlash(value);
  const baseColor = side === "bid" ? "text-red-500" : "text-emerald-500";
  const flashColor =
    dir === "up"
      ? "text-emerald-500"
      : dir === "down"
        ? "text-red-500"
        : baseColor;
  return (
    <span
      className={cn(
        "whitespace-nowrap rounded-[3px] font-tabular tabular-nums text-[11px] font-medium transition-colors",
        flashColor,
        // Tint the background too: a bid's base colour is already red, so its
        // down-tick flashed red-on-red and never showed (same for an ask's
        // up-tick). The tint reads in both directions.
        dir === "up" && "bg-buy/25",
        dir === "down" && "bg-sell/25",
      )}
    >
      {value != null ? formatPrice(value, segment, exchange) : "—"}
    </span>
  );
}
