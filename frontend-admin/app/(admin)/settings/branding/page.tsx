"use client";

/**
 * White-label branding admin page.
 *
 * Layout (top → bottom):
 *   1. **Branding Settings** — logo upload + brand name.
 *   2. **Branded Links** — two sub-sections of copy-able URLs:
 *        a. Platform host (works the moment the admin has a `user_code`,
 *           which every admin already has). Both signup + login carry
 *           `?ref=<user_code>` so the BrandingProvider on the user side
 *           applies the right brand even before login.
 *        b. Custom domain host (only when `custom_domain_status === "READY"`).
 *           No `?ref=` needed because the host itself attributes.
 *   3. **Connect Custom Domain** — Shopify-style 4-step wizard with a
 *      visible progress stepper. Active step is highlighted, completed
 *      steps show a green check, future steps are muted. The current
 *      step's content panel is rendered below the stepper.
 *
 * Status → step mapping:
 *   - no custom_domain                 → Step 1 (Enter Domain)
 *   - custom_domain set, PENDING_DNS   → Step 2 (Add DNS Records)
 *   - PROVISIONING / DNS_VERIFIED      → Step 3 (Verify + Provision SSL)
 *   - READY                            → Step 4 (Complete)
 *   - FAILED                           → stays on the step it failed at
 *                                        with a red error banner
 *
 * Polling: when a provisioning is in flight (PENDING_DNS / DNS_VERIFIED /
 * PROVISIONING), `/domain/status` is polled every 3 s so the UI flips
 * to the next step automatically.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  Copy,
  Globe2,
  Image as ImageIcon,
  Loader2,
  RefreshCw,
  ShieldCheck,
  Unplug,
  Upload,
} from "lucide-react";
import {
  ApiError,
  BrandingAPI,
  type BrandingPayload,
  type DnsPreview,
  type DomainStatus,
} from "@/lib/api";
import { API_URL } from "@/lib/constants";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { PageHeader } from "@/components/common/PageHeader";
import { cn } from "@/lib/utils";
import { useAdminAuthStore } from "@/stores/authStore";

// ─── Constants ────────────────────────────────────────────────────────
const STEPS = [
  { id: 1, label: "Enter Domain" },
  { id: 2, label: "Add DNS Records" },
  { id: 3, label: "Verify & Provision SSL" },
  { id: 4, label: "Complete" },
] as const;

/**
 * Map raw certbot / DNS stderr substrings to friendly, actionable hints
 * the admin can act on without reading Let's Encrypt docs. The first
 * match wins — order from most specific to most generic.
 */
const ERROR_HINTS: Array<{ match: RegExp; hint: string }> = [
  {
    match: /does not point to/i,
    hint:
      "Your DNS A records aren't pointing to our server yet. Verify the values in the table above and wait 5–30 minutes for propagation.",
  },
  {
    match: /(NXDOMAIN|name does not exist)/i,
    hint:
      "The domain doesn't resolve at all yet. Did you add the A records? DNS propagation can take up to 30 minutes.",
  },
  {
    match: /timeout|timed? out/i,
    hint:
      "DNS lookup timed out. Try again in a couple of minutes — your registrar's DNS may be slow.",
  },
  {
    match: /rate limit|too many certificates/i,
    hint:
      "Let's Encrypt rate limit hit (5 certs / week per domain). Wait a few hours and retry, or use a different domain.",
  },
  {
    match: /(unauthorized|invalid response from)/i,
    hint:
      "Let's Encrypt couldn't reach your domain on port 80. Check that nginx is running and port 80 is open in your firewall / security group.",
  },
  {
    match: /(certbot|sudo): not found/i,
    hint:
      "certbot isn't installed on the server, or sudoers isn't configured. Operator: see DEPLOY_BRANDING.md §4.",
  },
  {
    match: /PLATFORM_PUBLIC_IP/i,
    hint:
      "PLATFORM_PUBLIC_IP isn't set in the backend .env. Operator action required.",
  },
];

