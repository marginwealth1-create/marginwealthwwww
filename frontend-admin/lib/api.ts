"use client";

import axios, { AxiosError, AxiosRequestConfig, InternalAxiosRequestConfig } from "axios";
import { ADMIN_API_KEY, API_URL, STORAGE_KEYS } from "./constants";
import type { AdminTokenPair, ApiResponse } from "@/types";

export const api = axios.create({
  baseURL: `${API_URL}/api/v1`,
  withCredentials: false,
  timeout: 30_000,
});

// Refresh dedup state. Per-tab promise + cross-tab Web Locks below.
let inFlightRefresh: Promise<string | null> | null = null;
const REFRESH_LOCK = "mp.admin.auth.refresh";
const REFRESH_MARGIN_SEC = 120;

function getAccessToken() {
  return typeof window !== "undefined" ? window.localStorage.getItem(STORAGE_KEYS.accessToken) : null;
}
function getRefreshToken() {
  return typeof window !== "undefined" ? window.localStorage.getItem(STORAGE_KEYS.refreshToken) : null;
}
export function setTokens(access: string, refresh: string) {
  window.localStorage.setItem(STORAGE_KEYS.accessToken, access);
  window.localStorage.setItem(STORAGE_KEYS.refreshToken, refresh);
}
export function clearTokens() {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(STORAGE_KEYS.accessToken);
  window.localStorage.removeItem(STORAGE_KEYS.refreshToken);
  window.localStorage.removeItem(STORAGE_KEYS.user);
}

// ── JWT exp decoder — read-only, no signature check. Used solely to
//    decide whether to rotate the access token *before* sending the
//    next request, so the dashboard never sees a 401 storm on cold
//    open after a 24-h-stale login.
function jwtExpSec(token: string | null): number | null {
  if (!token || typeof token !== "string") return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    const b64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
    const json = typeof atob === "function" ? atob(padded) : "";
    const obj = JSON.parse(json);
    return typeof obj?.exp === "number" ? obj.exp : null;
  } catch {
    return null;
  }
}
function isExpiringSoon(token: string | null, marginSec = REFRESH_MARGIN_SEC): boolean {
  const exp = jwtExpSec(token);
  if (exp == null) return false;
  return exp * 1000 - Date.now() < marginSec * 1000;
}
function isExpired(token: string | null): boolean {
  const exp = jwtExpSec(token);
  if (exp == null) return false;
  return exp * 1000 <= Date.now();
}

// Cross-tab exclusive lock: only one admin tab runs the rotating
// /refresh endpoint at a time. Other tabs queue, then re-read
// localStorage and inherit the new pair without burning their own
// refresh token. Prevents the morning-logout race the user reported.
async function withRefreshLock<T>(fn: () => Promise<T>): Promise<T> {
  if (
    typeof navigator !== "undefined" &&
    typeof (navigator as any).locks?.request === "function"
  ) {
    return (navigator as any).locks.request(REFRESH_LOCK, fn);
  }
  return fn();
}

/**
 * Three-state refresh outcome — see the user-side api.ts for the full
 * rationale. Short version: a network glitch or 5xx mid-refresh MUST
 * NOT log the admin out, because the refresh token may still be valid;
 * only an explicit 401/403 from the /refresh endpoint counts as a
 * sign-out signal.
 */
async function callRefreshEndpoint(): Promise<
  { kind: "ok"; access: string } | { kind: "auth_failed" | "transient" }
> {
  const refresh = getRefreshToken();
  if (!refresh) return { kind: "auth_failed" };
  try {
    const headers: Record<string, string> = {};
    if (ADMIN_API_KEY) headers["X-Admin-Api-Key"] = ADMIN_API_KEY;
    const r = await axios.post<ApiResponse<AdminTokenPair>>(
      `${API_URL}/api/v1/admin/auth/refresh`,
      { refresh_token: refresh },
      { headers, timeout: 15_000 }
    );
    const pair = r.data.data;
    if (!pair) return { kind: "transient" };
    setTokens(pair.access_token, pair.refresh_token);
    return { kind: "ok", access: pair.access_token };
  } catch (err) {
    const ax = err as AxiosError;
    const status = ax.response?.status;
    if (status === 401 || status === 403) {
      clearTokens();
      return { kind: "auth_failed" };
    }
    return { kind: "transient" };
  }
}

/**
 * Single entry point for getting a fresh admin access token. Dedups
 * within the tab via inFlightRefresh, across tabs via withRefreshLock,
 * and short-circuits if a sibling tab already wrote a fresh pair to
 * localStorage while we were queued.
 */
export async function ensureFreshAccessToken(): Promise<string | null> {
  inFlightRefresh ||= (async () => {
    try {
      return await withRefreshLock(async () => {
        const current = getAccessToken();
        if (current && !isExpiringSoon(current, 30)) return current;
        const r = await callRefreshEndpoint();
        return r.kind === "ok" ? r.access : null;
      });
    } finally {
      inFlightRefresh = null;
    }
  })();
  return inFlightRefresh;
}

api.interceptors.request.use(async (config: InternalAxiosRequestConfig) => {
  if (!config.headers) return config;
  if (ADMIN_API_KEY) config.headers["X-Admin-Api-Key"] = ADMIN_API_KEY;
  let tok = getAccessToken();
  // Proactive rotation — catches the access token before it expires
  // so the rest of the dashboard never sees a 401 cascade.
  if (tok && getRefreshToken() && isExpiringSoon(tok)) {
    const fresh = await ensureFreshAccessToken();
    if (fresh) tok = fresh;
  }
  if (tok) config.headers.Authorization = `Bearer ${tok}`;
  return config;
});

api.interceptors.response.use(
  (resp) => resp,
  async (error: AxiosError) => {
    const original = error.config as (AxiosRequestConfig & { _retry?: boolean }) | undefined;
    const status = error.response?.status;
    if (status === 401 && original && !original._retry) {
      original._retry = true;
      const newToken = await ensureFreshAccessToken();
      if (newToken) {
        original.headers = { ...(original.headers || {}), Authorization: `Bearer ${newToken}` };
        return api.request(original);
      }
      // Only redirect when the refresh path actually cleared the
      // tokens (explicit auth failure). Transient errors keep the
      // refresh around so the next request can retry without making
      // the admin re-enter credentials.
      const stillHaveRefresh = typeof window !== "undefined" && !!getRefreshToken();
      if (
        !stillHaveRefresh &&
        typeof window !== "undefined" &&
        !window.location.pathname.startsWith("/login")
      ) {
        window.location.href = "/login";
      }
    }
    return Promise.reject(error);
  }
);

// Re-export under the old name so existing imports keep working.
export const refreshAccessToken = ensureFreshAccessToken;
export { jwtExpSec, isExpired, isExpiringSoon };

export class ApiError extends Error {
  code: string;
  details?: Record<string, unknown>;
  constructor(message: string, code: string, details?: Record<string, unknown>) {
    super(message);
    this.code = code;
    this.details = details;
    this.name = "ApiError";
  }
}

export async function unwrap<T>(p: Promise<{ data: ApiResponse<T> }>): Promise<T> {
  try {
    const res = await p;
    if (!res.data?.success || res.data.data == null) {
      throw new ApiError(res.data?.message || "Unknown error", "UNKNOWN");
    }
    return res.data.data as T;
  } catch (err) {
    if (err instanceof ApiError) throw err;
    const ax = err as AxiosError<{ error?: { code?: string; message?: string; details?: Record<string, unknown> } }>;
    const e = ax.response?.data?.error;
    throw new ApiError(e?.message || ax.message || "Network error", e?.code || "NETWORK", e?.details);
  }
}

export const AdminAuthAPI = {
  login: (body: { identifier: string; password: string; two_fa_code?: string }) =>
    unwrap<AdminTokenPair>(api.post("/admin/auth/login", body)),
  // Separate portal for EMPLOYEE (staff) accounts — same token machinery,
  // backend accepts only role == EMPLOYEE here.
  employeeLogin: (body: { identifier: string; password: string; two_fa_code?: string }) =>
    unwrap<AdminTokenPair>(api.post("/admin/auth/employee-login", body)),
  refresh: (refresh_token: string) => unwrap<AdminTokenPair>(api.post("/admin/auth/refresh", { refresh_token })),
  logout: (refresh_token?: string) => unwrap<any>(api.post("/admin/auth/logout", { refresh_token })),
  me: () => unwrap<any>(api.get("/admin/auth/me")),
  changePassword: (current_password: string, new_password: string) =>
    unwrap<any>(api.post("/admin/auth/change-password", { current_password, new_password })),
};

