"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  ArrowLeft,
  AtSign,
  Bell,
  ChevronRight,
  Coins,
  CreditCard,
  FileText,
  Gauge,
  HelpCircle,
  IdCard,
  KeyRound,
  Lock,
  LogOut,
  MessageCircle,
  Palette,
  Phone,
  ReceiptText,
  Shield,
  ShieldCheck,
  ShieldOff,
  User as UserIcon,
  Wallet as WalletIcon,
} from "lucide-react";
import { ProfileAPI, AuthAPI, SegmentSettingsAPI } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ThemeToggle } from "@/components/common/ThemeToggle";
import { useAuthStore } from "@/stores/authStore";
import {
  buildWhatsappUrl,
  useSupportContacts,
} from "@/lib/useSupport";
import { cn } from "@/lib/utils";

/**
 * Mobile-first profile screen modelled on Zerodha Kite / Groww — a
 * clean avatar header on top followed by grouped list sections
 * (Account, Security, Preferences, Support, About). Tapping any row
 * drills into a sub-screen (rendered in this same component, gated by
 * `subView` state) so the flow feels like a real mobile-app navigation
 * stack while staying on the single `/profile` route. Desktop keeps the
 * same screens but renders them stacked in a single column for now —
 * the visual treatment scales up cleanly.
 *
 * Replaces the earlier tab-based profile that the user called out as
 * "bekar sa hai yrr" — sections were a flat horizontal tab strip and
 * the cards inside felt like an admin form, not a consumer profile.
 */
type SubView =
  | "main"
  | "personal"
  | "brokerage"
  | "leverage"
  | "terms"
  | "security"
  | "appearance"
  | "support";

