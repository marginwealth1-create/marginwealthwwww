"use client";

/**
 * Last-known wallet snapshot, persisted to localStorage so the next login /
 * refresh paints real numbers instantly instead of flashing ₹0 while the
 * `/wallet/summary` fetch is in flight.
 *
 * Used as `placeholderData` for the wallet useQuery hooks (TopBar pill,
 * terminal footer, dashboard tiles). The fresh fetch lands a moment later
 * and overwrites the snapshot with the actual numbers.
 *
 * Cleared on logout so the next account doesn't briefly see the previous
 * user's balance.
 */

const KEY = "nb.walletSnapshot";

export type WalletSnapshot = {
  available_balance?: number;
  used_margin?: number;
  unrealized_pnl?: number;
  realized_pnl?: number;
  total_value?: number;
} & Record<string, any>;

export function readWalletSnapshot(): WalletSnapshot | undefined {
  if (typeof window === "undefined") return undefined;
  try {
    const raw = window.localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as WalletSnapshot) : undefined;
  } catch {
    return undefined;
  }
}

export function writeWalletSnapshot(s: WalletSnapshot | null | undefined): void {
  if (typeof window === "undefined" || !s) return;
  try {
    window.localStorage.setItem(KEY, JSON.stringify(s));
  } catch {
    // localStorage may be unavailable (private window, quota) — ignore.
  }
}

export function clearWalletSnapshot(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    // ignore
  }
}
