"use client";

import { memo, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronUp } from "lucide-react";
import { OptionChainAPI } from "@/lib/api";
import { useMarketStream } from "@/lib/useMarketStream";
import { usePriceFlash } from "@/lib/usePriceFlash";
import { cn, formatPrice } from "@/lib/utils";

// Cap on how many strike tokens we live-subscribe on the marketdata socket.
// Only the ACTIVE side (Call OR Put) is on screen at a time, and this stays
// well under useMarketStream's 70-token per-connection cap so the chain never
// starves the user's watchlist/positions stream. The chain is ATM-windowed by
// the backend, so these are the strikes actually visible.
const MAX_CHAIN_STREAM = 60;

// ─────────────────────────────────────────────────────────────────────
// Mobile Options tab for the markets page. A Groww-style single-side
// (Call OR Put) strike list with ITM / ATM / OTM tags and a live red/green
// blink on every tick. Deliberately separate from the desktop
// /option-chain table (CE | STRIKE | PE) — that page is left untouched per
// the operator: "dono alag hai, ye alag rahega". Reuses the SAME data API
// (OptionChainAPI.fetch) + the SAME live WS (useMarketStream) as the rest
// of the app, so no trading / data logic is duplicated — pure presentation.
// ─────────────────────────────────────────────────────────────────────

type Side = "CALL" | "PUT";

interface Props {
  onSelect: (token: string) => void;
  // When set, the chain is locked to this underlying (e.g. the terminal's
  // currently-charted stock) — the index "Filter by" pill is hidden and
  // only the expiry selector stays. Omit it for the markets-page picker.
  fixedUnderlying?: string;
}

// Index list shown in the "Filter by" sheet. Labels match the reference;
// symbols are what the backend option-chain endpoint expects. Underlyings
// the deployment hasn't subscribed simply render an empty chain (graceful).
const UNDERLYINGS: { label: string; symbol: string }[] = [
  { label: "Nifty 50", symbol: "NIFTY" },
  { label: "Nifty Bank", symbol: "BANKNIFTY" },
  { label: "Sensex", symbol: "SENSEX" },
  { label: "Nifty Fin Service", symbol: "FINNIFTY" },
  { label: "Nifty Mid Select", symbol: "MIDCPNIFTY" },
  { label: "Bankex", symbol: "BANKEX" },
];

