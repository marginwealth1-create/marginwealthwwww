import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

// ─────────────────────────────────────────────────────────────────────
//  Flagship-domain hiding.
//
//  When HIDE_PUBLIC_SITE=true, the public marketing site is made to look
//  "not found" ONLY on the flagship host (marginwealth.com / www) so the
//  platform isn't publicly discoverable via search.  Everything that
//  matters keeps working, untouched:
//    • /login, /register (+ ?ref=), /2fa, /forgot-password
//    • the whole trading app (/dashboard, /terminal, /wallet, …)
//    • api.marginwealth.com + admin.marginwealth.com (separate hosts)
//    • every branded tenant domain (their host isn't the flagship, so
//      this middleware passes through and their marketing still shows)
//
//  Toggle: set HIDE_PUBLIC_SITE=false and rebuild to restore the public
//  site.  (Middleware env is read at build time, so a rebuild is needed.)
// ─────────────────────────────────────────────────────────────────────
const HIDE = process.env.HIDE_PUBLIC_SITE === "true";

const FLAGSHIP_HOSTS = new Set(["marginwealth.com", "www.marginwealth.com"]);

// Platform hosts (flagship + dev/preview). Anything NOT in here is a branded
// tenant domain (trade5x.in, marginx.in, …) — those are login portals, so their
// bare domain opens /login instead of the marketing site.
const PLATFORM_HOSTS = new Set([
  "marginwealth.com",
  "www.marginwealth.com",
  "localhost",
  "127.0.0.1",
]);

function isBrandedHost(host: string): boolean {
  if (!host || PLATFORM_HOSTS.has(host)) return false;
  // Preview/deploy hosts are platform, not tenant domains.
  return !/\.(vercel\.app|netlify\.app|fly\.dev)$/.test(host);
}

// The public site — the ONLY paths that get hidden. Login, register, and
// every authenticated app route are deliberately absent. The landing page
// itself is `/`, handled in isMarketingPath below.
const MARKETING_PREFIXES = [
  "/terms",
  "/privacy-policy",
  "/refund-policy",
];

function isMarketingPath(path: string): boolean {
  if (path === "/") return true;
  return MARKETING_PREFIXES.some((p) => path === p || path.startsWith(`${p}/`));
}

export function middleware(req: NextRequest) {
  const host = (req.headers.get("host") ?? "").toLowerCase().split(":")[0];
  const path = req.nextUrl.pathname;

  // Branded tenant domains are login portals — the bare domain opens /login,
  // not the MarginWealth marketing site. Login / register / the app are untouched.
  if (isBrandedHost(host) && path === "/") {
    const url = req.nextUrl.clone();
    url.pathname = "/login";
    return NextResponse.redirect(url);
  }

  if (!HIDE) return NextResponse.next();
  if (!FLAGSHIP_HOSTS.has(host)) return NextResponse.next();

  // De-index the flagship domain wholesale so search engines drop it.
  if (path === "/robots.txt") {
    return new NextResponse("User-agent: *\nDisallow: /\n", {
      status: 200,
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  }

  // Hide the public marketing pages; leave login / register / the app alone.
  if (isMarketingPath(path)) {
    return new NextResponse(
      '<!doctype html><html><head><meta name="robots" content="noindex,nofollow"><title>Not available</title></head><body style="margin:0;background:#0a0a0a"></body></html>',
      {
        status: 404,
        headers: {
          "content-type": "text/html; charset=utf-8",
          "x-robots-tag": "noindex, nofollow",
        },
      },
    );
  }

  return NextResponse.next();
}

export const config = {
  // Run on everything except Next internals + API routes. The handler
  // itself narrows to the flagship host + marketing paths.
  matcher: ["/((?!_next/|api/).*)"],
};
