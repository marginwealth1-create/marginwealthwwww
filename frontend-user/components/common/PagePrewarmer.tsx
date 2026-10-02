"use client";

import { useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import {
  DashboardAPI,
  OrderAPI,
  PositionAPI,
  ProfileAPI,
  WalletAPI,
} from "@/lib/api";

/**
 * "Native-APK feel" prewarmer. Mounted once inside the authenticated
 * dashboard shell. After the first paint settles (requestIdleCallback,
 * so it never competes with the visible page), it does two things in the
 * background:
 *
 *   1. router.prefetch() the heavy routes that are NOT a <Link> in the
 *      always-visible bottom nav (terminal, option-chain, wallet,
 *      reports). The bottom-nav tabs already auto-prefetch their JS
 *      chunks via next/link; this extends the same warm-cache treatment
 *      to the routes reached by buttons / router.push so their FIRST
 *      open paints instantly instead of waiting on a chunk download.
 *
 *   2. queryClient.prefetchQuery() the primary data each main tab needs,
 *      using the EXACT same queryKey + queryFn the pages use. So by the
 *      time the user taps a tab the data is already in the React Query
 *      cache and the page renders populated — no "Loading…" skeleton on
 *      the first visit of the session. prefetchQuery respects the global
 *      staleTime (30 s), so a value just rehydrated from the persisted
 *      localStorage cache is reused, not re-fetched.
 *
 * Everything here is best-effort and fire-and-forget: prefetchQuery never
 * throws, router.prefetch failures are swallowed, and the whole pass runs
 * once per mount behind an idle callback. Renders nothing.
 */

// Routes worth warming that aren't already prefetched by the bottom-nav
// <Link>s. /terminal + /option-chain pull the heaviest chunks (charts),
// so warming them is the biggest single win.
const PREFETCH_ROUTES = [
  "/terminal",
  "/option-chain",
  "/wallet",
  // `/reports` has no page.tsx — it's a layout-only group whose only
  // landing page is `/reports/tradebook` (same href the profile "Reports"
  // row + sidebar use). Prefetching bare `/reports` fired an RSC request
  // that 404'd, so warm the actual route instead.
  "/reports/tradebook",
];

export function PagePrewarmer() {
  const router = useRouter();
  const qc = useQueryClient();
  const done = useRef(false);

  useEffect(() => {
    if (done.current) return;
    done.current = true;
    if (typeof window === "undefined") return;

    const idle = (cb: () => void) =>
      "requestIdleCallback" in window
        ? (window as any).requestIdleCallback(cb, { timeout: 2500 })
        : (window as any).setTimeout(cb, 1200);

    idle(() => {
      // 0) Warm the TradingView charting library (several MB, served from
      //    /public so next/link's router.prefetch can't reach it) while the
      //    user is idling on the dashboard. A low-priority <link rel=prefetch>
      //    downloads standalone.js in the background; the v7 service worker
      //    then cache-first stores it (and its sub-chunks on first real use),
      //    so tapping the chart opens instantly instead of waiting on the
      //    multi-MB download. Guarded so it's injected at most once.
      try {
        const CHART_LIB = "/charting_library/charting_library.standalone.js";
        if (!document.querySelector(`link[data-mp-prewarm="chart"]`)) {
          const link = document.createElement("link");
          link.rel = "prefetch";
          link.as = "script";
          link.href = CHART_LIB;
          link.setAttribute("data-mp-prewarm", "chart");
          document.head.appendChild(link);
        }
      } catch {
        /* non-fatal */
      }

      // 1) Warm route chunks (best-effort).
      for (const href of PREFETCH_ROUTES) {
        try {
          router.prefetch(href);
        } catch {
          /* non-fatal */
        }
      }

      // 2) Prime the primary tab queries. Keys/fns MUST match the page
      //    components exactly or the warm cache won't be picked up.
      void qc.prefetchQuery({
        queryKey: ["dashboard"],
        queryFn: () => DashboardAPI.summary(),
      });
      void qc.prefetchQuery({
        queryKey: ["positions", "open"],
        queryFn: () => PositionAPI.open(),
      });
      void qc.prefetchQuery({
        queryKey: ["positions", "active-trades"],
        queryFn: () => PositionAPI.activeTrades(),
      });
      void qc.prefetchQuery({
        queryKey: ["positions", "pnl-summary"],
        queryFn: () => PositionAPI.pnlSummary(),
      });
      void qc.prefetchQuery({
        queryKey: ["orders", "recent-dashboard"],
        queryFn: () => OrderAPI.list(),
      });
      void qc.prefetchQuery({
        queryKey: ["orders", "PENDING-LIKE"],
        queryFn: () => OrderAPI.list(),
      });
      // Wallet summary is read under two different keys across pages —
      // warm both so the wallet tab AND the positions status strip hit a
      // populated cache.
      void qc.prefetchQuery({
        queryKey: ["wallet-summary"],
        queryFn: () => WalletAPI.summary(),
      });
      void qc.prefetchQuery({
        queryKey: ["wallet", "summary"],
        queryFn: () => WalletAPI.summary(),
      });
      void qc.prefetchQuery({
        queryKey: ["me"],
        queryFn: () => ProfileAPI.me(),
      });
    });
  }, [router, qc]);

  return null;
}