function fmtExpiry(iso?: string | null): string {
  if (!iso) return "Expiry";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

type Moneyness = "ITM" | "ATM" | "OTM" | null;

function moneynessFor(strike: number, atm: number | null, side: Side): Moneyness {
  if (atm == null) return null;
  if (strike === atm) return "ATM";
  if (side === "CALL") return strike < atm ? "ITM" : "OTM";
  return strike > atm ? "ITM" : "OTM";
}

export function MobileOptionChain({ onSelect, fixedUnderlying }: Props) {
  const [underlying, setUnderlying] = useState<string>(fixedUnderlying ?? "NIFTY");
  const [expiry, setExpiry] = useState<string | undefined>(undefined);
  const [side, setSide] = useState<Side>("CALL");
  const [sheet, setSheet] = useState<"index" | "expiry" | null>(null);

  const { data, isFetching } = useQuery({
    queryKey: ["option-chain", underlying, expiry],
    queryFn: () => OptionChainAPI.fetch(underlying, expiry),
    // 1s poll keeps spot + ATM recentring snappy; WS overlay below pushes
    // per-strike LTP in realtime so the numbers feel live between polls.
    refetchInterval: 1000,
    placeholderData: (prev) => prev,
  });

  const expiries: string[] = data?.expiries ?? [];
  const activeExpiry: string | undefined =
    expiry ?? data?.expiry ?? expiries[0];
  const rows: any[] = data?.rows ?? [];
  const atm: number | null = data?.atm_strike ?? null;
  const spot: number | null = data?.atm_spot ?? data?.spot ?? null;
  const marketOpen = data?.market_open !== false;

  // LIVE overlay: subscribe the ACTIVE side's visible strike tokens on the
  // marketdata WS so per-strike LTP updates in realtime (~300-500 ms) instead
  // of only on the 1 s REST poll — the chain now ticks as live as the terminal.
  // Only the shown side, capped at MAX_CHAIN_STREAM, so we never blow the
  // per-connection cap. The 1 s poll still drives spot/ATM recentring + fills
  // any strike beyond the cap; `StrikeRow` reads `live.get(token)` (below) and
  // falls back to the poll price when a token isn't streamed.
  const streamTokens = useMemo<string[]>(() => {
    const toks: string[] = [];
    for (const r of rows) {
      const leg = side === "CALL" ? r.ce : r.pe;
      const tok = leg?.token;
      if (tok) toks.push(String(tok));
      if (toks.length >= MAX_CHAIN_STREAM) break;
    }
    return toks;
  }, [rows, side]);
  const live = useMarketStream(streamTokens);

  // Keep the locked underlying in sync when the parent swaps the charted
  // instrument (terminal: user opens a different stock).
  useEffect(() => {
    if (fixedUnderlying && fixedUnderlying !== underlying) {
      setUnderlying(fixedUnderlying);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fixedUnderlying]);

  // Reset expiry when the underlying changes so the new chain lands on its
  // own nearest expiry rather than a stale date from the previous index.
  const prevU = useRef(underlying);
  useEffect(() => {
    if (prevU.current !== underlying) {
      setExpiry(undefined);
      prevU.current = underlying;
    }
  }, [underlying]);

  const activeLabel =
    UNDERLYINGS.find((u) => u.symbol === underlying)?.label ?? underlying;

  // Auto-scroll the ATM row into the centre on load / underlying / side flip.
  const atmRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    atmRef.current?.scrollIntoView({ block: "center", behavior: "auto" });
  }, [underlying, expiry, atm, side]);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Call | Put toggle */}
      <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-2">
        <div className="flex gap-1 rounded-md bg-muted/40 p-1">
          <SideBtn label="Call" active={side === "CALL"} onClick={() => setSide("CALL")} />
          <SideBtn label="Put" active={side === "PUT"} onClick={() => setSide("PUT")} />
        </div>
        {spot != null && (
          <div className="ml-auto text-right text-[11px] leading-tight text-muted-foreground">
            <div className="text-[9px] uppercase tracking-wider">Spot</div>
            <div className="font-tabular tabular-nums text-sm font-bold text-foreground">
              {formatPrice(spot)}
            </div>
          </div>
        )}
      </div>

      {/* Strike list */}
      <div className="min-h-0 flex-1 overflow-y-auto scrollbar-thin">
        {rows.length === 0 ? (
          <div className="grid h-24 place-items-center px-4 text-center text-xs text-muted-foreground">
            {/* "Loading…" ONLY on the very first fetch (no response yet). The
                chain polls every 1 s, so `isFetching` flips true on every poll
                — gating the message on it pinned "Loading…" forever for a scrip
                that genuinely has no options (e.g. a plain equity like
                SENSEXADD): rows stay [] and the poll never lets it fall through
                to the empty state. Once ANY response has arrived (`data` set,
                even with rows=[]), show the real "no options" message. */}
            {data == null && isFetching
              ? "Loading…"
              : `No options available for ${underlying}`}
          </div>
        ) : (
          rows.map((r) => {
            const isAtm = r.strike === atm;
            const leg = side === "CALL" ? r.ce : r.pe;
            // Pass ONLY this strike's quote (stable object ref between ticks)
            // instead of the whole `live` Map. Combined with React.memo on
            // StrikeRow, a tick now re-renders just the strike that changed
            // — not all 70 rows. Fixes the Safari jank where every WS flush
            // (~10/s) reconciled the entire list.
            const liveQ = leg?.token ? live.get(String(leg.token)) : undefined;
            return (
              <StrikeRow
                key={r.strike}
                rowRef={isAtm ? atmRef : undefined}
                underlying={underlying}
                strike={r.strike}
                leg={leg}
                side={side}
                atm={atm}
                isAtm={isAtm}
                marketOpen={marketOpen}
                liveQ={liveQ}
                onSelect={onSelect}
              />
            );
          })
        )}
      </div>

      {/* Bottom filter pills — index + expiry. Expiry list is driven by the
          backend (data.expiries), which only carries the expiries the admin
          has actually subscribed, so the sheet shows exactly those. */}
      <div className="flex shrink-0 items-center justify-center gap-2 border-t border-border bg-card px-3 py-2.5 pb-[max(0.625rem,env(safe-area-inset-bottom))]">
        {!fixedUnderlying && (
          <Pill label={activeLabel} onClick={() => setSheet("index")} />
        )}
        <Pill label={fmtExpiry(activeExpiry)} onClick={() => setSheet("expiry")} />
      </div>

      {/* Filter bottom sheet */}
      {sheet && (
        <FilterSheet
          title={sheet === "expiry" ? "Expiry date" : "Filter by"}
          options={
            sheet === "expiry"
              ? expiries.map((e) => ({ key: e, label: fmtExpiry(e) }))
              : UNDERLYINGS.map((u) => ({ key: u.symbol, label: u.label }))
          }
          selectedKey={sheet === "expiry" ? activeExpiry ?? "" : underlying}
          onSelect={(key) => {
            if (sheet === "expiry") setExpiry(key);
            else setUnderlying(key);
            setSheet(null);
          }}
          onClose={() => setSheet(null)}
        />
      )}
    </div>
  );
}

function SideBtn({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded px-5 py-1.5 text-xs font-semibold transition-colors",
        active
          ? "bg-primary/15 text-primary"
          : "text-muted-foreground hover:text-foreground",
      )}
    >
      {label}
    </button>
  );
}

function Pill({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex h-9 items-center gap-1.5 rounded-full border border-border bg-muted/30 px-4 text-xs font-semibold hover:bg-muted/50"
    >
      <ChevronUp className="size-3.5 text-primary" />
      {label}
    </button>
  );
}

