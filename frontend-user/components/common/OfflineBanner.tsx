"use client";

import { useEffect, useRef, useState } from "react";
import { WifiOff } from "lucide-react";

// Tunables for the debounced offline detection. The banner only appears
// after the network has been confirmed down for several CONSECUTIVE
// probes — so a momentary blip on a weak mobile connection never trips
// it — and disappears the instant a single probe (or the OS `online`
// event) succeeds.
const PROBE_URL = "/manifest.webmanifest";
const HEALTHY_INTERVAL_MS = 20_000; // relaxed cadence while online
const SUSPECT_INTERVAL_MS = 3_000; // fast cadence while confirming / recovering
const FAILS_TO_SHOW = 3; // ~3 × 3s ≈ 9s sustained offline before showing

/**
 * Sticky top banner that surfaces network state to the user.
 *
 * Two signals drive the visible state:
 *   1. `navigator.onLine` flips to false whenever the OS reports the
 *      device left the network (airplane mode, Wi-Fi off, mobile data
 *      lost). This is the immediate, kernel-level signal.
 *   2. A periodic ping to `/manifest.webmanifest` (cheap, cached on
 *      the SW so we don't add real bandwidth) catches the case where
 *      the device thinks it's online but the captive portal / hotspot
 *      / DNS poisoning is silently dropping every request. Without
 *      this, the user sees `navigator.onLine === true` and a perfectly
 *      blank dashboard — exactly the "net off me open bhi nahi hota"
 *      complaint.
 *
 * The banner is intentionally:
 *   • Top-of-viewport, non-dismissable while offline (it's a status
 *     read-out, not a notification).
 *   • Auto-hidden when the connection comes back so the user knows
 *     the app self-recovered.
 *   • Render-only — no API calls, no React Query churn. The actual
 *     reconnect is driven by React Query's `refetchOnReconnect` and
 *     the WS bridge's auto-reconnect; this component just informs.
 */
export function OfflineBanner() {
  const [offline, setOffline] = useState(false);
  // Live mirrors of state the polling closure needs without re-arming the
  // effect on every flip (the effect runs once for the component's life).
  const offlineRef = useRef(false);
  const failCountRef = useRef(0);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    if (typeof window === "undefined") return;

    let alive = true;

    const setOfflineState = (v: boolean) => {
      offlineRef.current = v;
      setOffline(v);
    };

    const schedule = (ms: number) => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
      timerRef.current = window.setTimeout(probe, ms);
    };

    // A probe failed (network error or non-OK). Count CONSECUTIVE failures;
    // only surface the banner once we cross the threshold so a single weak
    // -net blip never trips it. Keep probing fast so recovery is snappy.
    const onFailure = () => {
      failCountRef.current += 1;
      if (failCountRef.current >= FAILS_TO_SHOW && !offlineRef.current) {
        setOfflineState(true);
      }
      schedule(SUSPECT_INTERVAL_MS);
    };

    // A probe succeeded → we're online. Reset the counter, clear the banner
    // instantly if it was up, and relax back to the slow cadence.
    const onSuccess = () => {
      failCountRef.current = 0;
      if (offlineRef.current) setOfflineState(false);
      schedule(HEALTHY_INTERVAL_MS);
    };

    // The probe is the single source of truth — `navigator.onLine` only
    // nudges the cadence (below). A cheap HEAD against the SW-cached
    // manifest: free when healthy, fails fast when the network is wedged.
    const probe = async () => {
      try {
        const r = await fetch(PROBE_URL, { method: "HEAD", cache: "no-store" });
        if (!alive) return;
        if (r.ok) onSuccess();
        else onFailure();
      } catch {
        if (alive) onFailure();
      }
    };

    // OS-level events do NOT flip the banner directly (they flicker on weak
    // networks). They only adjust how fast we confirm: `offline` starts the
    // fast confirmation window, `online` triggers an immediate recovery probe.
    const onOnline = () => schedule(0);
    const onOffline = () => schedule(SUSPECT_INTERVAL_MS);
    window.addEventListener("online", onOnline);
    window.addEventListener("offline", onOffline);

    // Kick off the first probe immediately.
    schedule(0);

    return () => {
      alive = false;
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
      window.removeEventListener("online", onOnline);
      window.removeEventListener("offline", onOffline);
    };
  }, []);

  if (!offline) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      className="fixed inset-x-0 top-0 z-[60] flex items-center justify-center gap-2 border-b border-amber-500/40 bg-amber-500/15 px-4 py-2 text-xs font-medium text-amber-200 backdrop-blur-md md:text-sm"
    >
      <WifiOff className="h-4 w-4" aria-hidden />
      <span>
        You&rsquo;re offline. Live prices and orders are paused — the app will
        resume automatically once you&rsquo;re back online.
      </span>
    </div>
  );
}
