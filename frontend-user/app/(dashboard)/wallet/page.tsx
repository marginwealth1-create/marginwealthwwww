"use client";

import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  ArrowDownToLine,
  ArrowUpToLine,
  Building2,
  CheckCircle2,
  ChevronRight,
  Clock,
  Copy,
  CreditCard,
  Loader2,
  Mail,
  MessageCircle,
  Plus,
  QrCode,
  Trash2,
  Upload,
  Wallet as WalletIcon,
  X,
  XCircle,
} from "lucide-react";
import { WalletAPI } from "@/lib/api";
import { API_URL } from "@/lib/constants";
import { useAuthStore } from "@/stores/authStore";
import { DemoUpgradeDialog } from "@/components/wallet/DemoUpgradeDialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { UpiQR, buildUpiUri } from "@/components/common/UpiQR";
import { WdRulesBanner } from "@/components/common/WdRulesBanner";
import { AddFundsWizard } from "@/components/wallet/AddFundsWizard";
import { BonusCard } from "@/components/wallet/BonusCard";
import { cn, formatINR, pnlColor } from "@/lib/utils";
import {
  buildMailtoUrl,
  buildWhatsappUrl,
  useSupportContacts,
} from "@/lib/useSupport";

// ─────────────────────────────────────────────────────────────────
// Official-style UPI logo mark (orange/green chevrons + UPI text).
// Inline SVG so no asset bundling needed.
// ─────────────────────────────────────────────────────────────────
function UpiLogo({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 80 80"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      aria-label="UPI"
      role="img"
    >
      <rect width="80" height="80" rx="14" fill="#fff" stroke="#e5e5e5" />
      {/* Orange chevron (left) */}
      <polygon points="18,16 36,16 26,50 8,50" fill="#f47920" />
      {/* Green chevron (right) */}
      <polygon points="38,16 56,16 46,50 28,50" fill="#75bf43" />
      {/* Outlined chevron */}
      <polygon points="40,16 64,16 54,50 30,50" fill="none" stroke="#0f3470" strokeWidth="2.5" />
      {/* UPI wordmark */}
      <text
        x="40"
        y="68"
        textAnchor="middle"
        fontFamily="system-ui, sui, sans-serif"
        fontSize="12"
        fontWeight="800"
        fill="#0f3470"
        letterSpacing="1"
      >
        UPI
      </text>
    </svg>
  );
}

