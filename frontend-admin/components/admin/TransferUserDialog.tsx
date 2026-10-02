"use client";

import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ArrowRightLeft, Loader2, Search } from "lucide-react";
import { BrokerMgmtAPI, ManagementAPI } from "@/lib/api";
import { useAdminAuthStore } from "@/stores/authStore";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

interface User {
  id: string;
  user_code?: string;
  full_name?: string;
  email?: string;
}

interface Props {
  user: User;
  open: boolean;
  onClose: () => void;
  /** Called after a successful transfer so the parent can refresh
   *  its user list. The dialog also invalidates the standard
   *  ["admin","users"] query on its own, but onChange covers any
   *  page-local cache the parent maintains. */
  onChange?: () => void;
  /** Cross-admin transfer: a NON-super admin (with the `transfer_users`
   *  permission) moves their user to ANOTHER admin. Lists platform admins and
   *  hits the scope-checked /transfer/to-admin endpoint instead of the broker
   *  flow. Ignored for SUPER_ADMIN (already lists admins). */
  toAdmin?: boolean;
  /** Bulk mode: move several users at once to the picked admin via
   *  /transfer/to-admin-bulk. Title shows the count instead of one name. */
  bulkUserIds?: string[];
}

/**
 * Role-aware "Transfer User" picker.
 *
 *   SUPER_ADMIN → picks from the platform's admins
 *                  (GET /management/sub-admins) →
 *                  POST /management/users/{id}/assign
 *
 *   ADMIN       → picks from the brokers they own
 *                  (GET /management/brokers, backend-scoped) →
 *                  POST /management/users/{id}/assign-to-broker
 *
 *   BROKER      → picks from their direct sub-brokers
 *                  (same brokers endpoint, backend auto-scopes
 *                  to `assigned_broker_id = self.id`) →
 *                  same assign-to-broker endpoint
 *
 * Backend always re-stamps `assigned_admin_id`, `assigned_broker_id`,
 * `broker_ancestry` correctly (`broker_management_service.py:488`),
 * so every downstream admin query (`scoped_user_ids` in
 * `core/dependencies.py:164`) starts seeing the user in the new
 * owner's dashboard from the very next poll — full trade / wallet /
 * position history travels with the user.
 */