function friendlyError(raw: string | null | undefined): string | null {
  if (!raw) return null;
  for (const { match, hint } of ERROR_HINTS) {
    if (match.test(raw)) return hint;
  }
  return raw;
}

// ─── Helpers ──────────────────────────────────────────────────────────
function copyToClipboard(text: string, label = "Copied") {
  if (!text) return;
  try {
    navigator.clipboard.writeText(text);
    toast.success(label);
  } catch {
    toast.error("Could not copy");
  }
}

function logoSrc(payload: BrandingPayload | undefined): string | null {
  if (!payload?.logo_url) return null;
  return payload.logo_url.startsWith("http")
    ? payload.logo_url
    : `${API_URL}${payload.logo_url}`;
}

function statusToStep(status: string | null | undefined, hasDomain: boolean): number {
  if (!hasDomain) return 1;
  switch (status) {
    case "PENDING_DNS":
      return 2;
    case "DNS_VERIFIED":
    case "PROVISIONING":
      return 3;
    case "READY":
      return 4;
    case "FAILED":
      // FAILED inherits the step the user was last on; we infer from
      // whether DNS was ever verified (verified_at is set on success too,
      // so we use a simpler proxy: if status was set, they got past step 2).
      return 3;
    default:
      return hasDomain ? 2 : 1;
  }
}

