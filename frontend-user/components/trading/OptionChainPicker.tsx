"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Search, X } from "lucide-react";
import { InstrumentAPI, OptionChainAPI } from "@/lib/api";
import { Dialog, DialogContent, DialogPortal, DialogOverlay } from "@/components/ui/dialog";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { cn, formatNumber } from "@/lib/utils";
import { useMarketStream } from "@/lib/useMarketStream";
import { usePriceFlash } from "@/lib/usePriceFlash";

// Cap on how many strike tokens the picker live-subscribes on its OWN
// marketdata socket. useMarketStream opens a dedicated WS per hook instance,
// so this budget is independent of the user's watchlist/positions streams —
// no starving. We stream the strikes NEAREST the ATM (both CE + PE) up to this
// count; strikes beyond the cap still update on the 1 s REST poll. 60 stays
// well under useMarketStream's 70-token per-connection cap.
const MAX_CHAIN_STREAM = 60;

interface UnderlyingCfg {
  label: string;
  symbol: string;
  color: string;
}

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called with the selected leg's token + symbol — parent should add it as a tab. */
  onPick: (token: string, symbol: string) => void;
  /** Optional underlying to pre-select when the picker opens (e.g. user
   *  tapped "Option Chain" on a NIFTY index row → opens with NIFTY
   *  active, expiry + strikes already populated). Falls back to the
   *  first admin-configured underlying when omitted or when the value
   *  doesn't match any configured row. */
  initialUnderlying?: string | null;
}

const FALLBACK_UNDERLYINGS: UnderlyingCfg[] = [
  { label: "Nifty", symbol: "NIFTY", color: "emerald" },
  { label: "BankNifty", symbol: "BANKNIFTY", color: "violet" },
  { label: "Sensex", symbol: "SENSEX", color: "rose" },
];

const COLOR_DOT: Record<string, string> = {
  emerald: "bg-emerald-500",
  violet: "bg-violet-500",
  rose: "bg-rose-500",
  amber: "bg-amber-500",
  sky: "bg-sky-500",
  fuchsia: "bg-fuchsia-500",
};

