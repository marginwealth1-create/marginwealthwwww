/* MarginWealth PWA service worker — offline-shell v2.
 *
 * Why we have one:
 *   • A live trading app must NEVER serve stale prices/orders, so
 *     /api/* and /ws/* always go straight to the network with no
 *     interception (pass-through return on those routes).
 *   • But the *app shell* (Next.js JS chunks, CSS, fonts, icons,
 *     manifest) is content-hashed and immutable — caching it makes
 *     repeat opens instant and, more importantly, makes the app
 *     *open at all* when the user has no signal.
 *   • When a navigation request fails (no network), we fall back to
 *     a precached `/offline.html` so the browser shows our branded
 *     "you're offline" screen instead of the device's grey
 *     dinosaur / "no internet" page. That's the difference between
 *     "app feels broken" and "app told me what's going on".
 *
 * Strategy summary:
 *   /api/*, /ws/*, ws/wss   → bypass (never cached, never intercepted)
 *   /_next/static/*         → cache-first (immutable hashed assets)
 *   /_next/image*           → stale-while-revalidate
 *   navigations (HTML)      → network-first with a 2 s timeout: a slow
 *                             or offline network instantly paints the
 *                             last cached shell (APK-like cold open)
 *                             while the network response still refreshes
 *                             the cache in the background; falls back to
 *                             /offline.html when there is no cache at all
 *   other GETs              → stale-while-revalidate, fall through
 *                             to /offline.html on navigation only
 *
 * Bumping VERSION evicts every old runtime cache on activate, so a
 * single deploy is enough to migrate users off broken caches if a
 * regression ships. NEVER reuse an old version string.
 */

const VERSION = "marginwealth-pwa-v9";
// How long a navigation waits for the network before it paints the last
// cached shell instead. Short enough to feel instant on weak networks,
// long enough that a healthy connection almost always wins the race and
// serves fresh HTML. The in-flight network request keeps running and
// refreshes the page cache regardless, so the NEXT open is up to date.
const NAV_NET_TIMEOUT_MS = 2000;
const PRECACHE = `${VERSION}-precache`;
const RUNTIME_STATIC = `${VERSION}-static`;
const RUNTIME_PAGES = `${VERSION}-pages`;

// Files we ALWAYS want available offline. Keep this list minimal —
// every byte here ships on first visit and on every SW update.
const PRECACHE_URLS = [
  "/offline.html",
  "/manifest.webmanifest",
  "/icon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(PRECACHE);
      // `addAll` is atomic — if any URL 404s the whole install
      // rejects, which is exactly what we want (a half-installed SW
      // is worse than no SW at all). Best-effort per-URL fetch keeps
      // the install resilient if e.g. /icon.svg gets renamed.
      await Promise.all(
        PRECACHE_URLS.map(async (url) => {
          try {
            const resp = await fetch(url, { cache: "no-store" });
            if (resp.ok) await cache.put(url, resp.clone());
          } catch {
            /* skip — non-fatal */
          }
        })
      );
      // Activate immediately on first install so the offline shell
      // is available without requiring a second reload.
      await self.skipWaiting();
    })()
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      // Drop every cache that isn't part of the current VERSION so
      // a deploy doesn't leave megabytes of dead chunks behind.
      const keys = await caches.keys();
      await Promise.all(
        keys
          .filter((k) => !k.startsWith(VERSION))
          .map((k) => caches.delete(k))
      );
      // Take control of every open tab right now so users on a stale
      // SW pick up the new caching rules without a full reload.
      await self.clients.claim();
    })()
  );
});

// ── Helpers ────────────────────────────────────────────────────────
function isApiOrWs(url) {
  return (
    url.pathname.startsWith("/api/") ||
    url.pathname.startsWith("/ws/") ||
    url.protocol === "ws:" ||
    url.protocol === "wss:"
  );
}