// ─── Sub-components ───────────────────────────────────────────────────
function Stepper({ active }: { active: number }) {
  return (
    <div className="flex items-center gap-1 overflow-x-auto pb-1">
      {STEPS.map((s, i) => {
        const done = active > s.id;
        const current = active === s.id;
        return (
          <div key={s.id} className="flex flex-1 items-center gap-1">
            <div className="flex flex-col items-center gap-1">
              <div
                className={cn(
                  "grid size-8 place-items-center rounded-full border-2 text-xs font-bold transition-colors",
                  done
                    ? "border-buy bg-buy text-buy-foreground"
                    : current
                      ? "border-primary bg-primary/15 text-primary"
                      : "border-border bg-card text-muted-foreground",
                )}
              >
                {done ? <Check className="size-4" strokeWidth={3} /> : s.id}
              </div>
              <span
                className={cn(
                  "whitespace-nowrap text-[10px] font-medium uppercase tracking-wider",
                  done || current ? "text-foreground" : "text-muted-foreground",
                )}
              >
                {s.label}
              </span>
            </div>
            {i < STEPS.length - 1 && (
              <div
                className={cn(
                  "mb-5 h-0.5 flex-1 transition-colors",
                  done ? "bg-buy" : "bg-border",
                )}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

/**
 * Side-by-side DNS records table — Shopify-style "Current → Update to"
 * layout so the admin sees exactly which records need editing without
 * leaving the page to run `dig`.
 *
 * Renders one row per (apex / www) host. The Status pill flips:
 *   - `OK` (green)        → at least one A record matches expected_ip
 *   - `Update`  (amber)   → records exist but none match
 *   - `Add`     (amber)   → no A records resolved
 *   - `…`       (muted)   → preview still loading
 */
function DnsRecordsTable({
  preview,
  domain,
}: {
  preview: DnsPreview | undefined;
  domain: string;
}) {
  const expected = preview?.expected_ip ?? "(ask operator)";
  const rows: { name: string; check: typeof preview extends infer P
    ? P extends DnsPreview
      ? P["apex"]
      : never
    : never }[] = [
    { name: "@", check: (preview?.apex ?? { current: [], ok: false, error: null }) as any },
    { name: "www", check: (preview?.www ?? { current: [], ok: false, error: null }) as any },
  ];
  const renderCurrent = (check: { current: string[]; error: string | null }) => {
    if (check.error) return <span className="text-sell">{check.error}</span>;
    if (!check.current || check.current.length === 0)
      return <span className="text-muted-foreground">(empty)</span>;
    return <span>{check.current.join(", ")}</span>;
  };
  const renderStatus = (check: { current: string[]; ok: boolean; error: string | null }) => {
    if (!preview) return <span className="text-muted-foreground">…</span>;
    if (check.ok) {
      return (
        <span className="rounded-full bg-buy/15 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-buy ring-1 ring-buy/30">
          OK
        </span>
      );
    }
    if (check.current && check.current.length > 0) {
      return (
        <span className="rounded-full bg-atm/20 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-atm ring-1 ring-atm/40">
          Update
        </span>
      );
    }
    return (
      <span className="rounded-full bg-atm/20 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-atm ring-1 ring-atm/40">
        Add
      </span>
    );
  };
  return (
    <div className="overflow-x-auto rounded-md border border-border bg-background">
      <table className="w-full text-xs">
        <thead className="bg-muted/30 text-muted-foreground">
          <tr>
            <th className="px-3 py-2 text-left">Type</th>
            <th className="px-3 py-2 text-left">Name</th>
            <th className="px-3 py-2 text-left">Current</th>
            <th className="px-3 py-2 text-center" aria-hidden>
              →
            </th>
            <th className="px-3 py-2 text-left">Update to</th>
            <th className="px-3 py-2 text-left">TTL</th>
            <th className="px-3 py-2 text-left">Status</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {rows.map((r) => (
            <tr key={r.name} className="border-t border-border">
              <td className="px-3 py-2">A</td>
              <td className="px-3 py-2">{r.name}</td>
              <td className="px-3 py-2">{renderCurrent(r.check)}</td>
              <td className="px-3 py-2 text-center text-muted-foreground">→</td>
              <td className="px-3 py-2 text-primary">{expected}</td>
              <td className="px-3 py-2">3600</td>
              <td className="px-3 py-2">{renderStatus(r.check)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {!preview && (
        <p className="border-t border-border px-3 py-2 text-[11px] text-muted-foreground">
          Resolving DNS for <span className="font-mono text-foreground">{domain}</span>…
        </p>
      )}
    </div>
  );
}

/**
 * Shopify-style sub-status checklist shown during provisioning + after
 * READY. Each row is a tiny circle that flips:
 *   - ⏳ pending  — muted spinner / dot
 *   - ✓ done    — green checkmark
 *   - ⚠ failed  — red banner via parent component
 *
 * Drives off the same `status` flag the stepper uses, so updates are
 * automatic the moment the polling tick mutates the React Query cache.
 */
function ProvisioningChecklist({
  status,
}: {
  status: string | null | undefined;
}) {
  // Each item declares which statuses count as "done" for it.
  const items: Array<{ label: string; done: boolean; active: boolean }> = [
    {
      label: "DNS records validated",
      done: ["DNS_VERIFIED", "PROVISIONING", "READY"].includes(status ?? ""),
      active: status === "PENDING_DNS",
    },
    {
      label: "SSL certificate issued (Let's Encrypt)",
      done: status === "READY",
      active: status === "PROVISIONING" || status === "DNS_VERIFIED",
    },
    {
      label: "nginx reloaded — domain live with HTTPS",
      done: status === "READY",
      active: false,
    },
  ];
  return (
    <ul className="space-y-2">
      {items.map((it) => (
        <li key={it.label} className="flex items-center gap-2 text-xs">
          <span
            className={cn(
              "grid size-5 place-items-center rounded-full transition-colors",
              it.done
                ? "bg-buy text-buy-foreground"
                : it.active
                  ? "bg-primary/20 text-primary"
                  : "bg-muted text-muted-foreground",
            )}
          >
            {it.done ? (
              <Check className="size-3" strokeWidth={3} />
            ) : it.active ? (
              <Loader2 className="size-3 animate-spin" />
            ) : (
              <span className="size-1.5 rounded-full bg-current opacity-60" />
            )}
          </span>
          <span
            className={cn(
              it.done
                ? "text-foreground"
                : it.active
                  ? "text-foreground"
                  : "text-muted-foreground",
            )}
          >
            {it.label}
          </span>
        </li>
      ))}
    </ul>
  );
}

function LinkRow({
  label,
  url,
  disabled,
}: {
  label: string;
  url: string;
  disabled?: boolean;
}) {
  return (
    <div className="grid grid-cols-[80px_1fr_auto] items-center gap-2">
      <Label className="text-xs text-muted-foreground">{label}</Label>
      <Input
        readOnly
        value={url}
        disabled={disabled}
        className="font-mono text-xs"
      />
      <Button
        variant="outline"
        size="sm"
        onClick={() => copyToClipboard(url, `${label} link copied`)}
        disabled={disabled || !url}
      >
        <Copy className="size-4" />
      </Button>
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────
export default function BrandingPage() {
  const qc = useQueryClient();
  // Used to refresh the admin auth store after saving brand_name /
  // logo_url so the sidebar's <BrandLogo> picks up the new values
  // without a page reload.
  const refreshMe = useAdminAuthStore((s) => s.refreshMe);
  const meQuery = useQuery({
    queryKey: ["admin", "branding", "me"],
    queryFn: () => BrandingAPI.me(),
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    staleTime: 5 * 60_000,
  });

  // ── Local form state ──────────────────────────────────────────────
  const [brandName, setBrandName] = useState("");
  const [telegramLink, setTelegramLink] = useState("");
  const [domain, setDomain] = useState("");
  useEffect(() => {
    if (meQuery.data) {
      setBrandName(meQuery.data.brand_name ?? "");
      setTelegramLink((meQuery.data as any).telegram_link ?? "");
      setDomain(meQuery.data.custom_domain ?? "");
    }
  }, [meQuery.data]);

  const featureOff =
    meQuery.error instanceof ApiError &&
    String(meQuery.error.message ?? "").toLowerCase().includes("not enabled");

  // ── DNS preview (current vs expected A records) ───────────────────
  // Fired only when there's a domain saved AND we're on Step 2 (where
  // the table is rendered). Auto-refreshes every 15 s while the admin
  // is on the page so they see propagation as it lands.
  const hasDomainForPreview =
    !!meQuery.data?.custom_domain && meQuery.data.custom_domain_status !== "READY";
  const dnsPreviewQuery = useQuery({
    queryKey: ["admin", "branding", "dns-preview", meQuery.data?.custom_domain ?? ""],
    queryFn: () => BrandingAPI.dnsPreview(),
    enabled: hasDomainForPreview,
    retry: false,
    refetchInterval: hasDomainForPreview ? 15_000 : false,
    refetchOnWindowFocus: false,
    staleTime: 10_000,
  });

  // ── Mutations ─────────────────────────────────────────────────────
  const saveBranding = useMutation({
    mutationFn: (body: {
      brand_name?: string | null;
      telegram_link?: string | null;
      custom_domain?: string | null;
    }) => BrandingAPI.update(body),
    onSuccess: (data) => {
      qc.setQueryData(["admin", "branding", "me"], data);
      // Sidebar <BrandLogo> reads brand_name from the auth store, so
      // refresh it whenever the admin saves a new brand name.
      void refreshMe();
      toast.success("Saved");
    },
    onError: (err: any) => toast.error(err?.message || "Save failed"),
  });

  const uploadLogo = useMutation({
    mutationFn: (file: File) => BrandingAPI.uploadLogo(file),
    onSuccess: (data) => {
      qc.setQueryData(["admin", "branding", "me"], data);
      // Refresh the auth store so the sidebar logo flips to the new
      // file immediately (otherwise the admin would need to reload).
      void refreshMe();
      toast.success("Logo uploaded");
    },
    onError: (err: any) => toast.error(err?.message || "Upload failed"),
  });

  const verifyDomain = useMutation({
    mutationFn: () => BrandingAPI.verifyDomain(),
    onSuccess: (data) => {
      const prev = qc.getQueryData<BrandingPayload>(["admin", "branding", "me"]);
      if (prev) {
        qc.setQueryData<BrandingPayload>(["admin", "branding", "me"], {
          ...prev,
          custom_domain: data.custom_domain ?? prev.custom_domain,
          custom_domain_status: data.custom_domain_status ?? prev.custom_domain_status,
        });
      }
    },
    onError: (err: any) => toast.error(err?.message || "Verify failed"),
  });

  const disconnect = useMutation({
    mutationFn: () => BrandingAPI.disconnectDomain(),
    onSuccess: (data) => {
      qc.setQueryData(["admin", "branding", "me"], data);
      setDomain("");
      toast.success("Domain disconnected");
    },
    onError: (err: any) => toast.error(err?.message || "Disconnect failed"),
  });

  // ── Polling while provisioning ────────────────────────────────────
  const status = meQuery.data?.custom_domain_status ?? null;
  const isPolling = status === "PROVISIONING" || status === "DNS_VERIFIED";
  useEffect(() => {
    if (!isPolling) return;
    const id = setInterval(async () => {
      try {
        const ds: DomainStatus = await BrandingAPI.domainStatus();
        const prev = qc.getQueryData<BrandingPayload>(["admin", "branding", "me"]);
        if (prev) {
          qc.setQueryData<BrandingPayload>(["admin", "branding", "me"], {
            ...prev,
            custom_domain: ds.custom_domain ?? prev.custom_domain,
            custom_domain_status: ds.custom_domain_status ?? prev.custom_domain_status,
          });
        }
        if (ds.custom_domain_status === "READY") {
          toast.success("Custom domain is live with SSL ✓");
        } else if (ds.custom_domain_status === "FAILED" && ds.custom_domain_last_error) {
          toast.error(`Domain failed: ${ds.custom_domain_last_error}`, { duration: 8000 });
        }
      } catch {
        /* next tick retries */
      }
    }, 3000);
    return () => clearInterval(id);
  }, [isPolling, qc]);

  // ── Derived values ────────────────────────────────────────────────
  const userCode = meQuery.data?.user_code ?? "";
  const domainSaved = (meQuery.data?.custom_domain ?? "").trim();
  // Branded links are CLIENT-FACING (signup / login) — they must point
  // at the END-USER frontend (e.g. https://marginwealth.com), NOT the
  // admin panel host (admin.marginwealth.com). Resolution order:
  //   1. NEXT_PUBLIC_USER_APP_URL  (explicit override, recommended)
  //   2. Strip a leading "admin." subdomain off the current origin
  //   3. Hard fallback to https://marginwealth.com (SSR / mis-config)
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
  // Platform-side links use ?ref= for attribution + branding hint.
  const platformSignup = userCode ? `${platformOrigin}/register?ref=${userCode}` : "";
  const platformLogin = userCode ? `${platformOrigin}/login?ref=${userCode}` : "";
  // Custom-domain links only become real once SSL is provisioned.
  const customReady = domainSaved && status === "READY";
  const customSignup = customReady ? `https://${domainSaved}/register` : "";
  const customLogin = customReady ? `https://${domainSaved}/login` : "";

  const lastError = meQuery.data?.custom_domain_last_error ?? null;
  const activeStep = statusToStep(status, !!domainSaved);

  // ── File picker ───────────────────────────────────────────────────
  const fileInputRef = useRef<HTMLInputElement>(null);
  function onPickLogo(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (!f) return;
    if (f.size > 2 * 1024 * 1024) {
      toast.error("Logo too large (max 2 MB)");
      return;
    }
    uploadLogo.mutate(f);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  // ── Render guards ─────────────────────────────────────────────────
  if (meQuery.isLoading) {
    return (
      <div className="space-y-4">
        <PageHeader title="Branding" description="Loading your white-label settings…" />
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Loading…
        </div>
      </div>
    );
  }

  if (featureOff) {
    return (
      <div className="space-y-4">
        <PageHeader title="Branding" description="White-label feature is not enabled on this server." />
        <Card>
          <CardContent className="flex items-center gap-3 py-6 text-sm text-muted-foreground">
            <AlertTriangle className="size-5 text-atm" />
            <span>
              Ask the operator to set <code className="font-mono">BRANDING_ENABLED=true</code> in
              the backend <code className="font-mono">.env</code> and redeploy.
            </span>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Branding"
        description="Customise your logo, brand name and (optionally) your own domain."
      />

      {/* ── 1. Branding Settings ─────────────────────────────────── */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ImageIcon className="size-4" /> Branding Settings
          </CardTitle>
          <CardDescription>
            Your logo and brand name appear on the login / signup / dashboard for every user
            assigned to your pool.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center gap-4">
            <div className="grid size-20 place-items-center rounded-md border border-border bg-card">
              {logoSrc(meQuery.data) ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={logoSrc(meQuery.data)!}
                  alt="logo"
                  className="size-full rounded-md object-contain"
                />
              ) : (
                <ImageIcon className="size-6 text-muted-foreground" />
              )}
            </div>
            <div className="space-y-1">
              <input
                ref={fileInputRef}
                type="file"
                accept="image/png,image/jpeg,image/webp,image/svg+xml"
                onChange={onPickLogo}
                className="hidden"
                id="logo-upload"
              />
              <Button
                onClick={() => fileInputRef.current?.click()}
                loading={uploadLogo.isPending}
                variant="outline"
                size="sm"
              >
                <Upload className="mr-2 size-4" />
                {meQuery.data?.logo_url ? "Replace logo" : "Upload logo"}
              </Button>
              <p className="text-[11px] text-muted-foreground">PNG / JPEG / WEBP / SVG, ≤ 2 MB</p>
            </div>
          </div>

          <div className="space-y-2">
            <Label htmlFor="brand_name">Brand name</Label>
            <Input
              id="brand_name"
              placeholder="e.g. MyBroker Capital"
              value={brandName}
              onChange={(e) => setBrandName(e.target.value)}
              maxLength={64}
            />
            <Button
              size="sm"
              onClick={() => saveBranding.mutate({ brand_name: brandName.trim() })}
              loading={saveBranding.isPending}
              disabled={brandName.trim() === (meQuery.data?.brand_name ?? "")}
            >
              Save brand name
            </Button>
          </div>

          <div className="space-y-2">
            <Label htmlFor="telegram_link">Telegram link</Label>
            <Input
              id="telegram_link"
              placeholder="https://t.me/yourchannel"
              value={telegramLink}
              onChange={(e) => setTelegramLink(e.target.value)}
              maxLength={300}
            />
            <p className="text-[11px] text-muted-foreground">
              Shown as a Telegram button on your login page. Must start with https://. Leave blank to hide it.
            </p>
            <Button
              size="sm"
              onClick={() => saveBranding.mutate({ telegram_link: telegramLink.trim() })}
              loading={saveBranding.isPending}
              disabled={telegramLink.trim() === (((meQuery.data as any)?.telegram_link) ?? "")}
            >
              Save Telegram link
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* ── 2. Branded Links ─────────────────────────────────────── */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShieldCheck className="size-4" /> Branded Links
          </CardTitle>
          <CardDescription>
            Share these URLs with prospects. Users that sign up via these links are automatically
            attributed to your pool.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          {/* Platform links — always available */}
          <div className="space-y-2 rounded-md border border-border bg-card/50 p-3">
            <div className="flex items-center gap-2">
              <Globe2 className="size-4 text-primary" />
              <p className="text-sm font-semibold">Platform links</p>
              <span className="rounded-full bg-buy/15 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-buy ring-1 ring-buy/30">
                Live
              </span>
            </div>
            <p className="text-[11px] text-muted-foreground">
              Works on the platform host. Attribution via{" "}
              <code className="font-mono">?ref={userCode}</code>.
            </p>
            <div className="space-y-2 pt-1">
              <LinkRow label="Signup" url={platformSignup} />
              <LinkRow label="Login" url={platformLogin} />
            </div>
          </div>

          {/* Custom-domain links — gated by READY status */}
          <div
            className={cn(
              "space-y-2 rounded-md border p-3",
              customReady
                ? "border-buy/40 bg-buy/5"
                : "border-border bg-muted/20",
            )}
          >
            <div className="flex items-center gap-2">
              <Globe2
                className={cn(
                  "size-4",
                  customReady ? "text-buy" : "text-muted-foreground",
                )}
              />
              <p className="text-sm font-semibold">Custom domain links</p>
              {customReady ? (
                <span className="rounded-full bg-buy/15 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-buy ring-1 ring-buy/30">
                  SSL Live
                </span>
              ) : (
                <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
                  Connect domain to enable
                </span>
              )}
            </div>
            <p className="text-[11px] text-muted-foreground">
              {customReady
                ? "Users land directly on your domain — no ref query needed."
                : "Connect a custom domain below to unlock branded URLs on your own host."}
            </p>
            <div className="space-y-2 pt-1">
              <LinkRow
                label="Signup"
                url={customSignup || `https://yourdomain.com/register`}
                disabled={!customReady}
              />
              <LinkRow
                label="Login"
                url={customLogin || `https://yourdomain.com/login`}
                disabled={!customReady}
              />
            </div>
          </div>
        </CardContent>
      </Card>

      {/* ── 3. Connect Custom Domain (wizard) ─────────────────────── */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Globe2 className="size-4" /> Connect Custom Domain
            {status === "FAILED" && (
              <span className="rounded-full bg-sell/15 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-sell ring-1 ring-sell/30">
                Failed
              </span>
            )}
          </CardTitle>
          <CardDescription>
            Optional. Use your own domain (e.g. <code className="font-mono">mybroker.com</code>)
            with a free auto-provisioned SSL certificate. You only add 2 DNS records.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          <Stepper active={activeStep} />

          {/* Step 1 — Enter Domain */}
          {activeStep === 1 && (
            <div className="space-y-3 rounded-md border border-border bg-card/50 p-4">
              <div className="space-y-2">
                <Label htmlFor="domain">Your domain</Label>
                <Input
                  id="domain"
                  placeholder="mybroker.com"
                  value={domain}
                  onChange={(e) => setDomain(e.target.value)}
                  maxLength={253}
                />
                <p className="text-[11px] text-muted-foreground">
                  Just the apex — no <code className="font-mono">https://</code> or{" "}
                  <code className="font-mono">www</code> needed.
                </p>
              </div>
              <Button
                size="sm"
                onClick={() => saveBranding.mutate({ custom_domain: domain.trim() })}
                loading={saveBranding.isPending}
                disabled={!domain.trim()}
              >
                Save &amp; continue
              </Button>
            </div>
          )}

          {/* Step 2 — Add DNS Records (with side-by-side current values) */}
          {activeStep === 2 && (
            <div className="space-y-3 rounded-md border border-border bg-card/50 p-4">
              <div className="flex items-center justify-between gap-2">
                <p className="text-sm font-semibold">
                  Add or update these DNS records at your registrar
                </p>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => dnsPreviewQuery.refetch()}
                  loading={dnsPreviewQuery.isFetching}
                  className="h-7 px-2 text-xs"
                >
                  <RefreshCw className="mr-1 size-3" /> Refresh
                </Button>
              </div>
              <DnsRecordsTable preview={dnsPreviewQuery.data} domain={domainSaved} />
              <p className="text-[11px] text-muted-foreground">
                DNS propagation usually takes 1–30 minutes. Once your records match the column on
                the right, click <strong>Verify &amp; Connect</strong>.
              </p>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  size="sm"
                  onClick={() => verifyDomain.mutate()}
                  loading={verifyDomain.isPending}
                >
                  <RefreshCw className="mr-1 size-4" /> Verify &amp; Connect
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => disconnect.mutate()}
                  loading={disconnect.isPending}
                >
                  <Unplug className="mr-1 size-4" /> Cancel
                </Button>
              </div>
            </div>
          )}

          {/* Step 3 — Verifying / Provisioning */}
          {activeStep === 3 && (
            <div className="space-y-4 rounded-md border border-border bg-card/50 p-4">
              <div className="flex items-center gap-3">
                <Loader2 className="size-5 animate-spin text-primary" />
                <div>
                  <p className="text-sm font-semibold">
                    {status === "FAILED"
                      ? "Provisioning paused — see details below"
                      : status === "DNS_VERIFIED"
                        ? "DNS verified — issuing SSL certificate…"
                        : status === "PROVISIONING"
                          ? "Issuing SSL certificate via Let's Encrypt…"
                          : "Checking DNS records…"}
                  </p>
                  <p className="text-[11px] text-muted-foreground">
                    Usually completes in 30–60 seconds. You can leave this page open.
                  </p>
                </div>
              </div>
              <ProvisioningChecklist status={status} />
              {status === "FAILED" && lastError && (
                <div className="rounded-md border border-sell/40 bg-sell/10 p-3 text-xs">
                  <div className="flex items-start gap-2">
                    <AlertTriangle className="mt-0.5 size-4 shrink-0 text-sell" />
                    <div className="space-y-1">
                      <p className="font-semibold text-sell">Provisioning failed</p>
                      <p className="text-foreground">{friendlyError(lastError)}</p>
                      {friendlyError(lastError) !== lastError && (
                        <details className="pt-1">
                          <summary className="cursor-pointer text-[11px] text-muted-foreground hover:text-foreground">
                            Show technical details
                          </summary>
                          <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap rounded bg-background/60 p-2 text-[10px] text-muted-foreground">
                            {lastError}
                          </pre>
                        </details>
                      )}
                    </div>
                  </div>
                </div>
              )}
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  size="sm"
                  onClick={() => verifyDomain.mutate()}
                  loading={verifyDomain.isPending}
                  variant={status === "FAILED" ? "default" : "outline"}
                >
                  <RefreshCw className="mr-1 size-4" />
                  {status === "FAILED" ? "Retry" : "Re-check now"}
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => disconnect.mutate()}
                  loading={disconnect.isPending}
                >
                  <Unplug className="mr-1 size-4" /> Disconnect
                </Button>
              </div>
            </div>
          )}

          {/* Step 4 — Complete */}
          {activeStep === 4 && (
            <div className="space-y-4 rounded-md border border-buy/40 bg-buy/5 p-4">
              <div className="flex items-start gap-3">
                <CheckCircle2 className="mt-0.5 size-5 text-buy" />
                <div className="space-y-1">
                  <p className="text-sm font-semibold text-buy">Domain connected</p>
                  <p className="text-xs text-muted-foreground">
                    <span className="font-mono text-foreground">{domainSaved}</span> is live with
                    SSL. Share the branded URLs from the section above with your users.
                  </p>
                </div>
              </div>
              <ProvisioningChecklist status={status} />
              {/* Auto-renewal reassurance — Let's Encrypt certs are 90 days
                  and certbot.timer (installed alongside certbot) renews
                  automatically every 12 h. Admin never has to touch this. */}
              <div className="rounded-md border border-border bg-card/40 p-3 text-xs">
                <div className="flex items-start gap-2">
                  <ShieldCheck className="mt-0.5 size-4 shrink-0 text-buy" />
                  <div className="space-y-0.5">
                    <p className="font-semibold text-foreground">
                      Auto-renewal enabled
                    </p>
                    <p className="text-muted-foreground">
                      Let's Encrypt certificates are valid for 90 days. The server's{" "}
                      <code className="font-mono text-foreground">certbot.timer</code> renews
                      automatically every 12 h, well before expiry. You don't have to do anything.
                    </p>
                  </div>
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <a
                  href={`https://${domainSaved}/login`}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 rounded-md border border-buy/40 bg-buy/10 px-3 py-1.5 text-xs font-semibold text-buy hover:bg-buy/20"
                >
                  <CheckCircle2 className="size-4" /> Open https://{domainSaved}/login
                </a>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => disconnect.mutate()}
                  loading={disconnect.isPending}
                >
                  <Unplug className="mr-1 size-4" /> Disconnect
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