// Employee (staff) management — available to every admin (super + sub) for
// their OWN employees. Mirrors ManagementAPI's sub-admin shape.
export const EmployeeMgmtAPI = {
  list: (params?: { q?: string; status?: string; page?: number; page_size?: number }) =>
    unwrap<any>(api.get("/admin/employees", { params })),
  create: (body: {
    full_name: string;
    email: string;
    mobile: string;
    password: string;
    permissions: Record<string, boolean>;
  }) => unwrap<any>(api.post("/admin/employees", body)),
  updatePermissions: (id: string, permissions: Record<string, boolean>) =>
    unwrap<any>(api.put(`/admin/employees/${id}/permissions`, { permissions })),
  block: (id: string) => unwrap<any>(api.post(`/admin/employees/${id}/block`)),
  unblock: (id: string) => unwrap<any>(api.post(`/admin/employees/${id}/unblock`)),
  resetPassword: (id: string, new_password: string) =>
    unwrap<any>(api.post(`/admin/employees/${id}/reset-password`, { new_password })),
  remove: (id: string) => unwrap<any>(api.delete(`/admin/employees/${id}`)),
};

export const DashboardAPI = {
  stats: () => unwrap<any>(api.get("/admin/dashboard/stats")),
  riskAlerts: () => unwrap<any[]>(api.get("/admin/dashboard/risk-alerts")),
};

export const UsersAPI = {
  list: (params?: any) => unwrap<{ items: any[]; meta: any }>(api.get("/admin/users", { params })),
  detail: (id: string) => unwrap<any>(api.get(`/admin/users/${id}`)),
  create: (body: any) => unwrap<any>(api.post("/admin/users", body)),
  update: (id: string, body: any) => unwrap<any>(api.put(`/admin/users/${id}`, body)),
  block: (id: string, reason?: string) => unwrap<any>(api.post(`/admin/users/${id}/block`, { reason })),
  unblock: (id: string) => unwrap<any>(api.post(`/admin/users/${id}/unblock`)),
  // Per-user auto-settlement toggle. ON (default): wallet auto-floors
  // at 0 + books shortfall to settlement_outstanding. OFF: wallet
  // allowed to go negative + a pending SettlementRequest queued for
  // admin approval (Payments → Settlement Requests).
  setAutoSettlement: (id: string, enabled: boolean) =>
    unwrap<any>(api.post(`/admin/users/${id}/auto-settlement`, { enabled })),
  // Per-user trade alert — pops a toast + plays `sound` in this admin's
  // panel whenever the user opens / closes a trade.
  setTradeAlert: (id: string, body: { enabled?: boolean; sound?: string }) =>
    unwrap<any>(api.post(`/admin/users/${id}/trade-alert`, body)),
  resetPassword: (id: string, new_password: string) =>
    unwrap<any>(api.post(`/admin/users/${id}/reset-password`, { new_password })),
  walletAdjust: (id: string, body: { amount: number; narration: string; transaction_type?: string }) =>
    unwrap<any>(api.post(`/admin/users/${id}/wallet-adjust`, body)),
  creditLimit: (id: string, body: { delta: number; narration: string }) =>
    api.patch(`/admin/users/${id}/credit-limit`, body).then((r) => r.data?.data ?? r.data),
  killSwitch: (id: string, reason?: string) =>
    api.post(`/admin/users/${id}/kill-switch`, { reason }).then((r) => r.data?.data ?? r.data),
  impersonate: (id: string) =>
    api.post(`/admin/users/${id}/impersonate`).then((r) => r.data?.data ?? r.data),
  delete: (id: string) => unwrap<any>(api.delete(`/admin/users/${id}`)),
  liveTradeStats: (id: string) =>
    unwrap<any>(api.get(`/admin/users/${id}/live-trade-stats`)),
  // Aggregated live snapshot of available balance + open P&L + equity
  // (available + open_pnl) across the given users. Polled by the
  // /users table so the OPEN P&L column updates at the same cadence
  // as the customer-side terminal. Pass the IDs visible on the current
  // page; the backend caps to 200 ids per call.
  liveStats: (user_ids?: string[]) =>
    unwrap<{
      items: Array<{
        user_id: string;
        available_balance: string;
        open_pnl: string;
        equity: string;
        used_margin: string;
        credit_limit: string;
      }>;
    }>(
      api.get("/admin/users/live-stats", {
        params: user_ids && user_ids.length > 0
          ? { user_ids: user_ids.join(",") }
          : undefined,
      }),
    ),
};

export const RiskAPI = {
  getGlobal: () => unwrap<any>(api.get("/admin/risk/global")),
  updateGlobal: (patch: any) => unwrap<any>(api.put("/admin/risk/global", { patch })),
  getUser: (userId: string) => unwrap<any>(api.get(`/admin/risk/user/${userId}`)),
  upsertUser: (userId: string, patch: any) =>
    unwrap<any>(api.put(`/admin/risk/user/${userId}`, { patch })),
  deleteUser: (userId: string) => unwrap<any>(api.delete(`/admin/risk/user/${userId}`)),
  copyFromUser: (userId: string, sourceUserId: string) =>
    unwrap<any>(api.post(`/admin/risk/user/${userId}/copy-from/${sourceUserId}`)),
  effective: (userId: string) => unwrap<any>(api.get(`/admin/risk/user/${userId}/effective`)),
  usersWithOverrides: () => unwrap<any[]>(api.get("/admin/risk/users-with-overrides")),
};

export const NettingAPI = {
  segments: () => unwrap<any[]>(api.get("/admin/netting/segments")),
  getSegment: (id: string) => unwrap<any>(api.get(`/admin/netting/segments/${id}`)),
  updateSegment: (id: string, patch: any) =>
    unwrap<any>(api.put(`/admin/netting/segments/${id}`, { patch })),
  scripts: (segment?: string) =>
    unwrap<any[]>(api.get("/admin/netting/scripts", { params: segment ? { segment } : {} })),
  createScript: (body: any) => unwrap<any>(api.post("/admin/netting/scripts", body)),
  // Bulk "Select all" — adds one override row per symbol in a single call.
  createScriptsBulk: (body: { segment_id: string; segment_name: string; symbols: string[] }) =>
    unwrap<{ created: number; total: number }>(api.post("/admin/netting/scripts/bulk", body)),
  updateScript: (id: string, patch: any) =>
    unwrap<any>(api.put(`/admin/netting/scripts/${id}`, { patch })),
  deleteScript: (id: string) => unwrap<any>(api.delete(`/admin/netting/scripts/${id}`)),
  userOverrides: (userId: string) => unwrap<any[]>(api.get(`/admin/netting/user/${userId}`)),
  // Per-segment camelCase values the user INHERITS (pool cascade below their
  // own override). Used to fill the User-Overrides cells' placeholder with the
  // currently-effective value instead of the word "inherit".
  userInherited: (userId: string) =>
    unwrap<Record<string, Record<string, any>>>(api.get(`/admin/netting/user/${userId}/effective`)),
  upsertUserOverride: (userId: string, segmentName: string, patch: any, symbol?: string) =>
    unwrap<any>(
      api.put(`/admin/netting/user/${userId}/${segmentName}`, { patch }, { params: symbol ? { symbol } : {} })
    ),
  deleteUserOverride: (userId: string, segmentName: string, symbol?: string) =>
    unwrap<any>(
      api.delete(`/admin/netting/user/${userId}/${segmentName}`, { params: symbol ? { symbol } : {} })
    ),
  /** Wipe ALL UserSegmentOverride rows for this user so they snap back
   *  to the inherited cascade (broker / admin / super-admin / platform
   *  defaults). Returns the count removed. */
  clearAllUserOverrides: (userId: string) =>
    unwrap<{ ok: boolean; deleted: number }>(
      api.delete(`/admin/netting/user/${userId}`),
    ),
  copy: (body: { source_user_id: string; target_user_ids: string[]; overwrite?: boolean }) =>
    unwrap<any>(api.post("/admin/netting/copy", body)),
  usersWithOverrides: (kind?: "segment" | "script") =>
    unwrap<any[]>(
      api.get("/admin/netting/users-with-overrides", { params: kind ? { kind } : {} }),
    ),
};

