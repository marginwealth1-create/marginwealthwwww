"use client";

import { useQuery } from "@tanstack/react-query";
import { PositionAPI, WalletAPI } from "@/lib/api";
import { cn, formatINR, pnlColor } from "@/lib/utils";

/**
 * Slim wallet stats strip for the desktop terminal layout — sits between
 * the chart card and the positions table so the trader always sees their
 * Total Balance / Equity / Used Margin / Available / open P&L without
 * leaving the page. Hidden on mobile (the same numbers are surfaced
 * inside the TradeDetailSheet's margin cards there).
 *
 * Values:
 *   • Total Balance = available + used (wallet capital, ignores P&L)
 *   • Equity        = available + used + open unrealised P&L (live mark-to-market)
 *   • Used Margin   = wallet.used_margin (locked in open positions)
 *   • Available     = wallet.available_balance (free to trade)
 *   • P/L           = live unrealised across all open positions
 *
 * `openPnL` prop is the same value the Positions tab displays — the
 * terminal page computes it from the 250 ms WS overlay using close-side
 * prices (bid for long / ask for short). Reusing that here keeps the
 * footer EXACTLY in sync with the header and per-row P/L numbers,
 * instead of polling `/positions/pnl-summary` separately (which uses
 * mid-LTP and was visibly off for wide-spread spot metals). The query
 * fallback handles the case when the prop isn't passed yet (e.g.,
 * during initial mount before positionsLive has aggregated).
 */
export function WalletStrip({
  className,
  openPnL,
}: {
  className?: string;
  openPnL?: number;
}) {
  const { data: wallet } = useQuery({
    queryKey: ["wallet", "summary"],
    queryFn: () => WalletAPI.summary(),
    refetchInterval: 10_000,
    staleTime: 5_000,
    refetchOnWindowFocus: false,
  });

  // Fallback only — used when the parent doesn't pass a live `openPnL`.
  const { data: pnl } = useQuery({
    queryKey: ["positions", "pnl-summary"],
    queryFn: () => PositionAPI.pnlSummary(),
    refetchInterval: 5_000,
    staleTime: 2_000,
    refetchOnWindowFocus: false,
    enabled: openPnL === undefined,
  });

  // Dabba / CFD KPI strip — Bal · Equity · Margin · Free + live Open P/L.
  // Backend's /wallet/summary now ships these as first-class fields. Legacy
  // `available_balance + used_margin` math kept as a fallback so the strip
  // doesn't blank out if an older API rolls back.
  const bal = Number(
    wallet?.bal ??
      Number(wallet?.available_balance ?? 0) + Number(wallet?.used_margin ?? 0),
  );
  const margin = Number(wallet?.margin ?? wallet?.used_margin ?? 0);
  // Prefer the parent's live `openPnL` (matches per-row P/L in the table)
  // over the wallet's `open_unrealized_pnl` (uses mid-LTP, off by a tick
  // on wide-spread instruments).
  const openUnrl =
    openPnL !== undefined
      ? openPnL
      : Number(pnl?.open_unrealised ?? pnl?.unrealized_pnl ?? wallet?.open_unrealized_pnl ?? 0);
  const equity = bal + openUnrl;
  const free = equity - margin;

  // Margin Level chip removed per user request — a residual ₹0.02 left
  // over from a closed position made the ratio explode into a nonsense
  // "40,005,950%" display whenever no real exposure was open. Bal /
  // Equity / Margin / Free + Open P/L convey all the info admins and
  // traders need anyway; the gauge lives in the backend stop-out check.

  return (
    <div
      className={cn(
        "hidden items-center gap-x-5 gap-y-1 rounded-md border border-border bg-card px-3 py-1.5 text-[11px] lg:flex",
        className,
      )}
    >
      <Stat label="Bal" value={formatINR(bal)} />
      <Sep />
      <Stat label="Equity" value={formatINR(equity)} valueClass={pnlColor(openUnrl)} />
      <Sep />
      <Stat label="Margin" value={formatINR(margin)} />
      <Sep />
      <Stat
        label="Free"
        value={formatINR(free)}
        valueClass={free < 0 ? "text-red-500" : undefined}
      />
      <Sep />
      <Stat
        label="P/L"
        value={`${openUnrl >= 0 ? "+" : ""}${formatINR(openUnrl)}`}
        valueClass={pnlColor(openUnrl)}
      />
      {/* Settlement pill removed on operator request — settlement is
          an informational broker-side metric, not something the
          trader needs to see while placing orders. The amount is
          still tracked on Wallet.settlement_outstanding and surfaces
          on the wallet/profile pages for transparency. */}
    </div>
  );
}

function Stat({
  label,
  value,
  valueClass,
}: {
  label: string;
  value: string;
  valueClass?: string;
}) {
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-muted-foreground">{label}</span>
      <span className={cn("font-tabular font-semibold tabular-nums", valueClass)}>
        {value}
      </span>
    </div>
  );
}

function Sep() {
  return <span className="h-3 w-px shrink-0 bg-border" aria-hidden />;
}