async function networkFirstNavigation(req) {
  // Cache the most-recent successful navigation so the user can at
  // least see the LAST page they visited if they re-open while
  // offline. Trading data inside that page will be missing (React
  // Query reconnects when navigator.onLine flips) but the shell,
  // sidebar, and last cached lists stay readable.
  //
  // Strategy: network-first WITH a timeout. We always kick off the live
  // fetch (and let it refresh the page cache when it lands), but if we
  // already have a cached shell AND the network hasn't answered within
  // NAV_NET_TIMEOUT_MS, we paint the cached shell immediately so the app
  // opens instantly on weak mobile networks — the APK-like cold open the
  // operator asked for. On a healthy connection the network almost always
  // wins the race, so fresh HTML is still the common case.
  //
  // Stale-code safety: the page cache is VERSION-scoped (RUNTIME_PAGES),
  // so a deploy that bumps VERSION wipes it on activate. Right after an
  // update there is therefore NO cached shell to serve and the first
  // navigation waits for the network (fresh). A cached shell only ever
  // exists once a fresh load on the current VERSION has succeeded, at
  // which point it references chunks that are already cache-first stored.
  const pages = await caches.open(RUNTIME_PAGES);

  const networkPromise = fetch(req)
    .then((fresh) => {
      if (fresh && fresh.ok) pages.put(req, fresh.clone()).catch(() => {});
      return fresh;
    })
    .catch(() => null);

  const cached = await pages.match(req);

  if (cached) {
    // Race the live fetch against a short timeout. A truthy, non-sentinel
    // winner means the network answered in time → prefer fresh HTML.
    const TIMEOUT = Symbol("nav-timeout");
    const timeout = new Promise((resolve) =>
      setTimeout(() => resolve(TIMEOUT), NAV_NET_TIMEOUT_MS)
    );
    const winner = await Promise.race([networkPromise, timeout]);
    if (winner && winner !== TIMEOUT) return winner;
    // Slow or failed network → instant cached shell. The networkPromise
    // above is still in flight and will refresh the cache for next time.
    return cached;
  }

  // No cached shell yet (first-ever open, or first open after a VERSION
  // bump): wait for the network, then fall back to the offline shell.
  const fresh = await networkPromise;
  if (fresh) return fresh;
  const offline = await caches.match("/offline.html");
  if (offline) return offline;
  return new Response("Offline", { status: 503, statusText: "Offline" });
}

async function cacheFirstStatic(req) {
  const cache = await caches.open(RUNTIME_STATIC);
  const hit = await cache.match(req);
  if (hit) return hit;
  try {
    const resp = await fetch(req);
    if (resp && resp.ok) cache.put(req, resp.clone()).catch(() => {});
    return resp;
  } catch {
    if (hit) return hit;
    throw new Error("offline-static");
  }
}

async function staleWhileRevalidate(req) {
  const cache = await caches.open(RUNTIME_STATIC);
  const cached = await cache.match(req);
  const fetchPromise = fetch(req)
    .then((resp) => {
      if (resp && resp.ok) cache.put(req, resp.clone()).catch(() => {});
      return resp;
    })
    .catch(() => null);
  return cached || (await fetchPromise) || Response.error();
}

