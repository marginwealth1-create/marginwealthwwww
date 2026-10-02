"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Bell, LogOut, Megaphone, Search, User as UserIcon, Wallet } from "lucide-react";
import { useAuthStore } from "@/stores/authStore";
import { api, unwrap, WalletAPI } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ThemeToggle } from "@/components/common/ThemeToggle";
import { BrandLogo } from "@/components/layout/BrandLogo";
import { cn, formatINR } from "@/lib/utils";
import { readWalletSnapshot, writeWalletSnapshot } from "@/lib/walletSnapshot";
import { buildWhatsappUrl, useSupportContacts } from "@/lib/useSupport";

/** WhatsApp brand glyph — Lucide doesn't ship the real WhatsApp mark so
 * we inline the official SVG path. Sized to match Lucide's icon
 * dimensions so it lines up with the other header buttons. */
function WhatsAppIcon({ className }: { className?: string }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="currentColor"
      className={className}
      aria-hidden
    >
      <path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.198-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 0 1-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 0 1-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 0 1 2.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0 0 12.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 0 0 5.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893A11.821 11.821 0 0 0 20.464 3.488" />
    </svg>
  );
}

export function TopBar() {
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);

  // Live wallet balance — drives the pill on the topbar.
  // `placeholderData` paints the last-known balance from localStorage so the
  // pill never flashes ₹0 between login and the first /wallet/summary
  // response. We persist on every fresh fetch so the snapshot stays current
  // across refreshes/tabs.
  const { data: wallet, isLoading: walletLoading } = useQuery({
    queryKey: ["wallet", "summary"],
    queryFn: async () => {
      const s = await WalletAPI.summary();
      writeWalletSnapshot(s);
      return s;
    },
    refetchInterval: 8000,
    placeholderData: () => readWalletSnapshot(),
  });
  const hasBalance = wallet?.available_balance != null;
  const balance = Number(wallet?.available_balance ?? 0);

  // The app no longer uses `viewport-fit=cover` / `black-translucent`, so iOS
  // reserves the status-bar region itself and `env(safe-area-inset-top)`
  // resolves to 0 in the installed PWA — the header sits at a clean 3.5rem
  // below the OS status bar. The inset terms below are kept as a harmless
  // belt-and-braces in case cover is ever re-enabled. `backdrop-blur` is
  // dropped and a solid bg used
  // instead — a full-width sticky blur bar forces iOS Safari to re-composite
  // the whole viewport every scroll/route frame, which is the iPhone-only
  // jank / "page won't switch" slowness (Android Chrome composites it
  // cheaply). Solid bar = same look, no per-frame GPU cost.
  return (
    <header
      className="sticky top-0 z-20 flex items-center gap-2 border-b border-border bg-background px-3 md:px-4"
      style={{
        paddingTop: "env(safe-area-inset-top)",
        height: "calc(3.5rem + env(safe-area-inset-top))",
      }}
    >
      {/* Mobile-only brand (sidebar is hidden ≤ md) */}
      <div className="md:hidden">
        <BrandLogo size="sm" />
      </div>

      {/* Desktop search */}
      <div className="relative hidden flex-1 md:block">
        <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          placeholder="Search RELIANCE, NIFTY, BANKNIFTY…"
          className="h-9 max-w-md pl-9"
          aria-label="Search instruments"
        />
      </div>

      {/* Wallet balance pill — always visible, click → /wallet. While the
          first /wallet/summary is still loading and we have no cached
          snapshot to fall back on, show a dim ellipsis instead of "₹0" so
          the user doesn't briefly think their wallet is empty. */}
      <Link
        href="/wallet"
        className="ml-auto inline-flex max-w-[55vw] items-center gap-1.5 truncate rounded-full border border-primary/20 bg-primary/5 px-2.5 py-1.5 text-xs font-semibold text-primary transition-colors hover:bg-primary/10 sm:max-w-none sm:gap-2 sm:px-3"
      >
        <Wallet className="size-3.5 shrink-0" />
        <span className="hidden text-[10px] font-medium uppercase tracking-wider text-muted-foreground sm:inline">
          Wallet
        </span>
        <span
          className={cn(
            "truncate font-tabular",
            !hasBalance && walletLoading && "text-muted-foreground/60",
          )}
        >
          {hasBalance ? formatINR(balance) : walletLoading ? "₹ —" : formatINR(balance)}
        </span>
      </Link>

      {/* Promo button — super-admin-managed blinking CTA. Renders nothing
          unless the super-admin turned it ON with a URL in Platform Settings. */}
      <PromoButton />

      {/* Notification bell — visible on mobile + desktop. */}
      <Button variant="ghost" size="icon" aria-label="Notifications" asChild>
        <Link href="/notifications">
          <Bell className="size-4" />
        </Link>
      </Button>

      {/* WhatsApp support — visible on mobile + desktop. Renders nothing
          when admin hasn't saved a WhatsApp number, so the bar stays
          clean.  Promoted out of the desktop-only cluster on user
          request: mobile clients couldn't reach their broker from the
          header before this. */}
      <SupportShortcut />

      {/* ── Desktop-only cluster ────────────────────────────────
         ThemeToggle / Profile / Logout. Mobile users get these from
         the Profile bottom-nav tab so the header stays uncluttered. */}
      <div className="hidden items-center gap-1 md:flex">
        <ThemeToggle />
        <Button variant="ghost" size="icon" aria-label="Profile" asChild>
          <Link href="/profile">
            <UserIcon className="size-4" />
          </Link>
        </Button>
        <Button
          variant="ghost"
          size="icon"
          aria-label="Sign out"
          onClick={() => logout().then(() => (window.location.href = "/login"))}
          title={user ? `Sign out ${user.full_name}` : "Sign out"}
        >
          <LogOut className="size-4" />
        </Button>
      </div>
    </header>
  );
}

