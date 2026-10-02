"use client";

/**
 * Soft two-tone "ding" for the user-facing live notifications (deposit
 * approved, withdrawal processed, admin Add/Deduct Fund). Pure Web
 * Audio — no asset file to ship. Mirrors the admin app's
 * `lib/notify-sound.ts` so the two surfaces sound identical.
 *
 * No-ops silently when the browser blocks audio until the first user
 * gesture, so the toast still pops even when the chime can't play.
 */

let _ctx: AudioContext | null = null;

function getCtx(): AudioContext | null {
  if (typeof window === "undefined") return null;
  try {
    const AC =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext?: typeof AudioContext })
        .webkitAudioContext;
    if (!AC) return null;
    if (!_ctx) _ctx = new AC();
    if (_ctx.state === "suspended") void _ctx.resume();
    return _ctx;
  } catch {
    return null;
  }
}

/**
 * Native OS notification (Android tray / Windows action center / macOS
 * banner). Falls through silently when the browser hasn't been granted
 * permission, so the in-app toast is still the source of truth.
 * Mirrors the admin app's helper.
 */
export function showNativeNotification(
  title: string,
  body: string,
  opts?: { onClick?: () => void; tag?: string; url?: string },
): void {
  if (typeof window === "undefined" || typeof Notification === "undefined") return;
  if (Notification.permission !== "granted") return;
  // Prefer the service worker path — Android Chrome PWAs silently drop
  // `new Notification(...)` called directly from the page; only
  // `ServiceWorkerRegistration.showNotification()` actually surfaces
  // in the system tray. We postMessage the SW with the payload and let
  // it call showNotification on our behalf (see public/sw.js).
  // Falls back to direct Notification for desktop browsers where the
  // SW registration might not be ready yet (the SW registers on idle).
  const url = opts?.url ?? window.location.pathname;
  if (
    "serviceWorker" in navigator &&
    navigator.serviceWorker.controller
  ) {
    try {
      navigator.serviceWorker.controller.postMessage({
        type: "notify",
        title,
        body,
        tag: opts?.tag,
        url,
      });
      return;
    } catch {
      // fall through to direct Notification
    }
  }
  try {
    const n = new Notification(title, {
      body,
      icon: "/icons/icon-192.png",
      badge: "/icons/icon-192.png",
      tag: opts?.tag,
      renotify: true,
      requireInteraction: false,
      silent: false,
    } as NotificationOptions);
    n.onclick = () => {
      window.focus();
      try { opts?.onClick?.(); } catch {}
      n.close();
    };
  } catch {}
}

/** Ask the OS for notification permission once. Idempotent. */
export async function ensureNotificationPermission(): Promise<boolean> {
  if (typeof window === "undefined" || typeof Notification === "undefined") return false;
  if (Notification.permission === "granted") return true;
  if (Notification.permission === "denied") return false;
  try {
    const res = await Notification.requestPermission();
    return res === "granted";
  } catch {
    return false;
  }
}


// ── Web Push subscription (mirror of admin notify-sound.ts) ────────
function _urlBase64ToUint8(s: string): Uint8Array {
  const padded = s + "=".repeat((4 - (s.length % 4)) % 4);
  const b64 = padded.replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

/** Subscribe this PWA to backend-sent Web Push. Survives a
 *  force-stopped PWA so the trader still gets the "deposit approved"
 *  buzz with the phone locked. Best-effort. */
export async function subscribeForWebPush(): Promise<boolean> {
  try {
    if (typeof window === "undefined") return false;
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) return false;
    if (Notification.permission !== "granted") return false;

    const { PushAPI } = await import("@/lib/api");
    const { public_key } = await PushAPI.vapidKey();
    if (!public_key) return false;

    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: _urlBase64ToUint8(public_key).buffer as ArrayBuffer,
      });
    }

    const raw = sub.toJSON() as { endpoint?: string; keys?: { p256dh?: string; auth?: string } };
    if (!raw.endpoint || !raw.keys?.p256dh || !raw.keys?.auth) return false;
    await PushAPI.subscribe({
      endpoint: raw.endpoint,
      keys: { p256dh: raw.keys.p256dh, auth: raw.keys.auth },
      label: navigator.userAgent.slice(0, 80),
    });
    return true;
  } catch (e) {
    console.warn("[push] subscribe failed", e);
    return false;
  }
}

