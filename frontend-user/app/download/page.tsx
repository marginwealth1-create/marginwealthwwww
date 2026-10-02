import type { Metadata } from "next";
import { headers } from "next/headers";
import { API_URL, APP_NAME } from "@/lib/constants";
import { DownloadClient } from "./DownloadClient";

/**
 * Public, branding-only "Download App" landing page.
 *
 * The link an admin/broker shares with their clients:
 *     https://<host>/download?ref=<user_code>
 *     https://<their-custom-domain>/download        (branding by domain)
 *
 * This is a SERVER component so `generateMetadata()` can resolve the tenant's
 * branding server-side and emit the correct Open Graph tags — WhatsApp /
 * Telegram / iMessage crawlers read the rendered <head> WITHOUT running JS,
 * so the client-side brand fetch alone never reached the link preview and
 * every shared link fell back to the platform default ("MarginWealth") logo +
 * name. The visible UI still hydrates via <DownloadClient>.
 */
type Branding = {
  brand_name: string | null;
  logo_url: string | null;
};

async function fetchBranding(path: string): Promise<Branding | null> {
  try {
    const res = await fetch(`${API_URL}/api/v1${path}`, {
      headers: { "Content-Type": "application/json" },
      // Server-to-server; cache briefly so brand edits surface within minutes
      // without hammering the API on every crawler hit.
      next: { revalidate: 300 },
    });
    if (!res.ok) return null;
    const body = await res.json();
    return (body?.data ?? null) as Branding | null;
  } catch {
    return null;
  }
}

function isPlatformHost(host: string): boolean {
  const h = host.toLowerCase().split(":")[0];
  return (
    /(^|\.)marginwealth\.live$/.test(h) ||
    h === "localhost" ||
    h === "127.0.0.1" ||
    /\.(vercel|netlify|fly)\.(app|dev)$/.test(h)
  );
}

/** Absolute URL for a stored logo path — OG images MUST be absolute. */
function resolveLogo(url: string | null | undefined): string | null {
  if (!url) return null;
  return url.startsWith("http") ? url : `${API_URL}${url}`;
}

export async function generateMetadata({
  searchParams,
}: {
  searchParams: { ref?: string };
}): Promise<Metadata> {
  const ref = (searchParams?.ref ?? "").trim();
  const host = headers().get("host") ?? "";

  // Same resolution order as the client / BrandingProvider:
  //   ?ref=<code>  →  custom-domain host  →  platform default.
  let brand: Branding | null = null;
  if (ref) brand = await fetchBranding(`/branding/by-code/${encodeURIComponent(ref)}`);
  if (!brand && host && !isPlatformHost(host)) {
    brand = await fetchBranding(
      `/branding/by-domain?domain=${encodeURIComponent(host.split(":")[0])}`,
    );
  }
  if (!brand) brand = await fetchBranding(`/branding/platform`);

  const name = brand?.brand_name?.trim() || APP_NAME;
  const logo = resolveLogo(brand?.logo_url);
  const title = `${name} — Download App`;
  const description = `Install the ${name} app to trade Indian stocks, F&O, commodities, currencies and crypto — fast, transparent, dark-themed.`;

  return {
    title,
    description,
    // Tenant favicon too, so the small link-preview glyph matches the brand.
    ...(logo ? { icons: { icon: logo, shortcut: logo, apple: logo } } : {}),
    openGraph: {
      title,
      description,
      siteName: name,
      type: "website",
      ...(logo ? { images: [{ url: logo, alt: name }] } : {}),
    },
    twitter: {
      card: "summary",
      title,
      description,
      ...(logo ? { images: [logo] } : {}),
    },
  };
}

export default function DownloadPage() {
  return <DownloadClient />;
}