// ── Fetch handler ──────────────────────────────────────────────────
self.addEventListener("fetch", (event) => {
  const req = event.request;
  // Only intercept GET — POST/PUT/DELETE/PATCH go straight to the
  // network so order placements / settlement actions are never
  // touched by the SW.
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // 1) Trading data: never intercept. Live ticks, REST API,
  //    auth/refresh, everything dynamic falls through to the
  //    network with full fidelity.
  if (isApiOrWs(url)) return;

  // Only handle same-origin requests. Cross-origin (CDNs, analytics,
  // third-party fonts) goes through the browser's default loader.
  if (url.origin !== self.location.origin) return;

  // 2) Top-level navigation requests (HTML).
  if (req.mode === "navigate") {
    event.respondWith(networkFirstNavigation(req));
    return;
  }

  // 3) Hashed Next.js chunks — immutable, cache-first.
  //    /charting_library/* is the TradingView bundle (standalone.js + its
  //    lazily-loaded sub-chunks/fonts, several MB total). It's versioned,
  //    immutable static content served from /public, so cache-first makes the
  //    FIRST-of-session chart open the only slow one — every subsequent
  //    terminal open serves the whole library from Cache Storage instantly
  //    (APK-like) and works offline. This is the biggest single "chart opens
  //    fast" win. PagePrewarmer downloads standalone.js on dashboard idle so
  //    even that first open is usually warm.
  if (
    url.pathname.startsWith("/_next/static/") ||
    url.pathname.startsWith("/charting_library/") ||
    url.pathname.startsWith("/icons/") ||
    url.pathname === "/icon.svg" ||
    url.pathname === "/manifest.webmanifest"
  ) {
    event.respondWith(cacheFirstStatic(req));
    return;
  }

  // 4) Everything else (images, public assets, _next/image): SWR.
  if (req.destination === "image" || req.destination === "font" || req.destination === "style") {
    event.respondWith(staleWhileRevalidate(req));
    return;
  }

  // 5) Default — pass-through with a graceful offline fallback.
  event.respondWith(
    fetch(req).catch(async () => {
      const cached = await caches.match(req);
      return cached || Response.error();
    })
  );
});

// ── One-shot message handler so the page can ping the SW ───────────
// e.g. a "Force update" button in Settings posts {type:"SKIP_WAITING"}
// and reloads. Also handles a `notify` message — the UserWsBridge
// posts the title/body/tag/url here when a wallet event lands, and
// the SW fires the OS notification on its behalf. PWAs on Android
// silently drop `new Notification(...)` called from a page; the only
// path that surfaces in the system tray is
// `ServiceWorkerRegistration.showNotification(...)`, which is why we
// proxy through here.
self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type === "SKIP_WAITING") {
    self.skipWaiting();
    return;
  }
  if (data.type === "notify") {
    const title = String(data.title || "MarginWealth");
    const body = String(data.body || "");
    // renotify:true REQUIRES a non-empty tag — passing renotify with an
    // empty/undefined tag makes showNotification throw a TypeError and the
    // notification silently never shows. Always give it a tag.
    const tag = data.tag || `mp-${Date.now()}`;
    const url = data.url || "/";
    event.waitUntil(
      self.registration.showNotification(title, {
        body,
        icon: "/icons/icon-192.png",
        badge: "/icons/icon-192.png",
        tag,
        renotify: true,
        data: { url },
      })
    );
  }
});

// Web Push handler — fires when the backend sends a webpush message
// via VAPID. This is the path that wakes the SW even when the PWA
// has been force-stopped and the phone is locked.
self.addEventListener("push", (event) => {
  let payload = { title: "MarginWealth", body: "", url: "/", tag: undefined };
  try {
    if (event.data) payload = { ...payload, ...event.data.json() };
  } catch {
    try {
      const text = event.data && event.data.text();
      if (text) payload.body = String(text);
    } catch {}
  }
  // renotify:true REQUIRES a non-empty tag — otherwise showNotification
  // throws a TypeError and the tray notification silently never appears
  // (this was why server pushes returned 201 but nothing showed on the
  // device). Fall back to a unique per-message tag when the payload has none.
  const tag = payload.tag || `mp-${Date.now()}`;
  event.waitUntil(
    self.registration.showNotification(payload.title || "MarginWealth", {
      body: payload.body || "",
      icon: "/icons/icon-192.png",
      badge: "/icons/icon-192.png",
      tag,
      renotify: true,
      data: { url: payload.url || "/" },
    })
  );
});

// Tap on tray notification → focus an existing PWA window, otherwise
// open a fresh one. Notification.data.url tells us where to go.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if (w.url.includes(url) && "focus" in w) return w.focus();
      }
      if (wins.length > 0 && "focus" in wins[0]) {
        const w = wins[0];
        if ("navigate" in w) w.navigate(url);
        return w.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
    })
  );
});
