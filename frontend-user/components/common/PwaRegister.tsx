"use client";

import { useEffect } from "react";

/**
 * Mounts the service worker exactly once on first render of the app
 * shell. Also wires a one-shot listener that stashes the
 * `beforeinstallprompt` event onto `window.__mpInstallPrompt` so the
 * <InstallPwaButton> (or any other affordance) can trigger it later
 * without owning the listener itself.
 *
 * Deliberately a stand-alone client component with no UI so it can be
 * dropped into the root layout without forcing the whole tree to be
 * client-rendered.
 */
export function PwaRegister() {
  useEffect(() => {
    if (typeof window === "undefined") return;

    // ── Service worker registration ─────────────────────────────────
    if ("serviceWorker" in navigator) {
      const isDev = process.env.NODE_ENV === "development";

      if (isDev) {
        // In dev mode, unregister any existing SW to prevent it from
        // caching stale webpack chunks (which causes "Cannot read
        // properties of undefined (reading 'call')" on every page).
        navigator.serviceWorker.getRegistrations().then((regs) => {
          regs.forEach((r) => r.unregister());
        });
        // Also clear SW caches so stale chunks are gone immediately.
        if ("caches" in window) {
          caches.keys().then((keys) => {
            keys.forEach((k) => caches.delete(k));
          });
        }
      } else {
        const idle = (cb: () => void) =>
          ("requestIdleCallback" in window
            ? (window as any).requestIdleCallback(cb)
            : setTimeout(cb, 1000));
        idle(() => {
          navigator.serviceWorker
            .register("/sw.js", { scope: "/" })
            .catch(() => {});
        });
      }
    }

    // ── Install prompt capture ─────────────────────────────────────
    const onBeforeInstall = (e: Event) => {
      // Prevent the browser's mini-infobar — we surface our own
      // button.
      e.preventDefault();
      (window as any).__mpInstallPrompt = e;
      window.dispatchEvent(new CustomEvent("mp:install-available"));
    };
    const onInstalled = () => {
      (window as any).__mpInstallPrompt = null;
      window.dispatchEvent(new CustomEvent("mp:installed"));
    };
    window.addEventListener("beforeinstallprompt", onBeforeInstall);
    window.addEventListener("appinstalled", onInstalled);
    return () => {
      window.removeEventListener("beforeinstallprompt", onBeforeInstall);
      window.removeEventListener("appinstalled", onInstalled);
    };
  }, []);

  return null;
}