const StrikeRow = memo(function StrikeRow({
  underlying,
  strike,
  leg,
  side,
  atm,
  isAtm,
  marketOpen,
  liveQ,
  onSelect,
  rowRef,
}: {
  underlying: string;
  strike: number;
  leg: any;
  side: Side;
  atm: number | null;
  isAtm: boolean;
  marketOpen: boolean;
  liveQ: any;
  onSelect: (token: string) => void;
  rowRef?: React.Ref<HTMLDivElement>;
}) {
  const ltp: number | null = liveQ?.ltp ?? leg?.ltp ?? null;
  const pct: number | null = liveQ?.change_pct ?? leg?.change_pct ?? null;
  const chg: number | null = liveQ?.change ?? null;
  // Snappy 300 ms decay so a fast-moving option visibly throbs — each tick
  // re-arms the flash, so a hot strike "shivers" green/red.
  const dir = usePriceFlash(ltp, 300);
  const money = moneynessFor(strike, atm, side);
  const sideLabel = side === "CALL" ? "CE" : "PE";

  const down = pct != null && pct < 0;
  const ltpColor =
    pct == null ? "text-foreground" : down ? "text-sell" : "text-buy";
  const flashBg =
    dir === "up" ? "bg-buy/20" : dir === "down" ? "bg-sell/20" : "bg-transparent";

  const changeText =
    chg != null && pct != null
      ? `${chg >= 0 ? "+" : ""}${chg.toFixed(2)}(${Math.abs(pct).toFixed(2)}%)`
      : pct != null
        ? `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`
        : "";

  return (
    <div
      ref={rowRef}
      role="button"
      tabIndex={0}
      onClick={() => leg?.token && onSelect(String(leg.token))}
      onKeyDown={(e) => {
        if ((e.key === "Enter" || e.key === " ") && leg?.token) {
          e.preventDefault();
          onSelect(String(leg.token));
        }
      }}
      className={cn(
        "flex cursor-pointer items-center gap-3 border-b border-border/40 px-3 py-2.5 transition-colors",
        isAtm ? "bg-primary/10" : "hover:bg-muted/30",
      )}
    >
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-semibold">
          {underlying} {strike.toLocaleString("en-IN")} {sideLabel}
        </div>
        {money && <MoneyBadge kind={money} />}
      </div>
      <div className="text-right">
        <div
          className={cn(
            "inline-block rounded px-1.5 font-tabular tabular-nums text-base font-bold transition-colors",
            ltpColor,
            flashBg,
          )}
        >
          {ltp != null ? formatPrice(ltp) : "—"}
        </div>
        {marketOpen && changeText ? (
          <div className={cn("font-tabular tabular-nums text-[11px]", ltpColor)}>
            {changeText}
          </div>
        ) : null}
      </div>
    </div>
  );
});

function MoneyBadge({ kind }: { kind: "ITM" | "ATM" | "OTM" }) {
  const cls =
    kind === "ITM"
      ? "bg-buy/15 text-buy"
      : kind === "ATM"
        ? "bg-atm/20 text-atm"
        : "bg-primary/15 text-primary";
  return (
    <span
      className={cn(
        "mt-1 inline-block rounded px-1.5 py-0.5 text-[9px] font-bold tracking-wide",
        cls,
      )}
    >
      {kind}
    </span>
  );
}

interface SheetOption {
  key: string;
  label: string;
}

function FilterSheet({
  title,
  options,
  selectedKey,
  onSelect,
  onClose,
}: {
  title: string;
  options: SheetOption[];
  selectedKey: string;
  onSelect: (key: string) => void;
  onClose: () => void;
}) {
  return (
    <div className="fixed inset-0 z-50" onClick={onClose}>
      <div className="absolute inset-0 bg-black/50" />
      <div
        onClick={(e) => e.stopPropagation()}
        className="absolute inset-x-0 bottom-0 rounded-t-2xl border-t border-border bg-card pb-[max(1rem,env(safe-area-inset-bottom))] pt-2 shadow-2xl"
      >
        <div className="mx-auto mb-3 h-1 w-10 rounded-full bg-border" />
        <div className="px-5 pb-2 text-lg font-bold">{title}</div>
        <div className="max-h-[55vh] overflow-y-auto pb-2 scrollbar-thin">
          {options.length === 0 ? (
            <div className="px-5 py-6 text-sm text-muted-foreground">
              Nothing to choose here.
            </div>
          ) : (
            options.map((o) => {
              const selected = o.key === selectedKey;
              return (
                <button
                  key={o.key}
                  type="button"
                  onClick={() => onSelect(o.key)}
                  className="flex w-full items-center gap-3 px-5 py-3.5 text-left hover:bg-muted/30"
                >
                  <span
                    className={cn(
                      "grid size-5 shrink-0 place-items-center rounded-full border-2",
                      selected ? "border-primary" : "border-muted-foreground/40",
                    )}
                  >
                    {selected && <span className="size-2.5 rounded-full bg-primary" />}
                  </span>
                  <span
                    className={cn(
                      "text-[15px]",
                      selected ? "font-bold text-foreground" : "font-medium",
                    )}
                  >
                    {o.label}
                  </span>
                </button>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}
