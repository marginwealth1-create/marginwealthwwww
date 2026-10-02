"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  Plus,
  ShieldOff,
  ShieldCheck,
  Pencil,
  LogIn,
  MoreVertical,
  Eye,
  EyeOff,
  KeyRound,
  CreditCard,
  Trash2,
} from "lucide-react";

import { BrokerMgmtAPI, ManagementAPI, setTokens } from "@/lib/api";
import { useAdminAuthStore } from "@/stores/authStore";
import { canSee } from "@/lib/permissions";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { PageHeader } from "@/components/common/PageHeader";
import { DataTable, type Column } from "@/components/common/DataTable";
import { STORAGE_KEYS } from "@/lib/constants";
import type {
  AdminUser,
  BrokerPermissions,
  PermissionLevel,
} from "@/types";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

const PERMISSION_LABELS: Array<{ key: keyof BrokerPermissions; label: string }> = [
  { key: "users", label: "Users" },
  { key: "kyc", label: "KYC review" },
  { key: "deposits", label: "Deposits" },
  { key: "withdrawals", label: "Withdrawals" },
  { key: "banks", label: "Bank accounts" },
  { key: "segment_settings", label: "Segment settings" },
  { key: "risk", label: "Risk management" },
  { key: "netting", label: "Netting overrides" },
  { key: "trading_view", label: "Positions & Orders" },
  { key: "ledger", label: "Ledger (Money txns & Broker deposits)" },
  { key: "reports", label: "Reports" },
  { key: "brokerage", label: "Brokerage" },
  { key: "sub_brokers", label: "Sub-brokers" },
  { key: "bonuses", label: "Bonuses" },
  { key: "user_password", label: "Change password" },
  { key: "support", label: "Support number" },
];

const ALL_OFF: BrokerPermissions = {
  users: "OFF",
  kyc: "OFF",
  deposits: "OFF",
  withdrawals: "OFF",
  segment_settings: "OFF",
  risk: "OFF",
  netting: "OFF",
  trading_view: "OFF",
  ledger: "OFF",
  reports: "OFF",
  brokerage: "OFF",
  sub_brokers: "OFF",
  banks: "OFF",
  bonuses: "OFF",
  user_password: "OFF",
  support: "OFF",
};

const LEVEL_ORDER: Record<PermissionLevel, number> = { OFF: 0, VIEW: 1, EDIT: 2 };

function levelAllowed(cap: PermissionLevel, level: PermissionLevel): boolean {
  return LEVEL_ORDER[cap] >= LEVEL_ORDER[level];
}

