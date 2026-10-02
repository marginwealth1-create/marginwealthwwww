"use client";

import * as React from "react";
import { useAdminAuthStore } from "@/stores/authStore";
import { STORAGE_KEYS } from "@/lib/constants";

/**
 * Background helper that keeps the admin signed in across days without
 * surprise logouts.
 *
 * What it does:
 *   1. On mount + every 5 minutes — calls `refreshMe()` which goes
 *      through the API interceptor and silently rotates the access
 *      token via the refresh token when it's close to expiry.
 *   2. On `visibilitychange` (tab becomes visible) AND `focus` — does
 *      the same, so when an admin reopens the PWA after sitting in
 *      another app for hours, the very first interaction already has
 *      a fresh token (no race against the 401 → refresh → retry
 *      cycle, which on a flaky mobile network sometimes feels like a
 *      forced logout).
 *
 * Renders nothing — pure side-effects keyed off the auth store.
 *
 * NOTE: This is COMPLEMENTARY to the in-flight `isExpiringSoon` rotation
 * inside `lib/api.ts`. That one runs per-request; this one runs
 * per-session so a long-idle PWA still gets a refreshed token before
 * the next user action.
 */
export function SessionKeepAlive() {
  const refreshMe = useAdminAuthStore((s) => s.refreshMe);

  React.useEffect(() => {
    if (typeof window === "undefined") return;

    function hasRefresh() {
      return !!window.localStorage.getItem(STORAGE_KEYS.refreshToken);
    }

    // Initial sync — picks up any permission changes since last visit.
    if (hasRefresh()) {
      refreshMe();
    }

    // Re-sync whenever the tab becomes visible / focused.
    const onVisible = () => {
      if (document.visibilityState === "visible" && hasRefresh()) {
        refreshMe();
      }
    };
    const onFocus = () => {
      if (hasRefresh()) refreshMe();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onFocus);

    // Periodic refresh — 5 minutes. Cheap (single GET /auth/me) and
    // guarantees we keep the access token rotated even when the
    // dashboard is left open and idle (e.g. risk monitor on a
    // wall-mounted screen overnight).
    const id = window.setInterval(
      () => {
        if (hasRefresh() && document.visibilityState !== "hidden") {
          refreshMe();
        }
      },
      5 * 60 * 1000,
    );

    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onFocus);
      window.clearInterval(id);
    };
  }, [refreshMe]);

  return null;
}
