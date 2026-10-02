"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, Search, TrendingDown, TrendingUp } from "lucide-react";
import { OptionChainAPI } from "@/lib/api";
import { useMarketStream } from "@/lib/useMarketStream";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PageHeader } from "@/components/common/PageHeader";
import { TradeDetailSheet } from "@/components/trading/TradeDetailSheet";
import { MobileOptionChain } from "@/components/trading/MobileOptionChain";
import { cn, formatNumber, pnlColor } from "@/lib/utils";

const UNDERLYINGS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"] as const;

export default function OptionChainPage() {
  const router = useRouter();
  const [underlying, setUnderlying] = useState<string>("NIFTY");
  const [expiry, setExpiry] = useState<string | undefined>(undefined);
  const [strikeFilter, setStrikeFilter] = useState("");
  const [sheetToken, setSheetToken] = useState<string | null>(null);

  const openTrade = useCallback(
    (token: string) => {
      if (!token) return;
      const isMobileUi =
        typeof window !== "undefined" &&
        window.matchMedia("(max-width: 1023px)").matches;
      if (isMobileUi) {
        setSheetToken(token);
      } else {
        router.push(`/terminal?token=${encodeURIComponent(token)}`);
      }
    },
    [router],
  );

  const { data, isFetching } = useQuery({
    queryKey: ["option-chain", underlying, expiry],
    queryFn: () => OptionChainAPI.fetch(underlying, expiry),
    refetchInterval: 1000,
  });

  const expiries: string[] = data?.expiries ?? [];
  const rows: any[] = data?.rows ?? [];
  const atmStrike: number | null = data?.atm_strike ?? null;
  const atmSpot: number | null = data?.atm_spot ?? null;

  const visibleTokens = useMemo<string[]>(() => {
    if (!rows.length) return [];
    const tokens: string[] = [];
    for (const r of rows) {
      if (r.ce?.token) tokens.push(String(r.ce.token));
      if (r.pe?.token) tokens.push(String(r.pe.token));
    }
    return tokens.slice(0, 400);
  }, [rows]);
  const liveQuotes = useMarketStream(visibleTokens);

  const liveRows = useMemo(() => {
    if (liveQuotes.size === 0) return rows;
    return rows.map((r) => {
      const ceLive = r.ce?.token ? liveQuotes.get(String(r.ce.token)) : undefined;
      const peLive = r.pe?.token ? liveQuotes.get(String(r.pe.token)) : undefined;
      if (!ceLive && !peLive) return r;
      return {
        ...r,
        ce: ceLive
          ? {
              ...r.ce,
              bid: Number(ceLive.bid ?? r.ce?.bid ?? 0),
              ask: Number(ceLive.ask ?? r.ce?.ask ?? 0),
              ltp: Number(ceLive.ltp ?? r.ce?.ltp ?? 0),
              change_pct: Number(ceLive.change_pct ?? r.ce?.change_pct ?? 0),
              volume: Number(ceLive.volume ?? r.ce?.volume ?? 0),
            }
          : r.ce,
        pe: peLive
          ? {
              ...r.pe,
              bid: Number(peLive.bid ?? r.pe?.bid ?? 0),
              ask: Number(peLive.ask ?? r.pe?.ask ?? 0),
              ltp: Number(peLive.ltp ?? r.pe?.ltp ?? 0),
              change_pct: Number(peLive.change_pct ?? r.pe?.change_pct ?? 0),
              volume: Number(peLive.volume ?? r.pe?.volume ?? 0),
            }
          : r.pe,
      };
    });
  }, [rows, liveQuotes]);

  const filteredRows = useMemo(() => {
    if (!strikeFilter.trim()) return liveRows;
    if (/^\d+$/.test(strikeFilter)) {
      return liveRows.filter((r) => String(r.strike).includes(strikeFilter));
    }
    return liveRows;
  }, [liveRows, strikeFilter]);

  // Auto-scroll to ATM row on load / underlying change
  const atmRowRef = useRef<HTMLTableRowElement | null>(null);
  useEffect(() => {
    if (!atmRowRef.current) return;
    atmRowRef.current.scrollIntoView({ block: "center", behavior: "auto" });
  }, [underlying, expiry, atmStrike]);

  return (
    <>
      {/* ── Mobile: full-height Groww-style option chain ──────────────
          Replaces the desktop CE|STRIKE|PE table on phones. The table
          has 11 columns that look broken on a 390 px viewport — the
          MobileOptionChain component (single-side strike list with
          ITM/ATM/OTM tags) was purpose-built for this. */}
      <div className="-mx-4 -mt-4 -mb-24 flex h-[calc(100dvh-7rem)] flex-col lg:hidden">
        <MobileOptionChain
          onSelect={(token) => setSheetToken(token)}
        />
        <TradeDetailSheet
          token={sheetToken}
          open={!!sheetToken}
          onClose={() => setSheetToken(null)}
          onSwap={(tok) => setSheetToken(tok)}
        />
      </div>

      {/* ── Desktop: full CE | STRIKE | PE table ─────────────────── */}
      <div className="hidden space-y-4 lg:block">
        <PageHeader
          title="Option chain"
          description="Live CE | STRIKE | PE grid. Click any leg to open the trading terminal."
        />

        <div className="flex flex-wrap items-center gap-2">
          <div className="flex gap-1 rounded-md bg-muted/40 p-1">
            {UNDERLYINGS.map((u) => (
              <button
                key={u}
                onClick={() => {
                  setUnderlying(u);
                  setExpiry(undefined);
                }}
                className={cn(
                  "rounded px-3 py-1.5 text-xs font-medium",
                  underlying === u ? "bg-primary/15 text-primary" : "text-muted-foreground hover:text-foreground"
                )}
              >
                {u}
              </button>
            ))}
          </div>

          <div className="relative">
            <select
              value={expiry ?? ""}
              onChange={(e) => setExpiry(e.target.value || undefined)}
              className="h-9 appearance-none rounded-md border border-border bg-background pl-3 pr-8 text-sm"
            >
              <option value="">Nearest expiry</option>
              {expiries.map((e) => (
                <option key={e} value={e}>
                  {new Date(e).toLocaleDateString("en-IN", {
                    day: "2-digit",
                    month: "short",
                    year: "numeric",
                  })}
                </option>
              ))}
            </select>
            <ChevronDown className="pointer-events-none absolute right-2 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          </div>

          <div className="relative flex-1 min-w-[180px] max-w-xs">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={strikeFilter}
              onChange={(e) => setStrikeFilter(e.target.value)}
              placeholder="Filter by strike"
              className="h-9 pl-9 text-sm"
            />
          </div>

          <div className="ml-auto flex items-center gap-3 text-xs text-muted-foreground">
            {atmSpot && (
              <span>
                Spot ≈ <span className="font-tabular text-foreground">{formatNumber(atmSpot)}</span>
              </span>
            )}
            {atmStrike != null && (
              <span>
                ATM <span className="font-tabular text-primary">{atmStrike.toLocaleString("en-IN")}</span>
              </span>
            )}
          </div>
        </div>

        <div className="overflow-x-auto rounded-lg border border-border bg-card scrollbar-thin">
          <table className="min-w-full text-xs">
            <thead className="sticky top-0 z-10 bg-card">
              <tr className="border-b border-border text-muted-foreground">
                <th colSpan={5} className="px-3 py-2 text-center text-[11px] uppercase tracking-wider text-buy">
                  Calls (CE)
                </th>
                <th className="px-3 py-2 text-center text-[11px] uppercase tracking-wider">Strike</th>
                <th colSpan={5} className="px-3 py-2 text-center text-[11px] uppercase tracking-wider text-sell">
                  Puts (PE)
                </th>
              </tr>
              <tr className="border-b border-border text-[10px] uppercase text-muted-foreground">
                <th className="px-2 py-1 text-right">Volume</th>
                <th className="px-2 py-1 text-right">Bid</th>
                <th className="px-2 py-1 text-right">LTP</th>
                <th className="px-2 py-1 text-right">Ask</th>
                <th className="px-2 py-1 text-right">%Chg</th>
                <th className="px-2 py-1 text-center"></th>
                <th className="px-2 py-1 text-right">%Chg</th>
                <th className="px-2 py-1 text-right">Bid</th>
                <th className="px-2 py-1 text-right">LTP</th>
                <th className="px-2 py-1 text-right">Ask</th>
                <th className="px-2 py-1 text-right">Volume</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {isFetching && filteredRows.length === 0 && (
                <tr>
                  <td colSpan={11} className="px-3 py-12 text-center text-muted-foreground">
                    Loading…
                  </td>
                </tr>
              )}
              {!isFetching && filteredRows.length === 0 && (
                <tr>
                  <td colSpan={11} className="px-3 py-12 text-center text-muted-foreground">
                    No options found for this underlying. Subscribe instruments in admin → Zerodha Connect.
                  </td>
                </tr>
              )}
              {filteredRows.map((r) => {
                const isATM = r.strike === atmStrike;
                const isITMCall = atmStrike != null && r.strike < atmStrike;
                const isITMPut = atmStrike != null && r.strike > atmStrike;
                return (
                  <tr
                    key={r.strike}
                    ref={isATM ? atmRowRef : undefined}
                    className={cn(
                      "transition-colors hover:bg-muted/40",
                      isATM && "bg-primary/10",
                      !isATM && (isITMCall || isITMPut) && "bg-muted/10"
                    )}
                  >
                    <ChainCell leg={r.ce} side="ce" align="right" onOpenTrade={openTrade} />
                    <td
                      className={cn(
                        "cursor-pointer px-2 py-1 text-center font-tabular hover:bg-primary/10",
                        isATM && "font-semibold text-primary",
                      )}
                      onClick={() => openTrade(r.ce?.token || r.pe?.token || "")}
                    >
                      {r.strike.toLocaleString("en-IN")}
                    </td>
                    <ChainCell leg={r.pe} side="pe" align="left" onOpenTrade={openTrade} />
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        <TradeDetailSheet
          token={sheetToken}
          open={!!sheetToken}
          onClose={() => setSheetToken(null)}
          onSwap={(tok) => setSheetToken(tok)}
        />
      </div>
    </>
  );
}

function ChainCell({
  leg,
  side,
  align,
  onOpenTrade,
}: {
  leg: any;
  side: "ce" | "pe";
  align: "left" | "right";
  onOpenTrade: (token: string) => void;
}) {
  if (!leg) {
    return (
      <>
        <td className="px-2 py-1 text-right text-muted-foreground">—</td>
        <td className="px-2 py-1 text-right text-muted-foreground">—</td>
        <td className="px-2 py-1 text-right text-muted-foreground">—</td>
        <td className="px-2 py-1 text-right text-muted-foreground">—</td>
        <td className="px-2 py-1 text-right text-muted-foreground">—</td>
      </>
    );
  }
  const Trend = (leg.change_pct ?? 0) >= 0 ? TrendingUp : TrendingDown;
  // Replace the previous next/link Link with a plain <button> + onClick
  // so the OPENTRADE branch can dispatch differently per viewport
  // (mobile → bottom sheet, desktop → /terminal navigate). Keeping the
  // visual treatment identical to the old Link so the chain looks the
  // same in both modes.
  const onClick = () => onOpenTrade(leg.token);
  const cells = [
    <td key="vol" className="px-2 py-1 text-right text-muted-foreground">
      {leg.volume?.toLocaleString("en-IN") || "—"}
    </td>,
    <td key="bid" className="px-2 py-1 text-right">
      <button
        type="button"
        onClick={onClick}
        className={cn(
          "rounded hover:underline",
          side === "ce" ? "text-buy" : "text-sell",
        )}
      >
        {formatNumber(leg.bid)}
      </button>
    </td>,
    <td key="ltp" className="px-2 py-1 text-right">
      <button
        type="button"
        onClick={onClick}
        className="rounded font-medium hover:underline"
      >
        {formatNumber(leg.ltp)}
      </button>
    </td>,
    <td key="ask" className="px-2 py-1 text-right">
      <button
        type="button"
        onClick={onClick}
        className={cn(
          "rounded hover:underline",
          side === "ce" ? "text-buy" : "text-sell",
        )}
      >
        {formatNumber(leg.ask)}
      </button>
    </td>,
    <td key="chg" className={cn("px-2 py-1 text-right", pnlColor(leg.change_pct))}>
      <span className="inline-flex items-center gap-1">
        <Trend className="size-3" />
        {(leg.change_pct ?? 0).toFixed(2)}%
      </span>
    </td>,
  ];
  return align === "right" ? <>{cells}</> : <>{cells.reverse()}</>;
}
