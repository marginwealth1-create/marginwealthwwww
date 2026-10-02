"use client";

import { Suspense, useState } from "react";
import { usePathname, useSearchParams } from "next/navigation";
import Link from "next/link";
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

  // One plain centred column — no card, no side panel. Register gets a
  // little more width for its two-up name fields.
  return (
    <main className="flex min-h-screen flex-col items-center bg-background px-5">
      <div
        className={cn(
          "flex w-full flex-1 flex-col justify-center py-12",
          pathname === "/register" ? "max-w-md" : "max-w-sm",
        )}
      >
        <div className="mb-10 flex justify-center">{brandMark}</div>
        {children}
      </div>
      <p className="py-6 text-xs text-muted-foreground">
        &copy; {new Date().getFullYear()} {tenantName || platformName}
      </p>
    </main>
  );
}
