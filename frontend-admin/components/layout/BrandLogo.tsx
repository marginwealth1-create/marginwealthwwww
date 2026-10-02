"use client";

import { BrandGlyph } from "./BrandGlyph";
import Link from "next/link";
import { cn } from "@/lib/utils";
import { useAdminAuthStore } from "@/stores/authStore";
import { API_URL } from "@/lib/constants";

interface BrandLogoProps {
  href?: string | null;
  size?: "sm" | "md" | "lg";
  showAdminBadge?: boolean;
  className?: string;
}

export function BrandLogo({ href = "/dashboard", size = "md", showAdminBadge = true, className }: BrandLogoProps) {
  const admin = useAdminAuthStore((s) => s.admin);
  const role = admin?.role;
  // Sub-broker = BROKER whose own creator was another broker (parent
  // broker id stamped on the user doc). Only the chip label flips —
  // routes, permissions, and APIs are identical to a top-level broker.
  const isSubBroker = role === "BROKER" && !!admin?.assigned_broker_id;

  // ── White-label branding override ─────────────────────────────────
  // Branding cascade (matches backend `_branding_fields_for`):
  //   - SUPER_ADMIN     → platform default ("MarginWealth")
  //   - ADMIN           → their OWN brand_name + logo_url
  //   - BROKER / sub-broker → INHERITS parent admin's brand (backend
  //                            already resolves this via assigned_admin_id
  //                            and ships the resolved values in the auth
  //                            payload, so the frontend just trusts them).
  // Top-level brokers under super-admin pool have no parent admin, so
  // brand_name/logo_url come back null → platform default — same as
  // super-admin.
  const useTenantBrand =
    (role === "ADMIN" || role === "BROKER") &&
    (!!admin?.brand_name || !!admin?.logo_url);
  const tenantName = useTenantBrand ? admin?.brand_name?.trim() : null;
  const tenantLogo = useTenantBrand && admin?.logo_url
    ? (admin.logo_url.startsWith("http")
        ? admin.logo_url
        : `${API_URL}${admin.logo_url}`)
    : null;

  const sizes = {
    sm: { wrap: "text-sm", icon: "size-5", badge: "p-1", img: "size-7" },
    md: { wrap: "text-lg", icon: "size-6", badge: "p-1.5", img: "size-9" },
    lg: { wrap: "text-2xl", icon: "size-8", badge: "p-2", img: "size-12" },
  }[size];

  // Role-aware chip — colour + label switch by tier so the brand bar
  // mirrors the "Signed in as … · ROLE" line in the top bar.
  //   SUPER_ADMIN → bold brand-blue "Super Admin"
  //   BROKER      → gold "Broker" (or "Sub-broker" when nested)
  //   ADMIN       → red "Admin" (default for any other admin-tier role)
  const badge =
    role === "SUPER_ADMIN"
      ? { label: "Super Admin", cls: "bg-primary/15 font-bold text-primary" }
      : role === "BROKER"
        ? {
            label: isSubBroker ? "Sub-broker" : "Broker",
            cls: "bg-amber-500/15 font-bold text-amber-500",
          }
        : { label: "Admin", cls: "bg-destructive/15 text-destructive" };

  // Logo block — tenant logo (img) when present, else the platform
  // sprout icon in a tinted tile.
  const logoBlock = tenantLogo ? (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={tenantLogo}
      alt={tenantName || "Brand logo"}
      className={cn("rounded-md object-contain bg-card ring-1 ring-border", sizes.img)}
    />
  ) : (
    <span className={cn("rounded-md bg-foreground/[0.06] text-foreground", sizes.badge)}>
      <BrandGlyph className={sizes.icon} />
    </span>
  );

  // Name block — tenant brand_name (single accent line) when set, else
  // the two-tone "MarginWealth" platform wordmark.
  const nameBlock = tenantName ? (
    <span className="truncate text-foreground">{tenantName}</span>
  ) : (
    <span className="truncate text-foreground">
      Margin<span className="text-brand">Wealth</span>
    </span>
  );

  const content = (
    <span className={cn("inline-flex items-center gap-2 font-semibold tracking-tight", sizes.wrap, className)}>
      {logoBlock}
      <span className="flex min-w-0 flex-col leading-tight">
        {nameBlock}
        {showAdminBadge && (
          <span
            className={cn(
              "mt-0.5 self-start rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wider",
              badge.cls,
            )}
          >
            {badge.label}
          </span>
        )}
      </span>
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
