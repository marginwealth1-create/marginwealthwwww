/**
 * Single source of truth for the admin navigation tree.
 *
 * Both the desktop sidebar (`AdminSidebar`) and the mobile drawer
 * (`AdminMobileDrawer`) consume `useAdminNav()` so the two surfaces
 * NEVER drift out of sync — same items, same labels, same permission
 * gates. Critical: a sub-admin who can't see /backup on desktop must
 * also not see it in the mobile drawer.
 */

import {
  ArrowRightLeft,
  Bitcoin,
  Activity,
  Banknote,
  Calendar,
  ClipboardList,
  Cog,
  DatabaseBackup,
  Download,
  FileText,
  Gift,
  Layers,
  History,
  Home,
  LineChart,
  ListChecks,
  ListOrdered,
  MessageCircle,
  Plug,
  Ruler,
  ShieldCheck,
  Users,
  Crown,
  Wallet,
  Handshake,
  BarChart3,
  type LucideIcon,
} from "lucide-react";
import { canSee, isSuperAdmin, type PermissionKey } from "@/lib/permissions";
import { useAdminAuthStore } from "@/stores/authStore";

export type AdminNavItem = {
  href: string;
  label: string;
  icon: LucideIcon;
  perm?: PermissionKey;
  brokerPerm?: PermissionKey;
  brokerLabel?: string;
  superOnly?: boolean;
  adminTierOnly?: boolean;
  hideForSuperAdmin?: boolean;
  // Visible to SUPER_ADMIN or ADMIN only (never EMPLOYEE / BROKER). Used by
  // the Employee-management item — every admin manages their own staff.
  superOrAdmin?: boolean;
  // Employee-only gate for items that are UN-gated for super/admin/broker
  // (they stay visible to those roles) but must be grantable per-section to
  // employees. e.g. Accounts, Audit, Support, P&L Sharing.
  empPerm?: PermissionKey;
};

export type AdminNavGroup = { title: string; items: AdminNavItem[] };

export const ADMIN_NAV_GROUPS: AdminNavGroup[] = [
  {
    title: "Overview",
    items: [
      { href: "/dashboard", label: "Dashboard", icon: Home },
      { href: "/accounts-dashboard", label: "Accounts", icon: BarChart3, empPerm: "accounts" },
    ],
  },
  {
    title: "Users",
    items: [
      { href: "/users", label: "All users", icon: Users, perm: "users" },
      { href: "/send-notification", label: "Send Notification", icon: MessageCircle, perm: "users" },
    ],
  },
  {
    title: "Payments",
    items: [
      { href: "/payments", label: "Payments", icon: Banknote, perm: "deposits" },
      { href: "/crypto-payments", label: "Crypto Payments", icon: Bitcoin, perm: "deposits" },
      { href: "/management/bonuses", label: "Bonuses", icon: Gift, perm: "bonuses" },
    ],
  },
  {
    title: "Money",
    items: [
      { href: "/money-transactions", label: "Money Transactions", icon: Wallet, perm: "ledger", empPerm: "money_transactions" },
      { href: "/broker-deposits", label: "Broker Deposits", icon: Handshake, perm: "ledger", empPerm: "broker_deposits" },
    ],
  },
  {
    title: "Risk & Settings",
    items: [
      { href: "/risk-management", label: "Risk Management", icon: ShieldCheck, perm: "risk" },
      { href: "/segment-settings", label: "Segment Settings", icon: Layers, perm: "segment_settings" },
      { href: "/option-chain", label: "Expiry Settings", icon: Calendar, perm: "segment_settings" },
    ],
  },
  {
    title: "Trading",
    items: [
      { href: "/orders", label: "Orders", icon: ListOrdered, perm: "trading_view", empPerm: "orders" },
      { href: "/positions", label: "Positions", icon: Activity, perm: "trading_view", empPerm: "positions" },
      { href: "/exposure", label: "Exposure", icon: Layers, perm: "trading_view" },
      { href: "/marketwatch", label: "Market Watch", icon: LineChart, perm: "trading_view", empPerm: "marketwatch" },
      { href: "/chart-levels", label: "Chart Lines", icon: Ruler, perm: "trading_view", empPerm: "marketwatch" },
      { href: "/instruments", label: "Instruments", icon: ListChecks, superOnly: true },
      { href: "/zerodha", label: "Zerodha Connect", icon: Plug, superOnly: true },
      { href: "/metaapi", label: "MetaAPI Connect", icon: Plug, superOnly: true },
    ],
  },
  {
    title: "Reports",
    items: [
      { href: "/reports/top-movers", label: "Top Gainers/Losers", icon: BarChart3, perm: "reports" },
      { href: "/reports/similar-activity", label: "Similar Activity", icon: Users, perm: "reports" },
      { href: "/reports/slippage", label: "Slippage", icon: Activity, perm: "reports" },
      { href: "/reports/latency", label: "Latency", icon: History, perm: "reports" },
      { href: "/reports/admin", label: "Admin Reports", icon: BarChart3, superOnly: true },
      { href: "/reports/users", label: "User reports", icon: Users, perm: "reports" },
      { href: "/reports/financial", label: "Financial", icon: Banknote, perm: "reports" },
      { href: "/reports/trades", label: "Trades", icon: ClipboardList, perm: "reports" },
      { href: "/reports/tradebook", label: "Tradebook PDF", icon: FileText, perm: "reports" },
    ],
  },
  {
    title: "Management",
    items: [
      { href: "/management/sub-admins", label: "Admin Management", icon: Crown, superOnly: true },
      { href: "/management/employees", label: "Employees", icon: Users, superOrAdmin: true },
      { href: "/management/settlements", label: "Settlements", icon: Wallet, superOnly: true },
      {
        href: "/management/brokers",
        label: "Brokers",
        icon: Crown,
        perm: "brokers",
        brokerPerm: "sub_brokers",
        brokerLabel: "Sub-brokers",
        hideForSuperAdmin: true,
      },
      { href: "/management/pnl-sharing", label: "P&L Sharing", icon: Handshake, empPerm: "pnl_sharing" },
      { href: "/management/transfer-users", label: "Transfer User", icon: ArrowRightLeft, perm: "transfer_users" },
    ],
  },
  {
    title: "System",
    items: [
      { href: "/settings/platform", label: "Platform settings", icon: Cog },
      { href: "/settings/branding", label: "Branding", icon: Cog, adminTierOnly: true },
      { href: "/download-app", label: "Download App", icon: Download, empPerm: "download_app" },
      { href: "/holidays", label: "Holiday calendar", icon: Calendar, superOnly: true },
      { href: "/backup", label: "Backup & EOD", icon: DatabaseBackup, superOnly: true },
      { href: "/support", label: "Support", icon: MessageCircle, empPerm: "support", brokerPerm: "support" },
      { href: "/audit", label: "Audit logs", icon: History, empPerm: "audit" },
      { href: "/admin-actions", label: "Admin Actions", icon: History, empPerm: "audit" },
    ],
  },
];

