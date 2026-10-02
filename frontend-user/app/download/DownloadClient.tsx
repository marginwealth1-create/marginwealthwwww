"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { API_URL, APP_NAME } from "@/lib/constants";
import { InstallPwaButton } from "@/components/common/InstallPwaButton";

/**
 * Client half of the public "Download App" landing page. Renders the
 * broker's logo + brand name + a bold "Download App" (PWA install) button.
 *
 * NOTE: the SERVER component (`page.tsx`) owns `generateMetadata()`, which
 * resolves the SAME branding server-side so the Open Graph link preview
 * (WhatsApp / Telegram / iMessage) shows the tenant's brand — crawlers don't
 * run this client fetch, so without the server metadata every shared link
 * fell back to the platform default ("MarginWealth") logo + name.
 */
type Branding = {
  brand_name: string | null;
  logo_url: string | null;
};

async function fetchBranding(path: string): Promise<Branding | null> {
  try {
    const res = await fetch(`${API_URL}/api/v1${path}`, {
      headers: { "Content-Type": "application/json" },
      credentials: "omit",
    });
    if (!res.ok) return null;
    const body = await res.json();
    return (body?.data ?? null) as Branding | null;
  } catch {
    return null;
  }
}

/** Turn a stored logo path (`/uploads/logos/x.png`) into a loadable URL. */
function resolveLogo(url: string | null | undefined): string | null {
  if (!url) return null;
  return url.startsWith("http") ? url : `${API_URL}${url}`;
}

export function DownloadClient() {
  return (
    <Suspense fallback={<Splash />}>
      <DownloadInner />
    </Suspense>
  );
}

function DownloadInner() {
  const searchParams = useSearchParams();
  const ref = searchParams?.get("ref")?.trim() || "";
  const [brand, setBrand] = useState<Branding | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      // Resolution order mirrors the BrandingProvider: explicit ?ref= code
      // first, then the custom-domain host, then the platform default.
      let b: Branding | null = null;
      if (ref) b = await fetchBranding(`/branding/by-code/${encodeURIComponent(ref)}`);
      if (!b && typeof window !== "undefined") {
        const host = window.location.hostname;
        const isPlatform = /(^|\.)marginwealth\.live$/i.test(host) ||
          host === "localhost" || host === "127.0.0.1" ||
          /\.(vercel|netlify|fly)\.(app|dev)$/i.test(host);
        if (!isPlatform) {
          b = await fetchBranding(`/branding/by-domain?domain=${encodeURIComponent(host)}`);
        }
      }
      if (!b) b = await fetchBranding(`/branding/platform`);
      if (!cancelled) {
        setBrand(b);
        setLoaded(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [ref]);

  const name = (brand?.brand_name?.trim() || APP_NAME) as string;
  const logo = resolveLogo(brand?.logo_url);

  if (!loaded) return <Splash />;

  return (
    <main className="flex min-h-[100dvh] flex-col items-center justify-center bg-background px-6 py-10">
      <div className="flex w-full max-w-sm flex-col items-center text-center">
        {/* Brand logo — falls back to the bold first letter in a rounded
            tile when the broker hasn't uploaded a logo yet. */}
        {logo ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={logo}
            alt={name}
            className="h-20 w-20 rounded-2xl object-contain shadow-sm sm:h-24 sm:w-24"
          />
        ) : (
          <div className="grid h-20 w-20 place-items-center rounded-2xl bg-primary/10 text-3xl font-extrabold text-primary sm:h-24 sm:w-24">
            {name.charAt(0).toUpperCase()}
          </div>
        )}

        {/* Brand name — the one bold headline. */}
        <h1 className="mt-5 text-2xl font-extrabold tracking-tight text-foreground sm:text-3xl">
          {name}
        </h1>

        <p className="mt-2 text-sm font-medium text-muted-foreground">
          Install the app to start trading
        </p>

        {/* The one action — bold, full-width, instant PWA install. */}
        <div className="mt-8 w-full">
          <InstallPwaButton
            label="Download App"
            className="h-14 w-full rounded-xl text-base font-bold"
          />
        </div>

        <p className="mt-4 text-xs font-medium text-muted-foreground">
          Works on Android &amp; iPhone · No app store needed
        </p>
      </div>
    </main>
  );
}

function Splash() {
  return (
    <main className="grid min-h-[100dvh] place-items-center bg-background">
      <div className="h-8 w-8 animate-spin rounded-full border-2 border-primary/30 border-t-primary" />
    </main>
  );
}
