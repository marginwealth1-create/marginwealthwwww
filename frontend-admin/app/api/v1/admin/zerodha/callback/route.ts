import { NextResponse, type NextRequest } from "next/server";

/**
 * Zerodha Kite Connect OAuth proxy (admin frontend).
 *
 * Mirrors the user-frontend handler so the OAuth callback succeeds whether the
 * admin pasted the admin-frontend port (3001), user-frontend port (3000), or
 * the backend port (8000) into developers.kite.trade. We forward the
 * `request_token` query param to the backend's GET callback and propagate
 * its redirect back to the browser.
 */
const BACKEND_URL =
  process.env.BACKEND_URL ||
  process.env.NEXT_PUBLIC_API_URL ||
  "http://localhost:8000";

export async function GET(req: NextRequest) {
  const incoming = new URL(req.url);
  const target = new URL(
    "/api/v1/admin/zerodha/callback",
    BACKEND_URL.replace(/\/$/, "")
  );
  incoming.searchParams.forEach((value, key) => target.searchParams.set(key, value));

  let location: string | null = null;
  try {
    const res = await fetch(target.toString(), { redirect: "manual" });
    location = res.headers.get("location");
  } catch (e) {
    const here = `${incoming.protocol}//${incoming.host}`;
    return NextResponse.redirect(
      `${here}/zerodha?error=${encodeURIComponent(`Backend unreachable: ${(e as Error).message}`)}`
    );
  }

  if (!location) {
    const here = `${incoming.protocol}//${incoming.host}`;
    return NextResponse.redirect(`${here}/zerodha?error=callback_proxy_no_redirect`);
  }
  return NextResponse.redirect(location);
}
