"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  Plus,
  ShieldOff,
  ShieldCheck,
  Pencil,
  MoreVertical,
  Eye,
  EyeOff,
  KeyRound,
  Trash2,
  Link as LinkIcon,
} from "lucide-react";

import { EmployeeMgmtAPI } from "@/lib/api";
import { useAdminAuthStore } from "@/stores/authStore";
import type { AdminPermissions } from "@/types";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
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

const PERMISSION_LABELS: Array<{ key: keyof AdminPermissions; label: string }> = [
  { key: "users", label: "Users" },
  { key: "kyc", label: "KYC review" },
  { key: "deposits", label: "Deposits" },
  { key: "withdrawals", label: "Withdrawals" },
  { key: "banks", label: "Bank accounts" },
  { key: "segment_settings", label: "Segment settings" },
  { key: "risk", label: "Risk management" },
  { key: "netting", label: "Netting overrides" },
  { key: "orders", label: "Orders" },
  { key: "positions", label: "Positions" },
  { key: "marketwatch", label: "Market Watch" },
  { key: "money_transactions", label: "Money Transactions" },
  { key: "broker_deposits", label: "Broker Deposits" },
  { key: "reports", label: "Reports (incl. Tradebook)" },
  { key: "brokerage", label: "Brokerage" },
  { key: "brokers", label: "Brokers" },
  { key: "accounts", label: "Accounts" },
  { key: "pnl_sharing", label: "P&L Sharing" },
  { key: "audit", label: "Audit logs" },
  { key: "support", label: "Support" },
  { key: "download_app", label: "Download App" },
  { key: "bonuses", label: "Bonuses" },
];

const ALL_OFF: AdminPermissions = {
  users: false, kyc: false, deposits: false, withdrawals: false, banks: false,
  segment_settings: false, risk: false, netting: false, trading_view: false,
  ledger: false, reports: false, brokers: false, brokerage: false,
  accounts: false, pnl_sharing: false, audit: false, support: false,
  orders: false, positions: false, marketwatch: false,
  money_transactions: false, broker_deposits: false, download_app: false,
  bonuses: false, transfer_users: false,
};

