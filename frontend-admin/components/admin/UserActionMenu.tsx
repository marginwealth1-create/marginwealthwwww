"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  Activity,
  ArrowRightLeft,
  Ban,
  CheckCircle2,
  Eye,
  Gift,
  LogIn,
  MinusCircle,
  MoreHorizontal,
  PlusCircle,
  Power,
  Settings2,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { UsersAPI, BonusesAdminAPI } from "@/lib/api";
import { TransferUserDialog } from "@/components/admin/TransferUserDialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";
import { useAdminAuthStore } from "@/stores/authStore";
import { canSee } from "@/lib/permissions";
import { useMarketStream } from "@/lib/useMarketStream";
import { useMemo } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

type ActionKind =
  | null
  | "addFund"
  | "deductFund"
  | "addBonus"
  | "deductBonus"
  | "ban"
  | "kill"
  | "delete"
  | "stats";

interface Props {
  user: any;
  onChange?: () => void;
}

export function UserActionMenu({ user, onChange }: Props) {
  const router = useRouter();
  const qc = useQueryClient();
  const me = useAdminAuthStore((s) => s.admin);
  const canBonus = canSee(me, "bonuses");
  // A BROKER's fund add/deduct + password reset are gated on separate perms
  // (Deposits / Withdrawals / Change-password) so Users-EDIT alone can't do
  // them. Admins / super-admin are unchanged (backend still enforces `users`).
  const isBroker = String(me?.role || "") === "BROKER";
  const canAddFund = !isBroker || canSee(me, "deposits");
  const canDeductFund = !isBroker || canSee(me, "withdrawals");
  const canResetPw = !isBroker || canSee(me, "user_password");
  const [menuOpen, setMenuOpen] = useState(false);
  const [action, setAction] = useState<ActionKind>(null);
  const [transferOpen, setTransferOpen] = useState(false);
  const [amount, setAmount] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  function close() {
    setAction(null);
    setAmount("");
    setNote("");
    setBusy(false);
  }

  // Wrap any per-action handler so clicking a menu item closes the
  // centered dialog menu before kicking off the action (which usually
  // opens its own sub-dialog). Two stacked dialogs flicker on mobile,
  // so we close-then-set-action in the same microtask.
  function pick(next: () => void) {
    setMenuOpen(false);
    next();
  }

  function refresh() {
    qc.invalidateQueries({ queryKey: ["admin", "users"] });
    qc.invalidateQueries({ queryKey: ["admin", "user", user.id] });
    onChange?.();
  }

  async function runWalletAdjust(kind: "addFund" | "deductFund") {
    const num = Number(amount);
    if (!num || num <= 0) {
      toast.error("Enter a positive amount");
      return;
    }
    if (!note.trim()) {
      toast.error("Reason is mandatory");
      return;
    }
    setBusy(true);
    try {
      await UsersAPI.walletAdjust(user.id, {
        amount: kind === "addFund" ? num : -num,
        narration: note.trim(),
        transaction_type: kind === "addFund" ? "ADJUSTMENT" : "ADJUSTMENT",
      });
      toast.success(
        kind === "addFund" ? `Credited ₹${num} to ${user.user_code}` : `Debited ₹${num} from ${user.user_code}`
      );
      refresh();
      close();
    } catch (e: any) {
      toast.error(e?.response?.data?.error?.message || e.message || "Failed");
    } finally {
      setBusy(false);
    }
  }

  async function runBonus(kind: "addBonus" | "deductBonus") {
    const num = Number(amount);
    if (!num || num <= 0) {
      toast.error("Enter a positive amount");
      return;
    }
    setBusy(true);
    try {
      if (kind === "addBonus") {
        await BonusesAdminAPI.grant({ user_id: user.id, amount: num, notes: note.trim() });
        toast.success(`Added ₹${num} bonus credit to ${user.user_code}`);
      } else {
        const r = await BonusesAdminAPI.deduct({ user_id: user.id, amount: num, reason: note.trim() });
        toast.success(`Deducted ₹${r.deducted} bonus credit from ${user.user_code}`);
      }
      refresh();
      close();
    } catch (e: any) {
      toast.error(
        e?.response?.data?.error?.message ||
          e?.response?.data?.detail ||
          e.message ||
          "Failed",
      );
    } finally {
      setBusy(false);
    }
  }

  async function runBan() {
    setBusy(true);
    try {
      if (user.status === "BLOCKED") {
        await UsersAPI.unblock(user.id);
        toast.success(`${user.user_code} unblocked`);
      } else {
        await UsersAPI.block(user.id, note.trim() || undefined);
        toast.success(`${user.user_code} blocked`);
      }
      refresh();
      close();
    } catch (e: any) {
      toast.error(e.message || "Failed");
    } finally {
      setBusy(false);
    }
  }

  async function runKillSwitch() {
    setBusy(true);
    try {
      const r = await UsersAPI.killSwitch(user.id, note.trim() || "kill switch");
      toast.success(
        `Kill switch ✓ — ${r.orders_cancelled} orders cancelled, ${r.positions_squared_off} positions squared off, account blocked`
      );
      refresh();
      close();
    } catch (e: any) {
      toast.error(e.message || "Failed");
    } finally {
      setBusy(false);
    }
  }

  async function runLoginAs() {
    setBusy(true);
    try {
      const r = await UsersAPI.impersonate(user.id);
      const userAppUrl = (r.user_app_url || "http://localhost:3000").replace(/\/$/, "");
      // The user app reads localStorage["nb.accessToken"] / "nb.refreshToken".
      // We push them into the user app's storage, then navigate.
      // Cross-origin localStorage write is blocked, so we open the user app
      // with the tokens in the URL hash and let it persist them.
      const params = new URLSearchParams({
        access: r.access_token,
        refresh: r.refresh_token,
        impersonating: "1",
      });
      window.open(`${userAppUrl}/login?${params.toString()}#impersonate`, "_blank");
      toast.success(`Opened user app as ${user.user_code}`);
      close();
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || e.message || "Failed");
    } finally {
      setBusy(false);
    }
  }

  async function runDelete() {
    setBusy(true);
    try {
      await UsersAPI.delete(user.id);
      toast.success(`${user.user_code} archived`);
      refresh();
      close();
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || e.message || "Failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Button
        variant="ghost"
        size="icon"
        aria-label="More actions"
        onClick={(e) => {
          e.stopPropagation();
          setMenuOpen(true);
        }}
      >
        <MoreHorizontal className="size-4" />
      </Button>

      {/* Centered action picker — replaces the old Radix dropdown which
          flipped above/below depending on the row's screen position
          (bottom rows got cut off; rows near the top scrolled the row
          itself out of view). A centered modal opens at the viewport's
          middle regardless of which row was clicked, fits all 11
          actions without measurement gymnastics, and works the same on
          mobile + desktop. */}
      <Dialog open={menuOpen} onOpenChange={setMenuOpen}>
        <DialogContent className="max-w-sm p-0">
          <DialogHeader className="px-4 pb-2 pt-4">
            <DialogTitle className="text-base">
              {user.user_code}
              {user.full_name ? (
                <span className="ml-2 text-xs font-normal text-muted-foreground">
                  {user.full_name}
                </span>
              ) : null}
            </DialogTitle>
          </DialogHeader>
          <div className="max-h-[70vh] overflow-y-auto px-1 pb-3">
            <MenuButton
              icon={<Eye className="size-4" />}
              label="View Profile"
              onClick={() => pick(() => router.push(`/users/${user.id}`))}
            />
            <MenuButton
              icon={<Settings2 className="size-4" />}
              label="Segment Overrides"
              onClick={() =>
                pick(() =>
                  router.push(`/segment-settings?tab=users&user=${user.id}`),
                )
              }
            />
            <MenuButton
              icon={<ShieldCheck className="size-4" />}
              label="Risk Settings"
              onClick={() =>
                pick(() => router.push(`/risk-management?user=${user.id}`))
              }
            />
            <MenuButton
              icon={<Activity className="size-4" />}
              label="Live Trade Stats"
              onClick={() => pick(() => setAction("stats"))}
            />

            <MenuSeparator />

            {canAddFund && (
              <MenuButton
                icon={<PlusCircle className="size-4" />}
                label="Add Fund"
                onClick={() => pick(() => setAction("addFund"))}
              />
            )}
            {canDeductFund && (
              <MenuButton
                icon={<MinusCircle className="size-4" />}
                label="Deduct Fund"
                onClick={() => pick(() => setAction("deductFund"))}
              />
            )}
            {canBonus && (
              <>
                <MenuButton
                  icon={<Gift className="size-4" />}
                  label="Add Bonus"
                  onClick={() => pick(() => setAction("addBonus"))}
                />
                <MenuButton
                  icon={<Gift className="size-4" />}
                  label="Deduct Bonus"
                  onClick={() => pick(() => setAction("deductBonus"))}
                />
              </>
            )}
            <MenuSeparator />

            {/* Transfer User — opens the role-aware destination picker.
                Super-admin sees admins, admin sees their brokers, broker
                sees their sub-brokers. All trade / wallet / position
                history travels with the user automatically (scoped via
                User.assigned_admin_id / broker_ancestry in
                `core/dependencies.py:scoped_user_ids`). */}
            <MenuButton
              icon={<ArrowRightLeft className="size-4" />}
              label="Transfer User"
              onClick={() => pick(() => setTransferOpen(true))}
            />

            <MenuSeparator />

            <MenuButton
              icon={
                user.status === "BLOCKED" ? (
                  <CheckCircle2 className="size-4" />
                ) : (
                  <Ban className="size-4" />
                )
              }
              label={user.status === "BLOCKED" ? "Unblock User" : "Ban User"}
              destructive
              onClick={() => pick(() => setAction("ban"))}
            />
            <MenuButton
              icon={<Power className="size-4" />}
              label="Kill Switch"
              destructive
              onClick={() => pick(() => setAction("kill"))}
            />

            <MenuSeparator />

            <MenuButton
              icon={<LogIn className="size-4" />}
              label="Login As User"
              onClick={() => pick(runLoginAs)}
            />
            <MenuButton
              icon={<Trash2 className="size-4" />}
              label="Delete User"
              destructive
              onClick={() => pick(() => setAction("delete"))}
            />
          </div>
        </DialogContent>
      </Dialog>

      {/* ── Dialogs ───────────────────────────────────────────── */}
      <AmountDialog
        open={action === "addFund"}
        title={`Add fund — ${user.user_code}`}
        description={`Credit a positive amount to ${user.full_name}'s wallet.`}
        actionLabel="Credit wallet"
        amount={amount}
        setAmount={setAmount}
        note={note}
        setNote={setNote}
        busy={busy}
        onCancel={close}
        onSubmit={() => runWalletAdjust("addFund")}
      />
      <AmountDialog
        open={action === "deductFund"}
        title={`Deduct fund — ${user.user_code}`}
        description="Debit will be rejected if balance + credit limit can't cover it."
        actionLabel="Debit wallet"
        actionVariant="destructive"
        amount={amount}
        setAmount={setAmount}
        note={note}
        setNote={setNote}
        busy={busy}
        onCancel={close}
        onSubmit={() => runWalletAdjust("deductFund")}
      />
      <AmountDialog
        open={action === "addBonus"}
        title={`Add bonus — ${user.user_code}`}
        description="Grants bonus credit. It absorbs losses AFTER real balance, counts toward the stop-out base, and has no wager (withdrawable freely)."
        actionLabel="Add bonus"
        amount={amount}
        setAmount={setAmount}
        note={note}
        setNote={setNote}
        busy={busy}
        onCancel={close}
        onSubmit={() => runBonus("addBonus")}
      />
      <AmountDialog
        open={action === "deductBonus"}
        title={`Deduct bonus — ${user.user_code}`}
        description="Claws back bonus credit from the user's active bonuses (oldest first)."
        actionLabel="Deduct bonus"
        actionVariant="destructive"
        amount={amount}
        setAmount={setAmount}
        note={note}
        setNote={setNote}
        busy={busy}
        onCancel={close}
        onSubmit={() => runBonus("deductBonus")}
      />
      {/* Ban / Unblock */}
      <Dialog open={action === "ban"} onOpenChange={(v) => !v && close()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {user.status === "BLOCKED" ? "Unblock" : "Ban"} {user.user_code}?
            </DialogTitle>
            <DialogDescription>
              {user.status === "BLOCKED"
                ? "User will be allowed to log in and trade again."
                : "User will be unable to log in or place orders. Existing positions stay open."}
            </DialogDescription>
          </DialogHeader>
          {user.status !== "BLOCKED" && (
            <div className="space-y-1.5">
              <Label>Reason (optional)</Label>
              <Input value={note} onChange={(e) => setNote(e.target.value)} />
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={close}>
              Cancel
            </Button>
            <Button
              variant={user.status === "BLOCKED" ? "default" : "destructive"}
              onClick={runBan}
              loading={busy}
            >
              {user.status === "BLOCKED" ? "Unblock" : "Ban"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Kill switch */}
      <Dialog open={action === "kill"} onOpenChange={(v) => !v && close()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Kill switch — {user.user_code}</DialogTitle>
            <DialogDescription>
              This will <strong>cancel all pending orders</strong>, <strong>square off all open positions</strong>{" "}
              at market, and <strong>block</strong> the account. Use only in emergencies.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5">
            <Label>Reason</Label>
            <Input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="e.g. risk breach"
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={close}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={runKillSwitch} loading={busy}>
              <Power className="size-4" /> Trigger kill switch
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Live Trade Stats */}
      <LiveTradeStatsDialog
        open={action === "stats"}
        userId={user.id}
        userCode={user.user_code}
        fullName={user.full_name}
        onClose={close}
      />

      {/* Delete */}
      <Dialog open={action === "delete"} onOpenChange={(v) => !v && close()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete {user.user_code}?</DialogTitle>
            <DialogDescription>
              The account is archived (status = CLOSED) — it cannot log in, but its trade history and
              ledger remain for compliance.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={close}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={runDelete} loading={busy}>
              <Trash2 className="size-4" /> Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Transfer flow lives in its own component — picks the right
          backend endpoint based on caller role and invalidates the
          relevant React-Query keys after success. */}
      <TransferUserDialog
        user={user}
        open={transferOpen}
        onClose={() => setTransferOpen(false)}
        onChange={() => {
          refresh();
        }}
      />
    </>
  );
}

function MenuButton({
  icon,
  label,
  destructive = false,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  destructive?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex w-full items-center gap-3 rounded-md px-3 py-2 text-left text-sm transition-colors",
        "hover:bg-muted/60 focus-visible:bg-muted/60 focus-visible:outline-none",
        destructive
          ? "text-destructive hover:bg-destructive/10 focus-visible:bg-destructive/10"
          : "text-foreground",
      )}
    >
      <span className="shrink-0 text-muted-foreground">{icon}</span>
      <span className="flex-1">{label}</span>
    </button>
  );
}

function MenuSeparator() {
  return <div className="my-1 h-px bg-border" />;
}

function AmountDialog({
  open,
  title,
  description,
  actionLabel,
  actionVariant = "default",
  amount,
  setAmount,
  note,
  setNote,
  busy,
  onCancel,
  onSubmit,
}: {
  open: boolean;
  title: string;
  description: string;
  actionLabel: string;
  actionVariant?: "default" | "destructive";
  amount: string;
  setAmount: (v: string) => void;
  note: string;
  setNote: (v: string) => void;
  busy: boolean;
  onCancel: () => void;
  onSubmit: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={(v) => !v && onCancel()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label>Amount (₹)</Label>
            <Input
              type="number"
              inputMode="decimal"
              step="0.01"
              min="0"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              autoFocus
            />
          </div>
          <div className="space-y-1.5">
            <Label>Reason / narration</Label>
            <Input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Mandatory for audit trail"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <Button variant={actionVariant} onClick={onSubmit} loading={busy}>
            {actionLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}


// ── Live Trade Stats dialog ────────────────────────────────────────
// Snapshot of the user's trading state right now: floating P/L, margin
// used, equity, carryforward requirement, weekly + all-time realised
// stats, and the open-positions list. Polled every 3 s while the
// dialog is open so the numbers stay live.

function _fmtINR(n: number | string | null | undefined): string {
  const v = Number(n ?? 0);
  if (!Number.isFinite(v)) return "₹0.00";
  return `₹${v.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function _pnlClass(n: number | string | null | undefined): string {
  const v = Number(n ?? 0);
  if (v > 0) return "text-emerald-500";
  if (v < 0) return "text-red-500";
  return "text-foreground";
}

export function LiveTradeStatsDialog({
  open,
  userId,
  userCode,
  fullName,
  onClose,
}: {
  open: boolean;
  userId: string;
  userCode: string;
  fullName: string;
  onClose: () => void;
}) {
  // REST snapshot — wallet figures (margin used, weekly P/L, weekly /
  // all-time trade counts) don't tick, so polling them every 5 s is
  // plenty. Floating P/L + equity + per-row LTP come from the WS stream
  // below and override the REST values on every tick.
  const { data, isLoading, error } = useQuery<any>({
    queryKey: ["admin", "live-trade-stats", userId],
    queryFn: () => UsersAPI.liveTradeStats(userId),
    enabled: open,
    refetchInterval: open ? 5000 : false,
    staleTime: 2000,
    refetchOnWindowFocus: false,
  });

  const restOpenPositions: any[] = Array.isArray(data?.open_positions)
    ? data!.open_positions
    : [];

  // Subscribe to the same `/ws/marketdata` stream the user terminal
  // uses — the backend pump emits per-token ticks at ~250 ms with the
  // overlaid bid/ask/LTP and live fx_rate. We re-derive floating P/L
  // entirely client-side from the latest ticks + the snapshot's
  // avg_price/qty/segment, so this dialog updates tick-to-tick without
  // any additional REST round-trips.
  const wsTokens = useMemo(
    () =>
      restOpenPositions
        .map((p: any) => String(p.instrument_token || ""))
        .filter(Boolean),
    [restOpenPositions],
  );
  const stream = useMarketStream(open ? wsTokens : []);

  // Apply live ticks on top of the REST snapshot — close-side price
  // (bid for long, ask for short) matches the trader's actual exit
  // price and what the user-side terminal renders.
  const live = useMemo(() => {
    let livePnl = 0;
    const rows = restOpenPositions.map((p: any) => {
      const tick = stream.get(String(p.instrument_token));
      const isLong = Number(p.quantity) > 0;
      const liveLtp = Number(tick?.ltp ?? p.ltp ?? 0);
      const bid = Number(tick?.bid ?? 0);
      const ask = Number(tick?.ask ?? 0);
      const closePrice = (isLong ? bid : ask) || liveLtp;
      // FX conversion disabled platform-wide — feed prices are INR.
      const rowPnl = (closePrice - Number(p.avg_price)) * Number(p.quantity);
      livePnl += rowPnl;
      return {
        ...p,
        ltp: closePrice || Number(p.ltp),
        unrealized_pnl_inr: rowPnl,
      };
    });
    return { rows, floating_pnl: livePnl };
  }, [restOpenPositions, stream]);

  // Equity = available + used + live floating P/L (matches the user
  // terminal's WalletStrip math).
  const liveEquity =
    Number(data?.available_balance ?? 0) +
    Number(data?.margin_used ?? 0) +
    live.floating_pnl;

  const open_positions = live.rows;

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>
            📊 Live Trading Stats — {fullName || userCode}
          </DialogTitle>
          <DialogDescription>
            Floating P/L &amp; Equity tick live from the market feed; wallet
            and realized stats re-poll every 5 s while this dialog is open.
          </DialogDescription>
        </DialogHeader>

        {isLoading && !data && (
          <div className="grid h-32 place-items-center text-sm text-muted-foreground">
            Loading…
          </div>
        )}
        {error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
            {(error as any)?.message || "Failed to load stats"}
          </div>
        )}

        {data && (
          <>
            {/* Cards are grouped into three sections so the admin can
                tell at a glance what's live vs polled:
                  1. Live state    — ticks every WS frame
                  2. Carry-forward — overnight (NRML) margin snapshot
                  3. Realized P/L  — closed-position aggregates
                Each section uses a 2-up grid on sm and a 4-up grid on lg,
                so card counts (4 / 2 / 4) align cleanly with no orphans. */}

            {/* ── Live state ─────────────────────────────────────── */}
            <Section title="Live state" hint="ticks live">
              <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
                <StatCard
                  label="Floating P/L"
                  value={_fmtINR(live.floating_pnl)}
                  valueClass={_pnlClass(live.floating_pnl)}
                />
                <StatCard
                  label="Margin Used"
                  value={_fmtINR(data.margin_used)}
                />
                <StatCard
                  label="Available"
                  value={_fmtINR(data.available_balance)}
                />
                <StatCard label="Equity" value={_fmtINR(liveEquity)} />
              </div>
            </Section>

            {/* ── Carry-forward ──────────────────────────────────── */}
            <Section title="Carry-forward (overnight)" hint="EOD snapshot">
              <div className="grid grid-cols-2 gap-2">
                <StatCard
                  label="CF Total (EOD)"
                  value={_fmtINR(data.cf_total_eod)}
                  valueClass="text-amber-500"
                />
                <StatCard
                  label="CF Extra Needed"
                  value={_fmtINR(data.cf_extra_needed)}
                  valueClass={
                    Number(data.cf_extra_needed) > 0
                      ? "text-red-500"
                      : "text-emerald-500"
                  }
                />
              </div>
            </Section>

            {/* ── Realized P/L ───────────────────────────────────── */}
            <Section title="Realized P/L" hint="closed positions">
              <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
                <StatCard
                  label="Weekly Net P/L"
                  value={_fmtINR(data.weekly_net_pnl)}
                  valueClass={_pnlClass(data.weekly_net_pnl)}
                />
                <StatCard
                  label="Weekly Trades"
                  value={String(data.weekly_trades ?? 0)}
                  meta={
                    <>
                      <span className="text-emerald-500">
                        {data.weekly_wins ?? 0}W
                      </span>{" "}
                      ·{" "}
                      <span className="text-red-500">
                        {data.weekly_losses ?? 0}L
                      </span>
                    </>
                  }
                />
                <StatCard
                  label="Closed P/L (All-time)"
                  value={_fmtINR(data.closed_pnl_all_time)}
                  valueClass={_pnlClass(data.closed_pnl_all_time)}
                />
                <StatCard
                  label="All-time Trades"
                  value={String(data.all_time_trades ?? 0)}
                  meta={
                    <>
                      <span className="text-emerald-500">
                        {data.all_time_wins ?? 0}W
                      </span>{" "}
                      ·{" "}
                      <span className="text-red-500">
                        {data.all_time_losses ?? 0}L
                      </span>
                    </>
                  }
                />
              </div>
            </Section>

            {/* Open positions table */}
            <div className="mt-3">
              <div className="mb-1.5 text-sm font-medium">
                Open positions ({open_positions.length})
              </div>
              {open_positions.length === 0 ? (
                <div className="rounded-md border border-border bg-muted/10 px-3 py-3 text-xs text-muted-foreground">
                  No open positions.
                </div>
              ) : (
                <div className="overflow-x-auto rounded-md border border-border">
                  <table className="min-w-full text-xs">
                    <thead className="bg-muted/30 text-muted-foreground">
                      <tr>
                        <th className="px-2 py-1.5 text-left">Symbol</th>
                        <th className="px-2 py-1.5 text-left">M</th>
                        <th className="px-2 py-1.5 text-right">Qty</th>
                        <th className="px-2 py-1.5 text-right">Avg</th>
                        <th className="px-2 py-1.5 text-right">LTP</th>
                        <th className="px-2 py-1.5 text-right">P/L (INR)</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {open_positions.map((p, i) => (
                        <tr key={`${p.symbol}-${i}`}>
                          <td className="px-2 py-1.5 font-medium">{p.symbol}</td>
                          <td className="px-2 py-1.5">{p.product_type}</td>
                          <td className="px-2 py-1.5 text-right font-tabular">
                            {Number(p.quantity).toLocaleString("en-IN")}
                          </td>
                          <td className="px-2 py-1.5 text-right font-tabular">
                            {Number(p.avg_price).toFixed(2)}
                          </td>
                          <td className="px-2 py-1.5 text-right font-tabular">
                            {Number(p.ltp).toFixed(2)}
                          </td>
                          <td
                            className={cn(
                              "px-2 py-1.5 text-right font-tabular",
                              _pnlClass(p.unrealized_pnl_inr),
                            )}
                          >
                            {_fmtINR(p.unrealized_pnl_inr)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Section({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mt-2 first:mt-0">
      <div className="mb-1.5 flex items-baseline justify-between">
        <div className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          {title}
        </div>
        {hint && (
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground/70">
            {hint}
          </div>
        )}
      </div>
      {children}
    </div>
  );
}

function StatCard({
  label,
  value,
  valueClass,
  meta,
}: {
  label: string;
  value: string;
  valueClass?: string;
  meta?: React.ReactNode;
}) {
  return (
    <div className="rounded-md border border-border bg-muted/10 px-2.5 py-2">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div
        className={cn(
          "mt-0.5 font-tabular text-base font-semibold tabular-nums",
          valueClass,
        )}
      >
        {value}
      </div>
      {meta && <div className="mt-0.5 text-[10px]">{meta}</div>}
    </div>
  );
}
