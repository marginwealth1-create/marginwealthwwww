import { NextResponse, type NextRequest } from "next/server";

/**
 * Zerodha Kite Connect OAuth proxy.
 *
 * Kite redirects the admin's browser here after they sign in with their
 * `request_token` in the query string. Our actual exchange handler lives on
 * the FastAPI backend (different port), so this Route Handler just forwards
 * the request — query string and all — to the backend so the same callback
 * URL works whether the admin pasted the user-frontend port (3000) or the
 * backend port (8000) into developers.kite.trade.
 *
 * The backend's GET /api/v1/admin/zerodha/callback handler exchanges the
 * request_token for an access_token and then issues its own redirect to
 * the admin SPA, which we propagate back to the browser as-is.
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
  // Preserve every query param Kite sent (request_token, action, status, type…)
  incoming.searchParams.forEach((value, key) => target.searchParams.set(key, value));

  // The backend handler responds with a 3xx redirect to the admin SPA. We
  // cannot just return its response (different origin to the user) — instead
  // we issue our own redirect to the same target the backend would have
  // chosen. To do that we follow `redirect: "manual"` and read the Location.
  let location: string | null = null;
  try {
    const res = await fetch(target.toString(), { redirect: "manual" });
    location = res.headers.get("location");
  } catch (e) {
    // Backend offline / unreachable — fall back to a generic error redirect.
    const adminBase = (process.env.NEXT_PUBLIC_ADMIN_URL || "http://localhost:3001").replace(/\/$/, "");
    return NextResponse.redirect(
      `${adminBase}/zerodha?error=${encodeURIComponent(`Backend unreachable: ${(e as Error).message}`)}`
    );
  }

  if (!location) {
    const adminBase = (process.env.NEXT_PUBLIC_ADMIN_URL || "http://localhost:3001").replace(/\/$/, "");
    return NextResponse.redirect(`${adminBase}/zerodha?error=callback_proxy_no_redirect`);
  }
  return NextResponse.redirect(location);
}