export function playNotifyPing(): void {
  const ctx = getCtx();
  if (!ctx) return;
  try {
    const now = ctx.currentTime;
    const gain = ctx.createGain();
    gain.connect(ctx.destination);
    gain.gain.setValueAtTime(0.0001, now);
    gain.gain.exponentialRampToValueAtTime(0.16, now + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.55);

    const notes: Array<[number, number]> = [
      [880, 0],
      [1320, 0.12],
    ];
    for (const [freq, at] of notes) {
      const osc = ctx.createOscillator();
      osc.type = "sine";
      osc.frequency.value = freq;
      osc.connect(gain);
      osc.start(now + at);
      osc.stop(now + at + 0.42);
    }
  } catch {
    // audio is best-effort
  }
}

/**
 * Speak a notification out loud via the browser's Speech Synthesis so an
 * admin broadcast lands like a "real" push — a voice announcement on top
 * of the toast + ping + tray popup. Best-effort:
 *   • no-ops when the API is missing or notifications are toggled off,
 *   • cancels any queued utterance so back-to-back broadcasts don't pile
 *     up into a long unstoppable monologue,
 *   • prefers an English voice when one is installed.
 * Some browsers gate speech behind a prior user gesture — in that case it
 * silently fails and the toast/ping still carry the message.
 */
/**
 * Browsers (especially mobile Chrome/Safari) block speechSynthesis until
 * the user has interacted with the page at least once. We "unlock" it by
 * speaking a near-silent empty utterance from within a real user gesture
 * (tap/click/keydown). After this runs once, later programmatic
 * `speakNotification()` calls work while the tab is in the foreground.
 * Also warms up getVoices() which loads asynchronously on some browsers.
 * Idempotent + self-detaching. */
let _speechPrimed = false;
export function primeVoiceOnFirstGesture(): (() => void) | void {
  if (typeof window === "undefined") return;
  const synth = (window as unknown as { speechSynthesis?: SpeechSynthesis })
    .speechSynthesis;
  if (!synth || typeof SpeechSynthesisUtterance === "undefined") return;
  const unlock = () => {
    if (_speechPrimed) return;
    try {
      synth.resume();
      const u = new SpeechSynthesisUtterance("");
      u.volume = 0;
      synth.speak(u);
      // Kick voices to load (Chrome returns [] until this or the
      // voiceschanged event fires).
      synth.getVoices();
      _speechPrimed = true;
    } catch {
      // best-effort
    }
    detach();
  };
  const detach = () => {
    window.removeEventListener("pointerdown", unlock);
    window.removeEventListener("keydown", unlock);
    window.removeEventListener("touchstart", unlock);
  };
  window.addEventListener("pointerdown", unlock, { once: false });
  window.addEventListener("keydown", unlock, { once: false });
  window.addEventListener("touchstart", unlock, { once: false });
  return detach;
}

export function speakNotification(text: string): void {
  if (typeof window === "undefined") return;
  const synth = (window as unknown as { speechSynthesis?: SpeechSynthesis })
    .speechSynthesis;
  if (!synth || typeof SpeechSynthesisUtterance === "undefined") return;
  const clean = String(text || "").trim();
  if (!clean) return;
  try {
    synth.cancel();
    const u = new SpeechSynthesisUtterance(clean);
    u.lang = "en-IN";
    u.rate = 1;
    u.pitch = 1;
    u.volume = 1;
    try {
      const voices = synth.getVoices();
      const en = voices.find((v) => /^en[-_]/i.test(v.lang));
      if (en) u.voice = en;
    } catch {}
    synth.speak(u);
  } catch {
    // speech is best-effort
  }
}

/** Read the persisted notification toggle (default ON). Mirror of the
 *  admin app's flag — keeps the user app silent if they flipped the
 *  switch in their profile. */
const NOTIFY_KEY = "user.notifications.enabled";
export function userNotificationsEnabled(): boolean {
  if (typeof window === "undefined") return true;
  const v = window.localStorage.getItem(NOTIFY_KEY);
  return v === null ? true : v === "1";
}

export function setUserNotificationsEnabled(v: boolean): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(NOTIFY_KEY, v ? "1" : "0");
  window.dispatchEvent(new StorageEvent("storage", { key: NOTIFY_KEY, newValue: v ? "1" : "0" }));
}
