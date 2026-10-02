import type { Metadata, Viewport } from "next";
import { headers } from "next/headers";
import { Inter, Space_Grotesk, IBM_Plex_Mono } from "next/font/google";
import { Providers } from "./providers";
import { PwaRegister } from "@/components/common/PwaRegister";
import { ThemeColorSync } from "@/components/common/ThemeColorSync";
import { API_URL } from "@/lib/constants";
import "./globals.css";

// Inter is what ChatGPT / Linear / Vercel / Stripe / most modern fintech
// dashboards use. Self-hosted via `next/font` so no extra request hits
// fonts.googleapis at runtime — the .woff2 ships from /_next/static and
// the font-display: swap behaviour comes free with the helper. The full
// 400/500/600/700 range covers every body / button / heading / numeric
// weight used across the app.
const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  weight: ["400", "500", "600", "700"],
  display: "swap",
});

// Marketing-site display + numeric faces. Space Grotesk is the tight,
// geometric heading face from the MarginWealth design system (the brief's
// "Clash Display" alternative, but Google-hosted so it ships via next/font
// with zero runtime CDN hit). IBM Plex Mono carries prices, payouts, and
// stats with a tabular/mono feel. Both are scoped to the marketing pages
// via the `font-display` / `font-numeric` Tailwind families — the trading
// app keeps Inter everywhere.
const spaceGrotesk = Space_Grotesk({
  subsets: ["latin"],
  variable: "--font-space-grotesk",
  weight: ["500", "600", "700"],
  display: "swap",
});

const plexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  variable: "--font-plex-mono",
  weight: ["400", "500", "600"],
  display: "swap",
});

// Hosts that are the platform itself (not a tenant's branded domain).
const PLATFORM_METADATA_HOSTS = new Set([
  "marginwealth.live",
  "www.marginwealth.live",
  "localhost",
  "127.0.0.1",
]);

/**
 * Resolve the brand for the REQUEST host, server-side. This is what makes
 * link-preview cards (WhatsApp / Telegram / Slack — crawlers that read the
 * SSR <head> and never run our client JS) show the TENANT's own name + logo
 * instead of the super-admin "MarginWealth" default. On the platform host it
 * returns the MarginWealth defaults; on a branded domain it fetches the
 * admin's brand_name + logo from the backend.
 */
async function resolveHostBrand(): Promise<{
  platform: boolean;
  name: string;
  logo: string | null;
}> {
  const host = (headers().get("host") ?? "").toLowerCase().split(":")[0];
  const platform =
    PLATFORM_METADATA_HOSTS.has(host) ||
    /\.(vercel|netlify|fly)\.(app|dev)$/.test(host);
  if (platform || !host) {
    return { platform: true, name: "MarginWealth", logo: null };
  }
  try {
    const res = await fetch(
      `${API_URL}/api/v1/branding/by-domain?domain=${encodeURIComponent(host)}`,
      // Cache the branding lookup 5 min so we don't hit the backend on every
      // crawl / page render; branding changes at most a few times a day.
      { next: { revalidate: 300 } },
    );
    if (res.ok) {
      const body = await res.json();
      const b = body?.data;
      const name = (b?.brand_name ?? "").trim();
      const logo = b?.logo_url ? `${API_URL}${b.logo_url}` : null;
      return { platform: false, name, logo };
    }
  } catch {
    /* fall through to the neutral branded default below */
  }
  return { platform: false, name: "", logo: null };
}

