"use client";

import { QueryClient, keepPreviousData } from "@tanstack/react-query";
import { PersistQueryClientProvider } from "@tanstack/react-query-persist-client";
import { createSyncStoragePersister } from "@tanstack/query-sync-storage-persister";
import { useState } from "react";
import { ThemeProvider, useTheme } from "next-themes";
import { Toaster } from "sonner";
import { AdminBrandingChrome } from "@/components/branding/AdminBrandingChrome";
import { SessionKeepAlive } from "@/components/auth/SessionKeepAlive";

// localStorage key for the persisted React Query cache. Exported so the admin
// logout flow can wipe it — a shared admin device must not leak the previous
// admin's cached user lists / financials. Bump the version to bust all caches
// after a breaking schema change.
export const ADMIN_RQ_CACHE_KEY = "mp-admin-rq-cache-v1";

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
            // 60 s stale window — navigating within this paints from cache
            // with no refetch. Lists that change live (positions, orders,
            // pnl summary) keep their own `refetchInterval` and aren't
            // affected by this.
            staleTime: 60_000,
            // Keep cached pages warm for 30 min so the admin can hop
            // around the sidebar without re-fetching the same data on
            // every visit. Combined with the prefetcher in the admin
            // layout, the second visit to any page is instant.
            gcTime: 30 * 60_000,
            refetchOnWindowFocus: true,
            refetchOnReconnect: true,
            refetchOnMount: true,
            // Paint the previous page's data while the new key fetches —
            // kills the "Loading…" flash when sidebar nav swaps params
            // (e.g. /kyc tab switch, /positions OPEN ↔ CLOSED).
            placeholderData: keepPreviousData,
            retry: (count, err: any) =>
              err?.response?.status >= 400 && err?.response?.status < 500 ? false : count < 2,
          },
        },
      })
  );

  // ── Persist the WHOLE query cache to localStorage (APK-like instant feel) ──
  // On every admin app open / reload, the last-known data for every page
  // (dashboard tiles, accounts, user lists, positions, …) is rehydrated from
  // localStorage and painted INSTANTLY — no blank / spinner while the heavy
  // admin aggregations round-trip. Each query then refetches in the background
  // per its own staleTime / refetchInterval, so the cached value is replaced
  // with fresh data a moment later. Survives a full browser close because
  // localStorage is device-persistent. createSyncStoragePersister no-ops on
  // the server (no window), so this stays SSR-safe. Shorter 12 h maxAge than
  // the user app because admin data is more sensitive + changes faster.
  const [persister] = useState(() =>
    createSyncStoragePersister({
      storage: typeof window !== "undefined" ? window.localStorage : undefined,
      key: ADMIN_RQ_CACHE_KEY,
      throttleTime: 1000,
    }),
  );

  return (
    <ThemeProvider
      attribute="class"
      defaultTheme="dark"
      enableSystem={false}
      disableTransitionOnChange
    >
      <PersistQueryClientProvider
        client={client}
        persistOptions={{
          persister,
          maxAge: 1000 * 60 * 60 * 12, // 12 h
          buster: ADMIN_RQ_CACHE_KEY,
          dehydrateOptions: {
            shouldDehydrateQuery: (q) => q.state.status === "success",
          },
        }}
      >
        {/* Tenant chrome (tab title + favicon) for ADMIN/BROKER. Renders
            null; pure side-effects keyed off the auth store. */}
        <AdminBrandingChrome />
        {/* Keeps the access token rotated on tab focus / visibility
            so admins reopening the PWA after a long break aren't
            kicked back to /login. Renders nothing. */}
        <SessionKeepAlive />
        {children}
        <ThemedToaster />
      </PersistQueryClientProvider>
    </ThemeProvider>
  );
}