export function TransferUserDialog({ user, open, onClose, onChange, toAdmin, bulkUserIds }: Props) {
  const qc = useQueryClient();
  const admin = useAdminAuthStore((s) => s.admin);
  const callerRole = admin?.role;
  const bulk = (bulkUserIds?.length ?? 0) > 0;
  // toAdmin routes a permitted ADMIN caller down the cross-admin path (list all
  // admins, hit /transfer/to-admin). SUPER_ADMIN already lists admins natively.
  // Bulk always targets admins too.
  const crossAdmin = (!!toAdmin || bulk) && callerRole !== "SUPER_ADMIN";
  const [query, setQuery] = useState("");
  const [pickedId, setPickedId] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // SUPER_ADMIN destinations — list of admins.
  const subAdminsQ = useQuery({
    queryKey: ["admin", "sub-admins", "all-for-transfer"],
    queryFn: () => ManagementAPI.listSubAdmins({ page_size: 200 }),
    enabled: open && callerRole === "SUPER_ADMIN",
    staleTime: 60_000,
  });

  // Cross-admin destinations — every platform admin (permitted ADMIN caller).
  const adminTargetsQ = useQuery({
    queryKey: ["admin", "transfer-admins"],
    queryFn: () => ManagementAPI.transferAdminTargets(),
    enabled: open && crossAdmin,
    staleTime: 60_000,
  });

  // ADMIN + BROKER destinations — backend-scoped broker list.
  // ADMIN gets their top brokers; BROKER gets their sub-brokers.
  const brokersQ = useQuery({
    queryKey: ["admin", "brokers", "all-for-transfer"],
    queryFn: () => BrokerMgmtAPI.list({ page_size: 200 }),
    enabled: open && !crossAdmin && (callerRole === "ADMIN" || callerRole === "BROKER"),
    staleTime: 60_000,
  });

  const loading = subAdminsQ.isLoading || brokersQ.isLoading || adminTargetsQ.isLoading;
  const rawList: any[] = useMemo(() => {
    if (crossAdmin) return adminTargetsQ.data?.items ?? [];
    if (callerRole === "SUPER_ADMIN") return subAdminsQ.data?.items ?? [];
    return brokersQ.data?.items ?? [];
  }, [crossAdmin, callerRole, subAdminsQ.data, brokersQ.data, adminTargetsQ.data]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return rawList;
    return rawList.filter((d) => {
      const hay = `${d.user_code ?? ""} ${d.full_name ?? ""} ${d.email ?? ""}`.toLowerCase();
      return hay.includes(q);
    });
  }, [rawList, query]);

  function close() {
    setPickedId(null);
    setQuery("");
    onClose();
  }

  async function submit() {
    if (!pickedId) {
      toast.error("Pick a destination first");
      return;
    }
    const dest = rawList.find((d) => String(d.id) === pickedId);
    if (!dest) {
      toast.error("Destination not found");
      return;
    }
    setSubmitting(true);
    try {
      if (bulk) {
        const res = await ManagementAPI.transferUsersToAdminBulk(bulkUserIds!, pickedId);
        toast.success(
          `Transferred ${res.transferred} user${res.transferred === 1 ? "" : "s"}${res.skipped ? ` · ${res.skipped} skipped` : ""} to ${dest.full_name ?? dest.user_code ?? "admin"}`,
        );
        qc.invalidateQueries({ queryKey: ["admin", "users"] });
        onChange?.();
        close();
        return;
      }
      if (crossAdmin) {
        await ManagementAPI.transferUserToAdmin(user.id, pickedId);
      } else if (callerRole === "SUPER_ADMIN") {
        await ManagementAPI.assignUser(user.id, pickedId);
      } else {
        // ADMIN + BROKER both hit the broker-assign endpoint; backend
        // scope check (assert_user_in_scope + assert_broker_in_scope)
        // rejects anything outside the caller's subtree.
        await BrokerMgmtAPI.assignUser(user.id, pickedId);
      }
      toast.success(
        `Transferred ${user.full_name ?? user.user_code ?? "user"} to ${dest.full_name ?? dest.user_code ?? "destination"}`,
      );
      qc.invalidateQueries({ queryKey: ["admin", "users"] });
      qc.invalidateQueries({ queryKey: ["admin", "user", user.id] });
      // Sub-admin / broker per-id user-list caches (used on the
      // sub-admin and broker detail pages) — invalidate so the
      // destination dashboard's user list reflects immediately.
      qc.invalidateQueries({ queryKey: ["admin", "sub-admin", "users"] });
      qc.invalidateQueries({ queryKey: ["admin", "broker", "users"] });
      onChange?.();
      close();
    } catch (e: any) {
      toast.error(e?.message || "Transfer failed");
    } finally {
      setSubmitting(false);
    }
  }

  const destinationLabel =
    crossAdmin || callerRole === "SUPER_ADMIN"
      ? "Pick an admin"
      : callerRole === "ADMIN"
        ? "Pick a broker"
        : "Pick a sub-broker";

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) close(); }}>
      <DialogContent className="max-w-md gap-3 p-5">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-base font-semibold">
            <ArrowRightLeft className="size-4 text-primary" />
            {bulk
              ? `Transfer ${bulkUserIds!.length} user${bulkUserIds!.length === 1 ? "" : "s"}`
              : `Transfer ${user.full_name || user.user_code || "user"}`}
          </DialogTitle>
          <DialogDescription className="text-xs">
            All trade history, wallet entries, positions and orders move with
            the user. The new owner sees them on their next dashboard refresh.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-2">
          <label className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
            {destinationLabel}
          </label>
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search code / name / email"
              className="h-9 pl-8 text-sm"
              autoFocus
            />
          </div>

          <div className="max-h-72 overflow-y-auto rounded-md border border-border">
            {loading && (
              <div className="flex items-center justify-center gap-2 py-6 text-xs text-muted-foreground">
                <Loader2 className="size-3.5 animate-spin" /> Loading…
              </div>
            )}
            {!loading && filtered.length === 0 && (
              <div className="py-6 text-center text-xs text-muted-foreground">
                {rawList.length === 0
                  ? `No ${crossAdmin || callerRole === "SUPER_ADMIN" ? "admins" : "brokers"} available.`
                  : "No matches."}
              </div>
            )}
            {!loading &&
              filtered.map((d) => {
                const id = String(d.id);
                const picked = id === pickedId;
                return (
                  <button
                    key={id}
                    type="button"
                    onClick={() => setPickedId(id)}
                    className={cn(
                      "flex w-full items-center gap-3 border-b border-border px-3 py-2 text-left text-sm transition-colors last:border-b-0",
                      picked
                        ? "bg-primary/10 text-foreground"
                        : "hover:bg-muted/40",
                    )}
                  >
                    <span
                      className={cn(
                        "grid size-4 place-items-center rounded-full border",
                        picked
                          ? "border-primary bg-primary text-primary-foreground"
                          : "border-border",
                      )}
                    >
                      {picked && <span className="size-1.5 rounded-full bg-current" />}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="truncate font-medium">
                        {d.full_name || d.user_code || "—"}
                      </div>
                      <div className="truncate text-[11px] text-muted-foreground">
                        {[d.user_code, d.email].filter(Boolean).join(" · ")}
                      </div>
                    </div>
                  </button>
                );
              })}
          </div>
        </div>

        <DialogFooter className="flex-row justify-end gap-2 pt-1">
          <Button variant="ghost" size="sm" onClick={close} disabled={submitting}>
            Cancel
          </Button>
          <Button
            size="sm"
            onClick={submit}
            disabled={!pickedId || submitting}
            loading={submitting}
          >
            <ArrowRightLeft className="size-4" /> Transfer
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
