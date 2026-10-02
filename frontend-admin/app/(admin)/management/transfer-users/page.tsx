"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowRightLeft, Loader2, Search } from "lucide-react";
import { UsersAPI } from "@/lib/api";
import { useAdminAuthStore } from "@/stores/authStore";
import { canSee } from "@/lib/permissions";
import { PageHeader } from "@/components/common/PageHeader";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { TransferUserDialog } from "@/components/admin/TransferUserDialog";

/**
 * Transfer User — a permitted admin moves their OWN users to ANOTHER admin on
 * the platform. Ownership (positions, wallet, trade history) travels with the
 * user; the user then logs in under the destination admin's link only. Gated
 * by the super-admin-granted `transfer_users` permission (nav hides it; this
 * page also guards against a direct URL hit).
 */
export default function TransferUsersPage() {
  const admin = useAdminAuthStore((s) => s.admin);
  const [q, setQ] = useState("");
  const [picked, setPicked] = useState<any | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkOpen, setBulkOpen] = useState(false);

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  const allowed = canSee(admin, "transfer_users");

  const { data, isFetching, refetch } = useQuery({
    queryKey: ["admin", "users", "transfer", q],
    queryFn: () => UsersAPI.list({ q: q || undefined, page: 1, page_size: 100 }),
    enabled: allowed,
  });

  const users = useMemo<any[]>(() => data?.items ?? [], [data?.items]);

  if (!allowed) {
    return (
      <div className="space-y-4">
        <PageHeader title="Transfer User" description="Move your users to another admin." />
        <div className="rounded-lg border border-border bg-card p-6 text-sm text-muted-foreground">
          You don&apos;t have permission to transfer users. Ask the super-admin to enable it.
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Transfer User"
        description="Select one of your users and move them to another admin. Positions, wallet and history travel with the user; they then log in under the new admin's link."
      />

      <div className="flex flex-wrap items-center gap-3">
        <div className="relative max-w-md flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search your users by code / name / mobile"
            className="h-10 pl-9"
          />
        </div>
        {selected.size > 0 && (
          <Button className="gap-1.5" onClick={() => setBulkOpen(true)}>
            <ArrowRightLeft className="size-4" /> Transfer {selected.size} selected
          </Button>
        )}
      </div>

      {users.length > 0 && (
        <label className="flex items-center gap-2 text-xs text-muted-foreground">
          <input
            type="checkbox"
            className="size-4 accent-primary"
            checked={users.every((u) => selected.has(u.id))}
            onChange={(e) =>
              setSelected(e.target.checked ? new Set(users.map((u) => u.id)) : new Set())
            }
          />
          Select all visible ({users.length})
        </label>
      )}

      <div className="overflow-hidden rounded-lg border border-border">
        {isFetching && users.length === 0 && (
          <div className="flex items-center justify-center gap-2 py-10 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> Loading…
          </div>
        )}
        {!isFetching && users.length === 0 && (
          <div className="py-10 text-center text-sm text-muted-foreground">No users found.</div>
        )}
        {users.map((u) => (
          <div
            key={u.id}
            className="flex items-center gap-3 border-b border-border px-4 py-3 last:border-b-0"
          >
            <input
              type="checkbox"
              className="size-4 shrink-0 accent-primary"
              checked={selected.has(u.id)}
              onChange={() => toggle(u.id)}
              aria-label={`Select ${u.user_code}`}
            />
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-semibold text-foreground">
                {u.full_name || u.user_code || "—"}
              </div>
              <div className="truncate text-[11px] text-muted-foreground">
                {[u.user_code, u.mobile, u.email].filter(Boolean).join(" · ")}
              </div>
            </div>
            <Button size="sm" variant="outline" className="gap-1.5" onClick={() => setPicked(u)}>
              <ArrowRightLeft className="size-4" /> Transfer
            </Button>
          </div>
        ))}
      </div>

      {picked && (
        <TransferUserDialog
          user={picked}
          open={!!picked}
          onClose={() => setPicked(null)}
          onChange={() => refetch()}
          toAdmin
        />
      )}

      {bulkOpen && (
        <TransferUserDialog
          user={{ id: "" }}
          open={bulkOpen}
          onClose={() => setBulkOpen(false)}
          onChange={() => {
            setSelected(new Set());
            refetch();
          }}
          toAdmin
          bulkUserIds={[...selected]}
        />
      )}
    </div>
  );
}
