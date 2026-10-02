"use client";

import type {
  AdminPermissions,
  AdminUser,
  BrokerPermissions,
  PermissionLevel,
} from "@/types";

// Union of every section key the sidebar / pages might gate on. Admin
// uses the AdminPermissions subset; broker uses BrokerPermissions (which
// includes `sub_brokers`).
export type PermissionKey =
  | keyof AdminPermissions
  | keyof BrokerPermissions;

const LEVEL_ORDER: Record<PermissionLevel, number> = {
  OFF: 0,
  VIEW: 1,
  EDIT: 2,
};

// Umbrella nav perms whose sidebar items are gated on the umbrella but whose
// grants are made through the granular child toggles in the permissions editor.
// Granting any child must reveal the umbrella section. Mirrors the backend
// `dependencies._UMBRELLA_CHILDREN`.
const UMBRELLA_CHILDREN: Record<string, string[]> = {
  trading_view: ["orders", "positions", "marketwatch"],
  ledger: ["money_transactions", "broker_deposits"],
};

function atLeast(actual: PermissionLevel, required: PermissionLevel): boolean {
  return LEVEL_ORDER[actual] >= LEVEL_ORDER[required];
}

// True when the current admin may see / use a section at the requested
// minimum level. SUPER_ADMIN always returns true. ADMIN's permissions
// are boolean → treated as EDIT when true and OFF when false. BROKER's
// permissions are tri-state and compared directly.
export function canSee(
  admin: AdminUser | null | undefined,
  perm: PermissionKey,
  minLevel: PermissionLevel = "VIEW",
): boolean {
  if (!admin) return false;
  if (admin.role === "SUPER_ADMIN") return true;
  // EMPLOYEE reuses the ADMIN boolean-permission model (its granted sections,
  // capped at its parent admin's perms) → same visibility logic.
  if (admin.role === "ADMIN" || admin.role === "EMPLOYEE") {
    const ap = admin.admin_permissions;
    if (!ap) return false;
    // `brokers` and other admin keys are boolean. Admin doesn't have
    // `sub_brokers` — treat that as not granted.
    let v = (ap as any)[perm];
    // Umbrella → children fallback (mirrors backend _UMBRELLA_CHILDREN): the
    // permissions editor only exposes the granular children (orders / positions
    // / marketwatch, money_transactions / broker_deposits), never the umbrella
    // (trading_view / ledger) the nav gates on. So granting any child must also
    // reveal the umbrella section — else the whole Trading / Money group stays
    // hidden even though its pages were granted.
    if (v !== true) {
      const children = UMBRELLA_CHILDREN[perm as string];
      if (children && children.some((c) => (ap as any)[c] === true)) {
        v = true;
      }
    }
    if (typeof v !== "boolean") return false;
    // Boolean → EDIT when true (admin always has full edit on what they have)
    return v ? atLeast("EDIT", minLevel) : false;
  }
  if (admin.role === "BROKER") {
    const bp = admin.broker_permissions;
    if (!bp) return false;
    const v = (bp as any)[perm] as PermissionLevel | undefined;
    if (!v) return false;
    return atLeast(v, minLevel);
  }
  return false;
}

// Convenience — true only when the actor can write (EDIT) on the section.
// Use this on mutation buttons (Approve, Reject, Save, Block, Delete, …)
// to flip them to disabled with a tooltip when VIEW-only.
export function canEdit(
  admin: AdminUser | null | undefined,
  perm: PermissionKey,
): boolean {
  return canSee(admin, perm, "EDIT");
}

export function isSuperAdmin(admin: AdminUser | null | undefined): boolean {
  return admin?.role === "SUPER_ADMIN";
}

export function isAdmin(admin: AdminUser | null | undefined): boolean {
  return admin?.role === "ADMIN";
}

export function isBroker(admin: AdminUser | null | undefined): boolean {
  return admin?.role === "BROKER";
}
