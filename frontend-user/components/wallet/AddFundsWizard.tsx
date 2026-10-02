"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  ChevronLeft,
  ChevronRight,
  Copy,
  Loader2,
  ShieldCheck,
  Building2,
  Check,
  Upload,
  MessageCircle,
  Camera,
  Bitcoin,
  Gift,
} from "lucide-react";
import { QRCodeSVG } from "qrcode.react";
import { WalletAPI, BonusesUserAPI } from "@/lib/api";
import { API_URL } from "@/lib/constants";
import { useAuthStore } from "@/stores/authStore";
import { DemoUpgradeDialog } from "@/components/wallet/DemoUpgradeDialog";
import { UpiQR } from "@/components/common/UpiQR";
import { cn, formatINR } from "@/lib/utils";
import { buildWhatsappUrl, useSupportContacts } from "@/lib/useSupport";

// ── Brand marks (inline SVG / badges — no asset bundling). Recognisable
//    by colour + glyph, sized for a clean payment row. ──────────────────
function GPayMark({ size = 36 }: { size?: number }) {
  return (
    <span
      className="grid place-items-center rounded-full bg-white shadow-sm ring-1 ring-black/5"
      style={{ width: size, height: size }}
    >
      <svg viewBox="0 0 24 24" style={{ width: size * 0.56, height: size * 0.56 }} aria-label="Google Pay">
        <path fill="#4285F4" d="M22 12.23c0-.71-.06-1.4-.18-2.06H12v3.9h5.6a4.8 4.8 0 0 1-2.08 3.15v2.6h3.36C20.84 18.1 22 15.43 22 12.23z" />
        <path fill="#34A853" d="M12 22c2.7 0 4.97-.9 6.62-2.43l-3.36-2.6c-.93.62-2.13.99-3.26.99-2.5 0-4.63-1.69-5.38-3.96H3.15v2.69A10 10 0 0 0 12 22z" />
        <path fill="#FBBC05" d="M6.62 13.99a6 6 0 0 1 0-3.82V7.48H3.15a10 10 0 0 0 0 9.04l3.47-2.53z" />
        <path fill="#EA4335" d="M12 6.21c1.47 0 2.79.51 3.83 1.5l2.86-2.86C16.96 3.2 14.7 2.3 12 2.3a10 10 0 0 0-8.85 5.18l3.47 2.69C7.37 7.9 9.5 6.21 12 6.21z" />
      </svg>
    </span>
  );
}

function CircleMark({
  bg,
  label,
  fg = "#fff",
  size = 36,
  fontSize,
}: {
  bg: string;
  label: string;
  fg?: string;
  size?: number;
  fontSize?: number;
}) {
  return (
    <span
      className="grid place-items-center rounded-full font-extrabold shadow-sm ring-1 ring-black/5"
      style={{ width: size, height: size, background: bg, color: fg, fontSize: fontSize ?? size * 0.3 }}
    >
      {label}
    </span>
  );
}

const PhonePeMark = ({ size = 36 }: { size?: number }) => (
  <CircleMark bg="#5f259f" label="Pe" size={size} />
);
const PaytmMark = ({ size = 36 }: { size?: number }) => (
  <CircleMark bg="#ffffff" fg="#00b9f1" label="P" size={size} fontSize={size * 0.46} />
);
const BhimMark = ({ size = 36 }: { size?: number }) => (
  <CircleMark bg="linear-gradient(135deg,#ee7d2f,#0f9d58)" label="₹" size={size} fontSize={size * 0.46} />
);
function SbiLogo({ className }: { className?: string }) {
  // Official-style SBI mark — light-blue keyhole circle + navy "SBI".
  return (
    <svg viewBox="0 0 150 60" className={className} role="img" aria-label="SBI">
      <circle cx="29" cy="30" r="26" fill="#1aa7e0" />
      <circle cx="29" cy="21" r="6.5" fill="#ffffff" />
      <rect x="27.2" y="21" width="3.6" height="25" rx="1.8" fill="#ffffff" />
      <text x="62" y="46" fontFamily="Arial, Helvetica, sans-serif" fontSize="42" fontWeight="800" fill="#23379b">
        SBI
      </text>
    </svg>
  );
}

function UpiLogo({ className }: { className?: string }) {
  // Official-style UPI mark — grey "UPI" wordmark + the orange/green
  // (tricolour) arrow chevron, with the "UNIFIED PAYMENTS INTERFACE" subline.
  return (
    <svg viewBox="0 0 170 62" className={className} role="img" aria-label="UPI — Unified Payments Interface">
      <text x="0" y="42" fontFamily="Arial, Helvetica, sans-serif" fontSize="46" fontWeight="800" fill="#75767a">
        UPI
      </text>
      <polygon points="112,10 143,31 112,31" fill="#f6821f" />
      <polygon points="112,31 143,31 112,52" fill="#3fae49" />
      <text x="1" y="59" fontFamily="Arial, Helvetica, sans-serif" fontSize="8.5" letterSpacing="1.1" fill="#75767a">
        UNIFIED PAYMENTS INTERFACE
      </text>
    </svg>
  );
}

