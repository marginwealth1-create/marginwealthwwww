"use client";

import { useMemo, useState } from "react";
import { Check, Copy, ExternalLink, Smartphone } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { BrandingAPI } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/common/PageHeader";

/**
 * "Download App" — gives the admin/broker a single shareable link they can
 * send to clients. Opening the link shows ONLY the broker's logo + brand
 * name + a bold "Download App" button that installs the web app (PWA).
 *
 * Link shapes (mirrors the Branding page's signup/login link resolution):
 *   • Platform host : <user-app-origin>/download?ref=<user_code>
 *   • Custom domain : https://<domain>/download        (branding by host)
 */
export default function DownloadAppPage() {
  const meQuery = useQuery({
    queryKey: ["admin", "branding", "me"],
    queryFn: () => BrandingAPI.me(),
  });

  const userCode = (meQuery.data as any)?.user_code ?? "";
  const domainSaved = ((meQuery.data as any)?.custom_domain ?? "").trim();
  const status = (meQuery.data as any)?.custom_domain_status ?? null;

  // Client-facing origin — the END-USER app, not the admin host. Same
  // resolution the Branding page uses: env override → strip "admin." →
  // fallback to marginwealth.com.
  const platformOrigin = useMemo(() => {
    const fromEnv = (process.env.NEXT_PUBLIC_USER_APP_URL || "").trim();
    if (fromEnv) return fromEnv.replace(/\/+$/, "").replace(/^http:/, "https:");
    if (typeof window === "undefined") return "https://marginwealth.com";
    const url = new URL(window.location.origin);
    url.protocol = "https:";
    if (url.hostname.startsWith("admin.")) {
      url.hostname = url.hostname.slice("admin.".length);
    }
    return url.origin;
  }, []);

  const platformLink = userCode
    ? `${platformOrigin}/download?ref=${userCode}`
    : "";
  const customReady = domainSaved && status === "READY";
  const customLink = customReady ? `https://${domainSaved}/download` : "";

  return (
    <div className="space-y-5">
      <PageHeader
        title="Download App"
        description="Share this link with your clients — it opens a page with just your logo, your brand name, and a Download button that installs your web app."
      />

      {meQuery.isLoading ? (
        <div className="rounded-lg border border-border bg-card px-4 py-12 text-center text-sm text-muted-foreground">
          Loading…
        </div>
      ) : !userCode ? (
        <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-4 py-6 text-sm text-amber-600 dark:text-amber-400">
          Your account has no user code yet — the share link can&apos;t be built.
          Set up your brand under <strong>Branding</strong> first.
        </div>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {/* Custom-domain link — the cleanest, fully-branded URL. Only
              shown once the domain's SSL is READY. */}
          {customReady && (
            <LinkCard
              badge="Your domain"
              highlight
              title="Branded download link"
              note="Cleanest link — your own domain, your branding, no code in the URL."
              url={customLink}
            />
          )}

          {/* Platform link with ?ref= — always available the moment the
              admin has a user_code. */}
          <LinkCard
            badge="Ready to share"
            title="Download link"
            note="Send this on WhatsApp, SMS or bio. Opens your branded install page."
            url={platformLink}
          />

          {/* How it works — kept short + bold so it's scannable. */}
          <div className="rounded-lg border border-border bg-card p-4 lg:col-span-2">
            <div className="flex items-center gap-2 text-sm font-bold text-foreground">
              <Smartphone className="size-4 text-primary" /> What your client sees
            </div>
            <ul className="mt-2.5 space-y-1.5 text-sm text-muted-foreground">
              <li>• Only your <strong className="text-foreground">logo</strong> and <strong className="text-foreground">brand name</strong> — nothing else.</li>
              <li>• One bold <strong className="text-foreground">Download App</strong> button — installs the web app instantly (Android &amp; iPhone).</li>
              <li>• No Play Store / App Store, no signup to install.</li>
            </ul>
            {platformLink && (
              <Button asChild variant="outline" size="sm" className="mt-3.5 gap-1.5">
                <a href={platformLink} target="_blank" rel="noopener noreferrer">
                  <ExternalLink className="size-3.5" /> Preview the page
                </a>
              </Button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/** A copyable share-link card with copy + open actions. */
function LinkCard({
  badge,
  title,
  note,
  url,
  highlight,
}: {
  badge: string;
  title: string;
  note: string;
  url: string;
  highlight?: boolean;
}) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      // Clipboard blocked (insecure origin / permissions) — select fallback.
      window.prompt("Copy this link:", url);
    }
  }

  return (
    <div
      className={
        "flex flex-col rounded-lg border bg-card p-4 " +
        (highlight ? "border-primary/50" : "border-border")
      }
    >
      <span
        className={
          "w-fit rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide " +
          (highlight
            ? "bg-primary/15 text-primary"
            : "bg-muted text-muted-foreground")
        }
      >
        {badge}
      </span>
      <h3 className="mt-2 text-sm font-bold text-foreground">{title}</h3>
      <p className="mt-0.5 text-xs text-muted-foreground">{note}</p>

      <div className="mt-3 flex items-center gap-2 rounded-md border border-border bg-background px-3 py-2">
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-foreground" title={url}>
          {url || "—"}
        </span>
      </div>

      <div className="mt-3 flex gap-2">
        <Button onClick={copy} size="sm" className="flex-1 gap-1.5 font-semibold" disabled={!url}>
          {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
          {copied ? "Copied!" : "Copy link"}
        </Button>
        <Button asChild variant="outline" size="sm" className="gap-1.5" disabled={!url}>
          <a href={url || "#"} target="_blank" rel="noopener noreferrer">
            <ExternalLink className="size-4" /> Open
          </a>
        </Button>
      </div>
    </div>
  );
}