export default function WalletPage() {
  const qc = useQueryClient();
  const authUser = useAuthStore((s) => s.user);
  const isDemo = authUser?.is_demo ?? false;
  const { data: summary } = useQuery({
    queryKey: ["wallet-summary"],
    queryFn: () => WalletAPI.summary(),
    // 3 s so the balance flips within one heartbeat of admin approval —
    // 8 s felt like the wallet was frozen after a deposit was approved.
    refetchInterval: 3000,
  });
  const { data: txns } = useQuery({
    queryKey: ["wallet-txns"],
    // 200 (not 50) so a heavy trader's recent deposits still fall inside
    // the window alongside all the trade P&L / charge rows now shown.
    queryFn: () => WalletAPI.transactions(200),
    refetchInterval: 5000,
  });
  const { data: deposits } = useQuery({
    queryKey: ["my-deposits"],
    queryFn: () => WalletAPI.myDeposits(),
    // Pending → Approved transition lives on the deposits row; poll fast so
    // the user sees the status change without hitting refresh.
    refetchInterval: 3000,
  });
  const { data: withdrawals } = useQuery({
    queryKey: ["my-withdrawals"],
    queryFn: () => WalletAPI.myWithdrawals(),
    refetchInterval: 5000,
  });
  const { data: companyBanks } = useQuery({ queryKey: ["company-banks"], queryFn: () => WalletAPI.companyBanks() });
  const { data: myBanks } = useQuery({ queryKey: ["my-banks"], queryFn: () => WalletAPI.myBankAccounts() });
  // Effective withdrawal rules — drives whether bank details are mandatory.
  const { data: wdRulesData } = useQuery({ queryKey: ["user", "wd-rules"], queryFn: () => WalletAPI.wdRules() });
  const requireBank = !!wdRulesData?.withdrawal?.require_bank_details;

  // ── Dialogs ─────────────────────────────────────────────────────
  const [depositOpen, setDepositOpen] = useState(false);
  const [withdrawOpen, setWithdrawOpen] = useState(false);
  const [demoUpgradeOpen, setDemoUpgradeOpen] = useState(false);

  function openDeposit() {
    if (isDemo) { setDemoUpgradeOpen(true); return; }
    setDepositOpen(true);
  }
  function openWithdraw() {
    if (isDemo) { setDemoUpgradeOpen(true); return; }
    setWithdrawOpen(true);
  }

  // ── Form state ──────────────────────────────────────────────────
  const [dep, setDep] = useState({
    amount: "",
    utr_number: "",
    payment_mode: "UPI",
    screenshot_url: "",
    user_remark: "",
    bank_account_id: "",
  });
  const [wd, setWd] = useState({
    amount: "",
    mode: "UPI" as "UPI" | "BANK",
    upi_id: "",
    qr_url: "",
    bank_name: "",
    account_number: "",
    ifsc_code: "",
    account_holder: "",
    remarks: "",
  });
  const [wdSubmitting, setWdSubmitting] = useState(false);
  // Stable idempotency key for the in-progress withdrawal — generated on the
  // first submit, reused on a retry (network blip / lost response), cleared
  // after success. Combined with the disabled button this guarantees a
  // double / triple click never creates more than ONE withdrawal request.
  const wdIdemRef = useRef<string>("");
  const [qrPreview, setQrPreview] = useState<{ upiId: string; payee?: string; amount?: number } | null>(null);

  // ── Saved payout methods (withdraw destination) ─────────────────
  // "PRIMARY" = send the primary bank + primary UPI together; otherwise a
  // specific saved-method id.
  const [wdChoice, setWdChoice] = useState<string>("PRIMARY");
  const [addOpen, setAddOpen] = useState(false);
  const [addSaving, setAddSaving] = useState(false);
  const [addForm, setAddForm] = useState({
    type: "UPI" as "UPI" | "BANK",
    upi_id: "",
    bank_name: "",
    account_holder: "",
    account_number: "",
    ifsc_code: "",
  });
  const upiAllowed = wdRulesData?.withdrawal?.allow_upi_payout !== false;
  const savedMethods = (myBanks ?? []).filter((m) => m.method_type === "BANK" || upiAllowed);
  const primaryBank = savedMethods.find((m) => m.method_type === "BANK" && m.is_default);
  const primaryUpi = savedMethods.find((m) => m.method_type === "UPI" && m.is_default);

  // Auto-pick the default (or first) company bank when dialog opens
  useEffect(() => {
    if (depositOpen && companyBanks?.length && !dep.bank_account_id) {
      const def = companyBanks.find((b: any) => b.is_default) ?? companyBanks[0];
      setDep((d) => ({ ...d, bank_account_id: def.id }));
    }
  }, [depositOpen, companyBanks]);

  const selectedBank = companyBanks?.find((b: any) => b.id === dep.bank_account_id) ?? companyBanks?.[0];

  // ── Helpers ────────────────────────────────────────────────────
  function copyText(text: string, label: string) {
    navigator.clipboard.writeText(text).then(
      () => toast.success(`${label} copied`),
      () => toast.error("Copy failed")
    );
  }

  const [uploading, setUploading] = useState(false);
  async function uploadScreenshot(file: File) {
    if (!file.type.startsWith("image/")) {
      toast.error("Pick an image file");
      return;
    }
    if (file.size > 15 * 1024 * 1024) {
      toast.error("File too large (max 15 MB)");
      return;
    }
    setUploading(true);
    try {
      // Phone screenshots are often 3–8 MB; compressing client-side cuts the
      // upload from "felt slow" (seconds) to instant on a 4G connection, and
      // keeps disk usage small on the server.
      const toUpload = await compressImage(file);
      const r = await WalletAPI.uploadScreenshot(toUpload);
      setDep((d) => ({ ...d, screenshot_url: r.url }));
      toast.success("Screenshot uploaded");
    } catch (e: any) {
      toast.error(e.message || "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  async function submitDeposit() {
    if (!dep.amount || Number(dep.amount) <= 0) return toast.error("Amount required");
    if (!dep.bank_account_id) return toast.error("Pick a payment method");
    // UTR is OPTIONAL — admin approves on screenshot + manual verification
    // anyway. Forcing UTR up-front blocked users who paid first then
    // came back later to copy the txn ref.
    try {
      const payload = {
        ...dep,
        amount: Number(dep.amount),
        utr_number: dep.utr_number.trim() || undefined,
      };
      await WalletAPI.createDeposit(payload as any);
      toast.success("Deposit submitted — awaiting admin approval");
      setDepositOpen(false);
      setDep({ amount: "", utr_number: "", payment_mode: "UPI", screenshot_url: "", user_remark: "", bank_account_id: "" });
      // Refresh the pending list immediately so the new request appears
      // without waiting for the 3 s poll. wallet-summary/txns also touched
      // so the pending-count tile updates in lock-step.
      qc.invalidateQueries({ queryKey: ["my-deposits"] });
      qc.invalidateQueries({ queryKey: ["wallet-summary"] });
      qc.invalidateQueries({ queryKey: ["wallet-txns"] });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  // Fold a saved method into the withdrawal `bank` snapshot the backend
  // expects (bank fields for BANK, upi_id for UPI).
  function applyMethodToBank(m: any, bank: Record<string, string>) {
    if (m.method_type === "UPI") {
      if (m.upi_id) bank.upi_id = m.upi_id;
    } else {
      bank.name = m.bank_name || "";
      bank.account_number = m.account_number || "";
      bank.ifsc = (m.ifsc_code || "").toUpperCase();
      bank.holder = m.account_holder || "";
    }
  }

  async function submitWithdrawal() {
    if (wdSubmitting) return; // guard against double / triple clicks
    if (!wd.amount || Number(wd.amount) <= 0) return toast.error("Amount required");
    if (!savedMethods.length) return toast.error("Add a bank or UPI to withdraw to");

    // Build the destination from the saved method(s) the user picked. Default
    // ("PRIMARY") sends the primary bank + primary UPI together so the admin
    // can pay by either; otherwise a single chosen method.
    const bank: Record<string, string> = {};
    if (wdChoice === "PRIMARY") {
      if (primaryBank) applyMethodToBank(primaryBank, bank);
      if (primaryUpi && upiAllowed) applyMethodToBank(primaryUpi, bank);
    } else {
      const m = savedMethods.find((x) => x.id === wdChoice);
      if (!m) return toast.error("Select where to withdraw");
      applyMethodToBank(m, bank);
    }
    const hasUpi = !!bank.upi_id;
    const hasBank = !!(bank.account_number && bank.ifsc);
    if (!hasUpi && !hasBank) return toast.error("Select where to withdraw");
    if (requireBank && !hasBank)
      return toast.error("Bank details are required — pick a bank account");

    // One stable key per intended withdrawal — reused if the user retries
    // after a network blip, so the backend dedups instead of double-booking.
    if (!wdIdemRef.current) {
      wdIdemRef.current =
        typeof crypto !== "undefined" && crypto.randomUUID
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }

    setWdSubmitting(true);
    try {
      await WalletAPI.createWithdrawal({
        amount: Number(wd.amount),
        remarks: wd.remarks,
        bank,
        idempotency_key: wdIdemRef.current,
      });
      toast.success("Withdrawal requested");
      wdIdemRef.current = "";
      setWithdrawOpen(false);
      setWd({
        amount: "",
        mode: "UPI",
        upi_id: "",
        qr_url: "",
        bank_name: "",
        account_number: "",
        ifsc_code: "",
        account_holder: "",
        remarks: "",
      });
      setWdChoice("PRIMARY");
      qc.invalidateQueries({ queryKey: ["my-withdrawals"] });
      qc.invalidateQueries({ queryKey: ["wallet-summary"] });
      qc.invalidateQueries({ queryKey: ["wallet-txns"] });
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setWdSubmitting(false);
    }
  }

  // ── Saved payout method management ──────────────────────────────
  async function savePayoutMethod() {
    if (addSaving) return;
    const body: any = { method_type: addForm.type };
    if (addForm.type === "UPI") {
      const v = addForm.upi_id.trim();
      if (!v.includes("@")) return toast.error("Enter a valid UPI id (e.g. name@bank)");
      body.upi_id = v;
    } else {
      if (!addForm.account_holder.trim()) return toast.error("Account holder name required");
      if (!addForm.account_number.trim()) return toast.error("Account number required");
      if (!addForm.ifsc_code.trim()) return toast.error("IFSC required");
      body.bank_name = addForm.bank_name.trim();
      body.account_holder = addForm.account_holder.trim();
      body.account_number = addForm.account_number.trim();
      body.ifsc_code = addForm.ifsc_code.trim().toUpperCase();
    }
    setAddSaving(true);
    try {
      const saved = await WalletAPI.addBankAccount(body);
      toast.success(addForm.type === "UPI" ? "UPI added" : "Bank added");
      setAddOpen(false);
      setAddForm({ type: "UPI", upi_id: "", bank_name: "", account_holder: "", account_number: "", ifsc_code: "" });
      await qc.invalidateQueries({ queryKey: ["my-banks"] });
      if (saved?.id) setWdChoice(saved.id); // use the just-added method
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setAddSaving(false);
    }
  }

  async function makePrimaryMethod(id: string) {
    try {
      await WalletAPI.setPrimaryBankAccount(id);
      qc.invalidateQueries({ queryKey: ["my-banks"] });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function removeMethod(id: string) {
    try {
      await WalletAPI.deleteBankAccount(id);
      qc.invalidateQueries({ queryKey: ["my-banks"] });
      setWdChoice((c) => (c === id ? "PRIMARY" : c));
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  // (UPI deeplink generation removed — payment is manual via the user's
  // own UPI app; we only show a static "UPI accepted" badge.)

  return (
    <div className="space-y-5">
      {/* ── Hero balance card ──────────────────────────────────── */}
      <section className="overflow-hidden rounded-2xl bg-gradient-to-br from-primary via-primary to-primary/85 p-5 text-primary-foreground shadow-lg shadow-primary/20">
        <div className="flex items-start justify-between">
          <div className="space-y-0.5">
            <div className="inline-flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider opacity-80">
              <WalletIcon className="size-3.5" /> Available balance
            </div>
            <div className="font-tabular text-3xl font-bold md:text-4xl">
              {/* Never display a negative balance on the user-facing hero —
                  any shortfall is surfaced as settlement_outstanding in
                  the banner below. */}
              {formatINR(Math.max(0, Number(summary?.available_balance ?? 0)))}
            </div>
            <div className="text-[11px] opacity-80">Wallet · {selectedBank?.account_holder ?? "MarginWealth"}</div>
          </div>
          <div className="hidden gap-2 sm:flex">
            <button
              onClick={openDeposit}
              className="inline-flex items-center gap-1.5 rounded-full bg-white px-4 py-2 text-xs font-semibold text-primary shadow-sm transition hover:bg-white/90"
            >
              <ArrowDownToLine className="size-3.5" /> Add funds
            </button>
            <Dialog open={withdrawOpen} onOpenChange={(v) => v ? openWithdraw() : setWithdrawOpen(false)}>
              <DialogTrigger asChild>
                <button className="inline-flex items-center gap-1.5 rounded-full bg-white/15 px-4 py-2 text-xs font-semibold text-primary-foreground backdrop-blur transition hover:bg-white/25">
                  <ArrowUpToLine className="size-3.5" /> Withdraw
                </button>
              </DialogTrigger>
            </Dialog>
          </div>
        </div>

        <div className="mt-5 grid grid-cols-3 divide-x divide-white/15 text-center">
          <HeroStat label="Used margin" value={formatINR(summary?.used_margin ?? 0)} />
          <HeroStat label="Credit limit" value={formatINR(summary?.credit_limit ?? 0)} />
          <HeroStat label="Realized P&L" value={formatINR(summary?.realized_pnl ?? 0)} />
        </div>
      </section>


      {/* ── Mobile-only action row (desktop has them in the hero) ── */}
      <div className="grid grid-cols-2 gap-3 sm:hidden">
        <Button onClick={openDeposit} className="h-12 rounded-xl">
          <ArrowDownToLine className="size-4" /> Add funds
        </Button>
        <Button onClick={openWithdraw} variant="outline" className="h-12 rounded-xl">
          <ArrowUpToLine className="size-4" /> Withdraw
        </Button>
      </div>

      {/* ── Stat tiles (totals) ───────────────────────────────── */}
      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="Total deposits" value={formatINR(summary?.total_deposits ?? 0)} />
        <StatTile label="Total withdrawals" value={formatINR(summary?.total_withdrawals ?? 0)} />
        <StatTile
          label="Pending deposits"
          value={String((deposits ?? []).filter((d: any) => d.status === "PENDING").length)}
          hint="awaiting admin approval"
        />
        <StatTile
          label="Pending withdrawals"
          value={String((withdrawals ?? []).filter((d: any) => d.status === "PENDING").length)}
          hint="awaiting admin approval"
        />
      </section>

      {/* Bonus credit tile — renders only when the user has bonus credit /
          active bonuses (inert while the feature is off). */}
      <BonusCard credit={summary?.bonus_free ?? summary?.credit} />

      {/* ── Unified Transactions section ─────────────────────────
          Operator request: drop the 4-box layout (Transaction history,
          My banks, Deposit requests, Withdrawal requests) and surface
          ONE clean activity feed showing every cash movement on the
          wallet — user deposits, user withdrawals, and admin Add /
          Deduct Fund (ADJUSTMENT). Trade brokerage, settlement
          accruals, etc. live on /ledger and /reports — never on this
          page. Status badges (Pending / Approved / Rejected /
          Completed) show inline so there is no need for a separate
          request-list panel.
       */}
      <TransactionsFeed
        txns={txns ?? []}
        deposits={deposits ?? []}
        withdrawals={withdrawals ?? []}
      />

      {/* ── Add funds — 4-step wizard (Amount → UPI app → QR → screenshot) ── */}
      <AddFundsWizard
        open={depositOpen}
        onClose={() => setDepositOpen(false)}
        companyBanks={(companyBanks as any[]) ?? []}
        payeeName={selectedBank?.account_holder}
        onSuccess={() => {
          qc.invalidateQueries({ queryKey: ["my-deposits"] });
          qc.invalidateQueries({ queryKey: ["wallet-summary"] });
          qc.invalidateQueries({ queryKey: ["wallet-txns"] });
        }}
      />
      {/* Legacy deposit dialog — disabled, superseded by AddFundsWizard.
          Kept (gated false) so its helpers stay referenced; remove in a
          follow-up cleanup once the wizard is verified live. */}
      {false && (
      <Dialog open={depositOpen} onOpenChange={setDepositOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <div className="flex items-start justify-between gap-3">
              <div className="flex-1">
                <DialogTitle>Add funds</DialogTitle>
                <DialogDescription>Pay → enter UTR → submit. Admin will approve in minutes.</DialogDescription>
              </div>
              {/* Support chat — opens admin-managed WhatsApp (preferred)
                  or mailto fallback. The deposit flow is the highest-
                  abandonment funnel; surfacing support here means a
                  stuck user doesn't have to close the dialog to ask
                  for help. */}
              <DepositSupportButton />
            </div>
          </DialogHeader>

          {/* Rules banner — admin-configured min / max / daily limit / day +
              time window. Resolves through the tier cascade so the user
              sees the same rules the server validator will enforce on
              submit. */}
          <WdRulesBanner kind="deposit" className="mb-2" />

          {/* Step indicator */}
          <div className="grid gap-4 md:grid-cols-2">
            {/* ─ Left: payment method ─────────────────── */}
            <div className="space-y-3">
              <SectionLabel num={1} title="Pay using" />

              {(companyBanks?.length ?? 0) === 0 ? (
                <div className="rounded-md border border-dashed border-border p-3 text-xs text-muted-foreground">
                  No payment methods configured yet.
                </div>
              ) : (
                <div className="grid grid-cols-1 gap-2">
                  {companyBanks!.map((b: any) => {
                    const active = b.id === dep.bank_account_id;
                    return (
                      <button
                        key={b.id}
                        type="button"
                        onClick={() => setDep((d) => ({ ...d, bank_account_id: b.id }))}
                        className={cn(
                          "flex items-center gap-3 rounded-lg border p-3 text-left text-xs transition",
                          active
                            ? "border-primary bg-primary/5 ring-2 ring-primary/30"
                            : "border-border bg-card hover:border-primary/40"
                        )}
                      >
                        <div className="grid size-9 place-items-center rounded-full bg-primary/10 text-primary">
                          <Building2 className="size-4" />
                        </div>
                        <div className="min-w-0 flex-1">
                          <div className="truncate text-sm font-semibold">{b.bank_name}</div>
                          <div className="truncate text-muted-foreground">{b.account_holder}</div>
                        </div>
                        {b.is_default && (
                          <span className="rounded-full bg-primary/15 px-1.5 py-0.5 text-[9px] font-semibold text-primary">
                            DEFAULT
                          </span>
                        )}
                      </button>
                    );
                  })}
                </div>
              )}

              {selectedBank && (
                <div className="space-y-2 rounded-lg border border-border bg-muted/20 p-3 text-xs">
                  <CopyRow label="A/C No." value={selectedBank.account_number} onCopy={copyText} />
                  <CopyRow label="IFSC" value={selectedBank.ifsc_code} onCopy={copyText} />
                  <CopyRow label="Holder" value={selectedBank.account_holder} onCopy={copyText} />
                  {selectedBank.upi_id && (
                    <CopyRow label="UPI ID" value={selectedBank.upi_id} onCopy={copyText} highlight />
                  )}
                  {selectedBank.upi_id && (
                    <button
                      type="button"
                      onClick={() =>
                        setQrPreview({
                          upiId: selectedBank.upi_id,
                          payee: selectedBank.account_holder,
                          amount: Number(dep.amount) || undefined,
                        })
                      }
                      className="inline-flex items-center gap-1 text-[11px] font-semibold text-primary hover:underline"
                    >
                      <QrCode className="size-3" /> Show QR code
                    </button>
                  )}
                </div>
              )}

              {/* "We accept UPI" badge — static, always visible. No button. */}
              <div className="flex items-center gap-3 rounded-xl border border-border bg-muted/20 p-3">
                <UpiLogo className="size-10 shrink-0" />
                <div className="min-w-0 text-xs">
                  <div className="font-semibold text-foreground">UPI accepted</div>
                  <div className="text-muted-foreground">
                    GPay · PhonePe · Paytm · BHIM and any UPI-enabled bank app
                  </div>
                </div>
              </div>
            </div>

            {/* ─ Right: amount + UTR + screenshot ────────── */}
            <div className="space-y-3">
              <SectionLabel num={2} title="Confirm your payment" />
              <Field label="Amount (₹)">
                <Input
                  type="number"
                  inputMode="decimal"
                  value={dep.amount}
                  onChange={(e) => setDep((d) => ({ ...d, amount: e.target.value }))}
                  className="h-11 text-lg font-semibold"
                  placeholder="500"
                />
              </Field>

              {/* Quick amount pills */}
              <div className="flex flex-wrap gap-1.5">
                {[500, 1000, 5000, 10000, 25000].map((v) => (
                  <button
                    key={v}
                    type="button"
                    onClick={() => setDep((d) => ({ ...d, amount: String(v) }))}
                    className="rounded-full border border-border bg-muted/30 px-3 py-1 text-[11px] font-medium hover:border-primary/40 hover:bg-primary/5"
                  >
                    +₹{v.toLocaleString("en-IN")}
                  </button>
                ))}
              </div>

              <Field label="Payment mode">
                <select
                  value={dep.payment_mode}
                  onChange={(e) => setDep((d) => ({ ...d, payment_mode: e.target.value }))}
                  className="h-10 w-full rounded-md border border-border bg-background px-3 text-sm"
                >
                  <option>UPI</option>
                  <option>NEFT</option>
                  <option>RTGS</option>
                  <option>IMPS</option>
                  <option>BANK_TRANSFER</option>
                </select>
              </Field>

              <Field label="UTR / Transaction reference (optional)">
                <Input
                  value={dep.utr_number}
                  onChange={(e) => setDep((d) => ({ ...d, utr_number: e.target.value }))}
                  placeholder="From your bank/UPI app receipt"
                />
              </Field>

              <Field label="Payment screenshot">
                {dep.screenshot_url ? (
                  <div className="relative inline-block">
                    <img
                      src={dep.screenshot_url.startsWith("http") ? dep.screenshot_url : `${API_URL}${dep.screenshot_url}`}
                      alt="Payment proof"
                      className="max-h-32 rounded-md border border-border object-contain"
                    />
                    <button
                      type="button"
                      onClick={() => setDep((d) => ({ ...d, screenshot_url: "" }))}
                      className="absolute -right-2 -top-2 rounded-full bg-destructive p-0.5 text-white shadow"
                      aria-label="Remove screenshot"
                    >
                      <X className="size-3" />
                    </button>
                  </div>
                ) : (
                  <label
                    className={cn(
                      "flex cursor-pointer items-center justify-center gap-2 rounded-lg border border-dashed border-border bg-muted/20 px-3 py-4 text-xs text-muted-foreground hover:border-primary/40 hover:bg-primary/5",
                      uploading && "pointer-events-none opacity-60"
                    )}
                  >
                    {uploading ? (
                      <>
                        <Loader2 className="size-4 animate-spin" /> Uploading…
                      </>
                    ) : (
                      <>
                        <Upload className="size-4" /> Click to upload (max 5 MB)
                      </>
                    )}
                    <input
                      type="file"
                      accept="image/png,image/jpeg,image/webp"
                      className="hidden"
                      onChange={(e) => {
                        const f = e.target.files?.[0];
                        if (f) uploadScreenshot(f);
                        e.target.value = "";
                      }}
                    />
                  </label>
                )}
              </Field>
              <Field label="Remarks (optional)">
                <Input
                  value={dep.user_remark}
                  onChange={(e) => setDep((d) => ({ ...d, user_remark: e.target.value }))}
                />
              </Field>
            </div>
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setDepositOpen(false)}>
              Cancel
            </Button>
            <Button onClick={submitDeposit}>
              <CheckCircle2 className="size-4" /> Submit for approval
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      )}

      {/* ── Demo upgrade dialog ─────────────────────────────── */}
      <DemoUpgradeDialog open={demoUpgradeOpen} onClose={() => setDemoUpgradeOpen(false)} />

      {/* ── Withdraw dialog ───────────────────────────────────── */}
      <Dialog open={withdrawOpen} onOpenChange={(v) => v ? openWithdraw() : setWithdrawOpen(false)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Withdraw funds</DialogTitle>
          </DialogHeader>
          {/* Rules banner — withdraw is the more-restricted side
              (day + time window + mandatory-remark typically matter). */}
          <WdRulesBanner kind="withdrawal" className="mb-2" />
          <div className="space-y-3">
            <Field label="Amount (₹)">
              <Input
                type="number"
                value={wd.amount}
                onChange={(e) => setWd((d) => ({ ...d, amount: e.target.value }))}
                className="h-11 text-lg font-semibold"
              />
            </Field>

            {/* Withdraw to — saved bank accounts + UPIs. Just pick one
                (default = primary bank + primary UPI); no re-typing. */}
            <div className="space-y-2 rounded-md border border-border p-3">
              <div className="flex items-center justify-between">
                <span className="text-sm font-semibold">Withdraw to</span>
                <button
                  type="button"
                  onClick={() => {
                    setAddOpen((o) => !o);
                    if (!upiAllowed) setAddForm((f) => ({ ...f, type: "BANK" }));
                  }}
                  className="text-xs font-semibold text-primary hover:underline"
                >
                  {addOpen ? "Close" : "+ Add bank / UPI"}
                </button>
              </div>

              {savedMethods.length === 0 && !addOpen && (
                <p className="text-[11px] text-muted-foreground">
                  Add a bank account{upiAllowed ? " or UPI" : ""} to withdraw to.
                </p>
              )}

              {savedMethods.length > 0 && (
                <div className="space-y-1.5">
                  {(primaryBank || (primaryUpi && upiAllowed)) && (
                    <button
                      type="button"
                      onClick={() => setWdChoice("PRIMARY")}
                      className={`flex w-full items-start gap-2 rounded-lg border p-2.5 text-left transition ${
                        wdChoice === "PRIMARY" ? "border-primary bg-primary/5" : "border-border"
                      }`}
                    >
                      <span className={`mt-0.5 size-4 shrink-0 rounded-full border-2 ${wdChoice === "PRIMARY" ? "border-primary bg-primary" : "border-muted-foreground/40"}`} />
                      <span className="min-w-0 flex-1">
                        <span className="block text-sm font-semibold">
                          Primary <span className="text-[10px] font-normal text-muted-foreground">(recommended)</span>
                        </span>
                        <span className="block truncate text-[11px] text-muted-foreground">
                          {[
                            primaryBank && `${primaryBank.bank_name || "Bank"} ••${primaryBank.account_number.slice(-4)}`,
                            primaryUpi && upiAllowed && primaryUpi.upi_id,
                          ].filter(Boolean).join("  •  ")}
                        </span>
                      </span>
                    </button>
                  )}

                  {savedMethods.map((m) => {
                    const label = m.method_type === "UPI"
                      ? (m.upi_id || "UPI")
                      : `${m.bank_name || "Bank"} ••${(m.account_number || "").slice(-4)}`;
                    const sub = m.method_type === "UPI"
                      ? (m.account_holder || "UPI")
                      : `${m.account_holder || ""}${m.ifsc_code ? " · " + m.ifsc_code : ""}`;
                    return (
                      <div
                        key={m.id}
                        className={`flex items-center gap-2 rounded-lg border p-2.5 ${
                          wdChoice === m.id ? "border-primary bg-primary/5" : "border-border"
                        }`}
                      >
                        <button
                          type="button"
                          onClick={() => setWdChoice(m.id)}
                          className="flex min-w-0 flex-1 items-start gap-2 text-left"
                        >
                          <span className={`mt-0.5 size-4 shrink-0 rounded-full border-2 ${wdChoice === m.id ? "border-primary bg-primary" : "border-muted-foreground/40"}`} />
                          <span className="min-w-0 flex-1">
                            <span className="flex items-center gap-1.5 text-sm font-medium">
                              <span className="truncate">{label}</span>
                              <span className="rounded bg-muted px-1 text-[9px] uppercase text-muted-foreground">{m.method_type}</span>
                              {m.is_default && <span className="shrink-0 text-[10px] text-primary">★ primary</span>}
                            </span>
                            <span className="block truncate text-[11px] text-muted-foreground">{sub}</span>
                          </span>
                        </button>
                        <div className="flex shrink-0 items-center gap-1">
                          {!m.is_default && (
                            <button
                              type="button"
                              onClick={() => makePrimaryMethod(m.id)}
                              title="Set as primary"
                              className="rounded px-1.5 py-0.5 text-[10px] text-muted-foreground hover:text-primary"
                            >
                              Set primary
                            </button>
                          )}
                          <button
                            type="button"
                            onClick={() => removeMethod(m.id)}
                            title="Remove"
                            className="rounded p-1 text-muted-foreground hover:text-red-500"
                          >
                            <Trash2 className="size-3.5" />
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}

              {addOpen && (
                <div className="mt-2 space-y-2 rounded-lg border border-dashed border-border p-2.5">
                  <div className="flex gap-2">
                    {(["UPI", "BANK"] as const).filter((t) => t === "BANK" || upiAllowed).map((t) => (
                      <button
                        key={t}
                        type="button"
                        onClick={() => setAddForm((f) => ({ ...f, type: t }))}
                        className={`flex-1 rounded-md border px-2 py-1.5 text-xs font-semibold ${
                          addForm.type === t ? "border-primary bg-primary/10 text-primary" : "border-border text-muted-foreground"
                        }`}
                      >
                        {t === "UPI" ? "UPI ID" : "Bank account"}
                      </button>
                    ))}
                  </div>
                  {addForm.type === "UPI" ? (
                    <Input
                      placeholder="name@bank"
                      value={addForm.upi_id}
                      onChange={(e) => setAddForm((f) => ({ ...f, upi_id: e.target.value }))}
                    />
                  ) : (
                    <div className="space-y-2">
                      <Input placeholder="Account holder name" value={addForm.account_holder} onChange={(e) => setAddForm((f) => ({ ...f, account_holder: e.target.value }))} />
                      <Input placeholder="Account number" value={addForm.account_number} onChange={(e) => setAddForm((f) => ({ ...f, account_number: e.target.value }))} />
                      <Input placeholder="IFSC code" className="uppercase" maxLength={11} value={addForm.ifsc_code} onChange={(e) => setAddForm((f) => ({ ...f, ifsc_code: e.target.value.toUpperCase() }))} />
                      <Input placeholder="Bank name (optional)" value={addForm.bank_name} onChange={(e) => setAddForm((f) => ({ ...f, bank_name: e.target.value }))} />
                    </div>
                  )}
                  <Button size="sm" className="w-full" onClick={savePayoutMethod} disabled={addSaving}>
                    {addSaving ? "Saving…" : "Save"}
                  </Button>
                </div>
              )}

              {requireBank && (
                <p className="text-[10px] text-amber-500">A bank account is required for withdrawals.</p>
              )}
            </div>

            <Field label="Remarks (optional)">
              <Input value={wd.remarks} onChange={(e) => setWd((d) => ({ ...d, remarks: e.target.value }))} />
            </Field>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setWithdrawOpen(false)}>
              Cancel
            </Button>
            <Button onClick={submitWithdrawal} disabled={wdSubmitting}>
              {wdSubmitting ? "Requesting…" : "Request withdrawal"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── QR preview ────────────────────────────────────────── */}
      <Dialog open={!!qrPreview} onOpenChange={(v) => !v && setQrPreview(null)}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>Scan to pay</DialogTitle>
          </DialogHeader>
          {qrPreview && (
            <div className="flex flex-col items-center gap-2 pb-2">
              <UpiQR
                upiId={qrPreview.upiId}
                payeeName={qrPreview.payee}
                amount={qrPreview.amount}
                size={256}
              />
              <div className="text-xs text-muted-foreground">
                Open any UPI app and scan{qrPreview.amount ? ` — ₹${qrPreview.amount} pre-filled` : ""}.
              </div>
              <div className="font-mono text-[11px] text-primary">{qrPreview.upiId}</div>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────
// Sub-components
// ─────────────────────────────────────────────────────────────────

/**
 * Unified transactions feed for the wallet page.
 *
 * Merges three data sources into a single chronological list:
 *   1. WalletTransaction rows filtered to DEPOSIT / WITHDRAWAL /
 *      ADJUSTMENT — the ledger-applied cash movements.
 *   2. PENDING DepositRequest rows — not yet in the ledger but the
 *      user should see them with a "Pending" badge.
 *   3. PENDING WithdrawalRequest rows — same idea.
 *   4. REJECTED requests, so the user knows why their submission
 *      didn't go through.
 *
 * Excluded by design: trade brokerage, charges, settlement accruals,
 * P&L bookings. Those live on /ledger and /reports — operator
 * explicitly wanted this page to read as "money in / money out" only.
 *
 * UI is mobile-first: card-style rows with the type icon on the
 * left, narration + date in the middle, signed amount + status pill
 * on the right. Filter chips (All / Deposits / Withdrawals) work on
 * both layouts.
 */
type FeedKind =
  | "deposit"
  | "withdrawal"
  | "admin_add"
  | "admin_deduct"
  | "pnl_in"
  | "pnl_out"
  | "charge"
  | "shortfall";

// Which kinds ADD money (green / "in"). Single source of truth so the
// filter chips and the row colour never drift apart.
const IN_KINDS = new Set<FeedKind>(["deposit", "admin_add", "pnl_in"]);
type FeedRow = {
  id: string;
  kind: FeedKind;
  amount: number; // always positive — sign comes from `kind`
  date: string;
  status: "completed" | "pending" | "rejected";
  narration: string;
  balance_after?: number | null;
  // Raw DepositRequest / WithdrawalRequest id — set only for a still-PENDING
  // request so the row can offer a delete action (approved/rejected are final).
  reqId?: string;
};

function TransactionsFeed({
  txns,
  deposits,
  withdrawals,
}: {
  txns: any[];
  deposits: any[];
  withdrawals: any[];
}) {
  const [filter, setFilter] = useState<"all" | "in" | "out">("all");

  const rows: FeedRow[] = (() => {
    const out: FeedRow[] = [];

    // 1) Ledger txns — the FULL money story so the timeline is never
    //    ambiguous: deposits, trade P&L (incl. stop-out losses),
    //    brokerage/charges and any stop-out shortfall, each with its own
    //    IST timestamp + running balance. Hiding P&L/charges was exactly
    //    what made users think "I deposited, so how did I get liquidated" —
    //    they couldn't see the loss that hit *before* their top-up.
    for (const t of txns) {
      const tt = String(t?.transaction_type ?? "").toUpperCase();
      const amt = Number(t?.amount ?? 0);
      const absAmt = Math.abs(amt);
      let kind: FeedKind | null = null;
      let narration = String(t?.narration ?? "").trim();
      if (tt === "DEPOSIT") {
        kind = "deposit";
        if (!narration) narration = "Deposit credited";
      } else if (tt === "WITHDRAWAL") {
        kind = "withdrawal";
        if (!narration) narration = "Withdrawal debited";
      } else if (tt === "ADJUSTMENT") {
        kind = amt >= 0 ? "admin_add" : "admin_deduct";
        if (!narration) narration = amt >= 0 ? "Admin credited funds" : "Admin debited funds";
      } else if (tt === "PNL") {
        kind = amt >= 0 ? "pnl_in" : "pnl_out";
        if (!narration) narration = amt >= 0 ? "Trade profit" : "Trade loss";
      } else if (tt === "CHARGES") {
        kind = "charge";
        if (!narration) narration = "Brokerage & charges";
      } else if (tt === "SETTLEMENT_OUTSTANDING_BOOKED") {
        kind = "shortfall";
        if (!narration) narration = "Loss exceeded your balance — recorded as outstanding (owed)";
      } else if (tt === "SETTLEMENT_OUTSTANDING_RECOVERY") {
        kind = "charge";
        if (!narration) narration = "Outstanding recovered from deposit";
      }
      if (!kind) continue;
      out.push({
        id: `txn-${t.id}`,
        kind,
        amount: absAmt,
        date: t.created_at,
        status: "completed",
        narration,
        balance_after: t.balance_after != null ? Number(t.balance_after) : null,
      });
    }

    // 2) Deposit requests still awaiting approval, plus rejected ones
    //    so the user can see why. Approved requests already surface
    //    via the ledger txns above so we'd double-count if we added
    //    them here too — skip.
    for (const d of deposits) {
      const status = String(d?.status ?? "").toUpperCase();
      // Crypto invoice the user opened but never completed (backed out / expired).
      // Show it as "not completed", NEVER as pending. A completed one flips to
      // APPROVED and surfaces via the ledger above instead.
      if (status === "INITIATED" || status === "FAILED") {
        out.push({
          id: `dep-${d.id}`,
          kind: "deposit",
          amount: Math.abs(Number(d.amount ?? 0)),
          date: d.created_at,
          status: "rejected",
          narration: "Payment not completed — crypto payment wasn't finished",
          balance_after: null,
        });
        continue;
      }
      if (status !== "PENDING" && status !== "REJECTED") continue;
      out.push({
        id: `dep-${d.id}`,
        kind: "deposit",
        amount: Math.abs(Number(d.amount ?? 0)),
        date: d.created_at,
        status: status === "PENDING" ? "pending" : "rejected",
        narration:
          status === "REJECTED"
            ? `Deposit rejected${d.admin_remark ? ` — ${d.admin_remark}` : ""}`
            : `Deposit request · ${d.payment_mode ?? "manual"}`,
        balance_after: null,
        reqId: status === "PENDING" ? String(d.id) : undefined,
      });
    }

    // 3) Same for withdrawals.
    for (const w of withdrawals) {
      const status = String(w?.status ?? "").toUpperCase();
      if (status !== "PENDING" && status !== "REJECTED") continue;
      out.push({
        id: `wd-${w.id}`,
        kind: "withdrawal",
        amount: Math.abs(Number(w.amount ?? 0)),
        date: w.created_at,
        status: status === "PENDING" ? "pending" : "rejected",
        narration:
          status === "REJECTED"
            ? `Withdrawal rejected${w.admin_remark ? ` — ${w.admin_remark}` : ""}`
            : `Withdrawal request · ${w.mode ?? "bank"}`,
        balance_after: null,
        reqId: status === "PENDING" ? String(w.id) : undefined,
      });
    }

    // Chronological, newest first.
    out.sort((a, b) => new Date(b.date).getTime() - new Date(a.date).getTime());
    return out;
  })();

  const filtered = rows.filter((r) => {
    if (filter === "all") return true;
    if (filter === "in") return IN_KINDS.has(r.kind);
    return !IN_KINDS.has(r.kind);
  });

  return (
    <section className="rounded-2xl border border-border bg-card shadow-sm">
      {/* Header + filter pills */}
      <div className="flex flex-col gap-3 border-b border-border p-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-base font-semibold">Transactions</h2>
          <p className="mt-0.5 text-[11px] text-muted-foreground">
            Every deposit, trade P&amp;L, brokerage, withdrawal and adjustment — in order, with time.
          </p>
        </div>
        <div className="flex gap-1.5">
          {(["all", "in", "out"] as const).map((k) => (
            <button
              key={k}
              type="button"
              onClick={() => setFilter(k)}
              className={cn(
                "h-8 rounded-full border px-3 text-[11px] font-semibold uppercase tracking-wider transition-colors",
                filter === k
                  ? "border-primary bg-primary text-primary-foreground"
                  : "border-border bg-card text-muted-foreground hover:text-foreground",
              )}
            >
              {k === "all" ? "All" : k === "in" ? "Money in" : "Money out"}
            </button>
          ))}
        </div>
      </div>

      {/* Rows */}
      {filtered.length === 0 ? (
        <div className="px-4 py-12 text-center">
          <div className="mx-auto grid size-12 place-items-center rounded-full bg-muted">
            <CreditCard className="size-5 text-muted-foreground" />
          </div>
          <div className="mt-3 text-sm font-medium">No transactions yet</div>
          <div className="mt-1 text-[12px] text-muted-foreground">
            {filter === "in"
              ? "Deposits and admin credits will show here."
              : filter === "out"
                ? "Withdrawals and admin debits will show here."
                : "Use the Add funds / Withdraw buttons above to get started."}
          </div>
        </div>
      ) : (
        <ul className="divide-y divide-border">
          {filtered.map((r) => (
            <FeedRowItem key={r.id} row={r} />
          ))}
        </ul>
      )}
    </section>
  );
}

function FeedRowItem({ row }: { row: FeedRow }) {
  const qc = useQueryClient();
  const [deleting, setDeleting] = useState(false);
  const isIn = IN_KINDS.has(row.kind);
  const Icon = isIn ? ArrowDownToLine : ArrowUpToLine;

  // Only a still-PENDING request carries `reqId` and can be withdrawn by the
  // user; once approved/rejected the backend rejects the delete.
  async function onDelete() {
    if (!row.reqId) return;
    if (typeof window !== "undefined" && !window.confirm("Delete this pending request?")) return;
    setDeleting(true);
    try {
      if (row.kind === "deposit") await WalletAPI.deleteDeposit(row.reqId);
      else await WalletAPI.deleteWithdrawal(row.reqId);
      toast.success("Request deleted");
      qc.invalidateQueries({ queryKey: ["my-deposits"] });
      qc.invalidateQueries({ queryKey: ["my-withdrawals"] });
    } catch (e: any) {
      toast.error(e?.message || "Couldn't delete the request");
    } finally {
      setDeleting(false);
    }
  }

  const iconBg = isIn
    ? "bg-emerald-500/15 text-emerald-500 ring-emerald-500/20"
    : "bg-rose-500/15 text-rose-500 ring-rose-500/20";

  const amountText = `${isIn ? "+" : "−"} ${formatINR(row.amount)}`;
  const amountColor = isIn ? "text-emerald-500" : "text-rose-500";

  const titleByKind: Record<FeedKind, string> = {
    deposit: "Deposit",
    withdrawal: "Withdrawal",
    admin_add: "Admin credit",
    admin_deduct: "Admin debit",
    pnl_in: "Trade profit",
    pnl_out: "Trade loss",
    charge: "Brokerage & charges",
    shortfall: "Stop-out shortfall",
  };

  const statusPill = (() => {
    if (row.status === "pending") {
      return (
        <span className="inline-flex items-center gap-1 rounded-full bg-amber-500/15 px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-amber-500 ring-1 ring-inset ring-amber-500/30">
          <Clock className="size-2.5" /> Pending
        </span>
      );
    }
    if (row.status === "rejected") {
      return (
        <span className="inline-flex items-center gap-1 rounded-full bg-rose-500/15 px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-rose-500 ring-1 ring-inset ring-rose-500/30">
          <XCircle className="size-2.5" /> Rejected
        </span>
      );
    }
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/15 px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-emerald-500 ring-1 ring-inset ring-emerald-500/30">
        <CheckCircle2 className="size-2.5" /> Completed
      </span>
    );
  })();

  return (
    <li className="flex items-start gap-3 px-4 py-3 transition-colors hover:bg-muted/30 sm:items-center">
      <div className={cn("grid size-10 shrink-0 place-items-center rounded-full ring-1 ring-inset", iconBg)}>
        <Icon className="size-4" />
      </div>

      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-sm font-semibold">{titleByKind[row.kind]}</span>
          {statusPill}
        </div>
        <div className="mt-0.5 truncate text-[11px] text-muted-foreground">{row.narration}</div>
        <div className="mt-0.5 text-[10px] text-muted-foreground">
          {new Date(row.date).toLocaleString("en-IN", {
            day: "2-digit",
            month: "short",
            year: "numeric",
            hour: "2-digit",
            minute: "2-digit",
            // Force IST so the timestamp never shifts with the device
            // timezone — the whole point is an unambiguous timeline.
            timeZone: "Asia/Kolkata",
          })}{" "}
          IST
        </div>
      </div>

      <div className="shrink-0 text-right">
        <div className={cn("font-tabular text-sm font-bold tabular-nums", amountColor)}>{amountText}</div>
        {row.balance_after != null && (
          <div className="mt-0.5 text-[10px] text-muted-foreground">
            Bal {formatINR(row.balance_after)}
          </div>
        )}
        {row.reqId && (
          <button
            type="button"
            onClick={onDelete}
            disabled={deleting}
            className="mt-1 inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] font-medium text-rose-500 transition-colors hover:bg-rose-500/10 disabled:opacity-50"
          >
            {deleting ? <Loader2 className="size-3 animate-spin" /> : <Trash2 className="size-3" />}
            Delete
          </button>
        )}
      </div>
    </li>
  );
}

function HeroStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="px-2">
      <div className="text-[10px] uppercase tracking-wider opacity-75">{label}</div>
      <div className="mt-0.5 font-tabular text-sm font-semibold">{value}</div>
    </div>
  );
}

function StatTile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-xl border border-border bg-card p-3">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className="mt-1 font-tabular text-lg font-semibold">{value}</div>
      {hint && <div className="mt-0.5 text-[10px] text-muted-foreground">{hint}</div>}
    </div>
  );
}

function PanelCard({
  title,
  subtitle,
  action,
  children,
  className,
}: {
  title: string;
  subtitle?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("rounded-xl border border-border bg-card p-4", className)}>
      <div className="mb-3 flex items-start justify-between">
        <div>
          <h3 className="text-sm font-semibold">{title}</h3>
          {subtitle && <p className="text-[11px] text-muted-foreground">{subtitle}</p>}
        </div>
        {action}
      </div>
      {children}
    </div>
  );
}

function EmptyState({ message, hint }: { message: string; hint?: string }) {
  return (
    <div className="py-8 text-center">
      <div className="text-sm text-muted-foreground">{message}</div>
      {hint && <div className="mt-1 text-[11px] text-muted-foreground">{hint}</div>}
    </div>
  );
}

function SectionLabel({ num, title }: { num: number; title: string }) {
  return (
    <div className="flex items-center gap-2">
      <span className="grid size-5 place-items-center rounded-full bg-primary text-[10px] font-bold text-primary-foreground">
        {num}
      </span>
      <span className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">{title}</span>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <Label className="text-xs capitalize">{label}</Label>
      {children}
    </div>
  );
}

function CopyRow({
  label,
  value,
  onCopy,
  highlight,
}: {
  label: string;
  value: string;
  onCopy: (text: string, label: string) => void;
  highlight?: boolean;
}) {
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-muted-foreground">{label}:</span>
      <span className="flex items-center gap-1.5">
        <span className={cn("font-mono", highlight && "text-primary font-semibold")}>{value}</span>
        <button
          type="button"
          onClick={() => onCopy(value, label)}
          className="text-muted-foreground hover:text-foreground"
          aria-label={`Copy ${label}`}
        >
          <Copy className="size-3" />
        </button>
      </span>
    </div>
  );
}

function RequestList({
  title,
  rows,
  kind,
}: {
  title: string;
  rows: any[] | undefined;
  kind: "deposit" | "withdrawal";
}) {
  return (
    <PanelCard title={title} subtitle={`${(rows ?? []).length} total`}>
      {(rows ?? []).length === 0 ? (
        <EmptyState message={`No ${kind} requests yet`} />
      ) : (
        <ul className="divide-y divide-border">
          {rows!.slice(0, 6).map((r: any) => {
            const status = r.status as string;
            const StatusIcon =
              status === "APPROVED" || status === "COMPLETED"
                ? CheckCircle2
                : status === "REJECTED"
                  ? XCircle
                  : Clock;
            const tone =
              status === "APPROVED" || status === "COMPLETED"
                ? "text-buy"
                : status === "REJECTED"
                  ? "text-sell"
                  : "text-amber-600 dark:text-amber-400";
            return (
              <li key={r.id} className="flex items-center justify-between py-2.5 text-xs">
                <div className="flex items-center gap-2.5">
                  <StatusIcon className={cn("size-4", tone)} />
                  <div>
                    <div className="font-tabular font-semibold">{formatINR(r.amount)}</div>
                    <div className="text-[10px] text-muted-foreground">
                      {new Date(r.created_at).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })}
                    </div>
                  </div>
                </div>
                <span className={cn("rounded-full bg-muted px-2 py-0.5 text-[10px] font-semibold", tone)}>
                  {status}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </PanelCard>
  );
}

// Canvas-based JPEG re-encode for payment screenshots. Caps the long edge at
// 1600 px (a phone screenshot is usually 1080–1290 px wide, so this is a
// no-op for native res but keeps a stray 4 K screenshot from going up at
// 8 MB). q=0.88 keeps UPI ref numbers / UTR text legible — visually
// indistinguishable from the original at typical viewing zoom.
async function compressImage(file: File): Promise<File> {
  // Already small or not a raster format → upload as-is (PDFs, tiny PNGs).
  if (file.size < 400 * 1024 || !file.type.startsWith("image/")) return file;

  const dataUrl = await new Promise<string>((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(fr.result as string);
    fr.onerror = () => reject(new Error("read failed"));
    fr.readAsDataURL(file);
  });
  const img = await new Promise<HTMLImageElement>((resolve, reject) => {
    const im = new Image();
    im.onload = () => resolve(im);
    im.onerror = () => reject(new Error("decode failed"));
    im.src = dataUrl;
  });

  const MAX_EDGE = 1600;
  const scale = Math.min(1, MAX_EDGE / Math.max(img.naturalWidth, img.naturalHeight));
  const w = Math.round(img.naturalWidth * scale);
  const h = Math.round(img.naturalHeight * scale);

  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  if (!ctx) return file;
  ctx.drawImage(img, 0, 0, w, h);

  const blob: Blob | null = await new Promise((resolve) =>
    canvas.toBlob((b) => resolve(b), "image/jpeg", 0.88),
  );
  if (!blob) return file;
  // If compression somehow ballooned the size (rare — only happens on
  // already-JPEG with very low entropy), fall back to the original.
  if (blob.size >= file.size) return file;

  const baseName = (file.name.replace(/\.[^.]+$/, "") || "screenshot") + ".jpg";
  return new File([blob], baseName, { type: "image/jpeg", lastModified: Date.now() });
}

/**
 * Compact Support pill rendered in the Add-funds dialog header.
 * WhatsApp is the preferred channel (deposit issues are usually back-
 * and-forth — UTR not visible, screenshot blurry, etc — so chat works
 * better than email). Falls back to email when no WhatsApp is
 * configured; renders nothing when neither is set.
 */
function DepositSupportButton() {
  const { data: support } = useSupportContacts();
  const waUrl = buildWhatsappUrl(
    support?.whatsapp,
    "Hi, I need help adding funds to my MarginWealth account",
  );
  const mailUrl = buildMailtoUrl(support?.email, {
    subject: "MarginWealth deposit help",
  });
  if (!waUrl && !mailUrl) return null;
  const target = waUrl ?? mailUrl!;
  const isWa = !!waUrl;
  return (
    <a
      href={target}
      target={isWa ? "_blank" : undefined}
      rel={isWa ? "noopener noreferrer" : undefined}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-semibold transition-colors",
        isWa
          ? "border-[#25D366]/30 bg-[#25D366]/10 text-[#25D366] hover:bg-[#25D366]/20"
          : "border-primary/30 bg-primary/10 text-primary hover:bg-primary/20",
      )}
    >
      {isWa ? <MessageCircle className="size-3" /> : <Mail className="size-3" />}
      Support
    </a>
  );
}