export const TradingAPI = {
  orders: (params?: any) => unwrap<{ items: any[]; meta: any }>(api.get("/admin/orders", { params })),
  // Today's trading summary (IST day) for the Orders monitor header —
  // executed BUY/SELL fill counts, their total, and the live pending count.
  ordersStats: () =>
    unwrap<{
      date_ist: string;
      total_trades: number;
      buy_trades: number;
      sell_trades: number;
      pending_orders: number;
    }>(api.get("/admin/orders/stats")),
  forceCancel: (id: string) => unwrap<any>(api.delete(`/admin/orders/${id}`)),
  positions: (params?: any) => unwrap<any[]>(api.get("/admin/positions", { params })),
  // Server-side paginated variant — pass `page` to get { rows, total, … }
  // instead of a flat array. Used by the Closed Trades tab so only one
  // page (e.g. 25 rows) is fetched + enriched per request.
  // House book: open positions folded per symbol (net / long / short, clients).
  exposure: (params?: any) => unwrap<any>(api.get("/admin/exposure", { params })),
  positionsPaged: (params?: any) =>
    unwrap<{ rows: any[]; total: number; page: number; page_size: number; total_pages: number }>(
      api.get("/admin/positions", { params }),
    ),
  // A user's FIFO closed blotter — the SAME per-opening-fill rows the user
  // sees in their own Closed history (one row per opening-fill × closing-fill
  // pairing), not one aggregated row per position. Per-user (`user_id` req).
  closedFifo: (params: { user_id: string; page?: number; page_size?: number }) =>
    unwrap<{ rows: any[]; total: number; page: number; page_size: number }>(
      api.get("/admin/positions/closed-fifo", { params }),
    ),
  orderQuotes: (tokens: string[]) =>
    unwrap<any[]>(api.get("/admin/orders/quotes", { params: { tokens: tokens.join(",") } })),
  squareoff: (id: string) => unwrap<any>(api.post(`/admin/positions/${id}/squareoff`)),
  deletePosition: (id: string) => unwrap<any>(api.delete(`/admin/positions/${id}`)),
  bulkDeletePositions: (position_ids: string[]) =>
    unwrap<{ total: number; deleted: number; failed: { id: string; error: string }[] }>(
      api.post("/admin/positions/bulk-delete", { position_ids }),
    ),
  positionNetting: (id: string) =>
    unwrap<any>(api.get(`/admin/positions/${id}/netting`)),
  positionRateHistory: (
    id: string,
    params?: { from?: string; to?: string; page?: number; page_size?: number },
  ) => unwrap<any>(api.get(`/admin/positions/${id}/rate-history`, { params })),
  pnlSummary: (params?: { user_id?: string; own_scope?: boolean }) =>
    unwrap<any>(api.get("/admin/positions/pnl-summary", { params })),
  emergencySquareoffAll: () => unwrap<any>(api.post("/admin/positions/emergency-squareoff")),
  editPosition: (
    id: string,
    body: Partial<{
      avg_price: number | string;
      quantity: number;
      opened_at: string;
      stop_loss: number | string | null;
      target: number | string | null;
      // Closed-position only — admin corrections + relabel
      realized_pnl: number | string;
      close_reason: string;
    }>,
  ) => unwrap<any>(api.patch(`/admin/positions/${id}`, body)),
  reopenPosition: (id: string) =>
    unwrap<any>(api.post(`/admin/positions/${id}/reopen`)),
  trades: (params?: any) => unwrap<any[]>(api.get("/admin/trades", { params })),
  holdings: (params?: any) => unwrap<any[]>(api.get("/admin/holdings", { params })),
};

// Per-admin / super-admin public-registration toggle.
export const RegistrationAPI = {
  status: () => unwrap<{ enabled: boolean }>(api.get("/admin/dashboard/registration-status")),
  set: (enabled: boolean) =>
    unwrap<{ enabled: boolean }>(api.put("/admin/dashboard/registration-status", { enabled })),
};

// Bonus Management (backend gated by BONUSES_ENABLED → 503 when off).
export const BonusesAdminAPI = {
  listTemplates: () => unwrap<any[]>(api.get("/admin/bonuses/templates")),
  createTemplate: (body: any) => unwrap<any>(api.post("/admin/bonuses/templates", body)),
  updateTemplate: (id: string, body: any) => unwrap<any>(api.put(`/admin/bonuses/templates/${id}`, body)),
  deleteTemplate: (id: string) => unwrap<any>(api.delete(`/admin/bonuses/templates/${id}`)),
  listGrants: (params?: { user_id?: string; status?: string; type?: string; page?: number; limit?: number }) =>
    unwrap<{ bonuses: any[]; pagination: { page: number; limit: number; total: number; totalPages: number } }>(
      api.get("/admin/bonuses", { params }),
    ),
  grant: (body: { user_id: string; template_id?: string; deposit_amount?: number; amount?: number; notes?: string }) =>
    unwrap<any>(api.post("/admin/bonuses/grant", body)),
  deduct: (body: { user_id: string; amount: number; reason?: string }) =>
    unwrap<{ deducted: string; user_wallet_credit: string }>(api.post("/admin/bonuses/deduct", body)),
  cancel: (id: string, reason: string) => unwrap<any>(api.post(`/admin/bonuses/${id}/cancel`, { reason })),
  ledger: (id: string) => unwrap<any[]>(api.get(`/admin/bonuses/${id}/ledger`)),
  recompute: (id: string) => unwrap<any>(api.post(`/admin/bonuses/${id}/recompute`)),
};

export const PayinOutAPI = {
  // Deposits / withdrawals are paginated (15 per page by default).
  // Pass `status` empty / undefined to get every status.
  deposits: (params?: { status?: string; search?: string; page?: number; page_size?: number; gateway?: boolean }) =>
    unwrap<{ items: any[]; meta: { page: number; page_size: number; total: number; total_pages: number } }>(
      api.get("/admin/deposits", { params }),
    ),
  gatewaySummary: () =>
    unwrap<{ enabled: boolean; today_total: string; today_count: number; week_total: string; week_count: number }>(
      api.get("/admin/gateway-summary"),
    ),
  approveDeposit: (id: string, admin_remark?: string) =>
    unwrap<any>(api.post(`/admin/deposits/${id}/approve`, { admin_remark })),
  rejectDeposit: (id: string, admin_remark: string) =>
    unwrap<any>(api.post(`/admin/deposits/${id}/reject`, { admin_remark })),
  // Settlement requests — queued by wallet_service when an auto-OFF
  // user's balance goes negative. Approval triggers the floor-to-0 +
  // settlement booking that auto-mode would have done; rejection
  // leaves the wallet negative (user stays blocked from new opens).
  settlementRequests: (status?: string) =>
    unwrap<any[]>(api.get("/admin/settlement-requests", { params: { status } })),
  approveSettlement: (id: string) =>
    unwrap<any>(api.post(`/admin/settlement-requests/${id}/approve`)),
  rejectSettlement: (id: string, reason: string) =>
    unwrap<any>(api.post(`/admin/settlement-requests/${id}/reject`, { reason })),
  withdrawals: (params?: { status?: string; search?: string; page?: number; page_size?: number }) =>
    unwrap<{ items: any[]; meta: { page: number; page_size: number; total: number; total_pages: number } }>(
      api.get("/admin/withdrawals", { params }),
    ),
  approveWithdrawal: (id: string, body: any) => unwrap<any>(api.post(`/admin/withdrawals/${id}/approve`, body)),
  rejectWithdrawal: (id: string, rejection_reason: string) =>
    unwrap<any>(api.post(`/admin/withdrawals/${id}/reject`, { rejection_reason })),
  bankAccounts: () => unwrap<any[]>(api.get("/admin/bank-accounts")),
  createBank: (body: any) => unwrap<any>(api.post("/admin/bank-accounts", body)),
  updateBank: (id: string, body: any) => unwrap<any>(api.put(`/admin/bank-accounts/${id}`, body)),
  deleteBank: (id: string) => unwrap<any>(api.delete(`/admin/bank-accounts/${id}`)),
  // Tier-aware: returns the caller's OWN tier override (sparse) plus the
  // fully-resolved effective values + per-field source labels. See the
  // backend `GET /admin/wd-rules` docstring for the response shape.
  wdRules: () =>
    unwrap<{
      tier: "super_admin" | "admin" | "broker";
      owner_id: string;
      rules: Array<{
        rule_type: "DEPOSIT" | "WITHDRAWAL";
        own: Record<string, any>;
        effective: Record<string, any>;
        sources: Record<string, string>;
      }>;
    }>(api.get("/admin/wd-rules")),
  // Sparse PATCH — fields omitted stay as-is, fields sent as `null`
  // explicitly clear the override at this tier (so the field starts
  // inheriting from the layer below). `tier=global` is honoured only
  // when the caller is super-admin.
  updateWdRule: (rule_type: string, body: any, tier?: "global") =>
    unwrap<any>(
      api.put(`/admin/wd-rules/${rule_type}`, body, {
        params: tier ? { tier } : undefined,
      }),
    ),
};

