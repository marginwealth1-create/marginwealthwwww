export const APP_NAME = process.env.NEXT_PUBLIC_APP_NAME ?? "MarginWealth";
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/** WebSocket origin for the live tick feed (`/ws/marketdata`) and the
 *  user event bridge (`/ws/user`). We derive it from `API_URL` when
 *  `NEXT_PUBLIC_WS_URL` isn't set — this is the single most common
 *  production-only bug: ops sets `NEXT_PUBLIC_API_URL=https://api.marginwealth.live`
 *  but forgets `NEXT_PUBLIC_WS_URL`, and the build embeds the hardcoded
 *  `ws://localhost:8000` fallback. The browser then tries to open a WS to
 *  the user's own machine from a public origin and the connection fails
 *  silently — quotes appear in the chart (REST `/quote` polling) but the
 *  instruments panel and positions strip show "—" because they depend on
 *  the tick stream.
 *
 *  Auto-derivation: `http://` → `ws://`, `https://` → `wss://`. An
 *  explicit `NEXT_PUBLIC_WS_URL` always wins so a split API/WS host (e.g.
 *  separate `ws.marginwealth.live` later) still works without changing this file. */
function _deriveWsFromApi(api: string): string {
  if (api.startsWith("https://")) return "wss://" + api.slice("https://".length);
  if (api.startsWith("http://")) return "ws://" + api.slice("http://".length);
  return "ws://localhost:8000";
}
export const WS_URL = process.env.NEXT_PUBLIC_WS_URL ?? _deriveWsFromApi(API_URL);

export const ROUTES = {
  login: "/login",
  register: "/register",
  forgotPassword: "/forgot-password",
  twoFa: "/2fa",
  dashboard: "/dashboard",
  marketwatch: "/marketwatch",
  orders: "/positions",
  positions: "/positions",
  wallet: "/wallet",
  ledger: "/ledger",
  reportsPnl: "/reports/pnl",
  reportsTradebook: "/reports/tradebook",
  reportsBrokerage: "/reports/brokerage",
  reportsMargin: "/reports/margin",
  alerts: "/alerts",
  notifications: "/notifications",
  profile: "/profile",
  chart: (symbol: string) => `/chart/${symbol}`,
} as const;

export const STORAGE_KEYS = {
  accessToken: "nb.accessToken",
  refreshToken: "nb.refreshToken",
  user: "nb.user",
  // Referral code captured from a shared ?ref= link. Persisted so the
  // admin/broker/sub-broker attribution survives a demo-login → logout →
  // register round-trip (the URL query is lost across those hops).
  // Deliberately NOT cleared by clearTokens() — only after a successful
  // registration consumes it.
  referralCode: "nb.ref",
} as const;

export const ROLES = {
  SUPER_ADMIN: "SUPER_ADMIN",
  ADMIN: "ADMIN",
  MASTER: "MASTER",
  DEALER: "DEALER",
  CLIENT: "CLIENT",
} as const;