export async function generateMetadata(): Promise<Metadata> {
  const { platform, name, logo } = await resolveHostBrand();
  const displayName = name || (platform ? "MarginWealth" : "Trading Platform");
  const title = platform
    ? "MarginWealth — Indian Trading Platform"
    : `${displayName} — Trading Platform`;
  const description = platform
    ? "Trade Indian stocks, F&O, commodities, currencies, and crypto with MarginWealth — fast, transparent, dark-themed."
    : `Trade Indian stocks, F&O, commodities, currencies, and crypto with ${displayName} — fast, transparent, dark-themed.`;
  // OG/favicon image: the tenant's uploaded logo on a branded domain, else the
  // platform febicon. Absolute URL required so crawlers can fetch it.
  const image = logo || (platform ? "/febicon.png" : null);
  const iconSet = logo
    ? { icon: logo, shortcut: logo, apple: logo }
    : { icon: "/febicon.png", shortcut: "/febicon.png", apple: "/febicon.png" };

  return {
    title: {
      default: title,
      template: `%s · ${displayName}`,
    },
    description,
    icons: iconSet,
    applicationName: displayName,
    appleWebApp: {
      capable: true,
      title: displayName,
      // `default` (NOT `black-translucent`) so iOS reserves the status-bar
      // region in the installed PWA and the web canvas starts BELOW the clock.
      statusBarStyle: "default",
    },
    manifest: "/manifest.webmanifest",
    // The link-preview card crawlers read. siteName/title/image drive the
    // WhatsApp/Telegram preview — this is the actual fix for "register link
    // shares with MarginWealth's name + logo instead of the admin's".
    openGraph: {
      type: "website",
      siteName: displayName,
      title,
      description,
      images: image ? [{ url: image }] : [],
    },
    twitter: {
      card: "summary",
      title,
      description,
      images: image ? [image] : [],
    },
    // Disable every browser's "Translate page?" infobar — the trading numbers
    // MUST NOT be auto-translated (BUY/SELL → kharidein/bechein) because the
    // broker UI relies on exact English labels for SOPs / screen-shares.
    other: {
      google: "notranslate",
      googlebot: "notranslate",
      yandex: "notranslate",
      "content-language": "en",
      "google-site-verification": "",
      "mobile-web-app-capable": "yes",
    },
  };
}

export const viewport: Viewport = {
  // Match the OS chrome (Android status bar / Chrome address bar) to
  // the app's current theme instead of the brand emerald everywhere.
  // The two media-keyed entries let the BROWSER pick the right one
  // when there's no user-set theme; the <ThemeColorSync> client
  // component below overrides this in real time when the user flips
  // light/dark from Profile → Preferences so the top safe-area band
  // never stays green on a light-theme app screen.
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
    { media: "(prefers-color-scheme: dark)", color: "#0a0a0a" },
  ],
  width: "device-width",
  initialScale: 1,
  // Lock pinch-zoom / double-tap-zoom on mobile so the installed PWA
  // (and the mobile browser view) feels like a native shell — user
  // spec: "zoom in zoom out mat ho". Desktop browsers ignore these,
  // accessibility users can still use Ctrl/Cmd-+/- which `userScalable`
  // doesn't block.
  maximumScale: 1,
  minimumScale: 1,
  userScalable: false,
  // Deliberately NOT `cover`. With `viewport-fit=cover` iOS draws the web
  // canvas UNDER the status bar / Dynamic Island, so every top-anchored
  // surface (terminal chart header, option-chain dialog, add-funds wizard)
  // overlapped the clock in the installed PWA. Default fit keeps content
  // inside the safe area — iOS reserves a solid themeColor status-bar band
  // on top and nothing overlaps, on any page or popup.
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  // `translate="no"` on <html> is the canonical W3C signal to all
  // translation engines (Chrome, Edge, Safari, third-party widgets)
  // that the page must not be auto-translated. Combined with the
  // <meta name="google" content="notranslate" /> in metadata.other,
  // it prevents the "Translate page?" popup from showing on refresh
  // — important because mistranslated trading labels (BUY → kharidein)
  // would be unsafe.
  return (
    <html
      lang="en"
      translate="no"
      suppressHydrationWarning
      className={`notranslate ${inter.variable} ${spaceGrotesk.variable} ${plexMono.variable}`}
    >
      <body className="notranslate font-sans antialiased" translate="no">
        <PwaRegister />
        <ThemeColorSync />
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