export const InstrumentAdminAPI = {
  list: (params?: any) => unwrap<{ items: any[]; meta: any }>(api.get("/admin/instruments", { params })),
  create: (body: any) => unwrap<any>(api.post("/admin/instruments", body)),
  update: (id: string, body: any) => unwrap<any>(api.put(`/admin/instruments/${id}`, body)),
  halt: (id: string, reason?: string) => unwrap<any>(api.post(`/admin/instruments/${id}/halt`, { reason })),
  resume: (id: string) => unwrap<any>(api.post(`/admin/instruments/${id}/resume`)),
  delete: (id: string) => unwrap<any>(api.delete(`/admin/instruments/${id}`)),
  // Deduped underlyings for the script-override typeahead. Each result
  // is just the underlying name (NIFTY, BANKNIFTY, …); the picker
  // appends `FUT` / `CE` / `PE` to form the pattern that the resolver
  // applies to every contract of that underlying.
  underlyings: (params: { exchange: string; contract_type?: "FUT" | "CE" | "PE"; q?: string; limit?: number }) =>
    unwrap<string[]>(api.get("/admin/instruments/underlyings", { params })),
  // Batch quote feed for the admin Market Watch page. Returns
  // [{token, bid, ask, ltp, change_pct, ...}] in the same shape as
  // useMarketStream's seed. Empty token list → empty result.
  quotesBatch: (tokens: string[]) =>
    unwrap<any[]>(
      api.get("/admin/instruments/quotes/batch", {
        params: { tokens: tokens.join(",") },
      }),
    ),
};

// ── Admin Market Watch ───────────────────────────────────────────────
// Per-segment managed lists + place orders on behalf of users in scope.
// Mirrors the user-side MarketwatchAPI shape so the row renderer can
// be lifted into a shared component later if needed.
export const AdminMarketwatchAPI = {
  segmentItems: (segment: string) =>
    unwrap<any[]>(api.get(`/admin/marketwatch/segment/${segment}/items`)),
  addItem: (segment: string, token: string) =>
    unwrap<any>(api.post(`/admin/marketwatch/segment/${segment}/items`, { token })),
  removeItem: (segment: string, token: string) =>
    unwrap<any>(api.delete(`/admin/marketwatch/segment/${segment}/items/${token}`)),
  quotes: (segment: string) =>
    unwrap<any[]>(api.get(`/admin/marketwatch/segment/${segment}/quotes`)),
  search: (segment: string, q: string, limit = 30) =>
    unwrap<any[]>(
      api.get(`/admin/marketwatch/segment/${segment}/search`, {
        params: { q, limit },
      }),
    ),
  placeOrders: (body: {
    token: string;
    user_ids: string[];
    action: "BUY" | "SELL";
    order_type: "MARKET" | "MANUAL";
    product_type: "MIS" | "NRML" | "CNC";
    lots: number;
    price?: number;
    // Historical entry: books now, then stamps the trade at this time.
    // Super-admin only, server-side; a reason is required with it.
    executed_at?: string;
    reason?: string;
  }) => unwrap<{ placed: any[]; failed: any[] }>(api.post("/admin/marketwatch/place-orders", body)),
};

export const BrokerageAPI = {
  list: () => unwrap<any[]>(api.get("/admin/brokerage/plans")),
  create: (body: any) => unwrap<any>(api.post("/admin/brokerage/plans", body)),
  update: (id: string, body: any) => unwrap<any>(api.put(`/admin/brokerage/plans/${id}`, body)),
  delete: (id: string) => unwrap<any>(api.delete(`/admin/brokerage/plans/${id}`)),
};

export const KycAPI = {
  list: (status?: string) =>
    unwrap<any[]>(api.get("/admin/kyc", { params: status ? { status } : {} })),
  detail: (id: string) => unwrap<any>(api.get(`/admin/kyc/${id}`)),
  approve: (id: string, admin_remark?: string) =>
    unwrap<any>(api.post(`/admin/kyc/${id}/approve`, { admin_remark })),
  reject: (id: string, rejection_reason: string, admin_remark?: string) =>
    unwrap<any>(api.post(`/admin/kyc/${id}/reject`, { rejection_reason, admin_remark })),
};

// Admin notification bell — backed by /admin/notifications endpoints.
// Each row is per-(recipient_admin, event); backend already filters by
// the caller's admin id, so no scope param is needed here.
export const NotificationsAPI = {
  list: (params?: { only_unread?: boolean; limit?: number }) =>
    unwrap<any[]>(api.get("/admin/notifications", { params })),
  unreadCount: () =>
    unwrap<{ count: number }>(api.get("/admin/notifications/unread-count")),
  markRead: (id: string) =>
    unwrap<any>(api.post(`/admin/notifications/${id}/read`)),
  markAllRead: () =>
    unwrap<{ marked: number }>(api.post("/admin/notifications/mark-all-read")),
  // Broadcast — push a notification (title + message + optional link) to every
  // user in the actor's own pool at once.
  broadcastRecipients: () =>
    unwrap<{ count: number }>(api.get("/admin/notifications/broadcast/recipients")),
  broadcast: (body: { title: string; message: string; link?: string; level?: string }) =>
    unwrap<{ count: number }>(api.post("/admin/notifications/broadcast", body)),
};

export type CryptoConfig = {
  enabled: boolean;
  mode: "manual" | "gateway" | "both";
  wallet_address: string | null;
  network: string | null;
  asset: string | null;
  gateway: "oxapay" | "none";
  has_gateway_key: boolean;
  webhook_url: string;
};

export type ChartLevelEntry = { price: number; color: string; label: string | null };

export type ChartLevelRow = {
  token: string;
  symbol: string;
  segment: string;
  levels: ChartLevelEntry[];
  /** Live price, so a level that is nowhere near it is obvious. */
  ltp: number | null;
};

export type ChartSegmentOption = { value: string; label: string; count: number };

export const ChartLevelsAPI = {
  /** Only segments that hold instruments, labelled like Segment Settings. */
  segments: () =>
    unwrap<ChartSegmentOption[]>(api.get("/admin/chart-levels/segments")),
  list: (segment?: string) =>
    unwrap<ChartLevelRow[]>(
      api.get("/admin/chart-levels", { params: segment ? { segment } : {} }),
    ),
  /** Returns the .xlsx bytes — the caller saves it. */
  template: async (segment: string) => {
    const res = await api.get("/admin/chart-levels/template", {
      params: { segment },
      responseType: "blob",
    });
    return res.data as Blob;
  },
  import: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return unwrap<{
      updated: number;
      cleared: number;
      errors: string[];
      warnings: string[];
    }>(
      api.post("/admin/chart-levels/import", fd, {
        headers: { "Content-Type": "multipart/form-data" },
      }),
    );
  },
  /** Replace one instrument's lines without the Excel trip. */
  save: (token: string, levels: { price: string; color: string; label: string }[]) =>
    unwrap<{ saved: boolean; levels: number }>(
      api.put(`/admin/chart-levels/${token}`, { levels }),
    ),
  clear: (token: string) =>
    unwrap<{ cleared: boolean }>(api.delete(`/admin/chart-levels/${token}`)),
};

