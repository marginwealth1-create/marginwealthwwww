"use client";

import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowDownToLine,
  ArrowUpRight,
  Briefcase,
  ChevronRight,
  Eye,
  EyeOff,
  Gift,
  LineChart,
  Table2,
  TrendingUp,
  Wallet,
} from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { useAuthStore } from "@/stores/authStore";
import { DashboardAPI, OrderAPI, PositionAPI, WalletAPI } from "@/lib/api";
import { cn, formatINR, formatPrice, pnlColor } from "@/lib/utils";
import { AddFundsWizard } from "@/components/wallet/AddFundsWizard";
import { MarketOverview } from "@/components/trading/MarketOverview";
import { TopMovers } from "@/components/trading/TopMovers";
import { useHomeTicker } from "@/lib/useSupport";

export default function DashboardPage() {
  const user = useAuthStore((s) => s.user);
  const { data: summary } = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => DashboardAPI.summary(),
    refetchInterval: 5000,
  });
  const { data: positions } = useQuery({
    queryKey: ["positions", "open"],
    queryFn: () => PositionAPI.open(),
    refetchInterval: 5000,
  });
  const { data: orders } = useQuery({
    queryKey: ["orders", "recent-dashboard"],
    queryFn: () => OrderAPI.list(),
  });
  // Today's P&L comes from the dedicated `/positions/pnl-summary` endpoint —
  // /dashboard/summary used to recompute it inline, but that path:
  //   1. only iterated currently-open positions, so trades CLOSED today were
  //      excluded from "Today's P&L";
  //   2. added each position's LIFETIME `realized_pnl` (not just today's),
  //      inflating the number with old realised slices; and
  //   3. didn't convert USD-quoted (crypto / forex / MCX) P&L to INR,
  //      reading ~83× too small for those users.
  // The pnl-summary endpoint already covers all three correctly and is the
  // same source the terminal's positions strip + PnlSummaryCards use, so the
  // dashboard, terminal and reports views now agree on a single number.
  const { data: pnlSummary } = useQuery({
    queryKey: ["positions", "pnl-summary"],
    queryFn: () => PositionAPI.pnlSummary(),
    refetchInterval: 5000,
  });

  // Add-funds wizard — same 4-step flow as the Wallet page, opened straight
  // from the home Deposit quick-action so users don't have to hop to /wallet.
  const qc = useQueryClient();
  const [depositOpen, setDepositOpen] = useState(false);
  const { data: companyBanks } = useQuery({
    queryKey: ["company-banks"],
    queryFn: () => WalletAPI.companyBanks(),
    staleTime: 5 * 60_000,
  });
  const defaultBank =
    companyBanks?.find((b: any) => b.is_default) ?? companyBanks?.[0];

  const wallet = summary?.wallet ?? {};
  const portfolio =
    Number(wallet.available_balance ?? 0) + Number(wallet.used_margin ?? 0);
  // Bonus credit pool (Bonus Management). Show the FREE (unlocked) portion —
  // it decrements as the user opens trades against it and restores on close.
  const bonus = Number(wallet.bonus_free ?? wallet.credit ?? 0);
  const bonusLocked = Number(wallet.bonus_locked ?? 0);
  // Prefer the canonical pnl-summary value; fall back to the dashboard
  // payload only while the dedicated query is still loading so we don't
  // flash ₹0 on first paint.
  const todayPnl = Number(pnlSummary?.today_pnl ?? summary?.today_pnl ?? 0);
  const todayPct = portfolio ? (todayPnl / portfolio) * 100 : 0;

  const [hideBalance, setHideBalance] = useState(false);

  return (
    <div className="space-y-5">
      {/* ── Announcement ticker (home only, admin-managed) ───────── */}
      <HomeTicker />

      {/* ── Greeting ─────────────────────────────────────────────── */}
      <header className="flex items-center justify-between">
        <div>
          <p className="text-xs uppercase tracking-wider text-muted-foreground">Welcome back</p>
          <h1 className="text-xl font-semibold tracking-tight md:text-2xl">
            {user?.full_name?.split(" ")[0] ?? "Trader"} 👋
          </h1>
          <p className="mt-0.5 text-[11px] text-muted-foreground">
            {user?.is_demo && <span className="mr-1 rounded bg-amber-500/15 px-1.5 py-0.5 text-amber-700 dark:text-amber-400">DEMO</span>}
            {user?.user_code}
          </p>
        </div>
      </header>

      {/* ── Hero portfolio card (Upstox-style) ───────────────────── */}
      <section className="overflow-hidden rounded-2xl bg-gradient-to-br from-primary via-primary to-primary/80 p-5 text-primary-foreground shadow-lg shadow-primary/20">
        <div className="flex items-start justify-between">
          <div className="space-y-0.5">
            <div className="flex items-center gap-2 text-xs uppercase tracking-wider opacity-90">
              <Wallet className="size-3.5" /> Portfolio value
            </div>
            <div className="flex items-baseline gap-3">
              <h2 className="font-tabular text-3xl font-bold text-white md:text-4xl">
                {hideBalance ? "₹ ••••••" : formatINR(portfolio)}
              </h2>
              <button
                type="button"
                onClick={() => setHideBalance((v) => !v)}
                aria-label="Toggle balance visibility"
                className="rounded-full p-1 opacity-80 transition hover:bg-white/15 hover:opacity-100"
              >
                {hideBalance ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
              </button>
            </div>
            <div
              className={cn(
                "mt-1 inline-flex items-center gap-1 rounded-full bg-white/15 px-2 py-0.5 text-xs font-semibold",
                todayPnl >= 0 ? "text-buy" : "text-sell"
              )}
              style={{ color: todayPnl >= 0 ? "#7df0a4" : "#ffadb5" }}
            >
              <TrendingUp className={cn("size-3", todayPnl < 0 && "rotate-180")} />
              {hideBalance ? "•••" : `${todayPnl >= 0 ? "+" : ""}${formatINR(todayPnl)}`}
              {!hideBalance && (
                <span className="opacity-80">
                  ({todayPct >= 0 ? "+" : ""}
                  {todayPct.toFixed(2)}%)
                </span>
              )}
              <span className="opacity-70">today</span>
            </div>
            {bonus > 0 && !hideBalance && (
              <div className="mt-1.5 inline-flex items-center gap-1 rounded-full bg-white/20 px-2 py-0.5 text-xs font-semibold">
                <Gift className="size-3" /> {formatINR(bonus)} bonus credit
              </div>
            )}
          </div>
          <button
            onClick={() => setDepositOpen(true)}
            className="hidden shrink-0 items-center gap-1.5 rounded-full bg-white/15 px-3 py-1.5 text-xs font-semibold backdrop-blur transition hover:bg-white/25 sm:inline-flex"
          >
            <ArrowDownToLine className="size-3.5" /> Add funds
          </button>
        </div>

        {/* Inline mini-stats — 2 columns (Available + Used margin).
            Holdings P/L tile removed — every trade on this platform is
            intraday / carry-forward, there's no separate delivery book. */}
        <div className={cn("mt-5 grid divide-x divide-white/15 text-center text-xs", bonus > 0 ? "grid-cols-3" : "grid-cols-2")}>
          <MiniStat
            label="Available"
            value={hideBalance ? "•••" : formatINR(wallet.available_free ?? wallet.available_balance ?? 0)}
          />
          <MiniStat
            label="Used margin"
            value={hideBalance ? "•••" : formatINR(wallet.used_margin ?? 0)}
          />
          {(bonus > 0 || bonusLocked > 0) && (
            <MiniStat
              label="Bonus credit"
              value={hideBalance ? "•••" : formatINR(bonus)}
              hint={bonusLocked > 0 ? `${formatINR(bonusLocked)} in use` : undefined}
            />
          )}
        </div>
      </section>

      {/* ── Quick actions ─────────────────────────────────────── */}
      <section className="grid grid-cols-4 gap-2 sm:gap-3">
        <QuickAction onClick={() => setDepositOpen(true)} icon={ArrowDownToLine} label="Deposit" />
        <QuickAction href="/option-chain" icon={Table2} label="Options" />
        <QuickAction href="/positions" icon={Briefcase} label="Position" />
        <QuickAction href="/marketwatch" icon={LineChart} label="Market" />
      </section>

      {/* Add-funds 4-step wizard — same flow as the Wallet page. */}
      <AddFundsWizard
        open={depositOpen}
        onClose={() => setDepositOpen(false)}
        companyBanks={(companyBanks as any[]) ?? []}
        payeeName={defaultBank?.account_holder}
        onSuccess={() => {
          qc.invalidateQueries({ queryKey: ["dashboard"] });
          qc.invalidateQueries({ queryKey: ["my-deposits"] });
          qc.invalidateQueries({ queryKey: ["wallet-summary"] });
          qc.invalidateQueries({ queryKey: ["wallet-txns"] });
        }}
      />

      {/* ── Mobile: live market overview (replaces the stat tiles) ──
          Phones get a live, color-coded market snapshot in place of the
          three small stat tiles — same data plumbing as the terminal's
          instruments panel, ticking via the marketdata WS. */}
      <MarketOverview className="sm:hidden" />

      {/* Mobile: live top gainers & losers from a NIFTY large-cap basket. */}
      <TopMovers className="sm:hidden" />

      {/* ── Stat tiles row — desktop only (sm+). Hidden on mobile where
          the MarketOverview above takes their place. ────────────────── */}
      <section className="hidden gap-3 sm:grid sm:grid-cols-3">
        <StatTile label="Open positions" value={String(summary?.open_positions ?? 0)} hint="live MTM" />
        <StatTile label="Pending orders" value={String(summary?.pending_orders ?? 0)} hint="awaiting fill" />
        <StatTile
          label="Today's P&L"
          value={hideBalance ? "•••" : formatINR(todayPnl)}
          tone={pnlColor(todayPnl)}
        />
      </section>

      {/* ── Open positions + Recent orders — desktop only (lg+).
          Hidden on mobile where the live MarketOverview above is the
          primary focus; the full positions/orders live on their own
          bottom-nav tabs. ──────────────────────────────────────────── */}
      <section className="hidden gap-4 lg:grid lg:grid-cols-3">
        <PanelCard
          className="lg:col-span-2"
          title="Open positions"
          subtitle="Live mark-to-market"
          action={{ label: "View all", href: "/positions" }}
        >
          {positions?.length ? (
            <ul className="divide-y divide-border">
              {positions.slice(0, 6).map((p: any) => {
                const isUp = Number(p.unrealized_pnl) >= 0;
                return (
                  <li key={p.id}>
                    <Link
                      href="/positions"
                      className="flex items-center justify-between gap-3 py-2.5 transition-colors hover:bg-muted/30"
                    >
                      <div className="flex items-center gap-3">
                        <div
                          className={cn(
                            "grid size-9 place-items-center rounded-full text-xs font-bold uppercase",
                            isUp ? "bg-buy/15 text-buy" : "bg-sell/15 text-sell"
                          )}
                        >
                          {p.symbol?.slice(0, 2)}
                        </div>
                        <div>
                          <div className="text-sm font-medium">{p.symbol}</div>
                          <div className="text-[11px] text-muted-foreground">
                            {p.product_type} · {p.quantity} @ {formatPrice(p.avg_price, p.segment_type, p.exchange)}
                          </div>
                        </div>
                      </div>
                      <div className="text-right">
                        <div className={cn("font-tabular text-sm font-semibold", pnlColor(p.unrealized_pnl))}>
                          {formatINR(p.unrealized_pnl)}
                        </div>
                        <div className="text-[10px] text-muted-foreground">
                          LTP {formatPrice(p.ltp, p.segment_type, p.exchange)}
                        </div>
                      </div>
                    </Link>
                  </li>
                );
              })}
            </ul>
          ) : (
            <EmptyState message="No open positions" cta={{ label: "Open a trade", href: "/terminal" }} />
          )}
        </PanelCard>

        <PanelCard
          title="Recent orders"
          subtitle="Last 6 placed"
          action={{ label: "All", href: "/positions" }}
        >
          {orders?.length ? (
            <ul className="divide-y divide-border">
              {orders.slice(0, 6).map((o: any) => {
                const isBuy = String(o.action).toUpperCase() === "BUY";
                return (
                  <li key={o.id}>
                    <Link
                      href="/positions"
                      className="flex items-center justify-between py-2 text-xs transition-colors hover:bg-muted/30"
                    >
                      <div className="flex items-center gap-2">
                        <span
                          className={cn(
                            "inline-flex w-12 justify-center rounded px-1.5 py-0.5 text-[10px] font-semibold",
                            isBuy ? "bg-buy/15 text-buy" : "bg-sell/15 text-sell"
                          )}
                        >
                          {isBuy ? "BUY" : "SELL"}
                        </span>
                        <span className="font-medium">{o.symbol}</span>
                        <span className="text-muted-foreground">×{o.quantity}</span>
                      </div>
                      <span
                        className={cn(
                          "rounded-full px-2 py-0.5 text-[10px] font-semibold",
                          o.status === "EXECUTED"
                            ? "bg-buy/15 text-buy"
                            : o.status === "REJECTED" || o.status === "CANCELLED"
                              ? "bg-muted text-muted-foreground"
                              : "bg-amber-500/15 text-amber-600 dark:text-amber-400"
                        )}
                      >
                        {o.status}
                      </span>
                    </Link>
                  </li>
                );
              })}
            </ul>
          ) : (
            <EmptyState message="No orders yet" cta={{ label: "Place an order", href: "/terminal" }} />
          )}
        </PanelCard>
      </section>
    </div>
  );
}

