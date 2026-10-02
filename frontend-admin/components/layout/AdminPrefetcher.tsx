"use client";

import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  DashboardAPI,
  UsersAPI,
  PayinOutAPI,
  TradingAPI,
  InstrumentAdminAPI,
  RiskAPI,
  ReportsAdminAPI,
  SettingsAPI,
  BrokerageAPI,
} from "@/lib/api";

// Fires once on admin layout mount and warms every sidebar
// destination so navigating between sections paints from cache
// instead of running a fresh fetch on first visit. Keys mirror
// the defaults each page uses verbatim — any drift will produce
// a cache miss and the page will fetch normally.
export function AdminPrefetcher() {
  const qc = useQueryClient();

  useEffect(() => {
    let cancelled = false;

    const run = async () => {
      if (cancelled) return;

      // Tier 1 — dashboard and headline counts (visible first thing
      // after login). Fire these in parallel right away.
      void Promise.allSettled([
        qc.prefetchQuery({
          queryKey: ["admin", "dashboard", "stats"],
          queryFn: () => DashboardAPI.stats(),
        }),
        qc.prefetchQuery({
          queryKey: ["admin", "dashboard", "alerts"],
          queryFn: () => DashboardAPI.riskAlerts(),
        }),
        qc.prefetchQuery({
          queryKey: ["admin", "positions", "pnl-summary"],
          queryFn: () => TradingAPI.pnlSummary(),
        }),
      ]);

      // Brief stagger so the dashboard's own queries get the wire
      // first — the rest fill in over the next second or so.
      await new Promise((r) => setTimeout(r, 150));
      if (cancelled) return;

      // Tier 2 — the rest of the sidebar. Each prefetch matches its
      // page's default query key exactly; otherwise we'd populate a
      // sibling key and the page would still spin.
      void Promise.allSettled([
        qc.prefetchQuery({
          queryKey: [
            "admin",
            "users",
            { q: "", role: "", status: "", page: 1, pageSize: 20 },
          ],
          queryFn: () => UsersAPI.list({ page: 1, page_size: 20 }),
        }),
        qc.prefetchQuery({
          queryKey: ["admin", "deposits", undefined],
          queryFn: () => PayinOutAPI.deposits(),
        }),
        qc.prefetchQuery({
          queryKey: ["admin", "withdrawals", undefined],
          queryFn: () => PayinOutAPI.withdrawals(),
        }),
        qc.prefetchQuery({
          queryKey: ["admin", "bank-accounts"],
          queryFn: () => PayinOutAPI.bankAccounts(),
        }),
        qc.prefetchQuery({
          queryKey: ["admin", "orders", { status: "", page: 1, userId: null }],
          queryFn: () => TradingAPI.orders({ page: 1, page_size: 50 }),
        }),
        qc.prefetchQuery({
          queryKey: ["admin", "positions", "OPEN", null],
          queryFn: () => TradingAPI.positions({ status: "OPEN" }),
        }),
        qc.prefetchQuery({
          queryKey: [
            "admin",
            "instruments",
            { q: "", exchange: "", page: 1 },
          ],
          queryFn: () => InstrumentAdminAPI.list({ page: 1, page_size: 50 }),
        }),
        qc.prefetchQuery({
          queryKey: ["admin", "risk", "global"],
          queryFn: () => RiskAPI.getGlobal(),
        }),
        qc.prefetchQuery({
          queryKey: ["admin", "risk", "users-with-overrides"],
          queryFn: () => RiskAPI.usersWithOverrides(),
        }),
        qc.prefetchQuery({
          queryKey: ["admin", "settings", "platform"],
          queryFn: () => SettingsAPI.platformList(),
        }),
        qc.prefetchQuery({
          queryKey: ["admin", "holidays", new Date().getFullYear()],
          queryFn: () => SettingsAPI.holidays(new Date().getFullYear()),
        }),
        qc.prefetchQuery({
          queryKey: ["admin", "backups"],
          queryFn: () => SettingsAPI.backupList(),
        }),
        qc.prefetchQuery({
          queryKey: ["admin", "brokerage"],
          queryFn: () => BrokerageAPI.list(),
        }),
        qc.prefetchQuery({
          queryKey: ["admin", "reports", "users"],
          queryFn: () => ReportsAdminAPI.users(),
        }),
        qc.prefetchQuery({
          queryKey: ["admin", "reports", "financial"],
          queryFn: () => ReportsAdminAPI.financial(),
        }),
        qc.prefetchQuery({
          queryKey: ["admin", "reports", "trades"],
          queryFn: () => ReportsAdminAPI.trades(),
        }),
      ]);
    };

    void run();
    return () => {
      cancelled = true;
    };
  }, [qc]);

  return null;
}