export const CryptoConfigAPI = {
  get: () => unwrap<CryptoConfig>(api.get("/admin/crypto-config")),
  update: (body: {
    enabled?: boolean;
    mode?: string;
    wallet_address?: string | null;
    network?: string | null;
    asset?: string | null;
    gateway?: string;
    oxapay_api_key?: string;
  }) => unwrap<CryptoConfig>(api.put("/admin/crypto-config", body)),
};

export const SupportAPI = {
  get: () =>
    unwrap<{ whatsapp: string; role: string; can_edit?: boolean }>(
      api.get("/admin/support"),
    ),
  set: (whatsapp: string) =>
    unwrap<{ whatsapp: string; role: string; can_edit?: boolean }>(
      api.put("/admin/support", { whatsapp }),
    ),
  getTerms: () =>
    unwrap<{ text: string; enabled: boolean; role: string }>(
      api.get("/admin/support/terms"),
    ),
  setTerms: (text: string, enabled: boolean) =>
    unwrap<{ text: string; enabled: boolean; role: string }>(
      api.put("/admin/support/terms", { text, enabled }),
    ),
  // Home-page scrolling ticker — per-admin announcement lines (multiple).
  getTicker: () =>
    unwrap<{ messages: string[]; role: string }>(api.get("/admin/support/ticker")),
  setTicker: (messages: string[]) =>
    unwrap<{ messages: string[] }>(api.put("/admin/support/ticker", { messages })),
};

export const LedgerAdminAPI = {
  list: (params?: any) => unwrap<{ items: any[]; meta: any }>(api.get("/admin/ledger", { params })),
  manualEntry: (body: any) => unwrap<any>(api.post("/admin/ledger/manual-entry", body)),
};

export type MoneyFilterParams = { preset?: string; from_date?: string; to_date?: string };
export const MoneyAPI = {
  users: (params?: MoneyFilterParams) =>
    unwrap<{ totals: any; users: any[]; filter: { label: string } }>(
      api.get("/admin/money-transactions/users", { params }),
    ),
  brokers: (params?: MoneyFilterParams) =>
    unwrap<{ totals: any; brokers: any[]; filter: { label: string } }>(
      api.get("/admin/money-transactions/brokers", { params }),
    ),
};

export const ReportsAdminAPI = {
  // Instruments by live % move + clients by realised P&L.
  topMovers: (params: { days?: number; include_options?: boolean; limit?: number }) =>
    unwrap<any>(api.get("/admin/reports/top-movers", { params })),
  // Clients trading the same symbol + side inside one time window.
  similarActivity: (params: {
    days?: number;
    window_sec?: number;
    min_users?: number;
    limit?: number;
  }) => unwrap<any>(api.get("/admin/reports/similar-activity", { params })),
  // Fills vs the price the client was shown.
  slippage: (params: { days?: number; group_by?: "user" | "symbol"; limit?: number }) =>
    unwrap<any>(api.get("/admin/reports/slippage", { params })),
  // Fill timing + how stale the price was at each fill.
  latency: (params: { days?: number; stale_ms?: number; limit?: number }) =>
    unwrap<any>(api.get("/admin/reports/latency", { params })),
  users: () => unwrap<any>(api.get("/admin/reports/users")),
  financial: () => unwrap<any>(api.get("/admin/reports/financial")),
  trades: () => unwrap<any>(api.get("/admin/reports/trades")),
  compliance: () => unwrap<any>(api.get("/admin/reports/compliance")),
  tradebookPdf: async (
    userId: string,
    fromDate?: string,
    toDate?: string,
  ): Promise<Blob> => {
    const params: Record<string, string> = { user_id: userId };
    if (fromDate) params.from_date = fromDate;
    if (toDate) params.to_date = toDate;
    const res = await api.get("/admin/reports/tradebook/pdf", {
      params,
      responseType: "blob",
    });
    return res.data;
  },
};

export type MetaApiSettings = {
  enabled: boolean;
  has_token: boolean;
  account_id: string;
  region: string;
  symbols: string[];
  symbol_map: Record<string, string>;
  max_symbols: number;
  source: "admin" | "env" | "";
};

export type MetaApiStatus = {
  enabled: boolean;
  configured: boolean;
  connected: boolean;
  account_id: string;
  region: string;
  source: string;
  symbols: string[];
  max_symbols: number;
  tick_count: number;
  last_error: string;
  last_rx_age_sec: number | null;
  // false = the feed process hasn't reported recently (feed down / never started)
  live: boolean;
};

export const MetaApiAPI = {
  settings: () =>
    api.get("/admin/metaapi/settings").then((r) => r.data?.settings as MetaApiSettings),
  saveSettings: (body: {
    token?: string;
    account_id?: string;
    region?: string;
    max_symbols?: number;
    symbol_map?: Record<string, string>;
  }) =>
    api.put("/admin/metaapi/settings", body).then((r) => r.data?.settings as MetaApiSettings),
  status: () => api.get("/admin/metaapi/status").then((r) => r.data?.status as MetaApiStatus),
  connect: () => api.post("/admin/metaapi/connect").then((r) => r.data?.status as MetaApiStatus),
  disconnect: () =>
    api.post("/admin/metaapi/disconnect").then((r) => r.data?.status as MetaApiStatus),
  // Broker symbol list straight from the MT account (server caches 10 min).
  brokerSymbols: (refresh = false) =>
    api
      .get("/admin/metaapi/symbols", { params: { refresh } })
      .then((r) => r.data as { symbols: string[]; cached: boolean }),
  saveSymbols: (symbols: string[]) =>
    api
      .put("/admin/metaapi/symbols", { symbols })
      .then((r) => r.data as { settings: MetaApiSettings; status: MetaApiStatus }),
};

export const ZerodhaAPI = {
  status: (account = 0) =>
    api.get("/admin/zerodha/status", { params: { account } }).then((r) => (r.data?.status ?? r.data)),
  settings: (account = 0) =>
    api.get("/admin/zerodha/settings", { params: { account } }).then((r) => (r.data?.settings ?? r.data)),
  saveSettings: (body: any, account = 0) =>
    api.post("/admin/zerodha/settings", body, { params: { account } }).then((r) => r.data),
  loginUrl: (account = 0) =>
    api.get("/admin/zerodha/login-url", { params: { account } }).then((r) => r.data?.loginUrl as string),
  logout: (account = 0) => api.post("/admin/zerodha/logout", null, { params: { account } }).then((r) => r.data),
  connectWs: () => api.post("/admin/zerodha/connect-ws").then((r) => r.data),
  disconnectWs: () => api.post("/admin/zerodha/disconnect-ws").then((r) => r.data),
  // Same effect as a backend systemctl restart for the ticker: stops
  // the existing socket, clears the heal-failure counter (so the
  // self-heal loop drops back to 30 s cadence instead of the 5 min
  // exponential-cap), refreshes the captured event loop, then runs
  // connect_ws(force=True). Lets the operator fix a stuck WS after
  // the daily 08:00 IST token rotation without SSH'ing into the box.
  forceReconnectWs: () =>
    api.post("/admin/zerodha/force-reconnect-ws").then((r) => r.data),
  // Dual-account HA failover: exchange→account routing + live health.
  routing: () => api.get("/admin/zerodha/routing").then((r) => r.data),
  updateRouting: (body: any) =>
    api.put("/admin/zerodha/routing", body).then((r) => r.data),
  failoverTestDisconnect: (account: number) =>
    api
      .post("/admin/zerodha/routing/test-disconnect", null, { params: { account } })
      .then((r) => r.data),
  searchInstruments: (query: string, segment?: string) =>
    api
      .get("/admin/zerodha/instruments/search", { params: { query, segment } })
      .then((r) => (r.data?.instruments ?? []) as any[]),
  subscribe: (instrument: any) =>
    api.post("/admin/zerodha/instruments/subscribe", { instrument }).then((r) => r.data),
  subscribeBulk: (instruments: any[]) =>
    api.post("/admin/zerodha/instruments/subscribe-bulk", { instruments }).then((r) => r.data),
  unsubscribe: (token: number) =>
    api.delete(`/admin/zerodha/instruments/${token}`).then((r) => r.data),
  syncInstruments: () =>
    api.post("/admin/zerodha/instruments/sync").then((r) => r.data),
  clearInstruments: () =>
    api.post("/admin/zerodha/instruments/clear").then((r) => r.data),
  trimInstruments: (keep_count = 700) =>
    api
      .post("/admin/zerodha/instruments/trim", { keep_count })
      .then((r) => r.data as { kept: number; removed: number; must_keep_added: number }),
  listForExchange: (exchange: string) =>
    api
      .get(`/admin/zerodha/instruments/exchange/${encodeURIComponent(exchange)}`)
      .then((r) => (r.data?.instruments ?? []) as any[]),
  listSubscribed: () =>
    api
      .get("/admin/zerodha/instruments/subscribed")
      .then((r) => (r.data?.instruments ?? []) as any[]),
  connectWithToken: (request_token: string, account = 0) =>
    api
      .post("/admin/zerodha/connect-with-token", { request_token }, { params: { account } })
      .then((r) => r.data),
  debugCsv: (exchange = "NFO") =>
    api
      .get("/admin/zerodha/debug-csv", { params: { exchange } })
      .then((r) => r.data),
  diagnose: () =>
    api
      .get("/admin/zerodha/diagnose")
      .then((r) => r.data?.report ?? r.data),
};

