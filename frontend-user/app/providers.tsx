"use client";

import { QueryClient, keepPreviousData } from "@tanstack/react-query";
import { PersistQueryClientProvider } from "@tanstack/react-query-persist-client";
import { createSyncStoragePersister } from "@tanstack/query-sync-storage-persister";
import { Suspense, useState } from "react";
import { ThemeProvider, useTheme } from "next-themes";
import { Toaster } from "sonner";
import { BrandingProvider } from "@/lib/branding-context";

// localStorage key for the persisted React Query cache. Exported so the
// logout flow can wipe it (a shared device must not show the previous user's
// balance / positions). Bump the version suffix to bust every client's cache
// after a breaking schema change.
export const RQ_CACHE_KEY = "mp-rq-cache-v1";

function ThemedToaster() {
  const { resolvedTheme } = useTheme();
  return (
    <Toaster
      theme={(resolvedTheme === "light" ? "light" : "dark") as "light" | "dark"}
      position="top-right"
      toastOptions={{
        style: {
          background: "hsl(var(--card))",
          border: "1px solid hsl(var(--border))",
          color: "hsl(var(--foreground))",
        },
      }}
    />
  );
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // 30 s stale window — within this, navigating around the app
            // serves data from cache (instant). After 30 s, the next mount
            // / focus / reconnect triggers a single refetch. Components
            // that need tighter freshness set their own staleTime /
            // refetchInterval (positions strip = 500 ms, wallet = 4 s,
            // option chain = 2 s, PnL summary = 10 s).
            staleTime: 30_000,
            // Keep cached data for 5 min after last use so back/forward
            // navigation is snappy.
            gcTime: 5 * 60_000,
            // APK-like feel: do NOT refetch every time the app regains focus
            // (switching back to the tab / reopening the PWA). That was firing
            // a refetch storm on every open → visible "loading". Cached data
            // paints instantly; live prices keep flowing over the WebSocket and
            // each query still background-refreshes on its own refetchInterval,
            // so nothing goes stale. Reconnect refetch stays on (network drop
            // → come back → refresh once).
            refetchOnWindowFocus: false,
            refetchOnReconnect: true,
            // First mount of a query renders cached data immediately and
            // refetches in the background if stale — no "Loading…" flash.
            refetchOnMount: true,
            // Paint the previous key's data while a new key fetches, so
            // switching tabs / filters / searched symbols never flashes a
            // skeleton or empty state (positions OPEN↔CLOSED, marketwatch
            // symbol switch, ledger date range, etc). Same setting the admin
            // app already uses. Does NOT affect same-key refetch or the
            // optimistic trade-close flow (those mutate the cache directly).
            placeholderData: keepPreviousData,
            retry: (count, err: any) => {
              const status = err?.response?.status;
              if (status && status >= 400 && status < 500) return false;
              return count < 2;
            },
          },
        },
      })
  );

  // ── Persist the WHOLE query cache to localStorage ──────────────────────
  // This is the "feel-fast like the APK" change: on every app open / reload,
  // the last-known data for every page (portfolio balance, positions, market
  // overview, orders, …) is rehydrated from localStorage and painted INSTANTLY
  // — no blank / ₹0 flash while the network round-trips. Each query then
  // refetches in the background per its own staleTime / refetchInterval, so
  // the cached value is replaced with fresh data a moment later (exactly how
  // the APK's MMKV cache behaves). Survives a full browser/app close because
  // localStorage is device-persistent. createSyncStoragePersister handles the
  // SSR case (no window → no-op storage) so this stays safe to render on the
  // server.
  const [persister] = useState(() =>
    createSyncStoragePersister({
      storage: typeof window !== "undefined" ? window.localStorage : undefined,
      key: RQ_CACHE_KEY,
      // Don't thrash the disk on every tick — coalesce writes.
      throttleTime: 1000,
    }),
  );

  return (
    <ThemeProvider
      attribute="class"
      defaultTheme="light"
      enableSystem={false}
      disableTransitionOnChange
    >
      <PersistQueryClientProvider
        client={client}
        persistOptions={{
          persister,
          // Discard a cache older than 24 h — a day-old balance isn't worth
          // flashing; the user gets a fresh fetch instead.
          maxAge: 1000 * 60 * 60 * 24,
          // Schema version — changing this invalidates every persisted cache.
          buster: RQ_CACHE_KEY,
          dehydrateOptions: {
            // Only persist SETTLED-SUCCESS queries — never errors or
            // in-flight loaders (those would restore a broken state).
            shouldDehydrateQuery: (q) => q.state.status === "success",
          },
        }}
      >
        {/* BrandingProvider uses `useSearchParams` (for `?ref=`),
            which Next 14 requires to live inside a <Suspense> when
            used at the root layout. The fallback is just the raw
            children — branding is applied imperatively via
            document.title / favicon swap, so unmounted children
            still render unbranded for one tick before the effect
            runs. That's identical to today's behaviour. */}
        <Suspense fallback={children}>
          <BrandingProvider>{children}</BrandingProvider>
        </Suspense>
        <ThemedToaster />
      </PersistQueryClientProvider>
    </ThemeProvider>
  );
}
