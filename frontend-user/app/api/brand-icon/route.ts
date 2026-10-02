import { NextRequest } from "next/server";

/**
 * Same-origin icon proxy for PWA manifest.
 *
 * Chrome Android prioritises same-origin icons over cross-origin ones.
 * Admin logos live on api.marginwealth.live (cross-origin), so Chrome
 * always picked the platform default /icons/icon-192.png instead.
 *
 * This route proxies the admin logo through the Next.js server so the
 * manifest can reference `/api/brand-icon?u=ADM123` (same-origin) and
 * Chrome picks it up as the PWA launcher icon.
 *
 * GET /api/brand-icon?u=<USER_CODE>
 */

export const dynamic = "force-dynamic";

const API_BASE = (process.env.NEXT_PUBLIC_API_URL || "").replace(/\/+$/, "");

export async function GET(req: NextRequest) {
  const userCode = (req.nextUrl.searchParams.get("u") || "").trim().toUpperCase();
  if (!userCode || !API_BASE) {
    return new Response("Missing user code", { status: 400 });
  }

  try {
    const brandRes = await fetch(
      `${API_BASE}/api/v1/branding/by-code/${encodeURIComponent(userCode)}`,
      { cache: "no-store", headers: { Accept: "application/json" } },
    );
    if (!brandRes.ok) {
      return new Response("Branding not found", { status: 404 });
    }
    const json = await brandRes.json();
    const logoUrl = json?.data?.logo_url;
    if (!logoUrl) {
      return new Response("No logo configured", { status: 404 });
    }

    const imgRes = await fetch(`${API_BASE}${logoUrl}`, { cache: "no-store" });
    if (!imgRes.ok) {
      return new Response("Logo fetch failed", { status: 502 });
    }

    const contentType = imgRes.headers.get("content-type") || "image/png";
    const buffer = await imgRes.arrayBuffer();

    return new Response(buffer, {
      headers: {
        "Content-Type": contentType,
        "Cache-Control": "public, max-age=3600",
      },
    });
  } catch {
    return new Response("Proxy error", { status: 500 });
  }
}