// ─────────────────────────────────────────────────────────────────────
// Zerodha auto-login (daily scheduled token refresh via Playwright).
// All endpoints are super-admin gated server-side.
// ─────────────────────────────────────────────────────────────────────

export type ZerodhaAutoLoginStatus = {
  is_configured: boolean;
  is_enabled: boolean;
  schedule_time_ist: string;
  last_attempt_at: string | null;
  last_success_at: string | null;
  last_status: "" | "success" | "failed";
  last_error_detail: string | null;
  last_stage: string | null;
  consecutive_failures: number;
  last_duration_ms: number | null;
  username_masked: string;
};

export const ZerodhaAutoLoginAPI = {
  status: (account = 0) =>
    api
      .get("/admin/zerodha/auto-login", { params: { account } })
      .then((r) => r.data?.status as ZerodhaAutoLoginStatus),

  updateCredentials: (
    body: { username: string; password: string; totp_secret: string },
    account = 0,
  ) =>
    api
      .put("/admin/zerodha/auto-login/credentials", body, { params: { account } })
      .then((r) => r.data?.status as ZerodhaAutoLoginStatus),

  toggle: (enabled: boolean, account = 0) =>
    api
      .post("/admin/zerodha/auto-login/toggle", { enabled }, { params: { account } })
      .then((r) => r.data?.status as ZerodhaAutoLoginStatus),

  setSchedule: (schedule_time_ist: string, account = 0) =>
    api
      .put("/admin/zerodha/auto-login/schedule", { schedule_time_ist }, { params: { account } })
      .then((r) => r.data?.status as ZerodhaAutoLoginStatus),

  // Only queues the login on the feed server — the outcome lands in status().
  testNow: (account = 0) =>
    api
      .post("/admin/zerodha/auto-login/test", null, { params: { account } })
      .then((r) => r.data?.status as ZerodhaAutoLoginStatus),

  resetLock: (account = 0) =>
    api
      .post("/admin/zerodha/auto-login/reset-lock", null, { params: { account } })
      .then((r) => r.data?.status as ZerodhaAutoLoginStatus),
};

export const BrokerMgmtAPI = {
  // Cap — drives the create/edit form so OFF/VIEW/EDIT radio options
  // above the actor's own level are greyed out.
  maxGrantable: () =>
    unwrap<{ cap: Record<string, "OFF" | "VIEW" | "EDIT"> }>(
      api.get("/admin/management/brokers/max-grantable"),
    ),

  // Broker CRUD (admin/super-admin creates; broker can create sub-broker
  // when broker_permissions.sub_brokers == EDIT)
  list: (params?: { q?: string; status?: string; page?: number; page_size?: number; include_sub?: boolean }) =>
    unwrap<{ items: any[]; meta: any }>(api.get("/admin/management/brokers", { params })),
  get: (id: string) => unwrap<any>(api.get(`/admin/management/brokers/${id}`)),
  create: (body: {
    full_name: string;
    email: string;
    mobile: string;
    password: string;
    permissions: Record<string, "OFF" | "VIEW" | "EDIT">;
    pnl_share_pct: number | string;
    brokerage_share_pct?: number | string;
    assigned_admin_id?: string;
  }) => unwrap<any>(api.post("/admin/management/brokers", body)),
  update: (id: string, body: { full_name?: string }) =>
    unwrap<any>(api.put(`/admin/management/brokers/${id}`, body)),
  updatePermissions: (id: string, permissions: Record<string, "OFF" | "VIEW" | "EDIT">) =>
    unwrap<{ broker: any; cascaded_changes: any[] }>(
      api.put(`/admin/management/brokers/${id}/permissions`, { permissions }),
    ),
  updatePnlShare: (id: string, pct: number | string, brokerage_pct?: number | string) =>
    unwrap<any>(api.put(`/admin/management/brokers/${id}/pnl-share`, { pct, brokerage_pct })),
  block: (id: string) => unwrap<any>(api.post(`/admin/management/brokers/${id}/block`)),
  unblock: (id: string) => unwrap<any>(api.post(`/admin/management/brokers/${id}/unblock`)),
  setPaymentGateway: (id: string, enabled: boolean) =>
    unwrap<any>(api.put(`/admin/management/brokers/${id}/payment-gateway`, { enabled })),
  deleteBroker: (id: string) =>
    unwrap<any>(api.delete(`/admin/management/brokers/${id}`)),
  resetPassword: (id: string, new_password: string) =>
    unwrap<any>(
      api.post(`/admin/management/brokers/${id}/reset-password`, { new_password }),
    ),

  // Subtree clients (every CLIENT/DEALER/MASTER under this broker)
  listSubtreeUsers: (id: string, params?: { page?: number; page_size?: number }) =>
    unwrap<{ items: any[]; meta: any }>(
      api.get(`/admin/management/brokers/${id}/users`, { params }),
    ),

  // Reassignment
  assignUser: (userId: string, broker_id: string | null) =>
    unwrap<any>(api.post(`/admin/management/users/${userId}/assign-to-broker`, { broker_id })),
  bulkAssign: (user_ids: string[], broker_id: string | null) =>
    unwrap<any>(api.post("/admin/management/users/bulk-assign-to-broker", { user_ids, broker_id })),

  // Detail-page aggregator
  report: (id: string) =>
    unwrap<any>(api.get(`/admin/management/brokers/${id}/report`)),

  // Login-as — same shape as ManagementAPI.impersonateSubAdmin
  impersonate: (id: string) =>
    api
      .post(`/admin/management/brokers/${id}/impersonate`)
      .then((r) => r.data?.data ?? r.data),

  // Settlements (admin reconciliation surface — broker doesn't see this)
  listSettlements: (week_start?: string) =>
    unwrap<{
      period_start: string;
      period_end: string;
      items: any[];
      totals: { user_count: number; net_house_pnl_inr: string; broker_share_inr: string };
    }>(api.get("/admin/management/broker-settlements", { params: week_start ? { week_start } : {} })),
  historyForBroker: (id: string, params?: { from_date?: string; to_date?: string }) =>
    unwrap<{ broker: any; items: any[] }>(
      api.get(`/admin/management/broker-settlements/broker/${id}`, { params }),
    ),
  recomputeSettlements: (body: { week_start: string; broker_id?: string }) =>
    unwrap<{ items: any[]; frozen_skipped: number }>(
      api.post("/admin/management/broker-settlements/recompute", body),
    ),
  finalizeSettlement: (id: string) =>
    unwrap<any>(api.post(`/admin/management/broker-settlements/${id}/finalize`)),
  markPaid: (id: string, notes?: string) =>
    unwrap<any>(api.post(`/admin/management/broker-settlements/${id}/mark-paid`, { notes })),
};