function MiniStat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="px-2">
      <div className="text-[10px] uppercase tracking-wider opacity-75">{label}</div>
      <div className="mt-0.5 font-tabular text-sm font-semibold">{value}</div>
      {hint && <div className="text-[9px] opacity-70">{hint}</div>}
    </div>
  );
}

function QuickAction({
  href,
  onClick,
  icon: Icon,
  label,
}: {
  href?: string;
  onClick?: () => void;
  icon: any;
  label: string;
}) {
  const cls = cn(
    "flex flex-col items-center justify-center gap-1.5 rounded-xl border border-border bg-card p-3 text-[11px] font-medium transition-all",
    "hover:border-primary/40 hover:bg-primary/5 active:scale-95",
  );
  const inner = (
    <>
      <div className="grid size-10 place-items-center rounded-full bg-primary/10 text-primary">
        <Icon className="size-5" strokeWidth={2.25} />
      </div>
      <span>{label}</span>
    </>
  );
  if (onClick) {
    return (
      <button type="button" onClick={onClick} className={cls}>
        {inner}
      </button>
    );
  }
  return (
    <Link href={href ?? "#"} className={cls}>
      {inner}
    </Link>
  );
}

function StatTile({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: string;
}) {
  return (
    <div className="rounded-xl border border-border bg-card p-3">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className={cn("mt-1 font-tabular text-lg font-semibold", tone)}>{value}</div>
      {hint && <div className="mt-0.5 text-[10px] text-muted-foreground">{hint}</div>}
    </div>
  );
}