/** Pure helper: filter nav for a given admin (role + permissions). */
export function filterAdminNav(
  admin: ReturnType<typeof useAdminAuthStore.getState>["admin"]
): AdminNavGroup[] {
  return ADMIN_NAV_GROUPS.map((g) => ({
    ...g,
    items: g.items.filter((it) => {
      // EMPLOYEE sees ONLY the sections they were granted (+ Dashboard). Every
      // un-gated admin convenience (Platform settings, Audit, P&L Sharing, etc.)
      // and every super/admin-tier item stays hidden.
      if (admin?.role === "EMPLOYEE") {
        if (it.href === "/dashboard") return true;
        // Employees are gated at the GRANULAR level: prefer the item's
        // employee perm (empPerm — e.g. "positions") over the shared umbrella
        // perm (perm — e.g. "trading_view") so one nav page can be granted
        // without the whole group.
        const key = it.empPerm ?? it.perm;
        return key ? canSee(admin, key) : false;
      }
      // BROKER-only gate: an item that carries a `brokerPerm` but NO shared
      // `perm` (e.g. Support) is visible to super/admin unconditionally, but a
      // broker only sees it when granted that tri-state permission. Items that
      // ALSO have `perm` (e.g. Brokers → Sub-brokers) are handled by the
      // `it.perm` branch below via its brokerPerm override, so skip them here.
      if (admin?.role === "BROKER" && it.brokerPerm && !it.perm) {
        return canSee(admin, it.brokerPerm);
      }
      if (it.hideForSuperAdmin && isSuperAdmin(admin)) return false;
      if (it.superOrAdmin) return isSuperAdmin(admin) || admin?.role === "ADMIN";
      if (it.adminTierOnly) return admin?.role === "ADMIN";
      if (it.superOnly) return isSuperAdmin(admin);
      if (it.perm) {
        const effective =
          admin?.role === "BROKER" && it.brokerPerm ? it.brokerPerm : it.perm;
        return canSee(admin, effective);
      }
      return true;
    }),
  })).filter((g) => g.items.length > 0);
}

/** Hook variant — bound to the auth store. */
export function useAdminNav(): AdminNavGroup[] {
  const admin = useAdminAuthStore((s) => s.admin);
  return filterAdminNav(admin);
}

/** Resolve label for an item given the current admin role. */
export function resolveNavLabel(
  it: AdminNavItem,
  admin: ReturnType<typeof useAdminAuthStore.getState>["admin"]
): string {
  return admin?.role === "BROKER" && it.brokerLabel ? it.brokerLabel : it.label;
}