/**
 * Support shortcut — WhatsApp only. Renders nothing when the admin
 * hasn't set a WhatsApp number. Clicking opens wa.me with a prefilled
 * message in a new tab.
 */
function SupportShortcut() {
  const { data: support } = useSupportContacts();
  const waUrl = buildWhatsappUrl(
    support?.whatsapp,
    "Hi, I need help with my MarginWealth account",
  );
  if (!waUrl) return null;
  return (
    <Button
      variant="ghost"
      size="icon"
      aria-label="Contact support on WhatsApp"
      title="Contact support on WhatsApp"
      asChild
    >
      <a href={waUrl} target="_blank" rel="noopener noreferrer">
        <WhatsAppIcon className="size-4 text-[#25D366]" />
      </a>
    </Button>
  );
}

/**
 * Promo button — a super-admin-managed blinking CTA in the header, shown to
 * EVERY user. Config comes from Platform Settings (promo.button_*). Renders
 * nothing unless it's enabled AND has a URL, so the header stays clean when
 * it's off. Polls once a minute so an admin toggle propagates without a
 * reload.
 */
type PromoConfig = { enabled: boolean; url: string; label: string };

function PromoButton() {
  const { data } = useQuery<PromoConfig>({
    queryKey: ["promo-button"],
    queryFn: () => unwrap<PromoConfig>(api.get("/user/support/promo")),
    // Short poll + always-refetch-on-mount/focus so a super-admin toggle (esp.
    // turning it OFF) clears within ~10 s and instantly on any nav / tab focus,
    // instead of lingering for a minute. Never persisted stale: refetchOnMount
    // "always" re-checks immediately even after a cache rehydrate.
    staleTime: 0,
    gcTime: 0,
    refetchInterval: 10_000,
    refetchIntervalInBackground: true,
    refetchOnMount: "always",
    refetchOnWindowFocus: true,
  });
  if (!data?.enabled || !data.url) return null;
  const href = /^https?:\/\//i.test(data.url) ? data.url : `https://${data.url}`;
  const label = (data.label || "Offer").trim() || "Offer";
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      title={label}
      className="inline-flex shrink-0 animate-pulse items-center gap-1 rounded-full bg-gradient-to-r from-orange-500 to-rose-600 px-2.5 py-1 text-[11px] font-extrabold uppercase tracking-wide text-white shadow-md shadow-rose-500/40 ring-1 ring-white/30 transition-transform hover:scale-105"
    >
      <Megaphone className="size-3 shrink-0" />
      <span className="max-w-[24vw] truncate sm:max-w-none">{label}</span>
    </a>
  );
}
