"use client";

import Link from "next/link";
import Script from "next/script";
import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  Layers,
  LogOut,
  Wallet as WalletIcon,
} from "lucide-react";
import { useAuthStore } from "@/stores/authStore";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { ThemeToggle } from "@/components/common/ThemeToggle";
import { UserWsBridge } from "@/components/common/UserWsBridge";
import { InstrumentsPanel } from "@/components/trading/InstrumentsPanel";
import { OptionChainPicker } from "@/components/trading/OptionChainPicker";
import { TradeDetailSheet } from "@/components/trading/TradeDetailSheet";
import { InstrumentAPI, OptionChainAPI } from "@/lib/api";

type SidePanel = "instruments" | null;

// Remembers whether the trader kept the MARKET (instruments) panel open.
const INSTRUMENTS_OPEN_KEY = "setupfx.terminal.instrumentsOpen";

/**
 * Full-bleed broker layout — top header (back · instruments toggle ·
 * option-chain · theme · wallet · sign-out) and main canvas. Footer
 * status bar (Equity / Free Margin / Balance / Margin / level) and the
 * left tool rail were removed per user request — the header's
 * instruments-toggle absorbed the rail, and the wallet numbers already
 * live on the dashboard wallet page. Body content (chart + order
 * panel) is rendered by `terminal/page.tsx`.
 */