export default function ProfilePage() {
  // The persisted login user (zustand `nb.auth`) is our offline-safe seed.
  // It carries every field the main profile screen renders (name, code,
  // email, status, role, is_demo, 2FA), so we can paint a correct profile
  // instantly and never show the error wall while a logged-in user's
  // /users/me round-trip is in flight or briefly fails on a weak network.
  const storeUser = useAuthStore((s) => s.user);
  const {
    data: fetched,
    refetch,
    isLoading,
  } = useQuery({
    queryKey: ["me"],
    queryFn: () => ProfileAPI.me(),
    // Seed from the persisted login user so the screen is populated on the
    // very first paint. `initialDataUpdatedAt: 0` marks it stale so a fresh
    // /users/me still fetches immediately on mount to fill the extra fields
    // (kyc, created_at, last_login_at, …).
    initialData: storeUser ? (storeUser as any) : undefined,
    initialDataUpdatedAt: 0,
    // Profile was the ONLY screen backed by a single one-shot fetch with no
    // poll — every other page (positions 2s / orders 4s / wallet 10s) self-
    // heals a transient mobile-network blip on its next tick, so the blip is
    // invisible there. On Profile that one failed fetch stuck on "Could not
    // load profile" until a manual Retry that also failed on weak signal
    // ("bar bar problem"). Give it the same safety net: keep it gently
    // refreshing and recover on focus / reconnect.
    refetchOnWindowFocus: true,
    refetchOnReconnect: true,
    refetchInterval: 30_000,
  });
  // Prefer the freshest server copy; fall back to the persisted login user
  // so a logged-in client always sees their profile, never a dead wall.
  const me = fetched ?? storeUser;

  const [subView, setSubView] = useState<SubView>("main");
  const [name, setName] = useState("");
  useEffect(() => {
    if (me?.full_name) setName(me.full_name);
  }, [me?.full_name]);

  // Only a genuinely-logged-out state (no cached user AND nothing fetched)
  // shows the loader / error wall now.
  if (!me && isLoading) return <div className="text-sm text-muted-foreground">Loading…</div>;
  if (!me) return (
    <div className="flex flex-col items-center gap-3 py-12 text-center">
      <p className="text-sm text-muted-foreground">Could not load profile. Please try again.</p>
      <button
        type="button"
        onClick={() => void refetch()}
        className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
      >
        Retry
      </button>
    </div>
  );

  // ── Sub-screens ─────────────────────────────────────────────────
  if (subView !== "main") {
    return (
      <SubScreen
        title={subViewTitle(subView)}
        onBack={() => setSubView("main")}
      >
        {subView === "personal" && (
          <PersonalForm
            me={me}
            name={name}
            setName={setName}
            onSave={() => save(name, refetch)}
          />
        )}
        {subView === "brokerage" && <SegmentOverview kind="brokerage" />}
        {subView === "leverage" && <SegmentOverview kind="leverage" />}
        {subView === "terms" && <TermsScreen />}
        {subView === "security" && <SecurityForm me={me} />}
        {subView === "appearance" && <AppearanceForm />}
        {subView === "support" && <SupportLinks />}
      </SubScreen>
    );
  }

  // ── Main screen ────────────────────────────────────────────────
  return (
    <div className="space-y-4 pb-2">
      <ProfileHeader me={me} />

      <ListGroup title="Account">
        <ListRow
          icon={UserIcon}
          tone="primary"
          label="Personal information"
          sub="Name, email, mobile, user code"
          onClick={() => setSubView("personal")}
        />
        <ListRowLink
          icon={WalletIcon}
          tone="buy"
          label="Wallet"
          sub="Deposit, withdraw, balance"
          href="/wallet"
        />
        <ListRowLink
          icon={CreditCard}
          tone="primary"
          label="Bank accounts"
          sub="Linked payout accounts"
          href="/bank-accounts"
        />
      </ListGroup>

      <ListGroup title="Charges & leverage">
        <ListRow
          icon={Coins}
          tone="buy"
          label="Brokerage details"
          sub="Your brokerage per segment"
          onClick={() => setSubView("brokerage")}
        />
        <ListRow
          icon={Gauge}
          tone="warn"
          label="Leverage"
          sub="Intraday & carry-forward per segment"
          onClick={() => setSubView("leverage")}
        />
      </ListGroup>

      <ListGroup title="Trading & reports">
        <ListRowLink
          icon={ReceiptText}
          tone="primary"
          label="Reports"
          sub="Download your tradebook"
          href="/reports/tradebook"
        />
        <ListRowLink
          icon={Bell}
          tone="warn"
          label="Notifications"
          sub="Alerts, account activity, system"
          href="/notifications"
        />
        <ListRowLink
          icon={FileText}
          tone="muted"
          label="Ledger"
          sub="Cash entries, charges"
          href="/ledger"
        />
      </ListGroup>

      <ListGroup title="Security">
        <ListRow
          icon={KeyRound}
          tone="primary"
          label="Change password"
          sub="Update your account password"
          onClick={() => setSubView("security")}
        />
        <ListRowLink
          icon={Shield}
          tone={me.two_fa_enabled ? "buy" : "warn"}
          label="Two-factor authentication"
          sub={me.two_fa_enabled ? "Enabled" : "Add a second login step"}
          badge={me.two_fa_enabled ? "On" : "Off"}
          badgeTone={me.two_fa_enabled ? "buy" : "warn"}
          href="/2fa"
        />
      </ListGroup>

      <ListGroup title="Preferences">
        <ListRow
          icon={Palette}
          tone="primary"
          label="Appearance"
          sub="Switch between light and dark theme"
          onClick={() => setSubView("appearance")}
        />
      </ListGroup>

      <ListGroup title="Support">
        <ListRow
          icon={HelpCircle}
          tone="info"
          label="Help & support"
          sub="WhatsApp · email"
          onClick={() => setSubView("support")}
        />
      </ListGroup>

      <ListGroup title="About">
        <ListRow
          icon={FileText}
          tone="muted"
          label="Terms & Conditions"
          sub="Your broker's trading terms"
          onClick={() => setSubView("terms")}
        />
      </ListGroup>

      <SignOutRow />

      <p className="px-1 pb-4 pt-2 text-center text-[10px] text-muted-foreground">
        MarginWealth · v1.0.0
      </p>
    </div>
  );
}

