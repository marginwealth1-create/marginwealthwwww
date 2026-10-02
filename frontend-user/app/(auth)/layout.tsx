"use client";

import { Suspense, useState } from "react";
import { usePathname, useSearchParams } from "next/navigation";
import Link from "next/link";
import { Lock, ShieldCheck } from "lucide-react";
import { BrandGlyph } from "@/components/layout/BrandGlyph";
import { Wordmark } from "@/components/layout/Wordmark";
import { useBranding } from "@/lib/branding-context";
import { API_URL } from "@/lib/constants";
import { cn } from "@/lib/utils";

/**
 * Tenant brand tile — ALWAYS renders the brand glyph on the brand
 * gradient so something is visible no matter what.  When the admin
 * has uploaded a logo AND the image successfully loads, it's
 * rendered on top of the glyph (covers it).  If the image fails
 * to load or hasn't been configured, the user still sees the
 * original glyph-on-gradient look from before the logo feature
 * existed.  This guarantees zero blank tiles in production.
 */
function BrandTile({
  logoSrc,
  alt,
  size = "lg",
}: {
  logoSrc: string | null;
  alt: string;
  size?: "sm" | "lg";
}) {
  const [imgLoaded, setImgLoaded] = useState(false);
  const sizeCls = size === "lg" ? "size-16" : "size-10";
  const iconCls = size === "lg" ? "size-8" : "size-5";
  return (
    <div
      className={cn(
        "relative grid place-items-center overflow-hidden rounded-2xl bg-foreground text-background shadow-lg shadow-foreground/20 ring-1 ring-foreground/10",
        sizeCls,
      )}
    >
      <BrandGlyph className={iconCls} />
      {logoSrc && (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={logoSrc}
          alt={alt}
          onLoad={() => setImgLoaded(true)}
          onError={() => setImgLoaded(false)}
          className={cn(
            "absolute inset-0 rounded-2xl bg-card object-contain p-1 transition-opacity",
            imgLoaded ? "opacity-100" : "opacity-0",
          )}
        />
      )}
    </div>
  );
}

export default function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <Suspense
      fallback={<main className="min-h-screen w-full bg-background" />}
    >
      <AuthLayoutInner>{children}</AuthLayoutInner>
    </Suspense>
  );
}

function AuthLayoutInner({ children }: { children: React.ReactNode }) {
  const searchParams = useSearchParams();
  const pathname = usePathname() || "";
  const { branding, showPlatformDefault } = useBranding();
  const tenantName = (branding?.brand_name ?? "").trim();
  // On a tenant's branded domain the super-admin "MarginWealth" name/logo must
  // never appear. Use it only on the confirmed platform host; elsewhere fall
  // back to empty / a neutral glyph until the tenant brand resolves.
  const platformName = showPlatformDefault ? "MarginWealth" : "";
  // Tenant logo uploaded by admin / super-admin via /settings/branding.
  // Mirrors BrandLogo.tsx: paths are server-relative, so prefix API_URL.
  // Falls back to the default brand glyph when no logo is configured.
  const logoSrc = branding?.logo_url
    ? `${API_URL}${branding.logo_url}`
    : null;

  const isImpersonating = !!(
    searchParams?.get("access") && searchParams?.get("refresh")
  );

  if (isImpersonating) {
    return (
      <main className="grid min-h-screen w-full place-items-center bg-background">
        {children}
      </main>
    );
  }

  const brandMark = (
    <Link href="/" className="inline-flex items-center gap-2.5">
      {logoSrc ? (
        <>
          <BrandTile logoSrc={logoSrc} alt={tenantName || "Logo"} size="sm" />
          <span className="text-lg font-bold tracking-tight text-foreground">
            {tenantName || platformName}
          </span>
        </>
      ) : showPlatformDefault ? (
        // Platform host only — the super-admin's MarginWealth lockup.
        <Wordmark className="text-foreground" markClassName="size-8" textClassName="text-xl" />
      ) : (
        // Branded domain (or brand not resolved yet) — neutral glyph tile,
        // NEVER the MarginWealth logo. The tenant logo replaces it once the
        // /branding/by-domain fetch (or its per-host cache) lands.
        <BrandTile logoSrc={null} alt="" size="sm" />
      )}
    </Link>
  );

  // Full-bleed dark canvas with a centred glass card. `dark` scopes the dark
  // theme tokens to the auth pages, so the shared Input / Button / Label pick
  // them up without per-field overrides. Register gets a wider card for its
  // two-up email + mobile row.
  return (
    <main className="dark relative flex min-h-screen flex-col overflow-hidden bg-[#060914] text-foreground">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_70%_50%_at_10%_0%,rgba(59,91,219,0.32),transparent_60%),radial-gradient(ellipse_50%_45%_at_95%_100%,rgba(230,184,57,0.16),transparent_60%)]"
      />
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 [background-image:linear-gradient(rgba(255,255,255,0.035)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,0.035)_1px,transparent_1px)] [background-size:56px_56px] [mask-image:radial-gradient(ellipse_at_center,black,transparent_75%)]"
      />
      {showPlatformDefault && (
        <BrandGlyph className="pointer-events-none absolute -bottom-24 -right-20 size-[30rem] opacity-[0.06]" />
      )}

      <header className="relative z-10 flex items-center justify-between px-5 py-5 sm:px-10">
        {brandMark}
        {showPlatformDefault && (
          <Link href="/" className="text-sm text-muted-foreground transition-colors hover:text-foreground">
            &larr; Back to website
          </Link>
        )}
      </header>

      <div className="relative z-10 flex flex-1 items-center justify-center px-4 py-6">
        <div
          className={cn(
            "w-full rounded-3xl border border-white/10 bg-[#0B1020]/75 p-6 shadow-2xl shadow-black/50 backdrop-blur-xl sm:p-9",
            pathname === "/register" ? "max-w-md" : "max-w-[25rem]",
          )}
        >
          {children}
        </div>
      </div>

      <footer className="relative z-10 flex flex-wrap items-center justify-center gap-x-5 gap-y-1 px-5 pb-6 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <Lock className="size-3.5" /> 256-bit encrypted
        </span>
        <span className="inline-flex items-center gap-1.5">
          <ShieldCheck className="size-3.5" /> 2FA available
        </span>
        <span>
          &copy; {new Date().getFullYear()} {tenantName || platformName}
        </span>
      </footer>
    </main>
  );
}