export function OptionChainPicker({ open, onOpenChange, onPick, initialUnderlying }: Props) {
  // Fetch admin-configured underlyings + visible expiry / strike count
  const { data: cfg } = useQuery({
    queryKey: ["option-chain-config"],
    queryFn: () => OptionChainAPI.config(),
    staleTime: 60_000,
    enabled: open,
  });

  const configuredUnderlyings: UnderlyingCfg[] = cfg?.underlyings ?? FALLBACK_UNDERLYINGS;

  // Extract the alphabetic root from `initialUnderlying`:
  //   "GOLD26JUNFUT"        → "GOLD"
  //   "NIFTY24DEC25000CE"   → "NIFTY"
  //   "BANKNIFTY"           → "BANKNIFTY"
  //   "COPPER26MAY1505PE"   → "COPPER"
  // Captures the leading uppercase run so any MCX commodity / Indian
  // F&O contract reduces to its underlying symbol. The backend's
  // `get_option_chain_fast(root)` accepts arbitrary underlyings, so any
  // root with options in the Kite catalog will render — we don't need
  // admin to have pre-configured it.
  const requestedRoot = (() => {
    if (!initialUnderlying) return null;
    const m = initialUnderlying.toUpperCase().match(/^([A-Z]+)/);
    return m ? m[1] : null;
  })();

  // Inject the requested root into the chip strip when it isn't already
  // configured. Without this, tapping "Option Chain" from a GOLD future
  // would open the picker on NIFTY because GOLD isn't in the admin's
  // configured list. Injection keeps the existing chips visible too so
  // the user can switch to NIFTY / BANKNIFTY / SENSEX without re-opening.
  const underlyings: UnderlyingCfg[] = (() => {
    if (!requestedRoot) return configuredUnderlyings;
    const exists = configuredUnderlyings.some(
      (u) => u.symbol.toUpperCase() === requestedRoot,
    );
    if (exists) return configuredUnderlyings;
    // Synthesise a chip for the requested root. Color cycles through
    // unused options so multiple injected roots stay visually distinct.
    const usedColors = new Set(configuredUnderlyings.map((u) => u.color));
    const palette = ["amber", "sky", "fuchsia", "emerald", "violet", "rose"];
    const color = palette.find((c) => !usedColors.has(c)) ?? "amber";
    return [
      { label: requestedRoot, symbol: requestedRoot, color },
      ...configuredUnderlyings,
    ];
  })();

  // Default to the first configured underlying (NIFTY) instead of "All" —
  // makes the chain immediately useful on open without needing a click.
  const [activeUnd, setActiveUnd] = useState<string | "ALL">(
    () => underlyings[0]?.symbol ?? "NIFTY"
  );
  const [activeExpiry, setActiveExpiry] = useState<string | undefined>(undefined);
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");

  // Debounce search input — 200ms delay for snappy feel without hammering API
  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search), 200);
    return () => clearTimeout(t);
  }, [search]);

  // Re-pin the default underlying every time the picker opens. When the
  // parent passed `initialUnderlying` we prefer the extracted root
  // (synthesised into the chip strip above if admin hadn't configured
  // it), then fall back to the first configured chip — typically NIFTY.
  useEffect(() => {
    if (!open) return;
    setSearch("");
    setDebouncedSearch("");
    setActiveExpiry(undefined);
    if (requestedRoot) {
      const match = underlyings.find(
        (u) => u.symbol.toUpperCase() === requestedRoot,
      );
      if (match) {
        setActiveUnd(match.symbol);
        return;
      }
    }
    setActiveUnd(underlyings[0]?.symbol ?? "NIFTY");
  }, [open, underlyings, requestedRoot]);

  // For "ALL" we just hit the first underlying — option chain is keyed by one
  // underlying. Picking "All" really means "first underlying with the global
  // expiry chips". We still surface the chip so the UI matches the screenshot.
  const focusedUnd = activeUnd === "ALL" ? underlyings[0]?.symbol ?? "NIFTY" : activeUnd;
  const focusedUndLabel =
    underlyings.find((u) => u.symbol === focusedUnd)?.label ?? focusedUnd;

  // Live option-chain data — refetches every 2s for tick-by-tick price moves.
  // `placeholderData: keep previous` so chip-switching never blanks the table
  // — the previously rendered chain stays on-screen until the new one lands.
  // `staleTime: 5000` matches the TerminalLayout's 6 s background prefetch
  // so the dialog hits the cache (instant paint) on every open.
  const { data: chain, isFetching } = useQuery({
    queryKey: ["option-chain-picker", focusedUnd, activeExpiry],
    queryFn: () => OptionChainAPI.fetch(focusedUnd, activeExpiry),
    enabled: open && !!focusedUnd && !search.trim(),
    refetchInterval: 1000,
    staleTime: 5000,
    placeholderData: (prev) => prev,
  });

  const expiries: string[] = chain?.expiries ?? [];
  const rows: any[] = chain?.rows ?? [];
  const atmStrike: number | null = chain?.atm_strike ?? null;
  // `atm_spot` is the put-call-parity-derived spot price. Falls back to the
  // ATM strike if no leg has a live LTP (then ATM strike ≈ spot).
  const atmSpot: number | null = (chain?.atm_spot ?? null) ?? atmStrike;
  const selectedExpiry = chain?.selected_expiry ?? activeExpiry ?? expiries[0];
  const dataSource: "live" | "rest" | "none" | undefined = chain?.data_source;
  const dataSourceError: string | null | undefined = chain?.data_source_error;

  // Free-text search across instruments — debounced for speed
  const { data: searchHits, isFetching: isSearching } = useQuery({
    queryKey: ["option-picker-search", debouncedSearch],
    queryFn: () => InstrumentAPI.search(debouncedSearch, undefined, undefined, 30),
    enabled: open && debouncedSearch.trim().length > 1,
    staleTime: 30_000,
  });

  const filteredRows = useMemo(() => {
    if (!search.trim()) return rows;
    const q = search.trim().toLowerCase();
    return rows.filter((r) => {
      const ce = r.ce?.symbol?.toLowerCase?.() ?? "";
      const pe = r.pe?.symbol?.toLowerCase?.() ?? "";
      const strike = String(r.strike);
      return ce.includes(q) || pe.includes(q) || strike.includes(q);
    });
  }, [rows, search]);

  // LIVE overlay — subscribe the strikes nearest the ATM (both CE + PE) on the
  // marketdata WS so per-strike LTP pushes in realtime (~100-250 ms) instead of
  // only on the 1 s REST poll. Previously the picker streamed NOTHING and rode
  // the 1 s poll alone, which felt 1-2 s behind Zerodha/Upstox. This runs on
  // the picker's OWN WS connection (useMarketStream opens one per instance), so
  // the ~60-token budget is independent of the user's watchlist/positions
  // stream — no "Subscription limit reached" starving. The 1 s poll still
  // drives spot/ATM recentring and fills any strike beyond the cap; `liveRows`
  // below reads `live.get(token)` and falls back to the poll price otherwise.
  const streamTokens = useMemo<string[]>(() => {
    if (rows.length === 0) return [];
    // Order rows by distance from the ATM so the visible centre of the chain
    // streams first when the strike count exceeds the token budget.
    const ordered =
      atmStrike == null
        ? rows
        : [...rows].sort(
            (a, b) =>
              Math.abs(Number(a.strike) - atmStrike) -
              Math.abs(Number(b.strike) - atmStrike),
          );
    const toks: string[] = [];
    for (const r of ordered) {
      const ceTok = r.ce?.token;
      const peTok = r.pe?.token;
      if (ceTok) toks.push(String(ceTok));
      if (peTok) toks.push(String(peTok));
      if (toks.length >= MAX_CHAIN_STREAM) break;
    }
    return toks.slice(0, MAX_CHAIN_STREAM);
  }, [rows, atmStrike]);
  const live = useMarketStream(streamTokens);

  const liveRows = useMemo(() => {
    if (live.size === 0) return filteredRows;
    const merge = (leg: any) => {
      if (!leg?.token) return leg;
      const l = live.get(String(leg.token));
      if (!l) return leg;
      return {
        ...leg,
        ltp: l.ltp ?? leg.ltp,
        bid: l.bid ?? leg.bid,
        ask: l.ask ?? leg.ask,
        change_pct: l.change_pct ?? leg.change_pct,
        volume: l.volume ?? leg.volume,
      };
    };
    return filteredRows.map((r) => ({ ...r, ce: merge(r.ce), pe: merge(r.pe) }));
  }, [filteredRows, live]);

  // Auto-scroll to ATM when it changes
  const atmRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const t = setTimeout(() => {
      atmRef.current?.scrollIntoView({ block: "center", behavior: "auto" });
    }, 50);
    return () => clearTimeout(t);
  }, [atmStrike, open, focusedUnd, selectedExpiry]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogPortal>
        <DialogOverlay />
        <DialogPrimitive.Content
          className={cn(
            // Mobile: true full-screen page. Desktop (md+): centered modal.
            "fixed inset-0 z-50 flex h-[100dvh] w-full flex-col overflow-hidden border-border bg-card shadow-2xl",
            "md:inset-auto md:left-1/2 md:top-1/2 md:h-auto md:max-h-[85vh] md:w-[min(1100px,95vw)] md:-translate-x-1/2 md:-translate-y-1/2 md:rounded-lg md:border",
            "data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95"
          )}
        >
          {/* Search bar — hidden on mobile. User explicitly asked to
              drop the search row from the picker on phones ("option
              chain se search vale remove kar do mobile view me"); the
              underlying chip + strike grid already covers their need
              for narrowing to NIFTY / BANKNIFTY / a specific
              underlying. Desktop (md+) keeps the free-text search
              because typing on a keyboard is faster than tapping the
              underlying chips. The header itself stays so the LIVE /
              CLOSE chip and the X close button remain reachable. */}
          <div className="flex items-center gap-2 border-b border-border px-4 py-3">
            <div className="hidden flex-1 items-center gap-2 md:flex">
              <Search className="size-4 text-muted-foreground" />
              <input
                autoFocus
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search instruments…"
                className="flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
              />
            </div>
            {/* On mobile the header collapses to "Option chain" so the
                LIVE chip + close button still have somewhere to sit. */}
            <span className="text-sm font-semibold md:hidden">Option chain</span>
            <span className="flex-1 md:hidden" />
            {dataSource && (
              <span
                title={
                  dataSource === "live"
                    ? "Streaming live ticks from Zerodha KiteTicker"
                    : dataSource === "rest"
                      ? "Last close from Zerodha REST /quote (market closed or pre-open)"
                      : "No Zerodha data available — subscribe instruments in admin → Zerodha Connect"
                }
                className={cn(
                  "rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider",
                  dataSource === "live"
                    ? "bg-buy/15 text-buy"
                    : dataSource === "rest"
                      ? "bg-atm/20 text-atm"
                      : "bg-destructive/15 text-destructive"
                )}
              >
                {dataSource === "live" ? "LIVE" : dataSource === "rest" ? "LAST CLOSE" : "NO DATA"}
              </span>
            )}
            <DialogPrimitive.Close className="rounded-md border border-border p-1 text-muted-foreground hover:bg-accent hover:text-foreground" aria-label="Close">
              <X className="size-4" />
            </DialogPrimitive.Close>
          </div>

          {/* If user is searching globally, render search-hit list and skip the chain */}
          {search.trim() ? (
            <div className="flex-1 overflow-y-auto scrollbar-thin">
              {isSearching ? (
                <div className="grid h-32 place-items-center text-xs text-muted-foreground">Searching…</div>
              ) : (searchHits ?? []).length === 0 ? (
                <div className="grid h-32 place-items-center text-xs text-muted-foreground">
                  {debouncedSearch.trim().length < 2 ? "Type at least 2 characters…" : "No matches"}
                </div>
              ) : (
                <div className="divide-y divide-border">
                  {searchHits!.map((r: any) => (
                    <button
                      key={r.token}
                      type="button"
                      onClick={() => {
                        // Inside the Option Chain dialog the search hit
                        // should LOAD that symbol's option chain. The
                        // backend's `get_option_chain_fast` expects the
                        // UNDERLYING ROOT — e.g. "TCS", not the contract
                        // "TCS26MAYFUT" — so strip the leading
                        // alphabetic prefix before activating. Without
                        // this, picking a future from search activated
                        // the full symbol, no chain rendered, and the
                        // ad-hoc chip below showed the unstripped
                        // contract string (e.g. "TCS26MAYFUT").
                        const sym = String(r.symbol ?? "").toUpperCase();
                        const rootMatch = sym.match(/^([A-Z]+)/);
                        setActiveUnd(rootMatch ? rootMatch[1] : sym);
                        setActiveExpiry(undefined);
                        setSearch("");
                        setDebouncedSearch("");
                      }}
                      className="flex w-full items-center justify-between px-4 py-2.5 text-left text-sm hover:bg-muted/30"
                    >
                      <div>
                        <div className="font-medium">{r.symbol}</div>
                        <div className="text-[10px] text-muted-foreground">
                          {r.exchange} · {r.segment ?? r.instrument_type ?? ""}
                        </div>
                      </div>
                      <span className="text-xs text-muted-foreground">{r.name ?? ""}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <>
              {/* Underlying chips + spot price strip */}
              <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-3">
                <Chip
                  label="All"
                  active={activeUnd === "ALL"}
                  onClick={() => setActiveUnd("ALL")}
                />
                {underlyings.map((u) => (
                  <Chip
                    key={u.symbol}
                    label={u.label}
                    color={u.color}
                    active={activeUnd === u.symbol}
                    onClick={() => setActiveUnd(u.symbol)}
                  />
                ))}
                {/* Ad-hoc chip — shown when the user searched for an
                    instrument not in the admin-configured underlying
                    list (e.g. SBIN). Lets them see what's loaded and
                    flip back to a stock chip without re-typing. */}
                {activeUnd !== "ALL" &&
                  !underlyings.some((u) => u.symbol === activeUnd) && (
                    <Chip
                      label={activeUnd}
                      color="sky"
                      active
                      onClick={() => {}}
                    />
                  )}

                {/* Live underlying spot — derived from put-call parity on the
                    front-month chain (or ATM strike fallback). Sits beside
                    the underlying chips so the user always sees what NIFTY /
                    BANKNIFTY is trading at while picking strikes. */}
                {atmSpot != null && (
                  <div className="ml-auto flex items-baseline gap-1.5 rounded-md border border-border bg-muted/30 px-2.5 py-1">
                    <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                      {focusedUndLabel}
                    </span>
                    <span className="font-tabular text-sm font-semibold text-foreground">
                      ₹{Number(atmSpot).toLocaleString("en-IN", { maximumFractionDigits: 2 })}
                    </span>
                  </div>
                )}
              </div>

              {/* Expiry chips */}
              <div className="flex items-center gap-2 overflow-x-auto border-b border-border px-4 py-2 scrollbar-thin">
                {expiries.length === 0 ? (
                  <span className="text-xs text-muted-foreground">
                    {/* "Loading…" only before the first response — the chain
                        polls, so `isFetching` alone would never let a scrip with
                        no options settle to the empty message. */}
                    {chain == null && isFetching ? "Loading expiries…" : "No expiries available"}
                  </span>
                ) : (
                  expiries.map((iso) => {
                    const isActive = (selectedExpiry ?? expiries[0]) === iso;
                    return (
                      <button
                        key={iso}
                        type="button"
                        onClick={() => setActiveExpiry(iso)}
                        className={cn(
                          "shrink-0 rounded-full border px-3 py-1 text-xs transition-colors",
                          isActive
                            ? "border-foreground/40 bg-foreground/5 text-foreground"
                            : "border-border text-muted-foreground hover:bg-muted/30 hover:text-foreground"
                        )}
                      >
                        {formatExpiry(iso)}
                      </button>
                    );
                  })
                )}
              </div>

              {/* CE | STRIKE | PE header.
                  Mobile (<md): single LTP column per leg — 5-col table
                  was overlapping characters in a 340 px dialog.
                  Desktop (md+): full Vol / Chg% / Bid / Ask / LTP view. */}
              <div className="grid grid-cols-[1fr_72px_1fr] items-center border-b border-border bg-muted/10 px-2 py-2 text-[10px] font-semibold uppercase tracking-wider md:grid-cols-[1fr_100px_1fr]">
                {/* Mobile: just "LTP / CHG%" on each side */}
                <div className="text-right text-buy md:hidden">LTP</div>
                <div className="hidden grid-cols-5 gap-1 text-buy md:grid">
                  <span>Vol</span>
                  <span>Chg%</span>
                  <span>Bid</span>
                  <span>Ask</span>
                  <span className="text-right">LTP</span>
                </div>
                <div className="text-center text-muted-foreground">STRIKE</div>
                <div className="text-left text-sell md:hidden">LTP</div>
                <div className="hidden grid-cols-5 gap-1 text-sell md:grid">
                  <span>LTP</span>
                  <span>Bid</span>
                  <span>Ask</span>
                  <span>Chg%</span>
                  <span className="text-right">Vol</span>
                </div>
              </div>

              {/* Inline error banner — shown when the Kite REST batch fails. */}
              {dataSourceError && (
                <div className="mx-4 mt-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-[11px] text-destructive">
                  <span className="font-semibold">Zerodha returned no prices: </span>
                  <span className="text-muted-foreground">{dataSourceError}</span>
                </div>
              )}

              {/* Rows */}
              <div className="flex-1 overflow-y-auto scrollbar-thin">
                {liveRows.length === 0 ? (
                  <div className="grid h-32 place-items-center text-xs text-muted-foreground">
                    {chain == null && isFetching ? "Loading…" : "No options. Subscribe instruments via admin → Zerodha Connect."}
                  </div>
                ) : (
                  <div>
                    {liveRows.map((r) => {
                      const isATM = r.strike === atmStrike;
                      return (
                        <div
                          key={r.strike}
                          ref={isATM ? atmRef : undefined}
                          className={cn(
                            "relative grid grid-cols-[1fr_72px_1fr] items-center border-b border-border/50 px-2 py-1.5 text-sm transition-colors md:grid-cols-[1fr_100px_1fr]",
                            isATM && "bg-atm/10"
                          )}
                        >
                          {/* CE leg */}
                          <Leg
                            leg={r.ce}
                            expiryIso={selectedExpiry ?? expiries[0]}
                            onPick={onPick}
                            side="ce"
                          />

                          {/* Strike center */}
                          <div className="relative flex flex-col items-center">
                            <span
                              className={cn(
                                "font-tabular font-semibold",
                                isATM ? "text-atm" : "text-foreground"
                              )}
                            >
                              {Number(r.strike).toLocaleString("en-IN")}
                            </span>
                            {isATM && (
                              <span className="mt-0.5 rounded bg-atm px-1.5 py-0.5 text-[10px] font-semibold text-atm-foreground">
                                ATM {Number(r.strike).toLocaleString("en-IN")}
                              </span>
                            )}
                          </div>

                          {/* PE leg */}
                          <Leg
                            leg={r.pe}
                            expiryIso={selectedExpiry ?? expiries[0]}
                            onPick={onPick}
                            side="pe"
                          />
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            </>
          )}
        </DialogPrimitive.Content>
      </DialogPortal>
    </Dialog>
  );
}

function Chip({
  label,
  active,
  color,
  onClick,
}: {
  label: string;
  active: boolean;
  color?: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs transition-colors",
        active
          ? "border-foreground/50 bg-foreground/5 text-foreground"
          : "border-border text-muted-foreground hover:bg-muted/30 hover:text-foreground"
      )}
    >
      {color && (
        <span className={cn("size-1.5 rounded-full", COLOR_DOT[color] ?? "bg-foreground")} />
      )}
      {label}
    </button>
  );
}

function Leg({
  leg,
  expiryIso,
  onPick,
  side,
}: {
  leg: any;
  expiryIso?: string;
  onPick: (token: string, symbol: string) => void;
  side: "ce" | "pe";
}) {
  // Brief green/red flash whenever this leg's LTP ticks — gives the chain
  // the live "tick tick" pulse. Called before the early return so hook
  // order stays stable for strikes missing a leg on one side.
  const flashDir = usePriceFlash(leg?.ltp != null ? Number(leg.ltp) : null, 300);
  const flashBg =
    flashDir === "up" ? "bg-buy/20" : flashDir === "down" ? "bg-sell/20" : "";
  if (!leg) {
    return (
      <div className="font-tabular text-[11px] text-muted-foreground">
        {/* Mobile placeholder — single dash */}
        <div className={cn("px-2 py-1", side === "ce" ? "text-right" : "text-left", "md:hidden")}>
          —
        </div>
        {/* Desktop placeholder — 5-col empty grid */}
        <div className="hidden grid-cols-5 gap-1 md:grid">
          <span>—</span>
          <span>—</span>
          <span>—</span>
          <span>—</span>
          <span>—</span>
        </div>
      </div>
    );
  }

  const ltp = leg.ltp;
  const hasLtp = ltp !== null && ltp !== undefined;
  const bid = leg.bid;
  const ask = leg.ask;
  const changePct = leg.change_pct;
  const volume = leg.volume;
  const hasChange = changePct !== null && changePct !== undefined;
  const isPositive = hasChange && changePct >= 0;

  const fmtPrice = (v: any) => (v != null ? formatNumber(v) : "—");
  const fmtVol = (v: any) => {
    if (v == null) return "—";
    // Full number with Indian grouping (52,34,567) — user preference is
    // explicit digits over K/M abbreviation across the whole product.
    const n = Number(v);
    if (!Number.isFinite(n)) return "—";
    return n.toLocaleString("en-IN");
  };

  const ltpColor = hasLtp
    ? side === "ce" ? "text-buy" : "text-sell"
    : "text-muted-foreground";

  const changeColor = hasChange
    ? isPositive ? "text-buy" : "text-sell"
    : "text-muted-foreground";

  // Change bar width (max 100%)
  const barWidth = hasChange ? Math.min(Math.abs(changePct) * 3, 100) : 0;

  // Compact LTP + Chg% block for mobile (under md). 5-col detail view
  // for tablet/desktop. Keeping a single component so the row container
  // stays the same on both sizes — only the inner layout swaps.
  const mobileCell = (
    <div
      className={cn(
        "px-2 py-1 font-tabular",
        side === "ce" ? "text-right" : "text-left",
      )}
    >
      <div className={cn("inline-block rounded px-1 text-sm font-bold transition-colors", ltpColor, flashBg)}>
        {hasLtp ? formatNumber(ltp) : "—"}
      </div>
      {hasChange && (
        <div className={cn("text-[10px]", changeColor)}>
          {isPositive ? "+" : ""}
          {changePct.toFixed(1)}%
        </div>
      )}
    </div>
  );

  if (side === "ce") {
    return (
      <button
        type="button"
        onClick={() => onPick(leg.token, leg.symbol)}
        className="group relative rounded-md transition-colors hover:bg-muted/40"
      >
        {/* Movement bar underneath */}
        {barWidth > 0 && (
          <div
            className={cn(
              "absolute right-0 top-0 h-full rounded-r-md opacity-10",
              isPositive ? "bg-buy" : "bg-sell"
            )}
            style={{ width: `${barWidth}%` }}
          />
        )}
        <div className="relative md:hidden">{mobileCell}</div>
        <div className="relative hidden grid-cols-5 gap-1 px-1 py-0.5 text-[11px] font-tabular md:grid">
          <span className="text-muted-foreground">{fmtVol(volume)}</span>
          <span className={changeColor}>
            {hasChange ? `${isPositive ? "+" : ""}${changePct.toFixed(1)}%` : "—"}
          </span>
          <span className="text-muted-foreground">{fmtPrice(bid)}</span>
          <span className="text-muted-foreground">{fmtPrice(ask)}</span>
          <span className={cn("rounded text-right font-semibold transition-colors", ltpColor, flashBg)}>
            {hasLtp ? formatNumber(ltp) : "—"}
          </span>
        </div>
      </button>
    );
  }

  // PE side — mirrored column order
  return (
    <button
      type="button"
      onClick={() => onPick(leg.token, leg.symbol)}
      className="group relative rounded-md transition-colors hover:bg-muted/40"
    >
      {barWidth > 0 && (
        <div
          className={cn(
            "absolute left-0 top-0 h-full rounded-l-md opacity-10",
            isPositive ? "bg-buy" : "bg-sell"
          )}
          style={{ width: `${barWidth}%` }}
        />
      )}
      <div className="relative md:hidden">{mobileCell}</div>
      <div className="relative hidden grid-cols-5 gap-1 px-1 py-0.5 text-[11px] font-tabular md:grid">
        <span className={cn("rounded font-semibold transition-colors", ltpColor, flashBg)}>
          {hasLtp ? formatNumber(ltp) : "—"}
        </span>
        <span className="text-muted-foreground">{fmtPrice(bid)}</span>
        <span className="text-muted-foreground">{fmtPrice(ask)}</span>
        <span className={changeColor}>
          {hasChange ? `${isPositive ? "+" : ""}${changePct.toFixed(1)}%` : "—"}
        </span>
        <span className="text-right text-muted-foreground">{fmtVol(volume)}</span>
      </div>
    </button>
  );
}

function formatExpiry(iso: string): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d
      .toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "2-digit" })
      .toUpperCase()
      .replace(/,/g, "");
  } catch {
    return iso;
  }
}
