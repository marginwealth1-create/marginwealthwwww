"use client";

import { BrandGlyph } from "./BrandGlyph";
import Link from "next/link";
import { cn } from "@/lib/utils";
import { useBranding } from "@/lib/branding-context";
import { API_URL } from "@/lib/constants";

interface BrandLogoProps {
  href?: string | null;
  size?: "sm" | "md" | "lg";
  iconOnly?: boolean;
  className?: string;
}

// Whitelabel-aware brand mark.
// - When `BrandingProvider` has resolved a tenant brand (via ?ref= or
//   custom domain), we render the admin's uploaded logo + custom brand
//   name. The logo image is given the same size budget as the default
//   icon so layout doesn't shift.
// - When no branding is loaded, we fall back to the default
//   "🌱 MarginWealth" wordmark — keeping the existing UX byte-
//   identical for the bulk of traffic that isn't on a branded link.
export function BrandLogo({ href = "/dashboard", size = "md", iconOnly = false, className }: BrandLogoProps) {
  const { branding, showPlatformDefault } = useBranding();
  const customName = (branding?.brand_name ?? "").trim();
  // logo_url from the API is relative (e.g. "/static/branding/...");
  // prefix with API_URL so it loads from the backend host. Same logic
  // BrandingProvider uses for the favicon.
  const logoSrc = branding?.logo_url ? `${API_URL}${branding.logo_url}` : null;

  const sizes = {
    sm: { wrap: "text-sm", icon: "size-5", badge: "p-1", img: "size-5" },
    md: { wrap: "text-lg", icon: "size-6", badge: "p-1.5", img: "size-6" },
    lg: { wrap: "text-2xl", icon: "size-8", badge: "p-2", img: "size-8" },
  }[size];

  const content = (
    <span className={cn("inline-flex items-center gap-2 font-semibold tracking-tight", sizes.wrap, className)}>
      <span className={cn("rounded-md bg-foreground/[0.06] text-foreground", sizes.badge)}>
        {logoSrc ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={logoSrc}
            alt={customName || "Logo"}
            className={cn(sizes.img, "rounded object-contain")}
          />
        ) : (
          <BrandGlyph className={sizes.icon} />
        )}
      </span>
      {!iconOnly && (
        customName ? (
          <span className="text-foreground">{customName}</span>
        ) : showPlatformDefault ? (
          // Super-admin "MarginWealth" wordmark — ONLY on the platform
          // host. On a tenant's branded domain (or before the host/brand has
          // resolved) we show just the glyph, never MarginWealth.
          <span className="text-foreground">
            Margin<span className="text-brand">Wealth</span>
          </span>
        ) : null
      )}
    </span>
  );

  if (href) {
    return (
      <Link href={href} className="outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-md">
        {content}
      </Link>
    );
  }
  return content;
}