export default function EmployeesPage() {
  const qc = useQueryClient();
  const admin = useAdminAuthStore((s) => s.admin);
  const [q, setQ] = useState("");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<any | null>(null);
  const [resetTarget, setResetTarget] = useState<{ id: string; label: string } | null>(null);

  const isSuper = admin?.role === "SUPER_ADMIN";
  // Sections THIS admin may grant (the ceiling). Super-admin → all.
  const grantable = useMemo(
    () =>
      PERMISSION_LABELS.filter(
        (p) => isSuper || Boolean((admin?.admin_permissions as any)?.[p.key]),
      ),
    [admin, isSuper],
  );

  const { data, isFetching } = useQuery({
    queryKey: ["admin", "employees", q],
    queryFn: () => EmployeeMgmtAPI.list({ q: q || undefined, page_size: 100 }),
  });
  const rows: any[] = data?.items ?? [];

  const refresh = () => qc.invalidateQueries({ queryKey: ["admin", "employees"] });

  const blockMut = useMutation({
    mutationFn: (r: any) => (r.status === "BLOCKED" ? EmployeeMgmtAPI.unblock(r.id) : EmployeeMgmtAPI.block(r.id)),
    onSuccess: () => { toast.success("Updated"); refresh(); },
    onError: (e: any) => toast.error(e.message),
  });
  const delMut = useMutation({
    mutationFn: (id: string) => EmployeeMgmtAPI.remove(id),
    onSuccess: () => { toast.success("Employee removed"); refresh(); },
    onError: (e: any) => toast.error(e.message),
  });

  function copyLoginLink() {
    const url = `${window.location.origin}/employee-login`;
    navigator.clipboard?.writeText(url);
    toast.success("Employee login link copied");
  }

  const cols: Column<any>[] = [
    { key: "user_code", header: "Code", render: (r) => <span className="font-mono text-xs">{r.user_code}</span> },
    { key: "full_name", header: "Name", render: (r) => <span className="font-medium">{r.full_name}</span> },
    { key: "email", header: "Email" },
    ...(isSuper ? [{ key: "employer_name", header: "Employer", render: (r: any) => r.employer_name ?? "—" } as Column<any>] : []),
    {
      key: "perms",
      header: "Sections",
      render: (r) => {
        const n = r.permissions ? Object.values(r.permissions).filter(Boolean).length : 0;
        return <span className="text-xs text-muted-foreground">{n} granted</span>;
      },
    },
    {
      key: "status",
      header: "Status",
      render: (r) => (
        <span className={r.status === "BLOCKED" ? "text-destructive" : "text-emerald-600"}>{r.status}</span>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      render: (r) => (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon"><MoreVertical className="size-4" /></Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onClick={() => setEditing(r)}>
              <Pencil className="mr-2 size-4" /> Edit permissions
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => blockMut.mutate(r)}>
              {r.status === "BLOCKED" ? <ShieldCheck className="mr-2 size-4" /> : <ShieldOff className="mr-2 size-4" />}
              {r.status === "BLOCKED" ? "Unblock" : "Block"}
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => setResetTarget({ id: r.id, label: r.full_name })}>
              <KeyRound className="mr-2 size-4" /> Reset password
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              className="text-destructive"
              onClick={() => { if (confirm(`Delete employee ${r.full_name}?`)) delMut.mutate(r.id); }}
            >
              <Trash2 className="mr-2 size-4" /> Delete
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader
        title="Employees"
        description="Staff sub-users. Grant each only the sections they need — they log in via the separate Employee Login page."
        actions={
          <div className="flex gap-2">
            <Button variant="outline" onClick={copyLoginLink}><LinkIcon className="mr-2 size-4" /> Login link</Button>
            <Button onClick={() => setCreating(true)}><Plus className="mr-2 size-4" /> New employee</Button>
          </div>
        }
      />

      <Input placeholder="Search name / email / code…" value={q} onChange={(e) => setQ(e.target.value)} className="max-w-sm" />

      <div className="hidden md:block">
        <DataTable columns={cols} rows={rows} keyExtractor={(r) => r.id} loading={isFetching && !data} empty="No employees yet." />
      </div>
      <div className="space-y-2 md:hidden">
        {rows.length === 0 ? (
          <div className="rounded-lg border border-border p-6 text-center text-xs text-muted-foreground">No employees yet.</div>
        ) : rows.map((r) => (
          <div key={r.id} className="rounded-xl border border-border bg-card p-3">
            <div className="flex items-center justify-between">
              <div className="min-w-0">
                <div className="truncate font-semibold">{r.full_name}</div>
                <div className="font-mono text-[11px] text-muted-foreground">{r.user_code} · {r.email}</div>
              </div>
              <span className={`text-xs ${r.status === "BLOCKED" ? "text-destructive" : "text-emerald-600"}`}>{r.status}</span>
            </div>
            <div className="mt-2 flex flex-wrap gap-2">
              <Button size="sm" variant="outline" onClick={() => setEditing(r)}>Edit</Button>
              <Button size="sm" variant="outline" onClick={() => blockMut.mutate(r)}>{r.status === "BLOCKED" ? "Unblock" : "Block"}</Button>
              <Button size="sm" variant="outline" onClick={() => setResetTarget({ id: r.id, label: r.full_name })}>Reset PW</Button>
              <Button size="sm" variant="outline" className="text-destructive" onClick={() => { if (confirm(`Delete ${r.full_name}?`)) delMut.mutate(r.id); }}>Delete</Button>
            </div>
          </div>
        ))}
      </div>

      {creating && (
        <EmployeeDialog grantable={grantable} onClose={() => setCreating(false)} onSaved={() => { setCreating(false); refresh(); }} />
      )}
      {editing && (
        <EmployeeDialog employee={editing} grantable={grantable} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); refresh(); }} />
      )}
      {resetTarget && (
        <ResetPasswordDialog target={resetTarget} onClose={() => setResetTarget(null)} />
      )}
    </div>
  );
}

