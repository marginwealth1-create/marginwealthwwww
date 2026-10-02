"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTheme } from "next-themes";
import { toast } from "sonner";
import { ArrowLeft, ChevronLeft, ChevronRight } from "lucide-react";
import { InstrumentAPI, MarketwatchAPI, OrderAPI, PositionAPI } from "@/lib/api";
import { useMarketStream } from "@/lib/useMarketStream";
import { OrderPanel } from "@/components/trading/OrderPanel";
import { PositionsTabs } from "@/components/trading/PositionsTabs";
import { TradingViewChart } from "@/components/trading/TradingViewChart";
import { ChartTabs, type ChartTab } from "@/components/trading/ChartTabs";
import { TIMEFRAMES, type Timeframe } from "@/components/trading/ChartToolbar";
import { MobileQuickTradeBar } from "@/components/trading/MobileQuickTradeBar";
import { MobileOptionChain } from "@/components/trading/MobileOptionChain";
import { MobileNews } from "@/components/trading/MobileNews";
import { TradeDetailSheet } from "@/components/trading/TradeDetailSheet";
import { WalletStrip } from "@/components/trading/WalletStrip";
import { cn, formatPercent, pnlColor } from "@/lib/utils";

const ORDER_PANEL_COLLAPSED_KEY = "setupfx.terminal.orderPanelCollapsed";

