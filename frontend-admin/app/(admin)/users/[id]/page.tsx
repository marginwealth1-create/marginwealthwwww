"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useAdminAuthStore } from "@/stores/authStore";
import { canSee } from "@/lib/permissions";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  Activity,
  Ban,
  BookOpen,
  Check,
  Eye,
  EyeOff,
  KeyRound,
  ListOrdered,
  ShieldCheck,
  ShieldOff,
  TrendingUp,
  UserCog,
  Volume2,
  Mail,
} from "lucide-react";
import { UsersAPI } from "@/lib/api";
import { NOTIFY_SOUNDS, playSound } from "@/lib/notify-sound";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { PageHeader } from "@/components/common/PageHeader";
import { StatusPill } from "@/components/common/StatusPill";
import { cn, formatINR } from "@/lib/utils";

export default function UserDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const router = useRouter();
  const qc = useQueryClient();

  const { data, isLoading } = useQuery({
    queryKey: ["admin", "user", id],
    queryFn: () => UsersAPI.detail(id),
    enabled: !!id,
  });

  const blockMut = useMutation({
    mutationFn: (block: boolean) => (block ? UsersAPI.block(id) : UsersAPI.unblock(id)),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin", "user", id] });
      toast.success("Updated");
    },
    onError: (e: any) => toast.error(e.message || "Failed"),
  });

  // Auto-settlement toggle. Confirm before turning OFF (changes risk
  // behaviour: balance allowed to go negative until admin manually
  // approves each settlement from Payments → Settlement Requests).
  const autoSettlementMut = useMutation({
    mutationFn: (enabled: boolean) => UsersAPI.setAutoSettlement(id, enabled),
    onSuccess: (_d, enabled) => {
      qc.invalidateQueries({ queryKey: ["admin", "user", id] });
      toast.success(
        enabled
          ? "Auto-settlement ON · wallet auto-floors at 0"
          : "Auto-settlement OFF · settlements need your approval",
      );
    },
    onError: (e: any) => toast.error(e.message || "Failed"),
  });
  function toggleAutoSettlement() {
    const current = !!u?.auto_settlement;
    if (current) {
      // Turning OFF — confirm the operational impact.
      const ok = window.confirm(
        "Turn OFF auto-settlement?\n\n" +
          "The user's wallet will be ALLOWED to go negative on losses. " +
          "Until you manually approve each settlement from " +
          "Payments → Settlement Requests, the user is blocked from " +
          "opening new trades.",
      );
      if (!ok) return;
    }
    autoSettlementMut.mutate(!current);
  }

  // Trade alerts — watch this user's fills from the admin panel. The
  // sound is stored per user so two watched users can be told apart
  // without looking at the screen.
  const tradeAlertMut = useMutation({
    mutationFn: (body: { enabled?: boolean; sound?: string }) =>
      UsersAPI.setTradeAlert(id, body),
    onSuccess: (_d, body) => {
      qc.invalidateQueries({ queryKey: ["admin", "user", id] });
      if (body.sound) toast.success("Alert sound saved");
      else toast.success(body.enabled ? "Trade alerts ON" : "Trade alerts OFF");
    },
    onError: (e: any) => toast.error(e.message || "Failed"),
  });

  // Contact details. An admin-created account can start with no email and
  // no phone (the backend stores a placeholder in those uniquely-indexed
  // columns), so the form shows a placeholder as blank — the admin fills in
  // the real one whenever they get it.
  // A placeholder contact is an absence, not a value — the header and the
  // form below both render it as one.
  const isPlaceholder = (v: string | undefined | null) =>
    !!v && (v.endsWith("@noemail.marginwealth.live") || v.startsWith("NOMOB"));
  const [contact, setContact] = useState({ full_name: "", email: "", mobile: "" });
  const [contactDirty, setContactDirty] = useState(false);
  useEffect(() => {
    if (!data) return;
    // Don't stomp on what the admin is typing when the query refetches.
    if (contactDirty) return;
    setContact({
      full_name: data.full_name ?? "",
      email: isPlaceholder(data.email) ? "" : (data.email ?? ""),
      mobile: isPlaceholder(data.mobile) ? "" : (data.mobile ?? ""),
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data?.id, data?.email, data?.mobile, data?.full_name, contactDirty]);

  const contactMut = useMutation({
    mutationFn: () => UsersAPI.update(id, contact),
    onSuccess: () => {
      setContactDirty(false);
      qc.invalidateQueries({ queryKey: ["admin", "user", id] });
      qc.invalidateQueries({ queryKey: ["admin", "users"] });
      toast.success("Contact details saved");
    },
    onError: (e: any) => toast.error(e.message || "Failed"),
  });

  const [adjAmount, setAdjAmount] = useState("");
  const [adjNote, setAdjNote] = useState("");
  const [adjType, setAdjType] = useState("ADJUSTMENT");

  async function adjustWallet() {
    if (!adjAmount || isNaN(Number(adjAmount))) {
      toast.error("Enter a numeric amount (negative to debit)");
      return;
    }
    try {
      await UsersAPI.walletAdjust(id, {
        amount: Number(adjAmount),
        narration: adjNote || `${adjType} by admin`,
        transaction_type: adjType,
      });
      toast.success("Wallet adjusted");
      setAdjAmount("");
      setAdjNote("");
      qc.invalidateQueries({ queryKey: ["admin", "user", id] });
    } catch (e: any) {
      toast.error(e.message || "Failed");
    }
  }

  // Reset-password dialog state. Replaces the browser-native `prompt()`
  // (which showed the new password in plaintext while typing and had no
  // confirm step) with a proper modal: password + confirm fields, an
  // eye toggle, length / mismatch guards, and a single-flight submit so
  // a double-click can't fire two POSTs.
  const [resetOpen, setResetOpen] = useState(false);
  const [resetPw, setResetPw] = useState("");
  const [resetPw2, setResetPw2] = useState("");
  // A broker can reset a user's password only with the Change-password perm;
  // admins / super-admin unaffected (backend enforces regardless).
  const me = useAdminAuthStore((s) => s.admin);
  const canResetPw = String(me?.role || "") !== "BROKER" || canSee(me, "user_password");
  const [resetShow, setResetShow] = useState(false);
  const [resetSaving, setResetSaving] = useState(false);

  function openResetDialog() {
    setResetPw("");
    setResetPw2("");
    setResetShow(false);
    setResetOpen(true);
  }

  async function submitResetPassword() {
    if (resetPw.length < 6) {
      toast.error("Password must be at least 6 characters");
      return;
    }
    if (resetPw !== resetPw2) {
      toast.error("Passwords do not match");
      return;
    }
    setResetSaving(true);
    try {
      await UsersAPI.resetPassword(id, resetPw);
      toast.success("Password reset · user must change on next login");
      setResetOpen(false);
    } catch (e: any) {
      toast.error(e.message || "Failed");
    } finally {
      setResetSaving(false);
    }
  }

  if (isLoading) return <div className="text-sm text-muted-foreground">Loading…</div>;
  if (!data) return <div className="text-sm text-muted-foreground">User not found</div>;

  const u = data;

  return (
    <div className="space-y-6">
      <PageHeader
        title={u.full_name}
        description={[u.user_code, isPlaceholder(u.email) ? null : u.email, isPlaceholder(u.mobile) ? null : u.mobile]
          .filter(Boolean)
          .join(" · ")}
        actions={
          <div className="flex flex-wrap gap-2">
            <Button asChild variant="outline">
              <Link href={`/orders?tab=pending&user_id=${id}`}>
                <ListOrdered className="size-4" /> View orders
              </Link>
            </Button>
            {/* "View trades" used to point at the Executions tab,
                which duplicated the Orders view above (admin couldn't
                tell them apart). Repurposed to surface the user's
                live positions — what they actually have OPEN right
                now — via /positions filtered by user_id. */}
            <Button asChild variant="outline">
              <Link href={`/positions?user_id=${id}`}>
                <TrendingUp className="size-4" /> View positions
              </Link>
            </Button>
            <Button asChild variant="outline">
              <Link href={`/ledger?user_id=${id}`}>
                <BookOpen className="size-4" /> Ledger
              </Link>
            </Button>
            <Button asChild variant="outline">
              {/* `involving_user_id` widens the audit query to events
                  where the user is EITHER the actor or the subject — so
                  the admin sees both what was done TO this user
                  (block, KYC approve, manual wallet adjust) AND what
                  this user did themselves (logins, order placements,
                  cancels, squareoffs). */}
              <Link href={`/audit?involving_user_id=${id}`}>
                <Activity className="size-4" /> Activity
              </Link>
            </Button>
            <Button asChild variant="outline">
              <Link href={`/segment-settings/user/${id}`}>
                <UserCog className="size-4" /> Segment settings
              </Link>
            </Button>
            {canResetPw && (
              <Button variant="outline" onClick={openResetDialog}>
                <KeyRound className="size-4" /> Reset password
              </Button>
            )}
            {/* Auto-settlement toggle — same shape as the Block button.
                ON (default): green ShieldCheck. OFF: amber ShieldOff —
                signals risk mode at a glance. Click runs through a
                confirm dialog when switching ON → OFF (see
                `toggleAutoSettlement`). */}
            <Button
              variant="outline"
              onClick={toggleAutoSettlement}
              loading={autoSettlementMut.isPending}
              className={
                u.auto_settlement
                  ? ""
                  : "border-amber-500/50 text-amber-700 dark:text-amber-300"
              }
            >
              {u.auto_settlement ? (
                <ShieldCheck className="size-4" />
              ) : (
                <ShieldOff className="size-4" />
              )}
              Auto Settlement: {u.auto_settlement ? "ON" : "OFF"}
            </Button>
            <Button
              variant={u.status === "BLOCKED" ? "default" : "destructive"}
              onClick={() => blockMut.mutate(u.status !== "BLOCKED")}
              loading={blockMut.isPending}
            >
              {u.status === "BLOCKED" ? <Check className="size-4" /> : <Ban className="size-4" />}
              {u.status === "BLOCKED" ? "Unblock" : "Block"}
            </Button>
          </div>
        }
      />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle>Profile</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1 text-sm">
            <Row label="Status" value={<StatusPill status={u.status} />} />
            <Row label="Role" value={<StatusPill status={u.role} />} />
            <Row label="Account type" value={u.account_type} />
            <Row label="Demo" value={u.is_demo ? "Yes" : "No"} />
            <Row label="Created" value={new Date(u.created_at).toLocaleString()} />
            <Row label="Last login" value={u.last_login_at ? new Date(u.last_login_at).toLocaleString() : "—"} />
            <Row label="2FA" value={u.two_fa_enabled ? "Enabled" : "Disabled"} />
          </CardContent>
        </Card>

        <Card id="wallet">
          <CardHeader>
            <CardTitle>Wallet</CardTitle>
            <CardDescription>Manual credit / debit (admin)</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <div className="grid grid-cols-2 gap-3">
              <Stat label="Available" value={formatINR(u.wallet?.available_balance)} />
              <Stat label="Used margin" value={formatINR(u.wallet?.used_margin)} />
              <Stat label="Credit limit" value={formatINR(u.wallet?.credit_limit)} />
              <Stat label="Realized P&L" value={formatINR(u.wallet?.realized_pnl)} />
              <Stat label="Deposits" value={formatINR(u.wallet?.total_deposits)} />
              <Stat label="Withdrawals" value={formatINR(u.wallet?.total_withdrawals)} />
              {Number(u.wallet?.settlement_outstanding ?? 0) > 0 && (
                <Stat
                  label="Settlement"
                  value={formatINR(u.wallet?.settlement_outstanding)}
                  highlighted
                />
              )}
            </div>
            <div className="space-y-2 border-t border-border pt-3">
              <Label className="text-xs uppercase tracking-wider text-muted-foreground">Manual adjust</Label>
              <select
                value={adjType}
                onChange={(e) => setAdjType(e.target.value)}
                className="h-9 w-full rounded-md border border-border bg-background px-2 text-sm"
              >
                <option value="ADJUSTMENT">Adjustment</option>
              </select>
              <Input
                placeholder="Amount (negative to debit)"
                inputMode="numeric"
                value={adjAmount}
                onChange={(e) => setAdjAmount(e.target.value)}
              />
              <Input placeholder="Reason / note" value={adjNote} onChange={(e) => setAdjNote(e.target.value)} />
              <Button onClick={adjustWallet} className="w-full">
                Apply
              </Button>
            </div>
          </CardContent>
        </Card>

        <Card id="contact">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Mail className="size-4" /> Contact details
            </CardTitle>
            <CardDescription>
              Name, email and phone. Leave email or phone blank if you don't
              have it — the user logs in with their user ID either way.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <div className="space-y-1.5">
              <Label className="text-xs uppercase tracking-wider text-muted-foreground">
                Full name
              </Label>
              <Input
                value={contact.full_name}
                onChange={(e) => {
                  setContactDirty(true);
                  setContact((c) => ({ ...c, full_name: e.target.value }));
                }}
              />
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs uppercase tracking-wider text-muted-foreground">
                Email
              </Label>
              <Input
                placeholder="Not set"
                value={contact.email}
                onChange={(e) => {
                  setContactDirty(true);
                  setContact((c) => ({ ...c, email: e.target.value }));
                }}
              />
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs uppercase tracking-wider text-muted-foreground">
                Mobile
              </Label>
              <Input
                placeholder="Not set"
                maxLength={10}
                value={contact.mobile}
                onChange={(e) => {
                  setContactDirty(true);
                  setContact((c) => ({ ...c, mobile: e.target.value }));
                }}
              />
            </div>
            <Button
              className="w-full"
              loading={contactMut.isPending}
              disabled={!contactDirty || contactMut.isPending}
              onClick={() => contactMut.mutate()}
            >
              Save contact details
            </Button>
            <p className="text-xs text-muted-foreground">
              The user ID ({u.user_code}) never changes — changing the email or
              phone doesn't affect how they log in.
            </p>
          </CardContent>
        </Card>

        <Card id="trade-alerts">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Volume2 className="size-4" /> Trade alerts
            </CardTitle>
            <CardDescription>
              Pop a notification with sound in YOUR admin panel whenever this
              user opens or closes a trade.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <Button
              variant="outline"
              className={cn(
                "w-full",
                u.trade_alert && "border-emerald-500/50 text-emerald-700 dark:text-emerald-300"
              )}
              loading={tradeAlertMut.isPending}
              onClick={() => tradeAlertMut.mutate({ enabled: !u.trade_alert })}
            >
              <Volume2 className="size-4" />
              Alerts: {u.trade_alert ? "ON" : "OFF"}
            </Button>
            <div className="space-y-1.5">
              <Label className="text-xs uppercase tracking-wider text-muted-foreground">
                Sound
              </Label>
              <div className="flex gap-2">
                <select
                  value={u.trade_alert_sound || "chime"}
                  onChange={(e) => {
                    // Preview as you pick — picking a sound you can't hear
                    // first is how you end up with fifteen identical bells.
                    playSound(e.target.value);
                    tradeAlertMut.mutate({ sound: e.target.value });
                  }}
                  className="h-9 flex-1 rounded-md border border-border bg-background px-2 text-sm"
                >
                  {NOTIFY_SOUNDS.map((snd) => (
                    <option key={snd.id} value={snd.id}>
                      {snd.label}
                    </option>
                  ))}
                </select>
                <Button
                  variant="outline"
                  onClick={() => playSound(u.trade_alert_sound || "chime")}
                  title="Play this sound"
                >
                  Test
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">
                Only admins who own this user hear it. Silenced entirely when
                Platform settings → Notifications is off.
              </p>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Reset-password modal. Submitted on Enter from either field so
          the keyboard-only flow stays one-handed; the eye toggle swaps
          both fields together so the admin can verify what they typed
          without losing the visibility state on confirm. */}
      <Dialog open={resetOpen} onOpenChange={setResetOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <KeyRound className="size-4" /> Reset password — {u.full_name}
            </DialogTitle>
            <DialogDescription>
              The user will be forced to change this password on their next
              login. Their active web session is not signed out.
            </DialogDescription>
          </DialogHeader>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (!resetSaving) submitResetPassword();
            }}
            className="space-y-3"
          >
            <div className="space-y-1.5">
              <Label htmlFor="reset-pw">New password</Label>
              <div className="relative">
                <Input
                  id="reset-pw"
                  type={resetShow ? "text" : "password"}
                  autoFocus
                  autoComplete="new-password"
                  placeholder="Minimum 6 characters"
                  value={resetPw}
                  onChange={(e) => setResetPw(e.target.value)}
                  className="pr-10"
                />
                <button
                  type="button"
                  aria-label={resetShow ? "Hide password" : "Show password"}
                  onClick={() => setResetShow((v) => !v)}
                  className="absolute inset-y-0 right-0 flex items-center px-3 text-muted-foreground hover:text-foreground"
                >
                  {resetShow ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                </button>
              </div>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="reset-pw2">Confirm password</Label>
              <Input
                id="reset-pw2"
                type={resetShow ? "text" : "password"}
                autoComplete="new-password"
                placeholder="Re-enter the password"
                value={resetPw2}
                onChange={(e) => setResetPw2(e.target.value)}
              />
              {resetPw2.length > 0 && resetPw !== resetPw2 && (
                <p className="text-xs text-destructive">Passwords do not match</p>
              )}
            </div>
            <DialogFooter className="gap-2 pt-1">
              <Button
                type="button"
                variant="outline"
                onClick={() => setResetOpen(false)}
                disabled={resetSaving}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                loading={resetSaving}
                disabled={
                  resetSaving ||
                  resetPw.length < 6 ||
                  resetPw !== resetPw2
                }
              >
                Reset password
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-2 border-b border-border/50 py-1 last:border-b-0">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-tabular">{value}</span>
    </div>
  );
}

function Stat({ label, value, highlighted }: { label: string; value: string; highlighted?: boolean }) {
  return (
    <div className={cn(
      "rounded-md border p-2",
      highlighted
        ? "border-red-500/50 bg-red-500/10"
        : "border-border bg-muted/30"
    )}>
      <div className={cn("text-[10px] uppercase tracking-wider", highlighted ? "text-red-600" : "text-muted-foreground")}>{label}</div>
      <div className={cn("font-tabular text-sm", highlighted && "text-red-700")}>{value}</div>
    </div>
  );
}