function PoweredFooter() {
  return (
    <div className="mt-6 flex flex-col items-center gap-2.5 pb-4 text-center">
      <span className="text-[11px] text-muted-foreground">Secured Payments Powered by</span>
      <div className="flex items-center gap-4">
        <UpiLogo className="h-7 w-auto" />
        <span className="h-7 w-px bg-border" />
        <SbiLogo className="h-7 w-auto" />
      </div>
    </div>
  );
}

function Stepper({ active }: { active: "pay" | "upload" }) {
  return (
    <div className="mb-5 flex items-center justify-center gap-2 text-xs font-medium">
      <span
        className={cn(
          "inline-flex items-center gap-1 rounded-full px-3 py-1",
          active === "pay" ? "bg-primary/15 text-primary" : "bg-muted/40 text-muted-foreground",
        )}
      >
        <Camera className="size-3.5" /> Pay &amp; Screenshot
      </span>
      <ChevronRight className="size-3.5 text-muted-foreground" />
      <span
        className={cn(
          "inline-flex items-center gap-1 rounded-full px-3 py-1",
          active === "upload" ? "bg-primary/15 text-primary" : "bg-muted/40 text-muted-foreground",
        )}
      >
        <Upload className="size-3.5" /> Upload
      </span>
    </div>
  );
}

const QUICK = [500, 1000, 5000];

type Bank = {
  id: string;
  bank_name?: string;
  account_holder?: string;
  account_number?: string;
  ifsc_code?: string;
  upi_id?: string | null;
  qr_code_url?: string | null;
  is_default?: boolean;
};