export default function TradingTerminalPage() {
  const qc = useQueryClient();
  // Mirror the app theme into the embedded TradingView widget. Defaults to
  // dark while the theme provider hasn't hydrated to avoid a white flash.
  const { resolvedTheme } = useTheme();
  const chartTheme: "light" | "dark" = resolvedTheme === "light" ? "light" : "dark";

  // Active watchlist drives the chart-tabs row
  const { data: watchlists } = useQuery({
    queryKey: ["watchlists"],
    queryFn: () => MarketwatchAPI.list(),
  });
  const activeWl = watchlists?.[0];

  const { data: wlQuotes } = useQuery({
    queryKey: ["watchlist-quotes", activeWl?.id],
    queryFn: () => MarketwatchAPI.quotes(activeWl!.id),
    enabled: !!activeWl?.id,
    refetchInterval: 2000,
  });

  // Selected instrument — kept in sync with the ?token= URL param so that
  // soft-nav clicks from the side panel (router.push) actually swap the chart.
  const searchParams = useSearchParams();
  const urlToken = searchParams?.get("token") || null;
  const [selectedToken, setSelectedToken] = useState<string | null>(urlToken);

  // Mobile-only top-section view: Charts / Option chain / News. Always resets
  // to "chart" when the instrument changes so a new symbol opens on its chart.
  const [mobileChartView, setMobileChartView] = useState<
    "chart" | "options" | "news"
  >("chart");
  // Token for the TradeDetailSheet card that opens when a strike is tapped
  // in the mobile option chain tab. Separate from selectedToken so tapping
  // a strike doesn't also swap the chart underneath.
  const [ocSheetToken, setOcSheetToken] = useState<string | null>(null);
  useEffect(() => {
    setMobileChartView("chart");
  }, [selectedToken]);

  useEffect(() => {
    if (urlToken && urlToken !== selectedToken) {
      setSelectedToken(urlToken);
    }
  }, [urlToken]);

  const { data: instrument } = useQuery({
    queryKey: ["instrument", selectedToken],
    queryFn: () => InstrumentAPI.detail(selectedToken!),
    enabled: !!selectedToken,
  });

  // Quote poll relaxed from 1 s → 2.5 s. The `/ws/marketdata` WS pump
  // publishes ticks at ~250 ms so the bid/ask the OrderPanel cares about
  // is already live via `useMarketStream`; this REST poll is just a
  // backup for OHLC / volume / change_pct fields the header strip reads.
  // 1 s was burning a request every second on top of the WS feed, which
  // is what made the terminal feel laggy on slow networks.
  const { data: quote } = useQuery({
    queryKey: ["quote", selectedToken],
    queryFn: () => InstrumentAPI.quote(selectedToken!),
    enabled: !!selectedToken,
    refetchInterval: 2500,
    staleTime: 1500,
    refetchOnWindowFocus: false,
  });

  // Chart timeframe — fixed at 5m for the initial chart load + OHLC label.
  // TradingView's own toolbar handles in-chart timeframe switching.
  const tf: Timeframe = TIMEFRAMES[1];

  // Order panel collapse — toggleable via the chevron on the panel's left
  // edge. State persists across reloads so the trader's preferred layout
  // sticks. Hydrated client-side after mount to avoid SSR / localStorage
  // mismatch.
  const [orderPanelCollapsed, setOrderPanelCollapsed] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined") return;
    setOrderPanelCollapsed(
      window.localStorage.getItem(ORDER_PANEL_COLLAPSED_KEY) === "1",
    );
  }, []);
  function toggleOrderPanel() {
    setOrderPanelCollapsed((v) => {
      const next = !v;
      if (typeof window !== "undefined") {
        window.localStorage.setItem(ORDER_PANEL_COLLAPSED_KEY, next ? "1" : "0");
      }
      return next;
    });
  }

  // Tabs derived from watchlist quotes. `id` comes from `activeWl.items`
  // Chart tabs are now PURELY local state — decoupled from the watchlist
  // entirely. Earlier the tab strip was derived from `wlQuotes`, which meant
  // every time the user starred an instrument in the Instruments panel it
  // also popped up as a new chart tab. Per user request: tabs should
  // represent only the instruments the user has actively opened on the
  // chart, not their favorites/watchlist.
  //
  // Persisted to localStorage so the strip survives page reloads + browser
  // tab restores. Capped at 2 tabs (FIFO eviction) to keep the strip from
  // sprawling on mobile.
  const TABS_LOCAL_KEY = "setupfx.terminal.openTabs";
  const MAX_OPEN_TABS = 2;
  const [openTabs, setOpenTabs] = useState<ChartTab[]>([]);

  // Hydrate from localStorage on mount.
  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      const raw = window.localStorage.getItem(TABS_LOCAL_KEY);
      if (!raw) return;
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        setOpenTabs(
          parsed
            .filter((t: any) => t && typeof t.token === "string")
            .slice(0, MAX_OPEN_TABS)
            .map((t: any) => ({ token: String(t.token), symbol: String(t.symbol ?? "—") })),
        );
      }
    } catch {
      // ignore corrupt entries
    }
  }, []);

  // Persist tabs on every change.
  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(TABS_LOCAL_KEY, JSON.stringify(openTabs));
    } catch {
      // localStorage full / disabled — just skip
    }
  }, [openTabs]);

  // Pick something to show when nothing is selected. Order of preference:
  //   1. the chart tab restored from localStorage — the strip already SHOWS
  //      it, so leaving it unselected rendered a BTCUSD tab over an empty
  //      "Select an instrument" chart and an order panel with no price;
  //   2. the first favourite (no network call);
  //   3. a BTCUSD search as the last resort.
  //
  // Deliberately NOT keyed on `wlQuotes`: that query refetches every 2 s and
  // hands back a new array whenever a price moves, so the cleanup cancelled
  // the in-flight search every two seconds. Any search slower than that never
  // landed and nothing was ever selected. The dependencies below only change
  // when the answer could actually change.
  //
  // Must stay BELOW the `openTabs` useState: reading it earlier is a TDZ
  // ReferenceError at render and fails `next build` type-checking.
  const firstTabToken = openTabs[0]?.token ?? null;
  const firstFavToken = wlQuotes?.[0]?.instrument_token ?? null;
  useEffect(() => {
    if (selectedToken) return;
    if (firstTabToken) {
      setSelectedToken(firstTabToken);
      return;
    }
    if (firstFavToken) {
      setSelectedToken(firstFavToken);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const found = await InstrumentAPI.search("BTCUSD", undefined, undefined, 1);
        if (!cancelled && found && found[0]?.token) {
          setSelectedToken(found[0].token);
        }
      } catch {
        // ignore — the user can still pick from the panel
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedToken, firstTabToken, firstFavToken]);

  // Whenever the user opens an instrument (either via URL ?token=, watchlist
  // click, option-chain pick, or in-tab swap) ensure it's represented in
  // openTabs. FIFO-evict the oldest when over the cap.
  useEffect(() => {
    if (!selectedToken) return;
    const sym = instrument?.symbol ?? "—";
    setOpenTabs((prev) => {
      const existingIdx = prev.findIndex((t) => t.token === selectedToken);
      if (existingIdx >= 0) {
        // Keep position stable; just refresh the symbol label if it
        // arrived after the tab was already in the list.
        if (prev[existingIdx].symbol === sym) return prev;
        const next = prev.slice();
        next[existingIdx] = { token: selectedToken, symbol: sym };
        return next;
      }
      const next = [...prev, { token: selectedToken, symbol: sym }];
      return next.slice(-MAX_OPEN_TABS);
    });
  }, [selectedToken, instrument?.symbol]);

  const tabsWithSelected: ChartTab[] = openTabs;

  // Close a tab — purely local, NEVER touches the watchlist. The user can
  // still see the symbol starred in the instruments panel after closing.
  function closeTab(token: string) {
    setOpenTabs((prev) => {
      const next = prev.filter((t) => t.token !== token);
      if (token === selectedToken) {
        // Activate whatever tab is left; null if none.
        setSelectedToken(next[0]?.token ?? null);
      }
      return next;
    });
  }

  // Polling interval for the Positions / Orders queries. 2 s baseline,
  // BUT widened to 3.5 s for ~3 s after any optimistic update so an
  // in-flight stale read from Atlas doesn't wipe the just-mutated row.
  // Returning `false` here used to permanently disable polling — once
  // dataUpdatedAt was bumped by setQueryData or by the post-invalidate
  // refetch, the interval re-evaluated to `false` and the loop never
  // resumed. The visible symptom: a limit order would appear in Pending
  // for a few seconds and then "vanish" without ever showing up in
  // History, because the cache stayed at status=OPEN forever while the
  // backend had already moved it to EXECUTED. Returning a positive
  // number keeps the polling loop alive.
  const livePollInterval = (query: any) => {
    const last = (query?.state?.dataUpdatedAt as number) || 0;
    const sinceMs = Date.now() - last;
    return sinceMs < 3000 ? 3500 : 2000;
  };

  const { data: positions } = useQuery({
    queryKey: ["positions", "open"],
    queryFn: () => PositionAPI.open(),
    refetchInterval: livePollInterval,
  });

  const { data: orders } = useQuery({
    queryKey: ["orders", "recent"],
    queryFn: () => OrderAPI.list(),
    refetchInterval: livePollInterval,
  });

  const pendingOrders = useMemo(
    () =>
      (orders ?? []).filter((o: any) =>
        ["PENDING", "OPEN", "TRIGGERED"].includes(String(o.status).toUpperCase())
      ),
    [orders]
  );
  const history = useMemo(
    () =>
      (orders ?? []).filter((o: any) => {
        const st = String(o.status).toUpperCase();
        if (!["COMPLETE", "EXECUTED", "FILLED", "REJECTED"].includes(st)) return false;
        // Only show closing fills (have realized P&L). Opening orders are
        // visible in Positions/Active tabs — showing them here too causes
        // confusion when admin reopens a position (the new BUY shows as
        // "executed" in history while the position is still open).
        if (o.pnl_inr === null || o.pnl_inr === undefined || o.pnl_inr === "") return false;
        // Stop-outs are system-generated forced closes shown in the
        // Positions → Closed tab. Exclude them here so the user's order
        // History only shows trades they initiated themselves.
        if (String(o.reason ?? "").toUpperCase() === "STOP_OUT") return false;
        return true;
      }),
    [orders]
  );
  const cancelled = useMemo(
    () => (orders ?? []).filter((o: any) => String(o.status).toUpperCase() === "CANCELLED"),
    [orders]
  );

  // Live-LTP overlay for the positions table, driven by a WebSocket stream
  // (not REST polling). Two reasons this matters:
  //   1) The /positions/open REST endpoint polls at 2 s; sub-second moves on
  //      the order panel's BUY/SELL strip used to lap the CURRENT column by
  //      a full poll interval. The WS pump runs at 250 ms so updates land
  //      essentially as fast as the upstream Kite tick feed delivers them.
  //   2) Standard broker UX: a BUY (long) position is closed by SELLING, so
  //      CURRENT should reflect the price the user could exit at — the bid.
  //      A SELL (short) position is closed by BUYING — the ask. The order
  //      panel's BUY/SELL strip already encodes the same convention; this
  //      keeps the positions table consistent with it.
  const positionTokens = useMemo(
    () =>
      Array.from(
        new Set(
          (positions ?? [])
            .map((p: any) => p?.instrument_token ?? p?.token)
            .filter((t: any): t is string => !!t)
        )
      ),
    [positions]
  );
  // Combine position tokens with the currently-selected chart token so the
  // chart's datafeed gets the same WebSocket tick stream the order panel uses.
  const streamTokens = useMemo(
    () =>
      selectedToken
        ? [...new Set([...positionTokens, selectedToken])]
        : positionTokens,
    [positionTokens, selectedToken]
  );
  const liveQuotesByToken = useMarketStream(streamTokens);
  const chartLiveQuote = selectedToken ? (liveQuotesByToken.get(selectedToken) ?? null) : null;
  const positionsLive = useMemo(() => {
    if (!Array.isArray(positions) || liveQuotesByToken.size === 0) return positions ?? [];

    // Match the backend's `is_usd_quoted_segment` heuristic so the live
    // P&L overlay converts USD-quoted positions (Infoway feeds: crypto /
    // FX conversion is disabled platform-wide — every feed price is
    // treated as INR-native, so P&L is the raw (close − avg) × qty in
    // INR. No `× fx` branch here; the previous USD-segment fork has
    // been removed.

    return positions.map((p: any) => {
      const tok = String(p?.instrument_token ?? p?.token ?? "");
      const live = tok ? liveQuotesByToken.get(tok) : undefined;
      if (!live) return p;
      const qty = Number(p.quantity);
      const isLong = qty > 0;
      // Close-side price: long closes by selling → use bid; short closes by
      // buying → use ask. Fall back to LTP when the chosen side is missing.
      const liveLtp = Number(live.ltp ?? 0) || Number(p.ltp) || 0;
      const bid = Number(live.bid ?? 0);
      const ask = Number(live.ask ?? 0);
      const closePrice = (isLong ? bid : ask) || liveLtp;
      if (!closePrice) return p;
      const avg = Number(p.avg_price);
      const pnl =
        Number.isFinite(avg) && Number.isFinite(qty)
          ? (closePrice - avg) * qty
          : 0;
      return { ...p, ltp: closePrice, unrealized_pnl: pnl };
    });
  }, [positions, liveQuotesByToken]);

  const totalPnL = useMemo(
    () =>
      (positionsLive ?? []).reduce(
        (acc: number, p: any) => acc + (Number(p.unrealized_pnl) || 0),
        0
      ),
    [positionsLive]
  );

  // Prefer the LIVE WS quote (`chartLiveQuote` — the SAME /ws/marketdata
  // stream the chart + positions already use) over the 2.5 s REST `quote`
  // poll. When a user taps an instrument in the option chain / watchlist the
  // token is added to `streamTokens` and the backend answers the `subscribe`
  // with an immediate snapshot, so the WS price lands in ~one tick (~250 ms)
  // — instead of the OrderPanel waiting up to 2.5 s for the REST quote to
  // refetch on the new token (the "price 2 second baad aata hai" lag). REST
  // stays the fallback for the first paint before the tick and for OHLC /
  // change fields the WS omits.
  const liveLtp = Number(chartLiveQuote?.ltp ?? 0);
  const liveBid = Number(chartLiveQuote?.bid ?? 0);
  const liveAsk = Number(chartLiveQuote?.ask ?? 0);
  const bestBid = (liveBid > 0 ? liveBid : (quote?.bid ?? quote?.depth?.bids?.[0]?.price)) ?? null;
  const bestAsk = (liveAsk > 0 ? liveAsk : (quote?.ask ?? quote?.depth?.asks?.[0]?.price)) ?? null;

  // Option-chain eligibility — same heuristic as TradeDetailSheet: Indian
  // equity / index / future underlyings can have an option chain; Infoway
  // feeds (forex / crypto / metals / intl) and option rows themselves
  // cannot, so the "Option chain" tab is hidden for them entirely.
  const seg = (instrument?.segment ?? "").toUpperCase();
  const exch = (instrument?.exchange ?? "").toUpperCase();
  const showOptionChain =
    ["NSE", "BSE", "NFO", "BFO", "MCX"].includes(exch) && !seg.includes("OPTION");
  const ocUnderlying = (instrument?.symbol ?? "").toUpperCase();

  // Mobile top-section tabs: Charts always, Option chain only for F&O-eligible
  // underlyings, News always.
  const viewTabs: { key: "chart" | "options" | "news"; label: string }[] = [
    { key: "chart", label: "Charts" },
    ...(showOptionChain
      ? [{ key: "options" as const, label: "Option chain" }]
      : []),
    { key: "news", label: "News" },
  ];

  return (
    // Layout strategy:
    //  • lg+ (≥1024px): two-column grid filling the viewport height, no
    //    page scroll — chart and order panel are independently sized.
    //  • mobile / md: single column, page scrolls naturally. The chart
    //    keeps an aspect-ratio-driven min height so it doesn't collapse
    //    to nothing on a narrow screen, and the order panel + positions
    //    flow below where the user can scroll to them.
    //
    // Responsiveness across monitor sizes (16″ laptop → 32″ ultra-wide):
    //   • `max-w-[1800px] mx-auto` keeps the chart from ballooning into
    //     an awkward wide-screen rectangle on 4K / 32″ displays — past
    //     1800 px the content centres instead of stretching.
    //   • Order-panel column scales with breakpoint (`lg:340 → xl:380 →
    //     2xl:420 px`) so on bigger screens it doesn't look like a
    //     toy panel next to a giant chart.
    // lg+ uses flex so the order-panel column can animate its width when
    // the user collapses it. CSS grid template columns aren't transitionable
    // — flex + Tailwind's `transition-[width]` is.
    <div className="mx-auto flex min-h-0 w-full max-w-[1800px] flex-col lg:h-full lg:flex-row lg:gap-2">
      {/* ── CENTER: chart card + positions strip ──────────
          `min-w-0` is critical with `flex-row`: without it the TradingView
          chart's intrinsic content width keeps the section from shrinking,
          which pushes the order-panel column past the viewport's right
          edge (BUY/SELL prices get clipped). With `min-w-0` the section
          can compress as needed and the fixed-width order panel stays
          visible. Matches the `minmax(0,1fr)` behaviour of the old grid. */}
      <section className="flex min-h-0 min-w-0 flex-1 flex-col lg:gap-2">
        {/* Chart card. mobile / md keeps `min-h-[60vh]` so the chart
            can't collapse below ~60 % of the viewport on narrow screens.
            lg+: uses `flex-1 min-h-0` so the chart takes whatever space
            is LEFT OVER after PositionsTabs + WalletStrip claim their
            natural heights (both wrapped in `shrink-0` below). The old
            `lg:max-h-[70vh]` cap was the bug: the chart took 70vh and
            PositionsTabs added another 40vh → page overflowed by 10vh
            on a standard laptop, which is why the user saw the chart
            and the positions table overlapping with a stray horizontal
            scrollbar inside the chart card. */}
        <div className="relative flex h-[100dvh] flex-col overflow-hidden bg-card lg:h-auto lg:min-h-0 lg:flex-1 lg:rounded-lg lg:border lg:border-border">
          {/* Floating "expand order panel" button — only rendered when the
              OrderPanel column is fully hidden. Sits at the chart card's
              top-right edge so the user can recover the panel without a
              ghost 44 px strip on the right of the page. lg+ only — on
              mobile the order panel column doesn't render at all. */}
          {orderPanelCollapsed && (
            <button
              type="button"
              onClick={toggleOrderPanel}
              title="Expand order panel"
              aria-label="Expand order panel"
              className="absolute right-2 top-2 z-20 hidden size-7 place-items-center rounded-full border border-border bg-card text-muted-foreground shadow-sm transition-colors hover:bg-muted hover:text-foreground lg:grid"
            >
              <ChevronLeft className="size-4" />
            </button>
          )}

          {/* Mobile header — back · symbol · exchange · live price/change.
              Mirrors the reference broker chart screen. lg+ keeps the
              chart-tabs strip + OHLC header below instead. */}
          <div className="flex shrink-0 items-center gap-2 border-b border-border bg-card px-3 py-2 lg:hidden">
            <Link
              href="/marketwatch"
              aria-label="Back"
              className="-ml-1 grid size-8 place-items-center rounded text-muted-foreground hover:bg-muted/40 hover:text-foreground"
            >
              <ArrowLeft className="size-5" />
            </Link>
            <div className="min-w-0">
              <div className="flex items-center gap-1.5">
                <span className="truncate text-sm font-bold">
                  {instrument?.symbol ?? "—"}
                </span>
                {instrument?.exchange && (
                  <span className="rounded bg-muted px-1 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-muted-foreground">
                    {String(instrument.exchange).toUpperCase()}
                  </span>
                )}
              </div>
            </div>
            <div className="ml-auto text-right leading-tight">
              <div
                className={cn(
                  "font-tabular tabular-nums text-sm font-bold",
                  pnlColor(quote?.change_pct ?? 0),
                )}
              >
                {(quote?.ltp || (quote as any)?.last_ltp)
                  ? Number(quote?.ltp || (quote as any)?.last_ltp).toFixed(2)
                  : "—"}
              </div>
              <div
                className={cn(
                  "font-tabular tabular-nums text-[11px]",
                  pnlColor(quote?.change_pct ?? 0),
                )}
              >
                {(quote?.change ?? 0) >= 0 ? "+" : ""}
                {quote?.change?.toFixed?.(2) ?? "0.00"} ({formatPercent(quote?.change_pct ?? 0)})
              </div>
            </div>
          </div>

          {/* Chart-tabs strip — desktop only. The mobile header above
              replaces it on phones (single-symbol context, matches the
              reference design). */}
          <div className="hidden lg:block">
            <ChartTabs
              tabs={tabsWithSelected}
              active={selectedToken}
              onSelect={setSelectedToken}
              onClose={closeTab}
              onAdded={(token) => setSelectedToken(token)}
            />
          </div>

          {/* Symbol header strip — OHLC / change.
              The custom ChartToolbar that previously sat here was duplicating
              the TradingView widget's built-in timeframe / indicator / undo
              controls one row above its own toolbar — removed so the user
              sees one toolbar, the chart's own.
              Hidden on mobile per user request: the TradingView widget
              already shows the symbol + price + change in its own legend
              ("RELIANCE · 5 · MARKET / 1,366.30 −0.10 (−0.01%)") inside
              the chart pane, so duplicating it as a wrapping flex strip
              above the chart was eating ~50 px and pushing the chart down
              on phones. lg+ keeps it for the volume + source-badge
              metadata the desktop user can scan at a glance. */}
          <div className="hidden flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-border px-3 py-2 text-xs lg:flex">
            <div className="flex items-baseline gap-2">
              <span className="text-sm font-semibold text-foreground">
                {instrument?.symbol ?? "Select an instrument"}
              </span>
              <span className="text-muted-foreground">· {tf.label} ·</span>
              <span className="text-muted-foreground">
                {(instrument?.exchange ?? "MARKET").toUpperCase()}
              </span>
            </div>
            <div className="flex items-baseline gap-2 font-tabular text-muted-foreground">
              <span>O</span>
              <span className="text-foreground">{quote?.open ? quote.open.toFixed(2) : "—"}</span>
              <span>H</span>
              <span className="text-foreground">{quote?.high ? quote.high.toFixed(2) : "—"}</span>
              <span>L</span>
              <span className="text-foreground">{quote?.low ? quote.low.toFixed(2) : "—"}</span>
              <span>C</span>
              <span className="text-foreground">
                {(quote?.ltp || (quote as any)?.last_ltp)
                  ? Number(quote?.ltp || (quote as any)?.last_ltp).toFixed(2)
                  : "—"}
              </span>
              {!quote?.ltp && (quote as any)?.last_ltp ? (
                <span className="ml-1 rounded bg-muted px-1 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-muted-foreground">
                  last · market closed
                </span>
              ) : null}
              <span className={cn("ml-1", pnlColor(quote?.change_pct ?? 0))}>
                {quote?.change?.toFixed?.(2) ?? "0.00"} ({formatPercent(quote?.change_pct ?? 0)})
              </span>
            </div>
            <div className="ml-auto flex items-baseline gap-2 text-[11px] text-muted-foreground">
              <span>Volume</span>
              <span className="font-tabular text-foreground">
                {((quote?.volume ?? 0) / 1_000_000).toFixed(2)}M
              </span>
              {quote?.source && (
                <span
                  title={`Quote provider: ${quote.source}`}
                  className={cn(
                    "ml-1 rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider",
                    quote.source === "zerodha"
                      ? "bg-buy/15 text-buy"
                      : quote.source === "infoway"
                        ? "bg-info/15 text-info"
                        : "bg-muted text-muted-foreground"
                  )}
                >
                  {quote.source}
                </span>
              )}
            </div>
          </div>

          {/* Chart fills remaining height. On mobile we use a definite
              `calc(100vh − chrome)` height instead of `flex-1` because the
              flex chain from the page-level container doesn't propagate a
              concrete height down to the TradingView iframe — the widget
              ends up rendering at its initial 0 × 0 bounds and never
              recovers. Explicit pixel height = TV gets a definite size on
              first paint = chart fills the viewport.
              Chrome budget (mobile):
                terminal header     ~3.0 rem
                ChartTabs strip     ~2.5 rem
                bottom SELL/BUY bar ~4.0 rem
                main pb-14 padding   3.5 rem  (= BottomNav h-14, exact)
                border / gap         0.5 rem
              ───────────────────────────────
              total chrome         ~13.5 rem
              pb shrank from 5 rem → 3.5 rem after the gap-between-trade-
              strip-and-nav was removed; calc tracks that change so the
              chart still fills the visible viewport with no overlap.
              lg+ switches back to flex sizing so the desktop layout's
              chart card can share height with the positions strip
              below. */}
          {/* Mobile-only Chart | Option chain toggle. Shown only for
              F&O-eligible Indian underlyings; forex / crypto / option rows
              just get the chart. Its own strip above the chart so it never
              overlaps the TradingView price scale (the old floating vertical
              tab did — that's why it was removed). */}
          <div className="flex shrink-0 items-center gap-6 border-b border-border bg-card px-4 lg:hidden">
            {viewTabs.map((t) => (
              <button
                key={t.key}
                type="button"
                onClick={() => setMobileChartView(t.key)}
                className="relative py-2.5"
              >
                <span
                  className={cn(
                    "text-sm font-semibold transition-colors",
                    mobileChartView === t.key
                      ? "text-foreground"
                      : "text-muted-foreground",
                  )}
                >
                  {t.label}
                </span>
                {mobileChartView === t.key && (
                  <span className="absolute inset-x-0 -bottom-px h-0.5 rounded-full bg-primary" />
                )}
              </button>
            ))}
          </div>

          {/* Chart view — shown only when Charts tab is active on mobile.
              On desktop (lg+) always visible. flex-1 min-h-0 fills remaining
              height after header + tabs. */}
          {(mobileChartView === "chart" || typeof window === "undefined") && (
            <div className="relative min-h-0 flex-1 lg:!flex lg:!flex-col">
              {selectedToken ? (
                (() => {
                  const tvInterval =
                    tf.interval === "minute" ? "1" : tf.interval === "3minute" ? "3" : tf.interval === "5minute" ? "5" : tf.interval === "15minute" ? "15" : tf.interval === "30minute" ? "30" : tf.interval === "60minute" ? "60" : "1D";
                  // EVERY instrument — Indian, forex, metals, energy, crypto,
                  // US stocks — renders on the licensed chart over our own
                  // datafeed. The free TradingView embed that used to serve
                  // the international ones is gone: being a cross-origin
                  // iframe, admin chart lines could not be drawn on it and it
                  // quoted TradingView's liquidity rather than the price on
                  // our own BUY/SELL buttons.
                  return (
                    <TradingViewChart
                      token={selectedToken}
                      symbol={instrument?.symbol}
                      interval={tvInterval}
                      theme={chartTheme}
                      quote={chartLiveQuote}
                    />
                  );
                })()
              ) : (
                <div className="flex h-full items-center justify-center text-muted-foreground">
                  Select an instrument to view chart
                </div>
              )}
            </div>
          )}

          {/* Option chain view — replaces chart on mobile when tab active.
              Rendered as a flex sibling (not absolute overlay) so the header
              and tabs above always stay visible. */}
          {showOptionChain && mobileChartView === "options" && (
            <div className="flex min-h-0 flex-1 flex-col bg-background lg:hidden">
              <MobileOptionChain
                fixedUnderlying={ocUnderlying}
                onSelect={(tok) => setOcSheetToken(tok)}
              />
              <TradeDetailSheet
                token={ocSheetToken}
                open={!!ocSheetToken}
                onClose={() => setOcSheetToken(null)}
                onSwap={(tok) => setOcSheetToken(tok)}
              />
            </div>
          )}

          {/* News view — replaces chart on mobile when tab active. */}
          {mobileChartView === "news" && (
            <div className="min-h-0 flex-1 bg-background lg:hidden">
              <MobileNews
                symbol={instrument?.symbol}
                exchange={instrument?.exchange}
              />
            </div>
          )}

          {/* BUY/SELL bar — only on chart view, mobile only. */}
          {mobileChartView === "chart" && (
            <MobileQuickTradeBar
              instrument={instrument}
              ltp={Number(quote?.ltp ?? 0)}
              bid={bestBid}
              ask={bestAsk}
            />
          )}
        </div>

        {/* Bottom positions strip — restored from the earlier side-drawer
            experiment. Sits under the chart full-width so the trader can
            glance at Positions / Active Trades / Pending / History without
            losing the chart real-estate to a vertical drawer.
            Hidden on mobile — the user explicitly asked for a chart-only
            mobile view; positions / orders are reachable via the bottom-nav
            "Orders" tab which already shows Positions / Holdings / All Orders. */}
        <div className="hidden shrink-0 lg:block">
          <PositionsTabs
            positions={positionsLive ?? []}
            pendingOrders={pendingOrders}
            history={history}
            cancelled={cancelled}
            totalPnL={totalPnL}
          />
          {/* Slim wallet stats strip — Total Balance / Equity / Used Margin /
              Available / Open P&L. Sits at the bottom of the desktop terminal
              as a footer (per user request). Mobile gets the same numbers
              inside the TradeDetailSheet (wallet row there), so this strip is
              desktop-only via `lg:flex` inside the component.

              Pass `openPnL={totalPnL}` so the footer mirrors EXACTLY what
              the Positions tab header + per-row rows show. Otherwise the
              footer polls /positions/pnl-summary (mid-LTP) and shows a
              different number than the rows (close-side) — wide-spread
              instruments like XPTUSD made the gap obvious (₹5k off). */}
          <WalletStrip className="mt-2" openPnL={totalPnL} />
        </div>
      </section>

      {/* ── RIGHT: Order panel ──────────────────────────────────────
          When collapsed the WHOLE column is removed (`lg:hidden`) so
          the chart section claims the full viewport width — no more
          orphan 44 px strip eating chart real-estate on the right.
          The expand button lives on the chart card's top-right edge
          (see the `orderPanelCollapsed && <button>` above the tabs).

          Widths step with breakpoint so the panel matches the chart's
          natural scaling on bigger monitors:
            lg  → 340 px,  xl → 380 px,  2xl → 420 px

          Mobile / md the panel is unconditionally hidden — the
          quick-trade bar at the top of the chart now handles BUY/SELL,
          advanced options (LIMIT, SL-M, SL/TP, product type) stay on
          desktop only by user request. */}
      <div
        className={cn(
          "relative hidden shrink-0 transition-[width] duration-300 ease-out lg:block",
          orderPanelCollapsed
            ? "lg:hidden"
            : "lg:w-[340px] xl:w-[380px] 2xl:w-[420px]",
        )}
      >
        {/* Collapse chevron — TOP of the panel's left edge (was
            vertical-center earlier, which made the arrow hard to find
            in a tall column). User can also collapse via Esc-style
            keyboard later if needed. */}
        <button
          type="button"
          onClick={toggleOrderPanel}
          title="Collapse order panel"
          aria-label="Collapse order panel"
          className="absolute left-0 top-3 z-10 hidden -translate-x-1/2 size-6 place-items-center rounded-full border border-border bg-card text-muted-foreground shadow-sm transition-colors hover:bg-muted hover:text-foreground lg:grid"
        >
          <ChevronRight className="size-3.5" />
        </button>
        <div className="h-full">
          <OrderPanel
            instrument={instrument}
            ltp={liveLtp > 0 ? liveLtp : Number(quote?.ltp ?? 0)}
            bid={bestBid}
            ask={bestAsk}
            open={Number(quote?.open ?? 0)}
            high={Number(quote?.high ?? 0)}
            low={Number(quote?.low ?? 0)}
            close={Number(quote?.prev_close ?? 0)}
            fxRate={Number(quote?.fx_rate ?? 1)}
            lastLtp={Number((quote as any)?.last_ltp ?? 0)}
            stale={Boolean((quote as any)?.stale)}
          />
        </div>
      </div>
    </div>
  );
}