export default function BrokersPage() {
  const qc = useQueryClient();
  const router = useRouter();
  const admin = useAdminAuthStore((s) => s.admin);
  // Broker actor creates sub-brokers under their own subtree; the backend
  // wires `broker_ancestry` automatically. Only the UI noun changes.
  const isBrokerActor = admin?.role === "BROKER";
  const isSuperAdmin = admin?.role === "SUPER_ADMIN";
  const noun = isBrokerActor ? "Sub-broker" : "Broker";
  const nounPlural = isBrokerActor ? "Sub-brokers" : "Brokers";
  const [q, setQ] = useState("");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<any | null>(null);
  const [loginAsId, setLoginAsId] = useState<string | null>(null);
  // Reset-password dialog state — mirrors the sub-admins page so an
  // admin can hand a broker / sub-broker a new password from the
  // three-dot menu without bouncing through user.py reset flows.
  const [resetPwTarget, setResetPwTarget] = useState<{ id: string; label: string } | null>(null);
  const [newPw, setNewPw] = useState("");
  const [showNewPw, setShowNewPw] = useState(false);

  // Cap drives the form greying — only fetched once per session.
  const { data: capRes } = useQuery({
    queryKey: ["admin", "brokers-cap"],
    queryFn: () => BrokerMgmtAPI.maxGrantable(),
    enabled: !!admin && admin.role !== "BROKER" ? true : !!admin?.broker_permissions,
  });
  const cap = (capRes?.cap ?? {}) as Record<keyof BrokerPermissions, PermissionLevel>;

  const { data, isFetching } = useQuery({
    queryKey: ["admin", "brokers", q],
    queryFn: () => BrokerMgmtAPI.list({ q: q || undefined, page: 1, page_size: 100 }),
    enabled: !!admin,
    // Default 3-retry-with-backoff masquerades real errors as "slow loading"
    // — the broker list query is cheap, no flake retries needed. If it
    // fails, fail fast and let the error surface to the toast.
    retry: false,
  });

  // Super-admin only: load sub-admins so the create-broker form can
  // pin the new broker to a specific admin (otherwise → platform pool).
  const { data: adminsList = [] } = useQuery({
    queryKey: ["admin", "sub-admins", "for-broker-create"],
    queryFn: async () => {
      const { items } = await ManagementAPI.listSubAdmins({ page_size: 200 });
      return items as Array<{ id: string; full_name: string; user_code: string }>;
    },
    enabled: isSuperAdmin,
  });

  const blockMut = useMutation({
    mutationFn: (id: string) => BrokerMgmtAPI.block(id),
    onSuccess: () => {
      toast.success(`${noun} blocked`);
      qc.invalidateQueries({ queryKey: ["admin", "brokers"] });
    },
    onError: (e: any) => toast.error(e.message),
  });
  const unblockMut = useMutation({
    mutationFn: (id: string) => BrokerMgmtAPI.unblock(id),
    onSuccess: () => {
      toast.success(`${noun} unblocked`);
      qc.invalidateQueries({ queryKey: ["admin", "brokers"] });
    },
    onError: (e: any) => toast.error(e.message),
  });
  const brokerGwMut = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      BrokerMgmtAPI.setPaymentGateway(id, enabled),
    onSuccess: (_d, v) => {
      toast.success(
        v.enabled
          ? `Online payment ON — this ${noun}'s users get the UPI pay-in`
          : `Online payment OFF — this ${noun}'s users use manual deposits`,
      );
      qc.invalidateQueries({ queryKey: ["admin", "brokers"] });
    },
    onError: (e: any) =>
      toast.error(e?.response?.data?.detail ?? e?.message ?? "Failed"),
  });
  const deleteMut = useMutation({
    mutationFn: (id: string) => BrokerMgmtAPI.deleteBroker(id),
    onSuccess: () => {
      toast.success(`${noun} deleted`);
      qc.invalidateQueries({ queryKey: ["admin", "brokers"] });
    },
    onError: (e: any) =>
      toast.error(e?.response?.data?.detail ?? e?.message ?? "Delete failed"),
  });
  const resetPwMut = useMutation({
    mutationFn: ({ id, pw }: { id: string; pw: string }) =>
      BrokerMgmtAPI.resetPassword(id, pw),
    onSuccess: () => {
      toast.success("Password reset");
      setResetPwTarget(null);
      setNewPw("");
      setShowNewPw(false);
    },
    onError: (e: any) =>
      toast.error(e?.response?.data?.detail ?? e?.message ?? "Reset failed"),
  });

  async function loginAs(broker: any) {
    setLoginAsId(broker.id);
    try {
      const r = await BrokerMgmtAPI.impersonate(broker.id);
      try {
        const prevAccess = window.localStorage.getItem(STORAGE_KEYS.accessToken);
        const prevRefresh = window.localStorage.getItem(STORAGE_KEYS.refreshToken);
        const prevAdmin = window.localStorage.getItem("nb.admin.auth");
        if (prevAccess && prevRefresh) {
          window.localStorage.setItem(
            "nb.admin.impersonatorSession",
            JSON.stringify({
              access: prevAccess,
              refresh: prevRefresh,
              admin: prevAdmin,
              ts: Date.now(),
            }),
          );
        }
      } catch {
        /* ignore */
      }
      setTokens(r.access_token, r.refresh_token);
      const next: AdminUser = {
        id: r.admin.id,
        user_code: r.admin.user_code,
        email: r.admin.email,
        full_name: r.admin.full_name,
        role: r.admin.role,
        last_login_at: null,
        admin_permissions: r.admin.admin_permissions ?? null,
        broker_permissions: r.admin.broker_permissions ?? null,
        pnl_share_pct: r.admin.pnl_share_pct ?? null,
      };
      useAdminAuthStore.setState({ admin: next });
      window.localStorage.setItem(
        "nb.admin.auth",
        JSON.stringify({ state: { admin: next }, version: 0 }),
      );
      qc.clear();
      toast.success(`Logged in as ${r.admin.user_code}`);
      router.push("/dashboard");
    } catch (e: any) {
      toast.error(e?.response?.data?.error?.message || e.message || "Failed");
    } finally {
      setLoginAsId(null);
    }
  }

  // Permission gate: super-admin always; admin with `brokers`; broker with `sub_brokers >= VIEW`.
  const canManage =
    admin?.role === "SUPER_ADMIN" ||
    (admin?.role === "ADMIN" && !!admin.admin_permissions?.brokers) ||
    (admin?.role === "BROKER" &&
      !!admin.broker_permissions &&
      LEVEL_ORDER[admin.broker_permissions.sub_brokers] >= LEVEL_ORDER["VIEW"]);
  const canCreate =
    admin?.role === "SUPER_ADMIN" ||
    (admin?.role === "ADMIN" && !!admin.admin_permissions?.brokers) ||
    (admin?.role === "BROKER" &&
      !!admin.broker_permissions &&
      LEVEL_ORDER[admin.broker_permissions.sub_brokers] >= LEVEL_ORDER["EDIT"]);

  if (!canManage) {
    return (
      <div className="rounded-md border border-border bg-card p-6 text-sm text-muted-foreground">
        You don't have permission to manage {nounPlural.toLowerCase()}.
      </div>
    );
  }

  const cols: Column<any>[] = [
    { key: "user_code", header: "Code" },
    { key: "full_name", header: "Name" },
    { key: "email", header: "Email" },
    { key: "mobile", header: "Mobile" },
    {
      key: "pnl_share_pct",
      header: "PNL share %",
      render: (r) => `${r.pnl_share_pct ?? "0"}%`,
    },
    {
      key: "brokerage_share_pct",
      header: "Brokerage share %",
      render: (r) => `${r.brokerage_share_pct ?? r.pnl_share_pct ?? "0"}%`,
    },
    {
      key: "users",
      header: "Users",
      render: (r) => `${r.user_count} (${r.subtree_user_count} subtree)`,
    },
    {
      key: "status",
      header: "Status",
      render: (r) => (
        <span
          className={
            r.status === "ACTIVE"
              ? "rounded bg-emerald-500/10 px-2 py-0.5 text-xs text-emerald-500"
              : "rounded bg-red-500/10 px-2 py-0.5 text-xs text-red-500"
          }
        >
          {r.status}
        </span>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      render: (r) => (
        <div
          className="flex justify-end"
          onClick={(e) => e.stopPropagation()}
        >
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                aria-label="Open actions"
                disabled={loginAsId === r.id}
              >
                <MoreVertical className="size-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onSelect={() => loginAs(r)}>
                <LogIn className="size-4 text-primary" />
                Login
              </DropdownMenuItem>
              <DropdownMenuItem
                onSelect={() => router.push(`/management/brokers/${r.id}`)}
              >
                <Eye className="size-4" />
                View {noun.toLowerCase()} profile
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={() => setEditing(r)}>
                <Pencil className="size-4" />
                Edit permissions
              </DropdownMenuItem>
              {r.status === "ACTIVE" ? (
                <DropdownMenuItem onSelect={() => blockMut.mutate(r.id)}>
                  <ShieldOff className="size-4 text-red-500" />
                  Block
                </DropdownMenuItem>
              ) : (
                <DropdownMenuItem onSelect={() => unblockMut.mutate(r.id)}>
                  <ShieldCheck className="size-4 text-emerald-500" />
                  Unblock
                </DropdownMenuItem>
              )}
              <DropdownMenuItem
                onSelect={() =>
                  brokerGwMut.mutate({ id: r.id, enabled: !r.payment_gateway_enabled })
                }
              >
                <CreditCard
                  className={`size-4 ${r.payment_gateway_enabled ? "text-emerald-500" : "text-muted-foreground"}`}
                />
                {r.payment_gateway_enabled
                  ? "Online payment: ON (turn off)"
                  : "Online payment: OFF (turn on)"}
              </DropdownMenuItem>
              <DropdownMenuItem
                onSelect={() =>
                  setResetPwTarget({
                    id: r.id,
                    label: r.full_name || r.user_code || "broker",
                  })
                }
              >
                <KeyRound className="size-4" />
                Reset Password
              </DropdownMenuItem>
              <DropdownMenuItem
                className="text-red-500"
                onSelect={() => {
                  if (
                    confirm(
                      `Permanently delete ${r.user_code}? Their users and sub-${noun.toLowerCase()}s move up to the parent.`,
                    )
                  ) {
                    deleteMut.mutate(r.id);
                  }
                }}
              >
                <Trash2 className="size-4 text-red-500" />
                Delete
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader
        title={nounPlural}
        actions={
          <div className="flex items-center gap-2">
            <Input
              placeholder="Search…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              className="h-10 w-56"
            />
            {canCreate && (
              <Button onClick={() => setCreating(true)}>
                <Plus className="size-4" /> New {noun.toLowerCase()}
              </Button>
            )}
          </div>
        }
      />

      {/* Desktop table */}
      <div className="hidden md:block">
        <DataTable
          columns={cols}
          rows={data?.items}
          keyExtractor={(r) => r.id}
          loading={isFetching && !data}
          onRowClick={(r) => router.push(`/management/brokers/${r.id}`)}
        />
      </div>

      {/* Mobile cards — same shape as the sub-admin / user cards */}
      <div className="md:hidden">
        {isFetching && !data ? (
          <div className="py-10 text-center text-sm text-muted-foreground">Loading…</div>
        ) : (data?.items ?? []).length === 0 ? (
          <div className="py-10 text-center text-sm text-muted-foreground">
            No {nounPlural.toLowerCase()} yet.
          </div>
        ) : (
          <ul className="space-y-2">
            {(data?.items ?? []).map((r: any) => (
              <BrokerMobileCard
                key={r.id}
                row={r}
                noun={noun}
                loginAsBusy={loginAsId === r.id}
                onOpen={() => router.push(`/management/brokers/${r.id}`)}
                onLoginAs={() => loginAs(r)}
                onEdit={() => setEditing(r)}
                onBlock={() => blockMut.mutate(r.id)}
                onUnblock={() => unblockMut.mutate(r.id)}
                onPaymentGateway={() =>
                  brokerGwMut.mutate({ id: r.id, enabled: !r.payment_gateway_enabled })
                }
                onResetPw={() =>
                  setResetPwTarget({
                    id: r.id,
                    label: r.full_name || r.user_code || "broker",
                  })
                }
                onDelete={() => {
                  if (
                    confirm(
                      `Permanently delete ${r.user_code}? Their users and sub-${noun.toLowerCase()}s move up to the parent.`,
                    )
                  ) {
                    deleteMut.mutate(r.id);
                  }
                }}
              />
            ))}
          </ul>
        )}
      </div>

      <CreateBrokerDialog
        open={creating}
        onOpenChange={setCreating}
        cap={cap}
        noun={noun}
        isSuperAdmin={isSuperAdmin}
        adminsList={adminsList}
        onCreated={() => qc.invalidateQueries({ queryKey: ["admin", "brokers"] })}
      />
      {editing && (
        <EditBrokerDialog
          broker={editing}
          cap={cap}
          noun={noun}
          onClose={() => setEditing(null)}
          onSaved={() => qc.invalidateQueries({ queryKey: ["admin", "brokers"] })}
        />
      )}

      {/* Reset Password dialog — minted via three-dot menu. Same UX as
          the sub-admins page: show target name, password input with
          eye-toggle, min 8 char client guard, error toast on backend
          rejection. Backend enforces scope (assert_broker_in_scope)
          so a broker can only reset their own sub-brokers, an admin
          only their brokers, super-admin any broker. */}
      <Dialog
        open={!!resetPwTarget}
        onOpenChange={(o) => {
          if (!o) {
            setResetPwTarget(null);
            setNewPw("");
            setShowNewPw(false);
          }
        }}
      >
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>Reset password</DialogTitle>
          </DialogHeader>
          {resetPwTarget && (
            <div className="space-y-3">
              <p className="text-xs text-muted-foreground">
                You're resetting the password for{" "}
                <span className="font-semibold text-foreground">
                  {resetPwTarget.label}
                </span>
                . They'll be able to sign in immediately with the new value.
              </p>
              <div className="space-y-1.5">
                <Label>New password (min 8 chars)</Label>
                <div className="relative">
                  <Input
                    type={showNewPw ? "text" : "password"}
                    value={newPw}
                    onChange={(e) => setNewPw(e.target.value)}
                    autoFocus
                    className="pr-9"
                  />
                  <button
                    type="button"
                    onClick={() => setShowNewPw((v) => !v)}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                    aria-label={showNewPw ? "Hide password" : "Show password"}
                  >
                    {showNewPw ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                  </button>
                </div>
              </div>
            </div>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setResetPwTarget(null);
                setNewPw("");
                setShowNewPw(false);
              }}
            >
              Cancel
            </Button>
            <Button
              onClick={() => {
                if (newPw.length < 6) {
                  toast.error("Password must be at least 6 characters");
                  return;
                }
                if (resetPwTarget) {
                  resetPwMut.mutate({ id: resetPwTarget.id, pw: newPw });
                }
              }}
              disabled={resetPwMut.isPending}
            >
              {resetPwMut.isPending ? "Resetting…" : "Reset password"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ── Permission row (compact segmented control) ───────────────────────
//
// Layout: label on the left, 3-button segmented control on the right.
// Visual hierarchy:
//   OFF  → muted/grey (no permission, default)
//   VIEW → blue-ish (read-only)
//   EDIT → solid primary green (full access)
// Buttons above the actor's own cap are visibly disabled (40 % opacity +
// "not-allowed" cursor) and carry a tooltip so the user understands why.
function PermissionRow({
  label,
  cap,
  value,
  onChange,
}: {
  label: string;
  cap: PermissionLevel;
  value: PermissionLevel;
  onChange: (v: PermissionLevel) => void;
}) {
  const levels: Array<{ k: PermissionLevel; tone: string }> = [
    { k: "OFF", tone: "bg-muted text-foreground" },
    { k: "VIEW", tone: "bg-brand-500/15 text-brand-500" },
    { k: "EDIT", tone: "bg-primary text-primary-foreground" },
  ];
  return (
    <div className="flex items-center justify-between gap-3 px-3 py-1.5 text-sm hover:bg-accent/40">
      <div className="text-foreground">{label}</div>
      <div className="flex shrink-0 overflow-hidden rounded border border-border">
        {levels.map(({ k: lv, tone }, idx) => {
          const allowed = levelAllowed(cap, lv);
          const active = value === lv;
          const sep = idx > 0 ? "border-l border-border" : "";
          return (
            <button
              key={lv}
              type="button"
              disabled={!allowed}
              title={
                !allowed
                  ? `Your cap is ${cap}; you can't grant ${lv}`
                  : undefined
              }
              onClick={() => onChange(lv)}
              className={
                `h-7 px-3 text-[11px] font-semibold uppercase tracking-wider transition-colors ${sep} ` +
                (active
                  ? tone
                  : allowed
                    ? "bg-background text-muted-foreground hover:bg-accent"
                    : "cursor-not-allowed bg-background text-muted-foreground opacity-40")
              }
            >
              {lv}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// Bulk-set toolbar (Set all → OFF / VIEW / EDIT) — capped per key.
function BulkSetToolbar({
  cap,
  onApply,
}: {
  cap: Record<keyof BrokerPermissions, PermissionLevel>;
  onApply: (level: PermissionLevel) => void;
}) {
  const levels: PermissionLevel[] = ["OFF", "VIEW", "EDIT"];
  return (
    <div className="flex items-center gap-2 text-xs text-muted-foreground">
      <span>Quick set all:</span>
      {levels.map((lv) => (
        <button
          key={lv}
          type="button"
          onClick={() => onApply(lv)}
          className="rounded border border-border px-2 py-1 font-semibold uppercase tracking-wider hover:bg-accent"
        >
          {lv}
        </button>
      ))}
    </div>
  );
}

// Apply a bulk level to every permission, capping at each key's own ceiling.
function applyBulk(
  cap: Record<keyof BrokerPermissions, PermissionLevel>,
  level: PermissionLevel,
): BrokerPermissions {
  const next = { ...ALL_OFF };
  (Object.keys(next) as Array<keyof BrokerPermissions>).forEach((k) => {
    const ceiling = cap[k] ?? "OFF";
    // Clamp requested level down to the per-key cap.
    if (LEVEL_ORDER[level] <= LEVEL_ORDER[ceiling]) next[k] = level;
    else next[k] = ceiling;
  });
  return next;
}

// ── Create dialog ─────────────────────────────────────────────────────
function CreateBrokerDialog({
  open,
  onOpenChange,
  cap,
  noun,
  isSuperAdmin,
  adminsList,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  cap: Record<keyof BrokerPermissions, PermissionLevel>;
  noun: string;
  isSuperAdmin: boolean;
  adminsList: Array<{ id: string; full_name: string; user_code: string }>;
  onCreated: () => void;
}) {
  const [form, setForm] = useState({
    full_name: "",
    email: "",
    mobile: "",
    password: "",
    confirm_password: "",
    pnl_share_pct: "0",
    brokerage_share_pct: "0",
  });
  const [perms, setPerms] = useState<BrokerPermissions>({ ...ALL_OFF });
  const [selectedAdminId, setSelectedAdminId] = useState("");
  const [loading, setLoading] = useState(false);
  // Drives BOTH the password and confirm-password inputs so the admin
  // can visually verify the match before submitting.
  const [showPassword, setShowPassword] = useState(false);

  async function submit() {
    if (form.password.length < 6) {
      toast.error("Password must be at least 6 characters");
      return;
    }
    if (form.password !== form.confirm_password) {
      toast.error("Passwords do not match");
      return;
    }
    setLoading(true);
    try {
      await BrokerMgmtAPI.create({
        full_name: form.full_name,
        email: form.email,
        mobile: form.mobile,
        password: form.password,
        permissions: perms as unknown as Record<string, "OFF" | "VIEW" | "EDIT">,
        pnl_share_pct: form.pnl_share_pct,
        brokerage_share_pct: form.brokerage_share_pct,
        assigned_admin_id: selectedAdminId || undefined,
      });
      toast.success(`${noun} created`);
      onOpenChange(false);
      setForm({ full_name: "", email: "", mobile: "", password: "", confirm_password: "", pnl_share_pct: "0", brokerage_share_pct: "0" });
      setPerms({ ...ALL_OFF });
      setSelectedAdminId("");
      onCreated();
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/*
        Layout: pinned header + scrollable body + pinned footer so a tall
        permissions list never pushes the basics/buttons off-screen on
        small viewports. `max-h-[90vh]` keeps the dialog inside the
        viewport; `flex flex-col` + `flex-1 overflow-y-auto` on the body
        makes the middle area scroll. We override DialogContent's
        default `p-6 gap-4` because we want section-specific padding now.
      */}
      <DialogContent className="flex max-h-[90vh] w-[95vw] max-w-2xl flex-col gap-0 p-0">
        <DialogHeader className="border-b border-border px-5 py-4">
          <DialogTitle>New {noun.toLowerCase()}</DialogTitle>
        </DialogHeader>

        <div className="flex-1 space-y-5 overflow-y-auto px-5 py-4 scrollbar-thin">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label>Full name</Label>
              <Input value={form.full_name} onChange={(e) => setForm((f) => ({ ...f, full_name: e.target.value }))} />
            </div>
            <div className="space-y-1.5">
              <Label>Email</Label>
              <Input type="email" value={form.email} onChange={(e) => setForm((f) => ({ ...f, email: e.target.value }))} />
            </div>
            <div className="space-y-1.5">
              <Label>Mobile (10-digit)</Label>
              <Input value={form.mobile} onChange={(e) => setForm((f) => ({ ...f, mobile: e.target.value }))} />
            </div>
            <div className="space-y-1.5">
              <Label>Password</Label>
              <div className="relative">
                <Input
                  type={showPassword ? "text" : "password"}
                  className="pr-10"
                  value={form.password}
                  onChange={(e) => setForm((f) => ({ ...f, password: e.target.value }))}
                />
                <button
                  type="button"
                  onClick={() => setShowPassword((s) => !s)}
                  aria-label={showPassword ? "Hide password" : "Show password"}
                  className="absolute inset-y-0 right-0 grid w-10 place-items-center text-muted-foreground hover:text-foreground"
                >
                  {showPassword ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                </button>
              </div>
            </div>
            <div className="space-y-1.5">
              <Label>Confirm password</Label>
              <Input
                type={showPassword ? "text" : "password"}
                value={form.confirm_password}
                onChange={(e) => setForm((f) => ({ ...f, confirm_password: e.target.value }))}
              />
              {form.confirm_password.length > 0 && form.password !== form.confirm_password ? (
                <p className="text-xs text-destructive">Passwords do not match</p>
              ) : null}
            </div>
            <div className="space-y-1.5">
              <Label>PNL share %</Label>
              <Input
                type="number"
                min={0}
                max={100}
                step="0.01"
                value={form.pnl_share_pct}
                onChange={(e) => setForm((f) => ({ ...f, pnl_share_pct: e.target.value }))}
              />
            </div>
            <div className="space-y-1.5">
              <Label>Brokerage share %</Label>
              <Input
                type="number"
                min={0}
                max={100}
                step="0.01"
                value={form.brokerage_share_pct}
                onChange={(e) => setForm((f) => ({ ...f, brokerage_share_pct: e.target.value }))}
              />
            </div>
            {isSuperAdmin && (
              <div className="space-y-1.5 sm:col-span-2">
                <Label>Assign to admin (optional — leave empty for platform pool)</Label>
                <select
                  value={selectedAdminId}
                  onChange={(e) => setSelectedAdminId(e.target.value)}
                  className="w-full rounded border border-input bg-background px-3 py-2 text-foreground"
                >
                  <option value="">— Platform pool —</option>
                  {adminsList.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.full_name} ({a.user_code})
                    </option>
                  ))}
                </select>
              </div>
            )}
          </div>

          <div>
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <div>
                <div className="text-sm font-medium">Permissions</div>
                <div className="text-xs text-muted-foreground">
                  <span className="font-semibold text-foreground">OFF</span> = hidden ·{" "}
                  <span className="font-semibold text-brand-500">VIEW</span> = read-only ·{" "}
                  <span className="font-semibold text-primary">EDIT</span> = full access
                </div>
              </div>
              <BulkSetToolbar cap={cap} onApply={(lv) => setPerms(applyBulk(cap, lv))} />
            </div>
            <div className="overflow-hidden rounded-md border border-border bg-background">
              <div className="flex items-center justify-between border-b border-border bg-muted/30 px-3 py-1.5 text-[11px] uppercase tracking-wider text-muted-foreground">
                <span>Section</span>
                <span>Access level</span>
              </div>
              <div className="divide-y divide-border">
                {PERMISSION_LABELS.map((p) => (
                  <PermissionRow
                    key={p.key}
                    label={p.label}
                    cap={cap[p.key] ?? "OFF"}
                    value={perms[p.key]}
                    onChange={(v) => setPerms((cur) => ({ ...cur, [p.key]: v }))}
                  />
                ))}
              </div>
            </div>
          </div>
        </div>

        <DialogFooter className="border-t border-border bg-card px-5 py-3">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={loading}>
            Create {noun.toLowerCase()}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ── Edit dialog ──────────────────────────────────────────────────────
function EditBrokerDialog({
  broker,
  cap,
  noun,
  onClose,
  onSaved,
}: {
  broker: any;
  cap: Record<keyof BrokerPermissions, PermissionLevel>;
  noun: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [perms, setPerms] = useState<BrokerPermissions>({
    ...ALL_OFF,
    ...(broker.permissions || {}),
  });
  const [pnlPct, setPnlPct] = useState<string>(String(broker.pnl_share_pct ?? "0"));
  const [brokeragePct, setBrokeragePct] = useState<string>(
    String((broker as any).brokerage_share_pct ?? broker.pnl_share_pct ?? "0"),
  );
  const [loading, setLoading] = useState(false);

  async function save() {
    setLoading(true);
    try {
      const res = await BrokerMgmtAPI.updatePermissions(
        broker.id,
        perms as unknown as Record<string, "OFF" | "VIEW" | "EDIT">,
      );
      await BrokerMgmtAPI.updatePnlShare(broker.id, pnlPct, brokeragePct);
      const cascaded = res?.cascaded_changes ?? [];
      toast.success(
        cascaded.length > 0
          ? `Saved. ${cascaded.length} sub-broker(s) cascade-clipped.`
          : `${noun} updated`,
      );
      onSaved();
      onClose();
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="flex max-h-[90vh] w-[95vw] max-w-2xl flex-col gap-0 p-0">
        <DialogHeader className="border-b border-border px-5 py-4">
          <DialogTitle>
            {broker.full_name}{" "}
            <span className="text-xs text-muted-foreground">{broker.user_code}</span>
          </DialogTitle>
        </DialogHeader>
        <div className="flex-1 space-y-5 overflow-y-auto px-5 py-4 scrollbar-thin">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label>PNL share %</Label>
              <Input
                type="number"
                min={0}
                max={100}
                step="0.01"
                value={pnlPct}
                onChange={(e) => setPnlPct(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label>Brokerage share %</Label>
              <Input
                type="number"
                min={0}
                max={100}
                step="0.01"
                value={brokeragePct}
                onChange={(e) => setBrokeragePct(e.target.value)}
              />
            </div>
          </div>
          <div>
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <div>
                <div className="text-sm font-medium">Permissions</div>
                <div className="text-xs text-muted-foreground">
                  <span className="font-semibold text-foreground">OFF</span> = hidden ·{" "}
                  <span className="font-semibold text-brand-500">VIEW</span> = read-only ·{" "}
                  <span className="font-semibold text-primary">EDIT</span> = full access
                </div>
              </div>
              <BulkSetToolbar cap={cap} onApply={(lv) => setPerms(applyBulk(cap, lv))} />
            </div>
            <div className="overflow-hidden rounded-md border border-border bg-background">
              <div className="flex items-center justify-between border-b border-border bg-muted/30 px-3 py-1.5 text-[11px] uppercase tracking-wider text-muted-foreground">
                <span>Section</span>
                <span>Access level</span>
              </div>
              <div className="divide-y divide-border">
                {PERMISSION_LABELS.map((p) => (
                  <PermissionRow
                    key={p.key}
                    label={p.label}
                    cap={cap[p.key] ?? "OFF"}
                    value={perms[p.key]}
                    onChange={(v) => setPerms((cur) => ({ ...cur, [p.key]: v }))}
                  />
                ))}
              </div>
            </div>
          </div>
        </div>
        <DialogFooter className="border-t border-border bg-card px-5 py-3">
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={save} disabled={loading}>
            Save changes
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ── Mobile broker card — mirrors the sub-admin / user card layout so the
//    Brokers list reads cleanly on a phone instead of a cramped table. ──
function BrokerMobileCard({
  row,
  noun,
  loginAsBusy,
  onOpen,
  onLoginAs,
  onEdit,
  onBlock,
  onUnblock,
  onPaymentGateway,
  onResetPw,
  onDelete,
}: {
  row: any;
  noun: string;
  loginAsBusy: boolean;
  onOpen: () => void;
  onLoginAs: () => void;
  onEdit: () => void;
  onBlock: () => void;
  onUnblock: () => void;
  onPaymentGateway: () => void;
  onResetPw: () => void;
  onDelete: () => void;
}) {
  const initials = (row.full_name || row.user_code || "?")
    .split(/\s+/)
    .map((s: string) => s[0])
    .filter(Boolean)
    .slice(0, 2)
    .join("")
    .toUpperCase();
  const isActive = row.status === "ACTIVE";
  const users = row.user_count ?? 0;
  const subtree = row.subtree_user_count ?? users;
  return (
    <li
      onClick={onOpen}
      className="cursor-pointer rounded-lg border border-border bg-card p-3 transition-colors hover:bg-accent/30 active:bg-accent/40"
    >
      <div className="flex items-start gap-3">
        <div className="grid size-10 shrink-0 place-items-center rounded-full bg-primary/15 text-xs font-bold text-primary">
          {initials}
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-semibold">
            {row.full_name || row.user_code || "—"}
          </div>
          <div className="truncate font-mono text-[10px] text-muted-foreground">
            {row.user_code || "—"}
          </div>
        </div>
        <span
          className={
            "shrink-0 rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider " +
            (isActive
              ? "bg-emerald-500/10 text-emerald-500 ring-1 ring-inset ring-emerald-500/30"
              : "bg-red-500/10 text-red-500 ring-1 ring-inset ring-red-500/30")
          }
        >
          {row.status || "—"}
        </span>
        <div onClick={(e) => e.stopPropagation()}>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                aria-label="Open actions"
                disabled={loginAsBusy}
                className="-mr-1 size-8"
              >
                <MoreVertical className="size-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onSelect={onLoginAs}>
                <LogIn className="size-4 text-primary" />
                Login
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={onOpen}>
                <Eye className="size-4" />
                View {noun.toLowerCase()} profile
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={onEdit}>
                <Pencil className="size-4" />
                Edit permissions
              </DropdownMenuItem>
              {isActive ? (
                <DropdownMenuItem onSelect={onBlock}>
                  <ShieldOff className="size-4 text-red-500" />
                  Block
                </DropdownMenuItem>
              ) : (
                <DropdownMenuItem onSelect={onUnblock}>
                  <ShieldCheck className="size-4 text-emerald-500" />
                  Unblock
                </DropdownMenuItem>
              )}
              <DropdownMenuItem onSelect={onPaymentGateway}>
                <CreditCard
                  className={`size-4 ${row.payment_gateway_enabled ? "text-emerald-500" : "text-muted-foreground"}`}
                />
                {row.payment_gateway_enabled
                  ? "Online payment: ON (turn off)"
                  : "Online payment: OFF (turn on)"}
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={onResetPw}>
                <KeyRound className="size-4" />
                Reset Password
              </DropdownMenuItem>
              <DropdownMenuItem className="text-red-500" onSelect={onDelete}>
                <Trash2 className="size-4 text-red-500" />
                Delete
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>

      <div className="mt-3 space-y-1 text-xs">
        <div className="flex items-center gap-2 text-muted-foreground">
          <span className="w-12 text-[10px] uppercase tracking-wider">Email</span>
          <span className="truncate text-foreground">{row.email || "—"}</span>
        </div>
        <div className="flex items-center gap-2 text-muted-foreground">
          <span className="w-12 text-[10px] uppercase tracking-wider">Mobile</span>
          <span className="truncate text-foreground">{row.mobile || "—"}</span>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-3 gap-2 border-t border-border pt-3 text-center">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Users</div>
          <div className="text-sm font-semibold">
            {users}
            {subtree !== users ? (
              <span className="ml-1 text-[10px] font-normal text-muted-foreground">
                ({subtree} subtree)
              </span>
            ) : null}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">PNL share</div>
          <div className="text-sm font-semibold">{row.pnl_share_pct ?? "0"}%</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Brokerage</div>
          <div className="text-sm font-semibold">
            {row.brokerage_share_pct ?? row.pnl_share_pct ?? "0"}%
          </div>
        </div>
      </div>
    </li>
  );
}