function EmployeeDialog({
  employee,
  grantable,
  onClose,
  onSaved,
}: {
  employee?: any;
  grantable: Array<{ key: keyof AdminPermissions; label: string }>;
  onClose: () => void;
  onSaved: () => void;
}) {
  const isEdit = Boolean(employee);
  const [full_name, setFullName] = useState(employee?.full_name ?? "");
  const [email, setEmail] = useState(employee?.email ?? "");
  const [mobile, setMobile] = useState(employee?.mobile ?? "");
  const [password, setPassword] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [perms, setPerms] = useState<AdminPermissions>({ ...ALL_OFF, ...(employee?.permissions ?? {}) });

  const save = useMutation({
    mutationFn: async () => {
      if (isEdit) return EmployeeMgmtAPI.updatePermissions(employee.id, perms as any);
      return EmployeeMgmtAPI.create({ full_name, email, mobile, password, permissions: perms as any });
    },
    onSuccess: () => { toast.success(isEdit ? "Permissions updated" : "Employee created"); onSaved(); },
    onError: (e: any) => toast.error(e.message),
  });

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader><DialogTitle>{isEdit ? `Edit ${employee.full_name}` : "New employee"}</DialogTitle></DialogHeader>
        <div className="space-y-3">
          {!isEdit && (
            <>
              <div className="space-y-1"><Label>Full name</Label><Input value={full_name} onChange={(e) => setFullName(e.target.value)} /></div>
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1"><Label>Email</Label><Input value={email} onChange={(e) => setEmail(e.target.value)} /></div>
                <div className="space-y-1"><Label>Mobile</Label><Input value={mobile} onChange={(e) => setMobile(e.target.value)} /></div>
              </div>
              <div className="space-y-1">
                <Label>Password</Label>
                <div className="relative">
                  <Input type={showPw ? "text" : "password"} value={password} onChange={(e) => setPassword(e.target.value)} />
                  <button type="button" className="absolute right-2 top-2 text-muted-foreground" onClick={() => setShowPw((v) => !v)}>
                    {showPw ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                  </button>
                </div>
              </div>
            </>
          )}
          <div className="space-y-2">
            <Label>Sections this employee can access</Label>
            <div className="grid grid-cols-2 gap-2">
              {grantable.map((p) => (
                <label key={p.key} className="flex items-center gap-2 rounded-md border border-border p-2 text-sm">
                  <input
                    type="checkbox"
                    checked={Boolean((perms as any)[p.key])}
                    onChange={(e) => setPerms((prev) => ({ ...prev, [p.key]: e.target.checked }))}
                  />
                  {p.label}
                </label>
              ))}
            </div>
            {grantable.length === 0 && (
              <p className="text-xs text-destructive">You have no sections to grant.</p>
            )}
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button loading={save.isPending} onClick={() => save.mutate()}>{isEdit ? "Save" : "Create"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ResetPasswordDialog({ target, onClose }: { target: { id: string; label: string }; onClose: () => void }) {
  const [pw, setPw] = useState("");
  const [show, setShow] = useState(false);
  const mut = useMutation({
    mutationFn: () => EmployeeMgmtAPI.resetPassword(target.id, pw),
    onSuccess: () => { toast.success("Password reset"); onClose(); },
    onError: (e: any) => toast.error(e.message),
  });
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-sm">
        <DialogHeader><DialogTitle>Reset password — {target.label}</DialogTitle></DialogHeader>
        <div className="space-y-1">
          <Label>New password</Label>
          <div className="relative">
            <Input type={show ? "text" : "password"} value={pw} onChange={(e) => setPw(e.target.value)} />
            <button type="button" className="absolute right-2 top-2 text-muted-foreground" onClick={() => setShow((v) => !v)}>
              {show ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
            </button>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button loading={mut.isPending} disabled={pw.length < 8} onClick={() => mut.mutate()}>Reset</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
