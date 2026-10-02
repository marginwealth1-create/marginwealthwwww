"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAdminAuthStore } from "@/stores/authStore";
import { ensureFreshAccessToken, isExpiringSoon } from "@/lib/api";
import { STORAGE_KEYS } from "@/lib/constants";
import { AdminSidebar } from "@/components/layout/AdminSidebar";
import { AdminTopBar } from "@/components/layout/AdminTopBar";
import { AdminPrefetcher } from "@/components/layout/AdminPrefetcher";
import { AdminWsBridge } from "@/components/common/AdminWsBridge";
import { AdminMobileDrawer } from "@/components/layout/AdminMobileDrawer";
import { AdminBottomBar } from "@/components/layout/AdminBottomBar";
import { MobileNavProvider } from "@/components/layout/MobileNavContext";

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const admin = useAdminAuthStore((s) => s.admin);
  const hydrated = useAdminAuthStore((s) => s.hydrated);
  const refreshMe = useAdminAuthStore((s) => s.refreshMe);

  useEffect(() => {
    if (hydrated && !admin) router.replace("/login");
  }, [hydrated, admin, router]);

  // Refresh the cached admin object once on mount so any permissions
  // granted server-side after the last login (e.g. super-admin ticked
  // `brokers` for this sub-admin) become visible without a logout/login.
  // Errors are silent — the store handles that internally.
  useEffect(() => {
    if (hydrated && admin) void refreshMe();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hydrated]);

  // Preflight + resume-from-background access-token rotation. Mirrors
  // the user-side dashboard hook so admins who leave the tab open
  // overnight don't get the 401-storm-then-redirect-to-login dance
  // when they come back the next morning.
  useEffect(() => {
    if (!hydrated || !admin) return;
    const refreshIfNeeded = () => {
      const tok =
        typeof window !== "undefined"
          ? window.localStorage.getItem(STORAGE_KEYS.accessToken)
          : null;
      if (!tok || isExpiringSoon(tok)) void ensureFreshAccessToken();
    };
    refreshIfNeeded();
    const onVis = () => {
      if (document.visibilityState === "visible") refreshIfNeeded();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, [hydrated, admin]);

  if (!hydrated) {
    return <div className="grid h-screen place-items-center text-sm text-muted-foreground">Loading…</div>;
  }
  if (!admin) return null;

  return (
    <MobileNavProvider>
      <div className="grid min-h-screen grid-cols-1 md:grid-cols-[auto_1fr]">
        <AdminPrefetcher />
        {/* Live event bridge — subscribes to /ws/admin and invalidates the
            relevant React Query keys whenever a position closes / deposit
            lands / withdrawal is requested / KYC is submitted. Replaces the
            F5-after-every-action workflow with real-time updates across
            every open admin/broker tab. */}
        <AdminWsBridge />
        <AdminSidebar />
        {/* Mobile-only drawer + bottom bar. Both are `md:hidden` and
            mutually exclusive with the desktop `<AdminSidebar/>` so
            desktop layout is unchanged. */}
        <AdminMobileDrawer />
        {/* `min-w-0` is load-bearing here. The parent grid uses
            `md:grid-cols-[auto_1fr]`; without `min-w-0` on the right
            column, a wide child (e.g. the Positions blotter with 15+
            columns) forces the column to grow to fit its content's
            intrinsic width — pushing the sidebar off-screen and making
            the WHOLE page scroll horizontally instead of just the
            table. With it, the column happily shrinks below content
            width and the DataTable's own `overflow-auto` contains the
            horizontal scroll. */}
        <div className="flex min-h-screen min-w-0 flex-col">
          <AdminTopBar />
          <main className="flex-1 overflow-y-auto overflow-x-hidden bg-background scrollbar-thin">
            {/* `pb-20 md:pb-0` reserves 80px at the bottom on phones so
                the fixed `<AdminBottomBar/>` (56px + safe-area) never
                covers page content. Desktop keeps zero bottom padding. */}
            <div className="mx-auto max-w-screen-2xl p-3 pb-20 sm:p-4 md:p-6 md:pb-6">
              {children}
            </div>
          </main>
        </div>
        <AdminBottomBar />
      </div>
    </MobileNavProvider>
  );
}