export function AddFundsWizard({
  open,
  onClose,
  companyBanks,
  payeeName,
  onSuccess,
}: {
  open: boolean;
  onClose: () => void;
  companyBanks: Bank[];
  payeeName?: string;
  onSuccess?: () => void;
}) {
  const user = useAuthStore((s) => s.user);
  const [step, setStep] = useState<1 | 2 | 3 | 4>(1);
  const [amtStr, setAmtStr] = useState("");
  const [screenshotUrl, setScreenshotUrl] = useState("");
  const [uploading, setUploading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  // Stable idempotency key for this deposit — reused on retry, cleared after
  // success — so a double click / lost-response retry can't double-submit.
  const idemRef = useRef<string>("");
  // Which company bank the user is paying to. Admin can configure MANY
  // banks; the user picks the one they actually transfer to so the deposit
  // request references the correct account. Defaults to the default/first.
  const [selectedBankId, setSelectedBankId] = useState<string | null>(null);
  // Crypto: only offered when the user's admin enabled it (null → hidden, so
  // the INR flow stays exactly as-is for everyone else).
  const [chooseMethod, setChooseMethod] = useState(false);
  const [cryptoMode, setCryptoMode] = useState(false);
  const [cryptoTx, setCryptoTx] = useState("");
  // Divinepay UPI gateway (auto-crediting). When a gateway order exists we swap
  // the amount step for the UTR-verify step; the amount step keeps the exact
  // same "Paying …" UI so nothing visual changes vs the manual flow.
  const [gwOrder, setGwOrder] = useState<{ order_id: string; payment_url: string; amount: string } | null>(null);
  const [gwUtr, setGwUtr] = useState("");
  const [gwBusy, setGwBusy] = useState(false);
  const { data: cryptoCfg } = useQuery({
    queryKey: ["wallet-crypto-config"],
    queryFn: () => WalletAPI.cryptoConfig(),
    staleTime: 60_000,
  });
  const cryptoManual = cryptoCfg?.manual as
    | { wallet_address: string; network?: string; asset?: string }
    | undefined;
  const cryptoAvailable = !!cryptoCfg && (!!cryptoManual || !!cryptoCfg?.gateway);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const support = useSupportContacts();
  const { data: rules } = useQuery({
    queryKey: ["wd-rules", "deposit"],
    queryFn: () => WalletAPI.wdRules(),
    staleTime: 5 * 60_000,
    enabled: open,
  });
  const minAmount = Number(rules?.deposit?.min_amount ?? 0) || 500;

  // Deposit mode — GATEWAY (auto-crediting Divinepay UPI) when the super-admin
  // enabled it for this pool, else MANUAL (the bank-QR flow below).
  const { data: depMode } = useQuery({
    queryKey: ["deposit-mode"],
    queryFn: () => WalletAPI.depositMode(),
    staleTime: 60_000,
    enabled: open,
  });
  const isGateway = depMode?.mode === "GATEWAY";
  // The online gateway uses its OWN ₹100 floor, not the admin's manual-deposit
  // rule (which can be higher, e.g. ₹1000 for bank transfers). Manual keeps
  // using the admin rule min.
  const gatewayMin = Number(depMode?.min_amount ?? 100) || 100;
  const effMin = isGateway ? gatewayMin : minAmount;

  // Reset whenever the wizard opens.
  useEffect(() => {
    if (open) {
      setStep(1);
      setAmtStr("");
      setScreenshotUrl("");
      setSelectedBankId(null);
      setGwOrder(null);
      setGwUtr("");
    }
  }, [open]);

  // Lock background scroll while the full-screen flow is up.
  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open]);

  const bank = useMemo<Bank | undefined>(() => {
    if (!companyBanks?.length) return undefined;
    return (
      companyBanks.find((b) => b.id === selectedBankId) ??
      companyBanks.find((b) => b.is_default) ??
      companyBanks[0]
    );
  }, [companyBanks, selectedBankId]);

  const payee = payeeName || bank?.account_holder || "Merchant";
  const amount = Number(amtStr) || 0;
  // Live ₹→crypto quote so the manual screen shows the exact crypto amount to send.
  const cryptoAsset = cryptoManual?.asset || "";
  const { data: cryptoQuote } = useQuery({
    queryKey: ["wallet-crypto-quote", amount, cryptoAsset],
    queryFn: () => WalletAPI.cryptoQuote(amount, cryptoAsset),
    enabled: cryptoMode && !!cryptoManual && !!cryptoAsset && amount > 0,
    staleTime: 30_000,
  });

  // Debounced bonus-eligibility preview (Bonus Management). Returns null /
  // 503s when the feature is off, so the pill simply never renders then.
  const [debAmt, setDebAmt] = useState(0);
  useEffect(() => {
    const t = setTimeout(() => setDebAmt(amount), 400);
    return () => clearTimeout(t);
  }, [amount]);
  const { data: bonusPreview } = useQuery({
    queryKey: ["bonus-eligible", debAmt],
    queryFn: () => BonusesUserAPI.eligible(debAmt),
    enabled: debAmt >= minAmount && debAmt > 0,
    retry: false,
  });
  const waUrl = buildWhatsappUrl(
    support.data?.whatsapp,
    `Hi, I need help adding funds${amount > 0 ? ` (₹${amount})` : ""}.`,
  );

  if (!open) return null;

  // Demo users cannot deposit — block at wizard level as a safety net
  if (user?.is_demo) {
    return <DemoUpgradeDialog open={open} onClose={onClose} />;
  }

  function bump(n: number) {
    setAmtStr(String((Number(amtStr) || 0) + n));
  }

  // "Pay" just advances to the QR / UPI-ID step. We deliberately do NOT fire
  // a upi:// intent — it pops the OS app-chooser (PhonePe / GPay …) OVER the
  // QR screen, which the operator asked to remove. The user pays by scanning
  // the QR or copying the UPI ID, then uploads the screenshot.
  function payViaUpiApp() {
    setStep(3);
  }

  // ── Divinepay UPI gateway ───────────────────────────────────────────
  async function gatewayPay() {
    if (amount < effMin) return toast.error(`Minimum deposit is ${formatINR(effMin)}`);
    if (gwBusy) return;
    setGwBusy(true);
    try {
      const res = await WalletAPI.createGatewayDeposit(amount);
      setGwOrder({ order_id: res.order_id, payment_url: res.payment_url, amount: res.amount });
      window.open(res.payment_url, "_blank", "noopener,noreferrer");
    } catch (e: any) {
      toast.error(e?.message || "Could not start payment");
    } finally {
      setGwBusy(false);
    }
  }

  async function gatewayVerify() {
    if (!gwOrder) return;
    if (!/^\d{12}$/.test(gwUtr.trim())) return toast.error("Enter the 12-digit UPI reference (UTR)");
    if (gwBusy) return;
    setGwBusy(true);
    try {
      const res = await WalletAPI.submitGatewayUtr(gwOrder.order_id, gwUtr.trim());
      if (res.status === "success" || res.credited || res.already_credited) {
        toast.success("Payment confirmed — wallet credited");
        onSuccess?.();
        onClose();
      } else {
        toast.message("Not confirmed yet", {
          description: "We haven't received your payment from the bank yet — it credits automatically once confirmed. Try Verify again in a moment.",
        });
      }
    } catch (e: any) {
      toast.error(e?.message || "Verification failed");
    } finally {
      setGwBusy(false);
    }
  }

  async function onPickFile(file: File) {
    if (!file.type.startsWith("image/")) return toast.error("Pick an image file");
    if (file.size > 10 * 1024 * 1024) return toast.error("File too large (max 10 MB)");
    setUploading(true);
    try {
      const r = await WalletAPI.uploadScreenshot(file);
      setScreenshotUrl(r.url);
      toast.success("Screenshot added");
    } catch (e: any) {
      toast.error(e?.message || "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  async function submit() {
    if (amount < minAmount) return toast.error(`Minimum deposit is ${formatINR(minAmount)}`);
    if (!bank?.id) return toast.error("No payment account configured");
    if (!screenshotUrl) return toast.error("Upload your payment screenshot");
    if (submitting) return;
    if (!idemRef.current) {
      idemRef.current =
        typeof crypto !== "undefined" && crypto.randomUUID
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }
    setSubmitting(true);
    try {
      await WalletAPI.createDeposit({
        amount,
        payment_mode: "UPI",
        screenshot_url: screenshotUrl,
        bank_account_id: bank.id,
        idempotency_key: idemRef.current,
      } as any);
      toast.success("Deposit submitted — awaiting admin approval");
      idemRef.current = "";
      onSuccess?.();
      onClose();
    } catch (e: any) {
      toast.error(e?.message || "Could not submit");
    } finally {
      setSubmitting(false);
    }
  }

  async function submitCrypto() {
    if (amount < minAmount) return toast.error(`Minimum deposit is ${formatINR(minAmount)}`);
    if (!cryptoTx.trim() && !screenshotUrl)
      return toast.error("Enter the transaction hash or upload a screenshot");
    if (submitting) return;
    if (!idemRef.current) {
      idemRef.current =
        typeof crypto !== "undefined" && crypto.randomUUID
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }
    setSubmitting(true);
    try {
      await WalletAPI.createDeposit({
        amount,
        payment_mode: "CRYPTO",
        screenshot_url: screenshotUrl || undefined,
        crypto_asset: cryptoManual?.asset,
        crypto_network: cryptoManual?.network,
        crypto_address: cryptoManual?.wallet_address,
        crypto_tx_hash: cryptoTx.trim(),
        idempotency_key: idemRef.current,
      } as any);
      toast.success("Crypto deposit submitted — awaiting admin approval");
      idemRef.current = "";
      onSuccess?.();
      onClose();
    } catch (e: any) {
      toast.error(e?.message || "Could not submit");
    } finally {
      setSubmitting(false);
    }
  }

  // Crypto card tapped: oxapay gateway → create invoice + redirect to the
  // hosted checkout; manual → show the address/QR screen.
  async function startCrypto() {
    setChooseMethod(false);
    if (cryptoCfg?.gateway) {
      if (amount < minAmount) return toast.error(`Minimum deposit is ${formatINR(minAmount)}`);
      if (submitting) return;
      setSubmitting(true);
      try {
        const r = await WalletAPI.createOxapayDeposit({ amount });
        if (r?.payment_url) {
          window.location.href = r.payment_url;
          return;
        }
        toast.error("Could not start crypto payment");
      } catch (e: any) {
        toast.error(e?.message || "Could not start crypto payment");
      } finally {
        setSubmitting(false);
      }
    } else {
      setCryptoMode(true);
    }
  }

  function copy(text: string, label: string) {
    navigator.clipboard.writeText(text).then(
      () => toast.success(`${label} copied`),
      () => toast.error("Copy failed"),
    );
  }

  const HelpPill = waUrl ? (
    <a
      href={waUrl}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1.5 rounded-full border border-border bg-muted/40 px-3 py-1.5 text-xs font-medium text-foreground transition hover:bg-muted"
    >
      <MessageCircle className="size-4 text-emerald-500" /> Help with Add Funds?
    </a>
  ) : (
    <span />
  );

  const Header = ({ onBack }: { onBack: () => void }) => (
    <div className="flex items-center justify-between px-4 py-3">
      <button
        onClick={onBack}
        className="grid size-9 place-items-center rounded-full text-muted-foreground transition hover:bg-muted"
        aria-label="Back"
      >
        <ChevronLeft className="size-5" />
      </button>
      {HelpPill}
    </div>
  );

  return (
    <div className="fixed inset-0 z-[60] flex justify-center overflow-y-auto bg-background">
      <div className="relative flex min-h-full w-full max-w-md flex-col">
        {/* ─────────────── STEP 1 + 2: Amount (step 2 dims it) ─────────────── */}
        {(step === 1 || step === 2) && !gwOrder && (
          <div className={cn("flex flex-1 flex-col", step === 2 && "pointer-events-none opacity-40")}>
            <Header onBack={onClose} />
            <div className="flex flex-1 flex-col px-5 pt-6">
              <div className="flex flex-col items-center">
                <div className="grid size-24 place-items-center rounded-full bg-muted text-3xl font-bold text-foreground/70">
                  {payee.charAt(0).toUpperCase()}
                </div>
                <div className="mt-3 inline-flex items-center gap-1.5 text-base font-semibold">
                  <ShieldCheck className="size-4 text-primary" /> Paying {payee}
                </div>
                <div className="mt-5 flex items-center justify-center gap-1">
                  <span className="text-4xl font-semibold text-muted-foreground">₹</span>
                  <input
                    autoFocus
                    value={amtStr}
                    onChange={(e) => setAmtStr(e.target.value.replace(/[^0-9]/g, ""))}
                    inputMode="numeric"
                    placeholder="0"
                    className="w-[6ch] bg-transparent text-center text-6xl font-bold tabular-nums outline-none placeholder:text-muted-foreground/40"
                  />
                </div>
                <div className="mt-4 flex gap-2">
                  {QUICK.map((q) => (
                    <button
                      key={q}
                      onClick={() => bump(q)}
                      className="rounded-lg border border-border bg-muted/40 px-4 py-2 text-sm font-medium transition hover:bg-muted"
                    >
                      +{q}
                    </button>
                  ))}
                </div>
              </div>

              {Number(bonusPreview?.bonus_amount ?? 0) > 0 && (
                <div className="mt-4 flex items-center gap-1.5 rounded-lg bg-primary/10 px-3 py-2 text-xs font-medium text-primary">
                  <Gift className="size-3.5 shrink-0" />
                  +{formatINR(bonusPreview!.bonus_amount)} bonus credit on approval
                  {bonusPreview?.template_name ? ` · ${bonusPreview.template_name}` : ""}
                </div>
              )}
              {bonusPreview?.below_minimum && bonusPreview.minimum_required && (
                <div className="mt-2 text-[11px] text-muted-foreground">
                  Deposit {formatINR(bonusPreview.minimum_required)}+ to earn a bonus.
                </div>
              )}

              <button
                onClick={isGateway ? gatewayPay : () => (cryptoAvailable ? setChooseMethod(true) : setStep(2))}
                disabled={amount < effMin || gwBusy}
                className="mt-7 flex h-12 w-full items-center justify-center gap-1.5 rounded-xl bg-primary text-sm font-semibold text-primary-foreground shadow transition hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {gwBusy ? <Loader2 className="size-4 animate-spin" /> : null}
                Next <ChevronRight className="size-4" />
              </button>

              <div className="mt-5 rounded-xl bg-muted/40 p-4">
                <div className="mb-1.5 text-sm font-semibold">Add Funds rules</div>
                <div className="text-xs text-muted-foreground">
                  Minimum amount: <span className="font-semibold text-foreground">{formatINR(effMin)}</span>
                </div>
                <ol className="mt-2 space-y-1 text-xs text-muted-foreground">
                  <li>1. Minimum deposit amount is {effMin}</li>
                  {isGateway ? (
                    <li>2. Pay on the UPI page, then enter the 12-digit UTR to credit instantly</li>
                  ) : (
                    <li>2. Upload a clear screenshot including UTR no, date and time</li>
                  )}
                </ol>
              </div>

              <PoweredFooter />
            </div>
          </div>
        )}

        {/* ─────────────── Divinepay UPI — verify UTR after paying ─────────────── */}
        {gwOrder && (
          <div className="flex flex-1 flex-col">
            <Header onBack={() => setGwOrder(null)} />
            <div className="flex flex-1 flex-col px-5 pt-6">
              <div className="flex flex-col items-center">
                <div className="grid size-24 place-items-center rounded-full bg-muted text-3xl font-bold text-foreground/70">
                  {payee.charAt(0).toUpperCase()}
                </div>
                <div className="mt-3 inline-flex items-center gap-1.5 text-base font-semibold">
                  <ShieldCheck className="size-4 text-primary" /> Paying {payee}
                </div>
                <div className="mt-4 text-4xl font-bold tabular-nums">{formatINR(Number(gwOrder.amount))}</div>
                <div className="mt-1 font-mono text-[11px] text-muted-foreground">Order {gwOrder.order_id}</div>
              </div>

              <button
                onClick={() => window.open(gwOrder.payment_url, "_blank", "noopener,noreferrer")}
                className="mt-6 flex h-11 w-full items-center justify-center gap-2 rounded-xl border border-primary/40 bg-primary/5 text-sm font-semibold text-primary transition hover:bg-primary/10"
              >
                <ChevronRight className="size-4" /> Reopen payment page
              </button>

              <div className="mt-5">
                <div className="mb-1.5 text-sm font-semibold">Enter 12-digit UPI reference (UTR)</div>
                <input
                  inputMode="numeric"
                  maxLength={12}
                  value={gwUtr}
                  onChange={(e) => setGwUtr(e.target.value.replace(/[^0-9]/g, ""))}
                  placeholder="123456789012"
                  className="h-12 w-full rounded-xl border border-border bg-muted/40 px-3 font-mono text-lg tracking-widest outline-none focus:border-primary"
                />
                <div className="mt-1.5 text-[11px] text-muted-foreground">
                  Find the 12-digit UTR / reference number in your UPI app's payment receipt.
                </div>
              </div>

              <button
                onClick={gatewayVerify}
                disabled={gwBusy || gwUtr.trim().length !== 12}
                className="mt-5 flex h-12 w-full items-center justify-center gap-1.5 rounded-xl bg-primary text-sm font-semibold text-primary-foreground shadow transition hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {gwBusy ? <Loader2 className="size-4 animate-spin" /> : <ShieldCheck className="size-4" />}
                {gwBusy ? "Verifying…" : "Verify & credit"}
              </button>
              <div className="mt-2 text-center text-[11px] text-muted-foreground">
                Already paid? It also credits automatically within a minute.
              </div>

              <PoweredFooter />
            </div>
          </div>
        )}

        {/* ─────────────── Payment method choice (only when crypto is on) ─────────────── */}
        {chooseMethod && (
          <div className="absolute inset-0 z-20 flex flex-col justify-end">
            <button className="absolute inset-0 bg-black/40" onClick={() => setChooseMethod(false)} aria-label="Close" />
            <div className="relative animate-in slide-in-from-bottom-4 rounded-t-2xl border-t border-border bg-card p-5 shadow-2xl">
              <div className="mx-auto mb-3 h-1 w-10 rounded-full bg-muted-foreground/30" />
              <div className="mb-3 text-base font-semibold">Payment options</div>
              <div className="grid grid-cols-2 gap-3">
                <button
                  onClick={() => { setChooseMethod(false); setStep(2); }}
                  className="overflow-hidden rounded-2xl shadow-md transition active:scale-[0.98]"
                  aria-label="Pay with UPI / Bank"
                >
                  <img src="/inr_pay.png" alt="UPI / Bank" className="aspect-[16/10] size-full object-cover" />
                </button>
                <button
                  onClick={startCrypto}
                  disabled={submitting}
                  className="overflow-hidden rounded-2xl shadow-md transition active:scale-[0.98] disabled:opacity-60"
                  aria-label="Pay with crypto"
                >
                  <img src="/usdt_pay.png" alt="Crypto Payment" className="aspect-[16/10] size-full object-cover" />
                </button>
              </div>
            </div>
          </div>
        )}

        {/* ─────────────── Crypto: gateway-only (no manual address) ─────────────── */}
        {cryptoMode && !cryptoManual && (
          <div className="absolute inset-0 z-30 flex flex-col bg-background">
            <Header onBack={() => setCryptoMode(false)} />
            <div className="flex flex-1 flex-col items-center justify-center gap-3 px-8 text-center">
              <Bitcoin className="size-10 text-[#f7931a]" />
              <div className="text-base font-semibold">Crypto gateway is being set up</div>
              <p className="text-sm text-muted-foreground">
                Automatic crypto checkout isn&apos;t live yet. Please use UPI / Bank for now, or ask support.
              </p>
              <button onClick={() => setCryptoMode(false)} className="mt-2 rounded-xl bg-primary px-5 py-2.5 text-sm font-semibold text-primary-foreground">
                Back
              </button>
            </div>
          </div>
        )}

        {/* ─────────────── Crypto: manual address + QR + tx-hash ─────────────── */}
        {cryptoMode && cryptoManual && (
          <div className="absolute inset-0 z-30 flex flex-col bg-background">
            <Header onBack={() => setCryptoMode(false)} />
            <div className="flex flex-1 flex-col gap-4 overflow-y-auto px-5 pt-4">
              <div className="text-center">
                <div className="text-sm text-muted-foreground">Send exactly</div>
                {cryptoQuote ? (
                  <>
                    <div className="text-3xl font-bold">
                      {cryptoQuote.crypto_amount} <span className="text-lg">{cryptoQuote.asset}</span>
                    </div>
                    <div className="mt-0.5 text-xs text-muted-foreground">
                      ≈ {formatINR(amount)} · 1 {cryptoQuote.asset} = ₹{cryptoQuote.inr_per_unit.toLocaleString("en-IN")} (live)
                    </div>
                  </>
                ) : (
                  <div className="text-2xl font-bold">
                    {formatINR(amount)}{" "}
                    <span className="text-sm font-medium text-muted-foreground">worth of {cryptoManual.asset || "crypto"}</span>
                  </div>
                )}
              </div>
              <div className="mx-auto rounded-2xl border border-border bg-white p-3">
                <QRCodeSVG value={cryptoManual.wallet_address} size={168} />
              </div>
              <div className="flex flex-wrap justify-center gap-2 text-xs">
                {cryptoManual.asset && <span className="rounded-full bg-muted px-2.5 py-1 font-semibold">{cryptoManual.asset}</span>}
                {cryptoManual.network && <span className="rounded-full bg-muted px-2.5 py-1 font-semibold">{cryptoManual.network}</span>}
              </div>
              <div className="rounded-xl border border-border bg-muted/30 p-3">
                <div className="text-[11px] text-muted-foreground">Wallet address</div>
                <div className="mt-1 flex items-center gap-2">
                  <code className="min-w-0 flex-1 break-all text-xs font-semibold">{cryptoManual.wallet_address}</code>
                  <button onClick={() => copy(cryptoManual.wallet_address, "Address")} className="shrink-0 rounded-md border border-border p-1.5">
                    <Copy className="size-3.5" />
                  </button>
                </div>
              </div>
              <div className="space-y-1">
                <div className="text-sm font-medium">Transaction hash (after you pay)</div>
                <input
                  value={cryptoTx}
                  onChange={(e) => setCryptoTx(e.target.value)}
                  placeholder="Paste your on-chain tx hash"
                  className="h-11 w-full rounded-lg border border-border bg-muted/20 px-3 text-sm outline-none focus:border-primary"
                />
              </div>
              <button
                onClick={submitCrypto}
                disabled={submitting || !cryptoTx.trim()}
                className="mt-1 flex h-12 w-full items-center justify-center gap-1.5 rounded-xl bg-primary text-sm font-semibold text-primary-foreground shadow transition hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {submitting ? <Loader2 className="size-4 animate-spin" /> : "Submit for approval"}
              </button>
              <p className="pb-6 text-center text-[11px] text-muted-foreground">Admin verifies the transaction and credits your wallet.</p>
            </div>
          </div>
        )}

        {/* ─────────────── STEP 2: Choose UPI app sheet ─────────────── */}
        {step === 2 && (
          <div className="absolute inset-0 z-10 flex flex-col justify-end">
            <button className="absolute inset-0 bg-black/40" onClick={() => setStep(1)} aria-label="Close" />
            <div className="relative animate-in slide-in-from-bottom-4 rounded-t-2xl border-t border-border bg-card p-5 shadow-2xl">
              <div className="mx-auto mb-3 h-1 w-10 rounded-full bg-muted-foreground/30" />
              <div className="mb-1 flex items-center gap-2 text-base font-semibold">
                <Building2 className="size-4 text-primary" /> Choose an account to pay to
              </div>
              <p className="mb-4 text-xs text-muted-foreground">
                Please confirm account before paying into same account.
              </p>

              <button
                onClick={payViaUpiApp}
                className="flex w-full items-center justify-between rounded-xl border border-primary/40 bg-primary/5 p-3 text-left transition hover:bg-primary/10"
              >
                <div className="flex items-center gap-3">
                  <div className="flex -space-x-2">
                    <GPayMark size={34} />
                    <PhonePeMark size={34} />
                    <PaytmMark size={34} />
                    <BhimMark size={34} />
                  </div>
                  <div>
                    <div className="font-mono text-sm font-semibold tracking-widest">****</div>
                    <div className="text-xs text-muted-foreground">Pay to {payee}</div>
                  </div>
                </div>
                <ChevronRight className="size-5 text-muted-foreground" />
              </button>

              <button
                onClick={payViaUpiApp}
                className="mt-4 flex h-12 w-full items-center justify-center rounded-xl bg-primary text-sm font-semibold text-primary-foreground shadow transition hover:bg-primary/90"
              >
                Pay {formatINR(amount)}
              </button>

              <PoweredFooter />
            </div>
          </div>
        )}

        {/* ─────────────── STEP 3: Pay & Screenshot (QR + UPI id) ─────────────── */}
        {step === 3 && (
          <div className="flex flex-1 flex-col">
            <Header onBack={() => setStep(1)} />
            <div className="flex flex-1 flex-col px-5 pt-4">
              <Stepper active="pay" />

              {/* All configured payment accounts. Admin can add many — show
                  every one in a scrollable list so the user can see each
                  QR + UPI + bank details and tap the account they actually
                  pay to (that account is referenced on the deposit). */}
              <div className="flex flex-1 flex-col gap-3 overflow-y-auto">
                {companyBanks.length > 1 && (
                  <div className="text-center text-xs text-muted-foreground">
                    {companyBanks.length} payment accounts · scroll & tap the one you pay to
                  </div>
                )}

                {companyBanks.length === 0 && (
                  <div className="rounded-xl border border-border bg-muted/30 p-6 text-center text-sm text-muted-foreground">
                    No payment account configured. Please contact support.
                  </div>
                )}

                {companyBanks.map((b) => {
                  const isSel = b.id === bank?.id;
                  const multi = companyBanks.length > 1;
                  return (
                    <div
                      key={b.id}
                      onClick={() => setSelectedBankId(b.id)}
                      role={multi ? "button" : undefined}
                      className={cn(
                        "flex flex-col items-center rounded-2xl border bg-card p-4 shadow-sm transition",
                        multi && "cursor-pointer",
                        multi && isSel
                          ? "border-primary ring-2 ring-primary/30"
                          : "border-border",
                      )}
                    >
                      {multi && (
                        <div className="mb-2 flex w-full items-center justify-between gap-2">
                          <span className="truncate text-sm font-semibold">
                            {b.bank_name || "Payment account"}
                          </span>
                          <span
                            className={cn(
                              "shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold",
                              isSel
                                ? "bg-primary/15 text-primary"
                                : "bg-muted text-muted-foreground",
                            )}
                          >
                            {isSel ? "Selected" : "Tap to select"}
                          </span>
                        </div>
                      )}

                      {b.qr_code_url ? (
                        // Admin-uploaded QR for this exact account (most accurate).
                        // eslint-disable-next-line @next/next/no-img-element
                        <img
                          src={b.qr_code_url.startsWith("http") ? b.qr_code_url : `${API_URL}${b.qr_code_url}`}
                          alt="Pay QR"
                          className="size-44 rounded-md object-contain"
                        />
                      ) : (
                        <UpiQR upiId={b.upi_id} payeeName={payeeName || b.account_holder || "Merchant"} amount={amount} size={176} />
                      )}
                      <div className="mt-2 text-center text-sm font-medium text-muted-foreground">
                        Send {formatINR(amount)}
                      </div>

                      {b.upi_id ? (
                        <div className="mt-3 flex w-full items-center gap-2">
                          <div className="flex-1 truncate rounded-lg border border-border bg-muted/40 px-3 py-2.5 font-mono text-sm">
                            {b.upi_id}
                          </div>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              copy(b.upi_id!, "UPI ID");
                            }}
                            className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2.5 text-sm font-semibold text-primary-foreground transition hover:bg-primary/90"
                          >
                            <Copy className="size-4" /> Copy
                          </button>
                        </div>
                      ) : null}

                      <div className="mt-3 w-full space-y-2 rounded-lg border border-border bg-muted/20 p-3 text-sm">
                        <div className="mb-1 inline-flex items-center gap-2 text-xs font-semibold text-muted-foreground">
                          <Building2 className="size-4 text-primary" /> Bank details
                        </div>
                        <Row label="Bank" value={b.bank_name} />
                        <Row label="A/C holder" value={b.account_holder} />
                        <Row label="A/C number" value={b.account_number} onCopy={() => b.account_number && copy(b.account_number, "Account number")} />
                        <Row label="IFSC" value={b.ifsc_code} onCopy={() => b.ifsc_code && copy(b.ifsc_code, "IFSC")} />
                      </div>
                    </div>
                  );
                })}
              </div>

              <div className="mt-auto grid grid-cols-2 gap-3 pt-6">
                <button
                  onClick={() => setStep(1)}
                  className="flex h-12 items-center justify-center gap-1.5 rounded-xl border border-border bg-muted/40 text-sm font-semibold transition hover:bg-muted"
                >
                  <ChevronLeft className="size-4" /> Back
                </button>
                <button
                  onClick={() => setStep(4)}
                  className="flex h-12 items-center justify-center gap-1.5 rounded-xl bg-primary text-sm font-semibold text-primary-foreground shadow transition hover:bg-primary/90"
                >
                  Next <ChevronRight className="size-4" />
                </button>
              </div>
              <PoweredFooter />
            </div>
          </div>
        )}

        {/* ─────────────── STEP 4: Upload screenshot + Submit ─────────────── */}
        {step === 4 && (
          <div className="flex flex-1 flex-col">
            <Header onBack={() => setStep(3)} />
            <div className="flex flex-1 flex-col px-5 pt-4">
              <Stepper active="upload" />

              <div className="mb-2 text-sm font-semibold text-muted-foreground">Transaction screenshot</div>
              <input
                ref={fileRef}
                type="file"
                accept="image/*"
                className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) void onPickFile(f);
                  e.target.value = "";
                }}
              />
              <button
                onClick={() => fileRef.current?.click()}
                disabled={uploading}
                className={cn(
                  "flex min-h-[180px] w-full flex-col items-center justify-center gap-3 rounded-xl border-2 border-dashed p-6 text-center transition",
                  screenshotUrl ? "border-primary/50 bg-primary/5" : "border-border bg-muted/30 hover:bg-muted/50",
                )}
              >
                {uploading ? (
                  <Loader2 className="size-8 animate-spin text-primary" />
                ) : screenshotUrl ? (
                  <>
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={screenshotUrl.startsWith("http") ? screenshotUrl : `${API_URL}${screenshotUrl}`}
                      alt="Proof"
                      className="max-h-36 rounded-md object-contain"
                    />
                    <span className="inline-flex items-center gap-1 text-xs font-medium text-primary">
                      <Check className="size-3.5" /> Uploaded · tap to change
                    </span>
                  </>
                ) : (
                  <>
                    <div className="grid size-14 place-items-center rounded-full bg-primary/10">
                      <Upload className="size-6 text-primary" />
                    </div>
                    <div className="text-sm font-semibold">Tap to upload screenshot</div>
                    <div className="text-xs text-muted-foreground">or drop the image here</div>
                  </>
                )}
              </button>

              <button
                onClick={submit}
                disabled={submitting || !screenshotUrl}
                className="mt-5 flex h-12 w-full items-center justify-center gap-1.5 rounded-xl bg-primary text-sm font-semibold text-primary-foreground shadow transition hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {submitting ? <Loader2 className="size-4 animate-spin" /> : null}
                Submit
              </button>
              <button
                onClick={() => setStep(3)}
                className="mt-3 flex h-12 w-full items-center justify-center gap-1.5 rounded-xl border border-border bg-muted/40 text-sm font-semibold transition hover:bg-muted"
              >
                <ChevronLeft className="size-4" /> Back
              </button>

              <PoweredFooter />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function Row({ label, value, onCopy }: { label: string; value?: string | null; onCopy?: () => void }) {
  if (!value) return null;
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="inline-flex items-center gap-1.5 font-mono text-xs font-medium">
        {value}
        {onCopy ? (
          <button onClick={onCopy} className="text-muted-foreground transition hover:text-foreground" aria-label={`Copy ${label}`}>
            <Copy className="size-3.5" />
          </button>
        ) : null}
      </span>
    </div>
  );
}