function subViewTitle(v: SubView): string {
  switch (v) {
    case "personal":
      return "Personal information";
    case "brokerage":
      return "Brokerage details";
    case "leverage":
      return "Leverage";
    case "terms":
      return "Terms & Conditions";
    case "security":
      return "Security";
    case "appearance":
      return "Appearance";
    case "support":
      return "Help & support";
    default:
      return "Profile";
  }
}

// ─────────────────────────────────────────────────────────────────
// Header
// ─────────────────────────────────────────────────────────────────
function ProfileHeader({ me }: { me: any }) {
  const initials = (me.full_name || me.user_code || "U")
    .split(" ")
    .map((s: string) => s[0])
    .filter(Boolean)
    .slice(0, 2)
    .join("")
    .toUpperCase();
  return (
    <section className="overflow-hidden rounded-2xl border border-border bg-card">
      <div className="relative px-5 pt-6 pb-5">
        {/* Subtle gradient backdrop — not the full purple band the user
            disliked. Keeps brand presence while letting text breathe. */}
        <div className="absolute inset-x-0 top-0 h-24 bg-gradient-to-br from-primary/30 via-primary/10 to-transparent pointer-events-none" />
        <div className="relative flex items-center gap-4">
          <div className="grid size-16 shrink-0 place-items-center rounded-2xl bg-primary text-xl font-bold text-primary-foreground shadow-md ring-2 ring-card">
            {initials}
          </div>
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-base font-semibold">{me.full_name}</h1>
            <p className="mt-0.5 truncate text-[11px] text-muted-foreground">
              <span className="font-mono font-medium text-foreground">{me.user_code}</span>
              {" · "}
              {me.email}
            </p>
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <Pill tone={me.status === "ACTIVE" ? "buy" : "muted"}>{me.status}</Pill>
              <Pill tone="primary">{me.role}</Pill>
              {me.is_demo && <Pill tone="warn">DEMO</Pill>}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

// ─────────────────────────────────────────────────────────────────
// List primitives
// ─────────────────────────────────────────────────────────────────
function ListGroup({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <h2 className="px-3 pb-1.5 pt-1 text-[11px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">
        {title}
      </h2>
      <div className="overflow-hidden rounded-xl border border-border bg-card">
        <ul className="divide-y divide-border">{children}</ul>
      </div>
    </section>
  );
}

type Tone = "primary" | "buy" | "sell" | "warn" | "info" | "muted";
const TONE_BG: Record<Tone, string> = {
  primary: "bg-primary/12 text-primary",
  buy: "bg-buy/12 text-buy",
  sell: "bg-sell/12 text-sell",
  warn: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
  info: "bg-info/12 text-info",
  muted: "bg-muted text-muted-foreground",
};
const BADGE_TONE: Record<Tone, string> = {
  primary: "bg-primary/15 text-primary",
  buy: "bg-buy/15 text-buy",
  sell: "bg-sell/15 text-sell",
  warn: "bg-amber-500/20 text-amber-700 dark:text-amber-400",
  info: "bg-info/15 text-info",
  muted: "bg-muted text-muted-foreground",
};

function RowInner({
  icon: Icon,
  tone = "primary",
  label,
  sub,
  badge,
  badgeTone = "muted",
}: {
  icon: any;
  tone?: Tone;
  label: string;
  sub?: string;
  badge?: string | null;
  badgeTone?: Tone;
}) {
  return (
    <>
      <div className={cn("grid size-10 shrink-0 place-items-center rounded-xl", TONE_BG[tone])}>
        <Icon className="size-5" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium text-foreground">{label}</div>
        {sub && (
          <div className="mt-0.5 truncate text-[11px] text-muted-foreground">{sub}</div>
        )}
      </div>
      {badge && (
        <span
          className={cn(
            "rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider",
            BADGE_TONE[badgeTone],
          )}
        >
          {badge}
        </span>
      )}
      <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
    </>
  );
}

function ListRow(props: {
  icon: any;
  tone?: Tone;
  label: string;
  sub?: string;
  badge?: string | null;
  badgeTone?: Tone;
  onClick: () => void;
}) {
  const { onClick, ...rest } = props;
  return (
    <li>
      <button
        type="button"
        onClick={onClick}
        className="flex w-full items-center gap-3 px-3 py-2.5 text-left transition-colors hover:bg-muted/30 active:bg-muted/50"
      >
        <RowInner {...rest} />
      </button>
    </li>
  );
}

function ListRowLink(props: {
  icon: any;
  tone?: Tone;
  label: string;
  sub?: string;
  badge?: string | null;
  badgeTone?: Tone;
  href: string;
}) {
  const { href, ...rest } = props;
  return (
    <li>
      <Link
        href={href}
        className="flex w-full items-center gap-3 px-3 py-2.5 text-left transition-colors hover:bg-muted/30 active:bg-muted/50"
      >
        <RowInner {...rest} />
      </Link>
    </li>
  );
}

function SignOutRow() {
  const logout = useAuthStore((s) => s.logout);
  async function go() {
    try {
      await logout();
    } finally {
      window.location.href = "/login";
    }
  }
  return (
    <button
      type="button"
      onClick={go}
      className="flex w-full items-center justify-center gap-2 rounded-xl border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm font-semibold text-destructive transition-colors hover:bg-destructive/10"
    >
      <LogOut className="size-4" />
      Sign out
    </button>
  );
}

// ─────────────────────────────────────────────────────────────────
// Sub-screen frame
// ─────────────────────────────────────────────────────────────────
function SubScreen({
  title,
  onBack,
  children,
}: {
  title: string;
  onBack: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 pb-1">
        <button
          type="button"
          onClick={onBack}
          className="grid size-9 place-items-center rounded-full border border-border bg-card text-muted-foreground hover:bg-muted/40"
          aria-label="Back"
        >
          <ArrowLeft className="size-4" />
        </button>
        <h1 className="text-base font-semibold">{title}</h1>
      </div>
      {children}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────
// Personal info form
// ─────────────────────────────────────────────────────────────────
function PersonalForm({
  me,
  name,
  setName,
  onSave,
}: {
  me: any;
  name: string;
  setName: (v: string) => void;
  onSave: () => void;
}) {
  return (
    <section className="rounded-xl border border-border bg-card p-4">
      <div className="space-y-4">
        <Field label="Full name">
          <Input value={name} onChange={(e) => setName(e.target.value)} className="h-11" />
        </Field>
        <ReadRow icon={AtSign} label="Email" value={me.email} />
        <ReadRow icon={Phone} label="Mobile" value={me.mobile} />
        <ReadRow
          icon={IdCard}
          label="User code"
          value={<span className="font-mono">{me.user_code}</span>}
        />
        <div className="grid grid-cols-2 gap-3">
          <Fact label="Account" value={me.is_demo ? "Demo" : "Live"} />
          <Fact label="Role" value={me.role} />
          <Fact
            label="Status"
            value={me.status}
            tone={me.status === "ACTIVE" ? "buy" : "muted"}
          />
          <Fact
            label="2FA"
            value={me.two_fa_enabled ? "Enabled" : "Disabled"}
            tone={me.two_fa_enabled ? "buy" : "muted"}
          />
          {me.last_login_at && (
            <Fact
              label="Last login"
              value={new Date(me.last_login_at).toLocaleString("en-IN", {
                dateStyle: "medium",
                timeStyle: "short",
              })}
              wide
            />
          )}
          {me.created_at && (
            <Fact
              label="Joined"
              value={new Date(me.created_at).toLocaleDateString("en-IN", {
                day: "numeric",
                month: "long",
                year: "numeric",
              })}
              wide
            />
          )}
        </div>
        <div className="pt-1">
          <Button onClick={onSave}>Save changes</Button>
        </div>
      </div>
    </section>
  );
}

// ─────────────────────────────────────────────────────────────────
// Security form
// ─────────────────────────────────────────────────────────────────
function SecurityForm({ me }: { me: any }) {
  const [pwd, setPwd] = useState({ current_password: "", new_password: "" });
  const [busy, setBusy] = useState(false);

  async function changePassword() {
    if (pwd.new_password.length < 6) return toast.error("Min 6 characters");
    setBusy(true);
    try {
      await AuthAPI.changePassword(pwd);
      toast.success("Password changed");
      setPwd({ current_password: "", new_password: "" });
    } catch (e: any) {
      toast.error(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <section className="rounded-xl border border-border bg-card p-4">
        <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold">
          <Lock className="size-4 text-primary" /> Change password
        </h3>
        <div className="space-y-3">
          <Field label="Current password">
            <Input
              type="password"
              value={pwd.current_password}
              onChange={(e) =>
                setPwd((p) => ({ ...p, current_password: e.target.value }))
              }
              className="h-11"
            />
          </Field>
          <Field label="New password">
            <Input
              type="password"
              value={pwd.new_password}
              onChange={(e) => setPwd((p) => ({ ...p, new_password: e.target.value }))}
              className="h-11"
            />
            <p className="text-[11px] text-muted-foreground">Minimum 6 characters.</p>
          </Field>
          <Button onClick={changePassword} loading={busy} className="w-full">
            <KeyRound className="size-4" /> Update password
          </Button>
        </div>
      </section>

      <section className="rounded-xl border border-border bg-card p-4">
        <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold">
          <Shield className="size-4 text-primary" /> Two-factor authentication
        </h3>
        <div className="flex items-start gap-3 rounded-lg border border-border bg-muted/20 p-3">
          <div
            className={cn(
              "grid size-10 shrink-0 place-items-center rounded-full",
              me.two_fa_enabled ? "bg-buy/15 text-buy" : "bg-muted text-muted-foreground",
            )}
          >
            {me.two_fa_enabled ? (
              <ShieldCheck className="size-5" />
            ) : (
              <ShieldOff className="size-5" />
            )}
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-sm font-semibold">
              2FA is {me.two_fa_enabled ? "enabled" : "disabled"}
            </div>
            <p className="mt-1 text-[11px] text-muted-foreground">
              {me.two_fa_enabled
                ? "Authenticator app is required at login."
                : "Protect your account by requiring a 6-digit code from an authenticator app on every login."}
            </p>
          </div>
        </div>
        <div className="mt-3">
          <Button asChild variant={me.two_fa_enabled ? "outline" : "default"} className="w-full">
            <a href="/2fa">{me.two_fa_enabled ? "Manage 2FA" : "Set up 2FA"}</a>
          </Button>
        </div>
      </section>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────
// Appearance + Support
// ─────────────────────────────────────────────────────────────────
function AppearanceForm() {
  return (
    <section className="rounded-xl border border-border bg-card p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="grid size-10 place-items-center rounded-xl bg-primary/12 text-primary">
            <Palette className="size-5" />
          </div>
          <div className="min-w-0">
            <div className="text-sm font-medium">Theme</div>
            <p className="text-[11px] text-muted-foreground">
              Switch between light and dark
            </p>
          </div>
        </div>
        <ThemeToggle />
      </div>
    </section>
  );
}

function SupportLinks() {
  const { data: support } = useSupportContacts();
  const waUrl = buildWhatsappUrl(
    support?.whatsapp,
    "Hi, I need help with my MarginWealth account",
  );
  if (!waUrl) {
    return (
      <section className="rounded-xl border border-border bg-card p-6 text-center text-sm text-muted-foreground">
        Support channels haven't been configured yet. Please contact your broker.
      </section>
    );
  }
  return (
    <section className="overflow-hidden rounded-xl border border-border bg-card">
      <ul className="divide-y divide-border">
        {waUrl && (
          <li>
            <a
              href={waUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-3 px-3 py-3 transition-colors hover:bg-muted/30"
            >
              <div className="grid size-10 place-items-center rounded-xl bg-[#25D366]/15 text-[#25D366]">
                <MessageCircle className="size-5" />
              </div>
              <div className="min-w-0 flex-1">
                <div className="text-sm font-medium">WhatsApp support</div>
                <p className="truncate text-[11px] text-muted-foreground">
                  {support?.whatsapp}
                </p>
              </div>
              <ChevronRight className="size-4 text-muted-foreground" />
            </a>
          </li>
        )}
      </ul>
    </section>
  );
}

// ─────────────────────────────────────────────────────────────────
// Brokerage / Leverage — per-segment overview (resolved for THIS user)
// ─────────────────────────────────────────────────────────────────
const SEG_TOKENS: Record<string, string> = {
  NSE: "NSE", BSE: "BSE", MCX: "MCX", CDS: "CDS",
  EQUITY: "Equity", FUTURE: "Futures", INDEX: "Index", STOCK: "Stock",
  OPTION: "Option", BUY: "Buy", SELL: "Sell", CRYPTO: "Crypto", SPOT: "Spot",
};
function segName(s: string): string {
  return String(s || "")
    .split("_")
    .map((w) => SEG_TOKENS[w] ?? (w ? w[0] + w.slice(1).toLowerCase() : w))
    .join(" ");
}
function trimNum(n: number): string {
  if (!Number.isFinite(n)) return "—";
  return Number.isInteger(n) ? String(n) : String(Number(n.toFixed(2)));
}
function fmtBrokerage(r: any): string {
  const t = String(r.commission_type || "").toUpperCase();
  const v = Number(r.commission_value ?? 0);
  let s: string;
  if (t === "FLAT") s = `₹${trimNum(v)} flat`;
  else if (t === "PERCENTAGE") s = `${trimNum(v)}%`;
  else if (t === "PER_CRORE") s = `₹${trimNum(v)} / crore`;
  else if (t === "PER_LOT") s = `₹${trimNum(v)} / lot`;
  else s = v ? `₹${trimNum(v)}` : "—";
  const min = Number(r.min_brokerage ?? 0);
  if (min > 0) s += ` · min ₹${trimNum(min)}`;
  return s;
}
function fmtLev(lev: any, marginPct: any, mode: any, fixed: any): string {
  const m = String(mode || "").toLowerCase();
  const l = Number(lev ?? 0);
  const p = Number(marginPct ?? 0);
  const f = Number(fixed ?? 0);
  if (m === "fixed" && f > 0) return `₹${trimNum(f)}/lot`;
  if (l > 0) return `${trimNum(l)}×`;
  if (p > 0) return `${trimNum(100 / p)}×`;
  return "—";
}

function LevChip({ label, value, tone }: { label: string; value: string; tone: "primary" | "warn" }) {
  return (
    <div className="w-[74px] shrink-0 rounded-lg border border-border bg-muted/20 px-1.5 py-1 text-center">
      <div className="text-[9px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className={cn("text-xs font-bold", tone === "primary" ? "text-primary" : "text-amber-600 dark:text-amber-400")}>{value}</div>
    </div>
  );
}

function SegmentOverview({ kind }: { kind: "brokerage" | "leverage" }) {
  const { data, isLoading } = useQuery({
    queryKey: ["segment-overview"],
    queryFn: () => SegmentSettingsAPI.overview(),
    staleTime: 60_000,
  });
  if (isLoading) return <div className="text-sm text-muted-foreground">Loading…</div>;
  const rows: any[] = (data ?? []).filter((r) => r.allow !== false);
  if (rows.length === 0)
    return (
      <section className="rounded-xl border border-border bg-card p-6 text-center text-sm text-muted-foreground">
        No segments are enabled for your account.
      </section>
    );
  return (
    <div className="space-y-2">
      <p className="px-1 text-[11px] leading-snug text-muted-foreground">
        {kind === "brokerage"
          ? "Brokerage charged on your trades, per segment."
          : "Leverage available per segment — Intraday and Carry-forward (overnight)."}
      </p>
      {rows.map((r) => (
        <div
          key={r.segment}
          className="flex items-center gap-2.5 rounded-xl border border-border bg-card px-3 py-2.5"
        >
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-medium">{segName(r.segment)}</div>
          </div>
          {kind === "brokerage" ? (
            <div className="shrink-0 text-right text-[13px] font-semibold text-buy">
              {fmtBrokerage(r)}
            </div>
          ) : (
            <div className="flex shrink-0 items-center gap-1.5">
              <LevChip label="Intraday" value={fmtLev(r.leverage, r.margin_percentage, r.margin_calc_mode, r.fixed_margin_per_lot)} tone="primary" />
              <LevChip label="Carry-fwd" value={fmtLev(r.overnight_leverage, r.overnight_margin_percentage, r.margin_calc_mode, r.overnight_fixed_margin_per_lot)} tone="warn" />
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────
// Terms & Conditions — text the user's broker set in Platform Settings
// ─────────────────────────────────────────────────────────────────
function TermsScreen() {
  const { data, isLoading } = useQuery({
    queryKey: ["terms"],
    queryFn: () => ProfileAPI.terms(),
    staleTime: 60_000,
  });
  if (isLoading) return <div className="text-sm text-muted-foreground">Loading…</div>;
  const terms = (data?.terms ?? "").trim();
  if (!terms)
    return (
      <section className="rounded-xl border border-border bg-card p-8 text-center">
        <FileText className="mx-auto mb-2 size-8 text-muted-foreground" />
        <p className="text-sm text-muted-foreground">
          Your broker hasn&apos;t published Terms &amp; Conditions yet.
        </p>
      </section>
    );
  const lines = terms.split(/\r?\n/);
  return (
    <section className="rounded-2xl border border-border bg-card p-4">
      <div className="space-y-1.5">
        {lines.map((ln, i) => {
          const t = ln.trim();
          if (!t) return <div key={i} className="h-2" />;
          const isHeading =
            t.endsWith(":") ||
            /^\d+[.)]\s/.test(t) ||
            (t === t.toUpperCase() && /[A-Z]/.test(t) && t.length <= 60);
          return (
            <p
              key={i}
              className={cn(
                "leading-relaxed",
                isHeading
                  ? "mt-1.5 text-[13px] font-bold text-foreground first:mt-0"
                  : "text-[13px] text-foreground/80",
              )}
            >
              {t}
            </p>
          );
        })}
      </div>
    </section>
  );
}

// ─────────────────────────────────────────────────────────────────
// Save helper + atoms
// ─────────────────────────────────────────────────────────────────
async function save(name: string, refetch: () => any) {
  try {
    await ProfileAPI.update({ full_name: name });
    toast.success("Profile updated");
    refetch();
  } catch (e: any) {
    toast.error(e.message);
  }
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <Label className="text-xs font-medium">{label}</Label>
      {children}
    </div>
  );
}

function ReadRow({
  icon: Icon,
  label,
  value,
}: {
  icon: any;
  label: string;
  value: React.ReactNode;
}) {
  return (
    <div className="flex items-center gap-3 rounded-lg border border-border bg-muted/20 px-3 py-2.5 text-sm">
      <Icon className="size-4 shrink-0 text-muted-foreground" />
      <div className="min-w-0 flex-1">
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
          {label}
        </div>
        <div className="truncate text-sm">{value}</div>
      </div>
    </div>
  );
}

function Fact({
  label,
  value,
  tone,
  wide,
}: {
  label: string;
  value: React.ReactNode;
  tone?: "buy" | "muted";
  wide?: boolean;
}) {
  return (
    <div
      className={cn(
        "rounded-lg border border-border bg-muted/20 px-3 py-2",
        wide && "col-span-2",
      )}
    >
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div
        className={cn(
          "mt-0.5 text-sm font-semibold",
          tone === "buy" && "text-buy",
          tone === "muted" && "text-muted-foreground",
        )}
      >
        {value}
      </div>
    </div>
  );
}

function Pill({
  children,
  tone,
}: {
  children: React.ReactNode;
  tone: "primary" | "buy" | "warn" | "muted";
}) {
  const tones: Record<string, string> = {
    primary: "bg-primary/15 text-primary",
    buy: "bg-buy/15 text-buy",
    warn: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
    muted: "bg-muted text-muted-foreground",
  };
  return (
    <span
      className={cn(
        "rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider",
        tones[tone],
      )}
    >
      {children}
    </span>
  );
}

