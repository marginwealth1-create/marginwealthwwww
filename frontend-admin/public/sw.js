/* MarginWealth Admin — minimal notification-only service worker.
 *
 * Why this file exists: Android Chrome installed PWAs refuse to show
 * `new Notification(...)` in the system tray. They require a service
 * worker and `registration.showNotification(...)`. Desktop browsers
 * work either way, but the operator's PWA on a phone was silently
 * dropping every tray push until this SW landed.
 *
 * What it DOESN'T do: intercept fetches, cache assets, run background
 * sync. We're not building an offline app — the Next.js HTML stays
 * uncached (next.config Cache-Control: no-store) so a fresh build is
 * always picked up on next open. Adding a fetch handler here would
 * silently break that, so the SW is deliberately empty of cache logic.
 *
 * Activation: `skipWaiting` + `clients.claim` so a new SW takes over
 * immediately on the next page load instead of waiting for every tab
 * to close. The `message` handler lets the page request an explicit
 * `tray` notification via `postMessage` — used by `notify-sound.ts`'s
 * `showNativeNotification()` shim when running inside a PWA.
 */

self.addEventListener("install", () => {
  // Activate the new SW the moment it finishes installing instead of
  // waiting for existing tabs to close. Stale-code-after-deploy is
  // exactly the bug we're trying to avoid.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  // Take control of any open clients (the installed PWA shell) so
  // subsequent showNotification() calls reach OUR worker, not the
  // previous version.
  event.waitUntil(self.clients.claim());
});

// Page → SW bridge for showing a tray notification. The page sends
// { type: "notify", title, body, tag, url } via postMessage; the SW
// fires the OS notification on its behalf because PWAs only accept
// SW-originated notifications.
self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type !== "notify") return;
  const title = String(data.title || "MarginWealth");
  const body = String(data.body || "");
  const tag = data.tag || undefined;
  const url = data.url || "/";
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon: "/icon-192.png",
      badge: "/icon-192.png",
      tag,
      renotify: true,
      data: { url },
    })
  );
});

// Web Push handler — fires when the backend sends a webpush message
// via VAPID. This is the path that wakes the SW even when the PWA
// has been force-stopped and the phone is locked.
self.addEventListener("push", (event) => {
  let payload = { title: "MarginWealth", body: "", url: "/", tag: undefined };
  try {
    if (event.data) payload = { ...payload, ...event.data.json() };
  } catch {
    // Some push services deliver plain text — fall back to it.
    try {
      const text = event.data && event.data.text();
      if (text) payload.body = String(text);
    } catch {}
  }
  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      icon: "/icon-192.png",
      badge: "/icon-192.png",
      tag: payload.tag,
      renotify: true,
      data: { url: payload.url || "/" },
    })
  );
});

// Tap on tray notification → focus an existing PWA window if there's
// one, otherwise open a fresh window at the target URL.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if (w.url.includes(url) && "focus" in w) return w.focus();
      }
      if (wins.length > 0 && "focus" in wins[0]) {
        // Same-origin window exists but on a different route — navigate
        // it instead of opening a second copy.
        const w = wins[0];
        if ("navigate" in w) w.navigate(url);
        return w.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
    })
  );
});
