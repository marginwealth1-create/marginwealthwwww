"use client";

import { ArrowRightLeft, ChevronRight } from "lucide-react";
import type { AdminUser } from "@/types";

type Row = {
  assigned_admin_id?: string | null;
  assigned_admin_name?: string | null;
  assigned_broker_id?: string | null;
  assigned_broker_name?: string | null;
  // True when assigned_broker is itself a sub-broker (sits under another
  // broker). Drives the chip label "Sub-broker: <name>" vs. "Broker: <name>".
  assigned_broker_is_sub?: boolean | null;
  // Parent broker of the assigned broker (only when assigned_broker is a
  // sub-broker). Lets the badge show the full hierarchy chain
  // "Sub-broker: <sub> → Broker: <parent>" so the admin can tell at a
  // glance whose downline this user belongs to.
  parent_broker_id?: string | null;
  parent_broker_name?: string | null;
  // Stamped every time a Transfer User action lands the row in someone's
  // pool. Non-null means this user reached the current viewer's
  // dashboard via reassignment (not direct creation). Drives the small
  // "Transferred" chip rendered next to the owner chip below.
  last_transferred_at?: string | null;
};

/** Compact pill used in admin tables (Users / Deposits / Withdrawals /
 * Positions) to label each row as "Self" or "Broker: <name>" — and for
 * super-admin viewing, also "Admin: <name>".
 *
 * Self = the row belongs directly to the viewing admin's pool (no broker
 * in between). Broker = the row's user is in some broker's subtree; we
 * show the broker name so the admin can tell at a glance whose user it is.
 *
 * If the user was reassigned into this pool via the `Transfer User`
 * action, a secondary "Transferred" chip is rendered next to the owner
 * chip. Helps the destination admin / broker / sub-broker spot accounts
 * that landed in their dashboard through reassignment vs. ones they
 * personally created — important context when the user has trade history
 * the new owner didn't generate.
 */
export function OwnerBadge({
  row,
  me,
}: {
  row: Row;
  me: AdminUser | null | undefined;
}) {
  const wasTransferred = !!row.last_transferred_at;
  const transferredChip = wasTransferred ? (
    <span
      title={`Transferred on ${new Date(row.last_transferred_at!).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })}`}
      className="inline-flex items-center gap-1 rounded-full bg-violet-500/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-violet-400 ring-1 ring-inset ring-violet-500/30"
    >
      <ArrowRightLeft className="size-3" />
      Transferred
    </span>
  ) : null;

  let ownerChip: React.ReactNode;
  if (row.assigned_broker_id) {
    const label = row.assigned_broker_name || `…${row.assigned_broker_id.slice(-6)}`;
    const isSub = !!row.assigned_broker_is_sub;
    const cls = isSub
      ? "bg-brand-700/15 text-brand-300 ring-brand-500/30"
      : "bg-brand-500/10 text-brand-400 ring-brand-500/30";
    const subChip = (
      <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${cls}`}>
        <span className="text-[10px] uppercase tracking-wide opacity-70">
          {isSub ? "Sub-broker" : "Broker"}
        </span>
        <span>{label}</span>
      </span>
    );
    // When the assigned broker is itself a sub-broker, also surface the
    // parent broker chip so the admin sees the full chain at a glance.
    if (isSub && row.parent_broker_id) {
      const parentLabel = row.parent_broker_name || `…${row.parent_broker_id.slice(-6)}`;
      ownerChip = (
        <span className="inline-flex flex-wrap items-center gap-1">
          {subChip}
          <ChevronRight className="size-3 text-muted-foreground" />
          <span className="inline-flex items-center gap-1 rounded-full bg-brand-500/10 px-2 py-0.5 text-[11px] font-medium text-brand-400 ring-1 ring-inset ring-brand-500/30">
            <span className="text-[10px] uppercase tracking-wide opacity-70">Broker</span>
            <span>{parentLabel}</span>
          </span>
        </span>
      );
    } else {
      ownerChip = subChip;
    }
  } else if (me?.role === "SUPER_ADMIN" && row.assigned_admin_id) {
    const label = row.assigned_admin_name || `…${row.assigned_admin_id.slice(-6)}`;
    ownerChip = (
      <span className="inline-flex items-center gap-1 rounded-full bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-400 ring-1 ring-inset ring-amber-500/30">
        <span className="text-[10px] uppercase tracking-wide opacity-70">Admin</span>
        <span>{label}</span>
      </span>
    );
  } else {
    ownerChip = (
      <span className="inline-flex items-center rounded-full bg-emerald-500/10 px-2 py-0.5 text-[11px] font-medium text-emerald-400 ring-1 ring-inset ring-emerald-500/30">
        Self
      </span>
    );
  }

  if (!transferredChip) return <>{ownerChip}</>;
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      {ownerChip}
      {transferredChip}
    </span>
  );
}