export const ManagementAPI = {
  // Sub-admins
  listSubAdmins: (params?: { q?: string; status?: string; page?: number; page_size?: number }) =>
    unwrap<{ items: any[]; meta: any }>(api.get("/admin/management/sub-admins", { params })),
  getSubAdmin: (id: string) => unwrap<any>(api.get(`/admin/management/sub-admins/${id}`)),
  createSubAdmin: (body: {
    full_name: string;
    email: string;
    mobile: string;
    password: string;
    permissions: Record<string, boolean>;
    pnl_share_pct: number | string;
  }) => unwrap<any>(api.post("/admin/management/sub-admins", body)),
  updateSubAdmin: (id: string, body: { full_name?: string }) =>
    unwrap<any>(api.put(`/admin/management/sub-admins/${id}`, body)),
  updatePermissions: (id: string, permissions: Record<string, boolean>) =>
    unwrap<any>(api.put(`/admin/management/sub-admins/${id}/permissions`, { permissions })),
  updatePnlShare: (id: string, pct: number | string) =>
    unwrap<any>(api.put(`/admin/management/sub-admins/${id}/pnl-share`, { pct })),
  blockSubAdmin: (id: string) =>
    unwrap<any>(api.post(`/admin/management/sub-admins/${id}/block`)),
  unblockSubAdmin: (id: string) =>
    unwrap<any>(api.post(`/admin/management/sub-admins/${id}/unblock`)),
  setSubAdminMaintenance: (id: string, enabled: boolean) =>
    unwrap<any>(api.post(`/admin/management/sub-admins/${id}/maintenance`, { enabled })),
  setSubAdminPaymentGateway: (id: string, enabled: boolean) =>
    unwrap<any>(api.put(`/admin/management/sub-admins/${id}/payment-gateway`, { enabled })),
  deleteSubAdmin: (id: string) =>
    unwrap<any>(api.delete(`/admin/management/sub-admins/${id}`)),
  resetSubAdminPassword: (id: string, new_password: string) =>
    unwrap<any>(api.post(`/admin/management/sub-admins/${id}/reset-password`, { new_password })),
  listAssignedUsers: (id: string, params?: { page?: number; page_size?: number }) =>
    unwrap<{ items: any[]; meta: any }>(
      api.get(`/admin/management/sub-admins/${id}/users`, { params })
    ),
  // User reassignment
  assignUser: (userId: string, sub_admin_id: string | null) =>
    unwrap<any>(api.post(`/admin/management/users/${userId}/assign`, { sub_admin_id })),
  bulkAssign: (user_ids: string[], sub_admin_id: string | null) =>
    unwrap<any>(api.post("/admin/management/users/bulk-assign", { user_ids, sub_admin_id })),
  // Cross-admin transfer (permitted admins) — list target admins + move a user.
  transferAdminTargets: () =>
    unwrap<{ items: any[] }>(api.get("/admin/management/transfer/admins")),
  transferUserToAdmin: (userId: string, target_admin_id: string) =>
    unwrap<any>(api.post(`/admin/management/transfer/to-admin/${userId}`, { target_admin_id })),
  transferUsersToAdminBulk: (user_ids: string[], target_admin_id: string) =>
    unwrap<{ transferred: number; skipped: number }>(
      api.post("/admin/management/transfer/to-admin-bulk", { user_ids, target_admin_id }),
    ),
  // Settlements
  listSettlements: (week_start?: string) =>
    unwrap<{
      period_start: string;
      period_end: string;
      items: any[];
      totals: { user_count: number; net_house_pnl_inr: string; sub_admin_share_inr: string };
    }>(api.get("/admin/management/settlements", { params: week_start ? { week_start } : {} })),
  historyForSubAdmin: (id: string, params?: { from_date?: string; to_date?: string }) =>
    unwrap<{ sub_admin: any; items: any[] }>(
      api.get(`/admin/management/settlements/sub-admin/${id}`, { params })
    ),
  recomputeSettlements: (body: { week_start: string; sub_admin_id?: string }) =>
    unwrap<{ items: any[]; frozen_skipped: number }>(
      api.post("/admin/management/settlements/recompute", body)
    ),
  finalizeSettlement: (id: string) =>
    unwrap<any>(api.post(`/admin/management/settlements/${id}/finalize`)),
  markPaid: (id: string, notes?: string) =>
    unwrap<any>(api.post(`/admin/management/settlements/${id}/mark-paid`, { notes })),
  // Detail-page aggregator: user count, wallet, pnl windows, trade counts,
  // recent trades, weekly deposit/withdrawal flow.
  subAdminReport: (id: string) =>
    unwrap<any>(api.get(`/admin/management/sub-admins/${id}/report`)),
  // Login-as: returns admin-side tokens for the target sub-admin so the
  // frontend can drop them into the auth store and load that sub-admin's
  // dashboard view. Raw call (no unwrap) so we can read admin_app_url
  // alongside the token pair.
  impersonateSubAdmin: (id: string) =>
    api
      .post(`/admin/management/sub-admins/${id}/impersonate`)
      .then((r) => r.data?.data ?? r.data),
};

export const SettingsAPI = {
  platformList: (category?: string) => unwrap<any[]>(api.get("/admin/settings/platform", { params: { category } })),
  updatePlatform: (key: string, setting_value: any) =>
    unwrap<any>(api.put(`/admin/settings/platform/${encodeURIComponent(key)}`, { setting_value })),
  getPaymentGateway: () => unwrap<any>(api.get("/admin/settings/payment-gateway")),
  setPaymentGateway: (enabled: boolean) =>
    unwrap<any>(api.put("/admin/settings/payment-gateway", { setting_value: enabled })),
  // Per-admin Terms & Conditions shown to this admin's users on their profile.
  getTerms: () => unwrap<{ terms: string }>(api.get("/admin/settings/terms")),
  setTerms: (terms: string) => unwrap<{ terms: string }>(api.put("/admin/settings/terms", { terms })),
  // Per-admin per-position Carry-Forward toggle feature switch.
  getCarryToggle: () => unwrap<{ enabled: boolean }>(api.get("/admin/settings/carry-forward-toggle")),
  setCarryToggle: (enabled: boolean) =>
    unwrap<{ enabled: boolean }>(api.put("/admin/settings/carry-forward-toggle", { enabled })),
  // Weekly mark-to-market settlement engine (super-admin only).
  weeklySettlementRun: () =>
    unwrap<{ week_key?: string; batch_id?: string; total?: number; settled?: number; skipped?: number; failed?: number; skipped_reason?: string }>(
      api.post("/admin/settings/weekly-settlement/run", null, { params: { force: true } }),
    ),
  setWeeklySettlementEnabled: (enabled: boolean) =>
    unwrap<{ enabled: boolean }>(api.put("/admin/settings/weekly-settlement/enabled", { setting_value: enabled })),
  holidays: (year?: number) => unwrap<any[]>(api.get("/admin/holidays", { params: { year } })),
  createHoliday: (body: any) => unwrap<any>(api.post("/admin/holidays", body)),
  deleteHoliday: (id: string) => unwrap<any>(api.delete(`/admin/holidays/${id}`)),
  audit: (params?: any) => unwrap<{ items: any[]; meta: any }>(api.get("/admin/audit/logs", { params })),
  backupList: () => unwrap<any[]>(api.get("/admin/backup/list")),
  runBackup: () => unwrap<any>(api.post("/admin/backup/run")),
  eodReset: () => unwrap<any>(api.post("/admin/backup/eod-reset")),
};