export default function TerminalLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const user = useAuthStore((s) => s.user);
  const hydrated = useAuthStore((s) => s.hydrated);
  const logout = useAuthStore((s) => s.logout);

  const [pickerOpen, setPickerOpen] = useState(false);
  // Instruments ("MARKET") side panel. Starts null to avoid an SSR/mobile
  // flash, then the mount effect decides: first-ever visit opens it on
  // desktop by default (user wants the market list visible on open); after
  // that we honour the saved open/closed preference.
  const [sidePanel, setSidePanel] = useState<SidePanel>(null);
  useEffect(() => {
    if (typeof window === "undefined") return;
    const saved = window.localStorage.getItem(INSTRUMENTS_OPEN_KEY);
    if (saved === "0") {
      setSidePanel(null);
      return;
    }
    if (saved === "1") {
      setSidePanel("instruments");
      return;
    }
    // First visit — default OPEN on desktop; keep closed on phones so the
    // panel doesn't cover the chart (the header toggle is desktop-only).
    const isDesktop = window.matchMedia("(min-width: 768px)").matches;
    setSidePanel(isDesktop ? "instruments" : null);
  }, []);

  // Toggle + remember the choice so it sticks across reloads.
  function toggleInstruments() {
    setSidePanel((cur) => {
      const next: SidePanel = cur === "instruments" ? null : "instruments";
      if (typeof window !== "undefined") {
        window.localStorage.setItem(INSTRUMENTS_OPEN_KEY, next ? "1" : "0");
      }
      return next;
    });
  }
  function closeInstruments() {
    setSidePanel(null);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(INSTRUMENTS_OPEN_KEY, "0");
    }
  }
  // Slide-up trade card token — mobile-only. When the user picks a
  // strike from the Option Chain picker on the terminal page, instead
  // of swapping the chart underneath (the old behaviour) we open the
  // same TradeDetailSheet the marketwatch / option-chain pages use,
  // so the trader can place an order without leaving the chart view
  // for the underlying. Desktop still gets the chart-swap flow
  // because the order panel is already on screen there.
  const [sheetToken, setSheetToken] = useState<string | null>(null);

  // ── Option-chain warm cache ─────────────────────────────────────
  // The Option-chain dialog used to feel slow because its first network
  // round-trip (CSV catalog scan + Kite REST batch quote) costs 1-3 s on
  // a cold cache. Pre-fetching the three default underlyings here — using
  // the SAME query keys the picker uses — means the dialog finds cached
  // rows on open and renders instantly. Background refetch every 6 s keeps
  // the cache warm. When the picker actually opens, its own 2 s refetch
  // interval takes over (React Query uses the lowest interval among active
  // observers).
  const { data: ocCfg } = useQuery({
    queryKey: ["option-chain-config"],
    queryFn: () => OptionChainAPI.config(),
    enabled: !!user,
    staleTime: 60_000,
  });
  const ocUnderlyings: string[] = (ocCfg?.underlyings as any[] | undefined)
    ?.map((u) => u.symbol)
    .filter(Boolean) ?? ["NIFTY", "BANKNIFTY", "SENSEX"];
  // Run a fixed-shape set of prefetch hooks for the three defaults so the
  // hook order stays stable across renders even if admin reconfigures the
  // underlyings list. Extra underlyings beyond three rely on the in-picker
  // fetch (still benefits from the warm catalog cache on the backend).
  const [u0, u1, u2] = [ocUnderlyings[0], ocUnderlyings[1], ocUnderlyings[2]];
  useQuery({
    queryKey: ["option-chain-picker", u0, undefined],
    queryFn: () => OptionChainAPI.fetch(u0!),
    enabled: !!user && !!u0,
    refetchInterval: 6000,
    staleTime: 5000,
    notifyOnChangeProps: [],
  });
  useQuery({
    queryKey: ["option-chain-picker", u1, undefined],
    queryFn: () => OptionChainAPI.fetch(u1!),
    enabled: !!user && !!u1,
    refetchInterval: 6000,
    staleTime: 5000,
    notifyOnChangeProps: [],
  });
  useQuery({
    queryKey: ["option-chain-picker", u2, undefined],
    queryFn: () => OptionChainAPI.fetch(u2!),
    enabled: !!user && !!u2,
    refetchInterval: 6000,
    staleTime: 5000,
    notifyOnChangeProps: [],
  });

  useEffect(() => {
    if (hydrated && !user) router.replace("/login");
  }, [hydrated, user, router]);

  if (!hydrated) {
    return (
      <div className="grid h-screen place-items-center text-sm text-muted-foreground">Loading…</div>
    );
  }
  if (!user) return null;

  return (
    <div className="flex h-screen min-h-0 flex-col bg-background">
      {/* Preload the TradingView library the moment the terminal route mounts
          so the script is in the browser cache (or fully loaded) by the time
          TradingViewChart's effect runs a few render passes later. Without
          this, the chart's own injection waited until component mount, costing
          ~300-600 ms of blank container on cold load. `lazyOnload` keeps the
          download from blocking the page's interactive paint. */}
      <Script
        src="/charting_library/charting_library.standalone.js"
        strategy="afterInteractive"
      />

      <UserWsBridge />

      {/* ── Top header ───────────────────────────────────────────
          Mobile: only the "Option chain" pill is visible — back arrow,
          instruments toggle, theme, wallet, and sign-out are hidden
          since the new BottomNav covers navigation (Home/Market/Trade/
          Orders/Profile) and the chart canvas needs the vertical room.
          Desktop (md+) keeps the full toolbar untouched. */}
      <header className="relative z-20 hidden h-12 shrink-0 items-center gap-2 border-b border-border bg-card px-3 md:flex">
        <Button asChild variant="ghost" size="icon" aria-label="Back to dashboard" className="hidden size-8 md:inline-flex">
          <Link href="/dashboard">
            <ArrowLeft className="size-4" />
          </Link>
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          aria-label="Toggle market instruments panel"
          title="Market instruments"
          className={cn(
            "hidden h-8 px-3 text-xs font-bold uppercase tracking-wide md:inline-flex",
            sidePanel === "instruments"
              ? "border-primary bg-primary/10 text-primary hover:bg-primary/15"
              : "border-primary/60 text-primary hover:bg-primary/10",
          )}
          onClick={toggleInstruments}
        >
          MARKET
        </Button>

        <div className="ml-auto flex items-center gap-1.5">
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-8 gap-1.5"
            onClick={() => setPickerOpen(true)}
            title="Open option chain"
          >
            <Layers className="size-4" />
            <span className="text-xs font-bold">Option chain</span>
          </Button>
          <div className="hidden items-center gap-1.5 md:flex">
            <ThemeToggle />
            <Button asChild variant="ghost" size="icon" aria-label="Wallet" className="size-8">
              <Link href="/wallet">
                <WalletIcon className="size-4" />
              </Link>
            </Button>
            <Button
              variant="ghost"
              size="icon"
              aria-label="Sign out"
              className="size-8"
              onClick={() => logout().then(() => (window.location.href = "/login"))}
            >
              <LogOut className="size-4" />
            </Button>
          </div>
        </div>
      </header>

      {/* ── Body: main canvas only ─────────────────────────────────
          The left ToolRail was removed — its only button (Instruments
          toggle) now lives in the header next to the back arrow, so the
          rail's 40-px column was pure dead weight on phones. */}
      <div className="flex min-h-0 flex-1">
        {sidePanel === "instruments" && (
          <InstrumentsPanel onClose={closeInstruments} />
        )}
        {/* Mobile/md: allow vertical scroll so the chart + order panel +
            positions strip can all be reached. The previous unconditional
            `overflow-hidden` clipped everything past the chart card on
            narrow viewports, which is what made the chart appear tiny
            with a huge empty band below it on phones. lg+ stays fixed
            (no page scroll) — the grid columns there are self-contained.
            `pb-14` reserves EXACTLY the BottomNav's height (h-14 = 56px)
            on mobile so there's no dead band between the chart card's
            SELL/BUY strip and the nav. Earlier `pb-20` left a ~24 px
            gap which the user flagged ("niche me jo khali jagah bach
            rahi usko bhi ramove karo"). The nav adds its own
            safe-area-inset-bottom padding so iPhone home-bar overlap
            is handled there, not here. */}
        <main className="min-h-0 flex-1 overflow-hidden">{children}</main>
      </div>

      {/* App bottom-nav intentionally NOT rendered on the chart route
          (operator: "niche nav bar mat dikh chart me"). Navigation back
          is via the in-page header's back arrow; the bottom of the chart
          card is the compact SELL / BUY trade strip instead. */}

      {/* Footer status bar (Equity / Free / Margin / Balance / Margin
          level / connection) removed per user request — those numbers
          already live on the dashboard wallet page and on the per-row
          positions strip; duplicating them in a permanent bottom strip
          ate ~36 px of chart real-estate on every terminal session. */}

      {/* useSearchParams() must sit inside a Suspense boundary in the
          Next.js 14 App Router during static prerender, or the build
          fails with "useSearchParams() should be wrapped in a suspense
          boundary". Extracting the picker mount keeps the rest of the
          layout buildable while still letting it read `?token=` to
          default the Option-chain underlying. */}
      <Suspense fallback={null}>
        <TerminalOptionChainPicker
          open={pickerOpen}
          onOpenChange={setPickerOpen}
          onPick={(token) => {
            setPickerOpen(false);
            // Mobile + tablet (< lg / 1024 px) get the slide-up trade
            // card so they can place an order on the picked strike
            // without losing the chart view. Desktop swaps the chart
            // because the OrderPanel column is already on screen.
            // User pain point that drove this: "trade nav bar ke chart
            // page se option chain me click karta hu to chart open
            // hota hai, card nahi". The four-iteration loop we were
            // stuck in was because the matchMedia gate inside
            // TradeDetailSheet handled the marketwatch / option-chain
            // routes but the TERMINAL page's own picker was still
            // doing a router.push.
            const isMobileUi =
              typeof window !== "undefined" &&
              window.matchMedia("(max-width: 1023px)").matches;
            if (isMobileUi) {
              setSheetToken(token);
            } else {
              router.push(`/terminal?token=${encodeURIComponent(token)}`);
            }
          }}
        />
      </Suspense>

      {/* Mobile-only trade card — opens when a strike is picked from
          the Option Chain picker on the terminal page. Same component
          marketwatch + option-chain use; `onSwap` lets the in-sheet
          picker change strikes without bouncing the user back to the
          chart route. */}
      <TradeDetailSheet
        token={sheetToken}
        open={!!sheetToken}
        onClose={() => setSheetToken(null)}
        onSwap={(tok) => setSheetToken(tok)}
      />
    </div>
  );
}