function PanelCard({
  title,
  subtitle,
  action,
  children,
  className,
}: {
  title: string;
  subtitle?: string;
  action?: { label: string; href: string };
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("rounded-xl border border-border bg-card p-4", className)}>
      <div className="mb-3 flex items-start justify-between">
        <div>
          <h3 className="text-sm font-semibold">{title}</h3>
          {subtitle && <p className="text-[11px] text-muted-foreground">{subtitle}</p>}
        </div>
        {action && (
          <Link
            href={action.href}
            className="inline-flex items-center gap-0.5 text-xs font-medium text-primary hover:underline"
          >
            {action.label} <ChevronRight className="size-3" />
          </Link>
        )}
      </div>
      {children}
    </div>
  );
}

function EmptyState({ message, cta }: { message: string; cta?: { label: string; href: string } }) {
  return (
    <div className="flex flex-col items-center gap-2 py-8 text-center">
      <div className="text-sm text-muted-foreground">{message}</div>
      {cta && (
        <Button asChild variant="outline" size="sm">
          <Link href={cta.href}>
            <ArrowUpRight className="size-3.5" /> {cta.label}
          </Link>
        </Button>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────
// Home announcement ticker — admin-managed scrolling strip. Renders ONLY
// on this (home) page; other routes never mount it. Hidden when the admin
// has no lines. Seamless loop = content duplicated + translateX 0 → -50%.
// ─────────────────────────────────────────────────────────────────
function HomeTicker() {
  const { data } = useHomeTicker();
  const msgs = (data?.messages ?? []).filter((m) => m && m.trim());
  if (msgs.length === 0) return null;
  // One continuous string with a bullet between EVERY line INCLUDING the wrap
  // (trailing separator) so the loop seam reads the same as any other gap — the
  // text joins directly, no big carve between repeats.
  const sep = " • "; // em-space · bullet · em-space
  const content = msgs.join(sep) + sep;
  const dur = Math.max(16, Math.round(content.length * 0.3));
  // Soft fade at both inner edges so the scroll blends into the box instead of
  // hard-cutting at the border ("carve"). Kept inside the rounded container.
  const fade =
    "linear-gradient(to right, transparent 0, #000 22px, #000 calc(100% - 22px), transparent 100%)";
  return (
    <div
      className="relative overflow-hidden rounded-lg border border-primary/25 bg-primary/5"
      style={{ WebkitMaskImage: fade, maskImage: fade }}
    >
      <div
        className="mp-ticker-track flex w-max whitespace-nowrap py-1.5 text-[13px] font-semibold text-primary"
        style={{ animationDuration: `${dur}s` }}
      >
        <span>{content}</span>
        <span aria-hidden>{content}</span>
      </div>
      <style>{`
        .mp-ticker-track { animation: mp-ticker linear infinite; }
        .mp-ticker-track:hover { animation-play-state: paused; }
        @keyframes mp-ticker { from { transform: translateX(0); } to { transform: translateX(-50%); } }
      `}</style>
    </div>
  );
}