// Per-actor Expiry-Settings override (USER / BROKER / ADMIN tiers on top
// of the global PlatformSetting option_chain.* keys).
export const ExpiryOverridesAPI = {
  get: (actor_kind: "USER" | "BROKER" | "ADMIN", actor_id: string) =>
    unwrap<{
      id: string | null;
      actor_kind: string;
      actor_id: string;
      underlyings: any[] | null;
      max_expiries_fallback: number | null;
      max_expiries_by_exchange: Record<string, number> | null;
      strikes_around_atm_by_segment: Record<string, number> | null;
      exists: boolean;
    }>(api.get(`/admin/expiry-overrides/${actor_kind}/${actor_id}`)),
  upsert: (
    actor_kind: "USER" | "BROKER" | "ADMIN",
    actor_id: string,
    body: {
      underlyings: any[] | null;
      max_expiries_fallback: number | null;
      max_expiries_by_exchange?: Record<string, number> | null;
      strikes_around_atm_by_segment?: Record<string, number> | null;
    },
  ) => unwrap<any>(api.put(`/admin/expiry-overrides/${actor_kind}/${actor_id}`, body)),
  remove: (actor_kind: "USER" | "BROKER" | "ADMIN", actor_id: string) =>
    unwrap<any>(api.delete(`/admin/expiry-overrides/${actor_kind}/${actor_id}`)),
  effective: (user_id: string) =>
    unwrap<{ underlyings: any[]; max_expiries: number }>(
      api.get(`/admin/expiry-overrides/effective/${user_id}`),
    ),
};

// White-label branding — admin self-service (logo, brand name, custom
// domain + auto-SSL). Every endpoint 503s when the backend feature
// flag `BRANDING_ENABLED` is off, so the UI gracefully shows
// "feature not enabled" rather than crashing.
export type BrandingPayload = {
  admin_id: string;
  user_code: string;
  brand_name: string | null;
  logo_url: string | null;
  custom_domain: string | null;
  custom_domain_status: string | null;
  // Surfaced by the admin /me endpoint so the UI can render the FAILED
  // panel inline without a second /domain/status call. May be null at
  // any other status.
  custom_domain_last_error: string | null;
};

export type DomainStatus = {
  custom_domain: string | null;
  custom_domain_status: string | null;
  custom_domain_last_error: string | null;
  custom_domain_verified_at: string | null;
};

export type DnsRecordCheck = {
  current: string[];
  ok: boolean;
  error: string | null;
};
export type DnsPreview = {
  expected_ip: string | null;
  apex: DnsRecordCheck;
  www: DnsRecordCheck;
};

/**
 * Web Push subscriptions for the admin app. Used by `notify-sound.ts`
 * → `ensureNotificationPermission()` once permission is granted, so the
 * SW (public/sw.js) receives backend-sent pushes for deposit /
 * withdrawal events even when the PWA is force-stopped or the phone is
 * locked.
 */
export const PushAPI = {
  vapidKey: () => unwrap<{ public_key: string }>(api.get("/admin/push/vapid-key")),
  subscribe: (body: { endpoint: string; keys: { p256dh: string; auth: string }; label?: string }) =>
    unwrap<{ id: string; created: boolean }>(api.post("/admin/push/subscribe", body)),
  unsubscribe: (endpoint: string) =>
    unwrap<{ ok: boolean; found: boolean }>(api.post("/admin/push/unsubscribe", { endpoint })),
};

export const BrandingAPI = {
  me: () => unwrap<BrandingPayload>(api.get("/admin/branding/me")),
  update: (body: {
    brand_name?: string | null;
    telegram_link?: string | null;
    custom_domain?: string | null;
    clear_custom_domain?: boolean;
  }) => unwrap<BrandingPayload>(api.put("/admin/branding", body)),
  uploadLogo: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return unwrap<BrandingPayload>(
      api.post("/admin/branding/logo", fd, {
        headers: { "Content-Type": "multipart/form-data" },
      }),
    );
  },
  verifyDomain: () =>
    unwrap<DomainStatus>(api.post("/admin/branding/domain/verify")),
  domainStatus: () =>
    unwrap<DomainStatus>(api.get("/admin/branding/domain/status")),
  // Side-by-side current-vs-expected DNS record preview. The admin UI
  // calls this on Step 2 (and after a Refresh button) so the user sees
  // exactly which records they need to change at the registrar.
  dnsPreview: () =>
    unwrap<DnsPreview>(api.get("/admin/branding/domain/dns-preview")),
  disconnectDomain: () =>
    unwrap<BrandingPayload>(api.post("/admin/branding/domain/disconnect")),
};

// ── Accounts Dashboard ──────────────────────────────────────────

export type AccountEntity = {
  id: string;
  name: string;
  user_code?: string;
  role: string;
  broker_count?: number;
  deposits: number;
  withdrawals: number;
  net_deposit: number;
  realized_pnl: number;
  unrealized_pnl: number;
  net_pnl: number;
  brokerage: number;
  total_trades: number;
  profit_trades: number;
  loss_trades: number;
  win_rate: number;
  volume: number;
  balance: number;
  equity: number;
  open_positions: number;
  settlement_outstanding: number;
  user_count: number;
};

export type AccountsSummary = {
  entities: AccountEntity[];
  grand_total: AccountEntity;
  filter: {
    from_date: string | null;
    to_date: string | null;
    preset: string | null;
    is_lifetime: boolean;
  };
};

export type WeekOption = {
  label: string;
  start: string;
  end: string;
};

export type BrokerTotals = {
  net_client_pnl: string;
  net_client_bkg: string;
  total_of_both: string;
  settlement: string;
  actual_pnl: string;
  sharing_pnl: string;
  sharing_bkg: string;
  total_deposits: string;
  total_withdrawals: string;
  share_pct: string;
  agreement_type: string | null;
  client_count: number;
};

export type EntityUserRow = {
  user_id: string;
  user_code: string;
  username: string;
  owner_kind?: string;
  owner_name?: string;
  net_pnl: string;
  net_bkg: string;
  total_pnl: string;
  settlement: string;
  pnl_minus_settlement: string;
};

export type EntityUsersResponse = {
  items: EntityUserRow[];
  meta: { page: number; page_size: number; total: number; total_pages: number };
};

type DateParams = { from_date?: string; to_date?: string; preset?: string };

export const AccountsAPI = {
  summary: (params?: {
    scope?: string;
    from_date?: string;
    to_date?: string;
    preset?: string;
  }) =>
    unwrap<AccountsSummary>(
      api.get("/admin/accounts/summary", { params }),
    ),

  weeks: (numWeeks?: number) =>
    unwrap<WeekOption[]>(
      api.get("/admin/accounts/weeks", { params: numWeeks ? { num_weeks: numWeeks } : undefined }),
    ),

  brokerTotals: (entityId: string, params?: DateParams) =>
    unwrap<BrokerTotals>(
      api.get(`/admin/accounts/broker-totals/${entityId}`, { params }),
    ),

  entityUsers: (
    entityId: string,
    params?: DateParams & { page?: number; page_size?: number; search?: string },
  ) =>
    unwrap<EntityUsersResponse>(
      api.get(`/admin/accounts/entity-users/${entityId}`, { params }),
    ),

  exportEntityUsersExcel: async (entityId: string, params?: DateParams): Promise<Blob> => {
    const res = await api.get(`/admin/accounts/entity-users/${entityId}/export/excel`, {
      params,
      responseType: "blob",
    });
    return res.data;
  },

  exportEntityUsersPdf: async (entityId: string, params?: DateParams): Promise<Blob> => {
    const res = await api.get(`/admin/accounts/entity-users/${entityId}/export/pdf`, {
      params,
      responseType: "blob",
    });
    return res.data;
  },

  exportBrokerTotalsExcel: async (entityId: string, params?: DateParams): Promise<Blob> => {
    const res = await api.get(`/admin/accounts/broker-totals/${entityId}/export/excel`, {
      params,
      responseType: "blob",
    });
    return res.data;
  },

  exportBrokerTotalsPdf: async (entityId: string, params?: DateParams): Promise<Blob> => {
    const res = await api.get(`/admin/accounts/broker-totals/${entityId}/export/pdf`, {
      params,
      responseType: "blob",
    });
    return res.data;
  },
};

// ── Super-admin platform reports (cross-pool order + login activity) ──
export type PlatformReport = {
  orders: { today: number; this_week: number; last_week: number };
  logins: { today: number; this_week: number; last_week: number };
  admins: Array<{
    admin_id: string | null;
    admin_name: string;
    admin_code: string;
    orders_today: number;
    active_today: number;
    active_week: number;
    active_last_week: number;
  }>;
};

export const PlatformReportsAPI = {
  get: () => unwrap<PlatformReport>(api.get("/admin/platform-reports")),
};