/**
 * Suspense-wrapped Option Chain picker mount for the terminal layout.
 *
 * Lives in a child component so the `useSearchParams()` call that reads
 * `?token=` doesn't fail the production prerender — Next.js 14 forces
 * search-params hooks behind a Suspense boundary because they bail out
 * of static generation. While the picker is open this also resolves
 * the active token to its instrument detail (only then; idle terminal
 * sessions stay cheap) and passes `instrument.symbol` as the picker's
 * `initialUnderlying`, so opening the picker from a TCS / GOLD /
 * RELIANCE chart defaults to that root instead of NIFTY.
 */
function TerminalOptionChainPicker({
  open,
  onOpenChange,
  onPick,
}: {
  open: boolean;
  onOpenChange: (next: boolean) => void;
  onPick: (token: string) => void;
}) {
  const searchParams = useSearchParams();
  const activeToken = searchParams?.get("token") ?? null;
  const { data: activeInstrument } = useQuery({
    queryKey: ["instrument", activeToken],
    queryFn: () => InstrumentAPI.detail(activeToken!),
    enabled: !!activeToken && open,
    staleTime: 5 * 60_000,
  });
  return (
    <OptionChainPicker
      open={open}
      onOpenChange={onOpenChange}
      initialUnderlying={(activeInstrument as any)?.symbol ?? null}
      onPick={onPick}
    />
  );
}
