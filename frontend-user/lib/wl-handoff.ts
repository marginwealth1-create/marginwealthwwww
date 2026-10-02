/**
 * White-label cross-origin session handoff helpers.
 *
 * When a user who originally signed up via a branded URL lands on the
 * platform host (`marginwealth.com`) and logs in, the BrandingProvider
 * builds a `#wl=<base64>` hash and redirects them to the admin's
 * `custom_domain`. The destination page consumes the hash on first
 * paint, writes the tokens into localStorage, strips the hash from
 * the URL, and continues like a normal session.
 *
 * The hash is the only viable cross-origin transport here:
 *   - Cookies don't cross domains.
 *   - localStorage is per-origin.
 *   - Querystring would land in server access logs / referrer headers.
 *   - URL fragments are NEVER sent to the server, so the access token
 *     stays on the client.
 *
 * Tokens are short-lived JWTs (15 min by default) — even if leaked
 * via clipboard / browser history, the blast radius is bounded. The
 * refresh token rotates on every use server-side.
 *
 * Both functions are pure, idempotent, and safe to call on every
 * page load. They no-op gracefully on the server (SSR) and on
 * browsers without localStorage.
 */

import { STORAGE_KEYS } from "./constants";

const HANDOFF_KEY = "wl";
// 60 s grace window — tokens older than this are rejected (clock
// skew + transit budget). Prevents replay if a user accidentally
// pastes the URL somewhere later.
const HANDOFF_MAX_AGE_MS = 60_000;

type HandoffPayload = {
  v: 1;
  t: number; // issuedAt unix ms
  a: string; // access token
  r: string; // refresh token
  u?: unknown; // optional cached user blob
};

function isBrowser(): boolean {
  return typeof window !== "undefined" && typeof document !== "undefined";
}

function safeStorageGet(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}
function safeStorageSet(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* quota / privacy mode */
  }
}

function b64UrlEncode(s: string): string {
  // utf-8 → base64 → url-safe (RFC 4648 §5).
  const bytes = new TextEncoder().encode(s);
  let bin = "";
  bytes.forEach((b) => {
    bin += String.fromCharCode(b);
  });
  const b64 = typeof btoa === "function" ? btoa(bin) : "";
  return b64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64UrlDecode(s: string): string | null {
  try {
    const b64 = s.replace(/-/g, "+").replace(/_/g, "/");
    const padded = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
    const bin = typeof atob === "function" ? atob(padded) : "";
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new TextDecoder().decode(bytes);
  } catch {
    return null;
  }
}

/**
 * Build a `#wl=<encoded>` hash from the current session. Returns
 * `null` if there's no session to hand off (no access OR refresh
 * token in localStorage).
 */
export function buildWlSessionHash(): string | null {
  if (!isBrowser()) return null;
  const access = safeStorageGet(STORAGE_KEYS.accessToken);
  const refresh = safeStorageGet(STORAGE_KEYS.refreshToken);
  if (!access || !refresh) return null;
  let userJson: unknown = undefined;
  try {
    const raw = safeStorageGet(STORAGE_KEYS.user);
    if (raw) userJson = JSON.parse(raw);
  } catch {
    /* ignore — handoff still works without the cached user blob */
  }
  const payload: HandoffPayload = {
    v: 1,
    t: Date.now(),
    a: access,
    r: refresh,
    u: userJson,
  };
  return `#${HANDOFF_KEY}=${b64UrlEncode(JSON.stringify(payload))}`;
}

/**
 * Detect a `#wl=...` fragment, decode it, write the tokens to
 * localStorage, and strip the fragment from the URL. Idempotent —
 * subsequent calls on the same page see no fragment and no-op.
 *
 * Returns `true` if a session was successfully consumed (callers can
 * use this to skip an extra "fetch /me" round-trip). `false` for
 * "nothing to consume / invalid".
 */
export function consumeWlSessionHandoff(): boolean {
  if (!isBrowser()) return false;
  const hash = window.location.hash || "";
  if (!hash.startsWith(`#${HANDOFF_KEY}=`)) return false;
  const encoded = hash.slice(HANDOFF_KEY.length + 2);
  const json = b64UrlDecode(encoded);
  if (!json) {
    _stripHash();
    return false;
  }
  let payload: HandoffPayload | null = null;
  try {
    payload = JSON.parse(json) as HandoffPayload;
  } catch {
    payload = null;
  }
  if (!payload || payload.v !== 1 || !payload.a || !payload.r) {
    _stripHash();
    return false;
  }
  if (
    typeof payload.t !== "number" ||
    Math.abs(Date.now() - payload.t) > HANDOFF_MAX_AGE_MS
  ) {
    _stripHash();
    return false;
  }
  safeStorageSet(STORAGE_KEYS.accessToken, payload.a);
  safeStorageSet(STORAGE_KEYS.refreshToken, payload.r);
  if (payload.u !== undefined) {
    try {
      safeStorageSet(STORAGE_KEYS.user, JSON.stringify(payload.u));
    } catch {
      /* ignore */
    }
  }
  _stripHash();
  return true;
}

function _stripHash(): void {
  try {
    const url = window.location.pathname + window.location.search;
    window.history.replaceState(null, "", url);
  } catch {
    /* ignore — old browsers */
  }
}
