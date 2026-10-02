"use client";

/**
 * Last-known admin dashboard snapshot, persisted to localStorage so the
 * very first paint after login (or after a refresh) shows real numbers
 * instead of a "0" flash for every stat card.
 *
 * Used as `placeholderData` for the dashboard stats useQuery. The fresh
 * fetch lands a moment later and overwrites the snapshot.
 *
 * Cleared on admin logout so a different admin signing in on the same
 * browser doesn't briefly see the previous user's numbers.
 */

const KEY = "nb.admin.dashboardSnapshot";

export type DashboardSnapshot = Record<string, any>;

export function readDashboardSnapshot(): DashboardSnapshot | undefined {
  if (typeof window === "undefined") return undefined;
  try {
    const raw = window.localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as DashboardSnapshot) : undefined;
  } catch {
    return undefined;
  }
}

export function writeDashboardSnapshot(s: DashboardSnapshot | null | undefined): void {
  if (typeof window === "undefined" || !s) return;
  try {
    window.localStorage.setItem(KEY, JSON.stringify(s));
  } catch {
    // ignore
  }
}

export function clearDashboardSnapshot(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    // ignore
  }
}
