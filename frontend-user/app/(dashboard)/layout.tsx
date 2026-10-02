"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuthStore } from "@/stores/authStore";
import { ensureFreshAccessToken, isExpiringSoon } from "@/lib/api";
import { STORAGE_KEYS } from "@/lib/constants";
import { Sidebar } from "@/components/layout/Sidebar";
import { TopBar } from "@/components/layout/TopBar";
import { StatusBar } from "@/components/layout/StatusBar";
import { BottomNav } from "@/components/layout/BottomNav";
import { UserWsBridge } from "@/components/common/UserWsBridge";
import { OfflineBanner } from "@/components/common/OfflineBanner";
import { TermsGate } from "@/components/common/TermsGate";
import { PagePrewarmer } from "@/components/common/PagePrewarmer";

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const user = useAuthStore((s) => s.user);
  const hydrated = useAuthStore((s) => s.hydrated);

  useEffect(() => {
    if (hydrated && !user) router.replace("/login");
  }, [hydrated, user, router]);

  // ── Preflight + resume-from-background token refresh ───────────────
  // Dashboard children fire ~6 authenticated GETs in parallel on mount
  // (summary, positions, wallet, marketwatch, notifications, ...). If
  // the access token is already past its 24-h life when the user opens
  // the app the morning after, every one of those would 401 and trip
  // through the response interceptor. The cross-tab lock in lib/api.ts
  // serialises that recovery correctly, but a single preflight here is
  // cheaper AND eliminates the brief shimmer while six recoveries
  // settle. Same hook also re-runs when the tab becomes visible after
  // a long idle (phone resumed from pocket, laptop lid reopened) so
  // the FIRST tap after a long pause never sees a 401.
  useEffect(() => {
    if (!hydrated || !user) return;
    const refreshIfNeeded = () => {
      const tok =
        typeof window !== "undefined"
          ? window.localStorage.getItem(STORAGE_KEYS.accessToken)
          : null;
      if (!tok || isExpiringSoon(tok)) {
        // Fire-and-forget; ensureFreshAccessToken handles its own
        // dedup + cross-tab coordination. Failure here is non-fatal —
        // the response interceptor will still kick in if a child
        // request gets a 401.
        void ensureFreshAccessToken();
      }
    };
    refreshIfNeeded();
    const onVis = () => {
      if (document.visibilityState === "visible") refreshIfNeeded();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, [hydrated, user]);

  if (!hydrated) {
    return (
      <div className="grid h-screen place-items-center text-sm text-muted-foreground">Loading…</div>
    );
  }
  if (!user) return null;

  return (
    <div className="grid min-h-screen grid-cols-1 md:grid-cols-[auto_1fr]">
      <UserWsBridge />
      <PagePrewarmer />
      <OfflineBanner />
      <TermsGate />
      {/* Sidebar shows ≥ md only (already gated inside the component too). */}
      <Sidebar />
      <div className="flex min-h-screen flex-col">
        <TopBar />
        <main className="flex-1 overflow-y-auto bg-background scrollbar-thin">
          {/* Bottom-nav adds ~3.5rem of fixed height on mobile, so add safe
              bottom padding to the scroll area to prevent content clipping. */}
          <div className="mx-auto max-w-screen-2xl p-4 pb-24 md:p-6 md:pb-6">{children}</div>
        </main>
        <StatusBar />
        <BottomNav />
      </div>
    </div>
  );
}
