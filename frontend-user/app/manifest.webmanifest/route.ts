import { NextRequest } from "next/server";

// Dynamic Web App Manifest for the user-facing frontend.
//
// PWA installs commit the manifest's name + icons into the OS launcher at
// install time. To get a per-tenant install icon (so end users who reach a
// broker's branded site install THAT broker's PWA, not the platform
// default) we resolve the tenant two ways:
//
//   1. `?u=<USER_CODE>` — set by BrandingProvider once branding resolves
//      client-side (covers `?ref=` platform links).
//   2. The request HOST — for a tenant custom domain (e.g. stockcafe.live)
//      we resolve the admin by domain. THIS is the install-time path that
//      matters: Chrome reads the param-less `/manifest.webmanifest` BEFORE
//      our JS can swap in `?u=`, so without host resolution the install
//      captured the PLATFORM identity (wrong icon/name) and a not-yet-
//      branded manifest sometimes never became "installable" — so the
//      native prompt never fired and the manual "Add to Home screen" steps
//      showed instead. Resolving by host makes the FIRST read already the
//      tenant's branded, installable PWA.
//
// Falls back to the platform default when neither resolves — byte-identical
// behaviour for non-branded visitors.

export const dynamic = "force-dynamic";
export const revalidate = 0;

const API_BASE = (process.env.NEXT_PUBLIC_API_URL || "").replace(/\/+$/, "");

const PLATFORM_DEFAULT = {
  name: "MarginWealth",
  short_name: "MarginWealth",
  description:
    "Trade Indian stocks, F&O, commodities, currencies, and crypto with MarginWealth.",
  start_url: "/dashboard",
  scope: "/",
  display: "standalone",
  orientation: "portrait" as const,
  background_color: "#0a0a0a",
  theme_color: "#0a0a0a",
  categories: ["finance", "business"],
  icons: [
    { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
    { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
    { src: "/icons/icon-maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
  ],
};

type ResolvedBrand = {
  user_code: string;
  brand_name: string | null;
  logo_url: string | null;
};

async function fetchBrandingByCode(userCode: string): Promise<ResolvedBrand | null> {
  if (!API_BASE || !userCode) return null;
  try {
    const res = await fetch(
      `${API_BASE}/api/v1/branding/by-code/${encodeURIComponent(userCode)}`,
      { cache: "no-store", headers: { Accept: "application/json" } },
    );
    if (!res.ok) return null;
    const data = (await res.json())?.data ?? null;
    if (!data) return null;
    return {
      user_code: String(data.user_code ?? userCode),
      brand_name: data.brand_name ?? null,
      logo_url: data.logo_url ?? null,
    };
  } catch {
    return null;
  }
}

async function fetchBrandingByDomain(domain: string): Promise<ResolvedBrand | null> {
  if (!API_BASE || !domain) return null;
  try {
    const res = await fetch(
      `${API_BASE}/api/v1/branding/by-domain?domain=${encodeURIComponent(domain)}`,
      { cache: "no-store", headers: { Accept: "application/json" } },
    );
    // 404 on the platform host / an unknown domain — fall back to default.
    if (!res.ok) return null;
    const data = (await res.json())?.data ?? null;
    if (!data?.user_code) return null;
    return {
      user_code: String(data.user_code),
      brand_name: data.brand_name ?? null,
      logo_url: data.logo_url ?? null,
    };
  } catch {
    return null;
  }
}

function buildBrandedManifest(b: ResolvedBrand): Record<string, unknown> {
  const code = b.user_code.trim().toUpperCase();
  const name = b.brand_name?.trim() || PLATFORM_DEFAULT.name;
  const shortName = (b.brand_name?.trim() || PLATFORM_DEFAULT.short_name).slice(0, 12);
  const logo = b.logo_url ? `${API_BASE}${b.logo_url}` : null;
  // Same-origin proxy so Chrome Android accepts the launcher icon — it
  // ignores cross-origin icons on api.marginwealth.live and falls back to the
  // platform leaf. `id` keeps each tenant's PWA distinct so installing
  // admin-A doesn't overwrite admin-B on the same origin.
  const proxyIcon = `/api/brand-icon?u=${encodeURIComponent(code)}`;
  const ext = ((logo ?? "").split(".").pop() || "").toLowerCase();
  const mimeType =
    ext === "jpg" || ext === "jpeg" ? "image/jpeg"
    : ext === "svg" ? "image/svg+xml"
    : ext === "webp" ? "image/webp"
    : "image/png";
  const brandedIcons = [
    { src: proxyIcon, sizes: "512x512", type: mimeType, purpose: "any" },
    { src: proxyIcon, sizes: "192x192", type: mimeType, purpose: "any" },
    { src: proxyIcon, sizes: "512x512", type: mimeType, purpose: "maskable" },
  ];
  return {
    ...PLATFORM_DEFAULT,
    // `id` keeps each tenant's PWA distinct on the same origin — it's an
    // internal identifier, not a navigation URL, and the code is resolved
    // per-request from the host (nothing hardcoded).
    id: `/?brand=${encodeURIComponent(code)}`,
    // Open the tenant's LOGIN page, NOT the marketing homepage (`/`). On a
    // custom domain the branding is resolved by host, so the URL needs no
    // ?ref= code — `/login` is generic and works for EVERY admin's domain.
    // Authed users are auto-redirected to /dashboard by the login page.
    start_url: "/login",
    name,
    short_name: shortName,
    description: `${name} — trade Indian markets`,
    icons: logo ? brandedIcons : PLATFORM_DEFAULT.icons,
  };
}

export async function GET(req: NextRequest) {
  const userCode = (req.nextUrl.searchParams.get("u") || "").trim().toUpperCase();
  let resolved: ResolvedBrand | null = null;

  if (userCode) {
    // Explicit override (BrandingProvider sets ?u=<CODE> after it resolves).
    resolved = await fetchBrandingByCode(userCode);
  } else {
    // Install-time path: resolve the tenant from the request host so the
    // very first param-less read Chrome makes is already branded.
    const host = (
      req.headers.get("x-forwarded-host") ||
      req.headers.get("host") ||
      req.nextUrl.host ||
      ""
    )
      .split(",")[0]
      .split(":")[0]
      .trim()
      .toLowerCase();
    if (host) resolved = await fetchBrandingByDomain(host);
  }

  const manifest: Record<string, unknown> =
    resolved && (resolved.brand_name || resolved.logo_url)
      ? buildBrandedManifest(resolved)
      : { ...PLATFORM_DEFAULT };

  return new Response(JSON.stringify(manifest), {
    headers: {
      "Content-Type": "application/manifest+json; charset=utf-8",
      "Cache-Control": "private, no-store, max-age=0",
    },
  });
}
