"use client";

import { useEffect } from "react";

/**
 * Mounts the admin app's minimal service worker exactly once on first
 * render of the shell. The SW (public/sw.js) intentionally does NOT
 * cache anything — it exists only so we can call
 * `ServiceWorkerRegistration.showNotification()` from
 * `notify-sound.ts`, which is the ONLY notification path that surfaces
 * in the Android tray when the PWA is running standalone. Direct
 * `new Notification(...)` calls from the page get silently dropped on
 * Android Chrome PWAs.
 *
 * Dev mode: unregister any SW so webpack HMR isn't intercepted.
 */
export function PwaRegister() {
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!("serviceWorker" in navigator)) return;

    const isDev = process.env.NODE_ENV === "development";
    if (isDev) {
      navigator.serviceWorker.getRegistrations().then((regs) => {
        regs.forEach((r) => r.unregister());
      });
      return;
    }

    const idle = (cb: () => void) =>
      ("requestIdleCallback" in window
        ? (window as any).requestIdleCallback(cb)
        : setTimeout(cb, 1000));
    idle(() => {
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
    });
  }, []);

  return null;
}
