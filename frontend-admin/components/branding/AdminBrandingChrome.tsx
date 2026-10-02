"use client";

import { useEffect } from "react";
import { useAdminAuthStore } from "@/stores/authStore";
import { API_URL, APP_NAME } from "@/lib/constants";

// Applies tenant white-label chrome (browser tab title + favicon) on
// the admin panel for ADMIN / BROKER users whose authStore carries
// `brand_name` / `logo_url`. Super-admins always see the platform
// default. The favicon swap rewrites every existing
// <link rel="icon"> the SSR HTML shipped with — appending a new node
// alone doesn't visibly change the tab icon because the browser has
// already committed the SSR-supplied icon. We stash the original href
// once on a data-attribute so that signing out / SUPER_ADMIN sessions
// cleanly restore the platform sprout.
//
// Mounted inside `<Providers>` so it lives for the whole admin app.
export function AdminBrandingChrome() {
  const admin = useAdminAuthStore((s) => s.admin);
  const role = admin?.role;
  const brandName = (admin?.brand_name ?? "").trim();
  const logoPath = admin?.logo_url ?? null;
  const userCode = (admin?.user_code ?? "").trim();

  // SUPER_ADMIN never gets tenant chrome — always platform default.
  const isTenant = role === "ADMIN" || role === "BROKER";
  const tenantName = isTenant && brandName ? brandName : null;
  const tenantLogo =
    isTenant && logoPath
      ? logoPath.startsWith("http")
        ? logoPath
        : `${API_URL}${logoPath}`
      : null;

  useEffect(() => {
    if (typeof document === "undefined") return;

    // ── Title ───────────────────────────────────────────────────────
    const title = tenantName || APP_NAME;
    if (document.title !== title) document.title = title;

    // ── Favicon ────────────────────────────────────────────────────
    const head = document.head;
    if (!head) return;
    const icons = head.querySelectorAll<HTMLLinkElement>(
      'link[rel="icon"], link[rel="shortcut icon"], link[rel="apple-touch-icon"]',
    );
    // CRITICAL: mutate href in place — DO NOT clone+replaceWith.
    // Next.js renders these <link> nodes through React's metadata
    // system; removing them out-of-tree leaves the reconciler with
    // dangling refs and crashes the next navigation with
    //   "Cannot read properties of null (reading 'removeChild')".
    icons.forEach((el) => {
      if (!el.dataset.brandingOriginal) {
        el.dataset.brandingOriginal = el.getAttribute("href") || "";
      }
      if (tenantLogo) {
        if (el.getAttribute("href") !== tenantLogo) {
          el.setAttribute("href", tenantLogo);
        }
        el.setAttribute("data-branding", "1");
      } else {
        const original = el.dataset.brandingOriginal || "";
        if (original && el.getAttribute("href") !== original) {
          el.setAttribute("href", original);
        }
        el.removeAttribute("data-branding");
      }
    });
    // Append a SINGLE branding-owned <link> at the end of <head> so
    // browsers (which honour the LAST applicable icon) update the tab
    // immediately. This node is outside the React tree and safe to
    // mutate / remove without breaking reconciliation.
    const OWN_ID = "branding-favicon-runtime";
    const existing = head.querySelector<HTMLLinkElement>(`link#${OWN_ID}`);
    if (tenantLogo) {
      if (existing) {
        if (existing.getAttribute("href") !== tenantLogo) {
          existing.setAttribute("href", tenantLogo);
        }
      } else {
        const link = document.createElement("link");
        link.id = OWN_ID;
        link.rel = "icon";
        link.href = tenantLogo;
        if (tenantLogo.endsWith(".svg")) link.type = "image/svg+xml";
        else if (tenantLogo.endsWith(".png")) link.type = "image/png";
        else if (tenantLogo.endsWith(".webp")) link.type = "image/webp";
        else if (tenantLogo.endsWith(".jpg") || tenantLogo.endsWith(".jpeg"))
          link.type = "image/jpeg";
        head.appendChild(link);
      }
    } else if (existing) {
      existing.remove();
    }

    // ── PWA manifest ───────────────────────────────────────────────
    // Point the <link rel="manifest"> at the per-tenant URL so when the
    // admin clicks "Install app" the OS launcher picks up THEIR brand
    // name + logo. Super-admins / pre-login keep the platform default.
    const manifestEl = head.querySelector<HTMLLinkElement>('link[rel="manifest"]');
    if (manifestEl) {
      const desired = isTenant && userCode
        ? `/manifest.webmanifest?u=${encodeURIComponent(userCode)}`
        : "/manifest.webmanifest";
      if (manifestEl.getAttribute("href") !== desired) {
        manifestEl.setAttribute("href", desired);
      }
    }
  }, [tenantName, tenantLogo, isTenant, userCode]);

  return null;
}
