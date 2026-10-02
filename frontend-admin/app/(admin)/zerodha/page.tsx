"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertTriangle,
  CheckCircle2,
  DownloadCloud,
  Eraser,
  KeyRound,
  Link as LinkIcon,
  Plug,
  Plus,
  RefreshCw,
  Search,
  Stethoscope,
  Trash2,
  Unlink,
  XCircle,
} from "lucide-react";
import { ZerodhaAPI } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { PageHeader } from "@/components/common/PageHeader";
import { DataTable, type Column } from "@/components/common/DataTable";
import { StatusPill } from "@/components/common/StatusPill";
import { AutoLoginPanel } from "@/components/zerodha/AutoLoginPanel";
import { FeedRoutingCard } from "@/components/zerodha/FeedRoutingCard";

type Settings = {
  apiKey: string;
  apiSecret: string;
  apiSecretConfigured: boolean;
  isConnected: boolean;
  isTokenExpired: boolean;
  lastConnected: string | null;
  tokenExpiry: string | null;
  wsStatus: string;
  wsLastError: string | null;
  enabledSegments: Record<string, boolean>;
  subscribedInstruments: any[];
  redirectUrl: string;
  defaultRedirectUrl: string;
  redirectUrlMismatch: boolean;
};

const SEGMENT_TO_EXCHANGE: Record<string, string> = {
  nseEq: "NSE",
  bseEq: "BSE",
  nseFut: "NFO",
  nseOpt: "NFO",
  mcxFut: "MCX",
  mcxOpt: "MCX",
  bseFut: "BFO",
  bseOpt: "BFO",
};

const SEGMENTS: { key: string; label: string }[] = [
  { key: "nseEq", label: "NSE Equity" },
  { key: "bseEq", label: "BSE Equity" },
  { key: "nseFut", label: "NSE Futures" },
  { key: "nseOpt", label: "NSE Options" },
  { key: "mcxFut", label: "MCX Futures" },
  { key: "mcxOpt", label: "MCX Options" },
  { key: "bseFut", label: "BSE Futures" },
  { key: "bseOpt", label: "BSE Options" },
];

export default function ZerodhaConnectPage() {
  const qc = useQueryClient();
  const params = useSearchParams();

  // Which Zerodha account tab is active: 0 = Account A (primary), 1 = Account B
  const [activeAccount, setActiveAccount] = useState(0);

  const { data: settings, refetch } = useQuery<Settings>({
    queryKey: ["zerodha", "settings", activeAccount],
    queryFn: () => ZerodhaAPI.settings(activeAccount) as Promise<Settings>,
    refetchInterval: 5000,
  });

  // Local edit state for the credentials form
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [redirectUrl, setRedirectUrl] = useState("");
  const [enabled, setEnabled] = useState<Record<string, boolean>>({});
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!settings) return;
    setApiKey(settings.apiKey || "");
    setRedirectUrl(settings.redirectUrl || "");
    setEnabled(settings.enabledSegments || {});
  }, [settings?.apiKey, settings?.redirectUrl, settings?.enabledSegments, activeAccount]);

  // OAuth callback feedback
  useEffect(() => {
    const success = params.get("success");
    const error = params.get("error");
    const callbackAccount = Number(params.get("account") ?? "0");
    if (success === "true") {
      toast.success(`Account ${callbackAccount === 1 ? "B" : "A"} connected to Zerodha`);
      setActiveAccount(callbackAccount);
      refetch();
    } else if (error) {
      toast.error(`Connection failed: ${error}`);
    }
  }, [params, refetch]);

  async function save() {
    setSaving(true);
    try {
      await ZerodhaAPI.saveSettings({
        apiKey: apiKey.trim(),
        apiSecret: apiSecret.trim() || undefined,
        redirectUrl: redirectUrl.trim(),
        enabledSegments: enabled,
      }, activeAccount);
      setApiSecret("");
      toast.success("Settings saved");
      qc.invalidateQueries({ queryKey: ["zerodha"] });
    } catch (e: any) {
      toast.error(e.message || "Failed to save");
    } finally {
      setSaving(false);
    }
  }

  async function connectZerodha() {
    try {
      const url = await ZerodhaAPI.loginUrl(activeAccount);
      if (url) window.location.href = url;
    } catch (e: any) {
      toast.error(e.message || "Could not get login URL");
    }
  }

  async function disconnectZerodha() {
    const label = activeAccount === 1 ? "Account B" : "Account A";
    if (!confirm(`Disconnect ${label} from Zerodha? Live ticker will stop.`)) return;
    try {
      await ZerodhaAPI.logout(activeAccount);
      toast.success("Disconnected");
      refetch();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function connectWs() {
    try {
      await ZerodhaAPI.connectWs();
      toast.success("WebSocket connecting…");
      refetch();
    } catch (e: any) {
      toast.error(e.message || "Could not start ticker");
    }
  }

  async function disconnectWs() {
    try {
      await ZerodhaAPI.disconnectWs();
      toast.success("Ticker stopped");
      refetch();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  // Operator's "act like a backend restart" button — the daily token
  // rotation around 08:00 IST often leaves the self-heal failure
  // counter climbed to 5+, which puts the next reconnect attempt 5
  // minutes out. Force-reconnect resets that counter, refreshes the
  // captured asyncio loop, and drives connect_ws(force=True)
  // synchronously so the admin sees a green tick immediately instead
  // of waiting for self-heal to climb back down. Replaces SSH-ing in
  // to run `systemctl restart marginwealth-backend`.
  const [reconnecting, setReconnecting] = useState(false);
  async function forceReconnectWs() {
    if (reconnecting) return;
    setReconnecting(true);
    try {
      await ZerodhaAPI.forceReconnectWs();
      toast.success("Ticker reconnected");
      refetch();
    } catch (e: any) {
      toast.error(e.message || "Force reconnect failed — check backend logs");
    } finally {
      setReconnecting(false);
    }
  }

  // ── Instruments search ──────────────────────────────────────
  const [query, setQuery] = useState("");
  const [searchSeg, setSearchSeg] = useState("nseEq");
  const [searchResults, setSearchResults] = useState<any[]>([]);
  const [searching, setSearching] = useState(false);

  async function runSearch() {
    if (query.trim().length < 2) {
      toast.info("Enter at least 2 characters");
      return;
    }
    setSearching(true);
    try {
      const r = await ZerodhaAPI.searchInstruments(query.trim(), searchSeg);
      setSearchResults(r);
    } catch (e: any) {
      toast.error(e.message || "Search failed");
    } finally {
      setSearching(false);
    }
  }

  async function subscribe(inst: any) {
    try {
      await ZerodhaAPI.subscribe(inst);
      toast.success(`Subscribed to ${inst.symbol}`);
      refetch();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function subscribeAll() {
    const subs = settings?.subscribedInstruments ?? [];
    const tokens = new Set(subs.map((s: any) => s.token));
    const fresh = searchResults.filter((r) => !tokens.has(r.token));
    if (!fresh.length) {
      toast.info("All search results are already subscribed");
      return;
    }
    try {
      const r = await ZerodhaAPI.subscribeBulk(fresh);
      toast.success(`Subscribed ${r.count} instruments`);
      refetch();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function unsubscribe(token: number) {
    try {
      await ZerodhaAPI.unsubscribe(token);
      refetch();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  // ── Sync / Clear / Subscribe-all-from-exchange ─────────────
  const [busy, setBusy] = useState(false);

  async function syncCache() {
    setBusy(true);
    try {
      const r = await ZerodhaAPI.syncInstruments();
      toast.success(
        `Cache cleared${r.expired_removed ? ` · ${r.expired_removed} expired removed` : ""}`
      );
      refetch();
    } catch (e: any) {
      toast.error(e.message || "Sync failed");
    } finally {
      setBusy(false);
    }
  }

  async function clearAll() {
    if (!confirm("This unsubscribes every instrument and clears the in-memory cache. Continue?")) {
      return;
    }
    setBusy(true);
    try {
      const r = await ZerodhaAPI.clearInstruments();
      toast.success(`Cleared ${r.cleared} subscriptions`);
      refetch();
    } catch (e: any) {
      toast.error(e.message || "Clear failed");
    } finally {
      setBusy(false);
    }
  }

  async function trimSubscriptions() {
    const input = prompt(
      "Keep how many most-recently-used subscriptions? (Open positions always preserved)",
      "700",
    );
    if (!input) return;
    const keep = parseInt(input, 10);
    if (Number.isNaN(keep) || keep < 50) {
      toast.error("Enter a number ≥ 50");
      return;
    }
    setBusy(true);
    try {
      const r = await ZerodhaAPI.trimInstruments(keep);
      toast.success(
        `Trimmed: kept ${r.kept}, removed ${r.removed}` +
          (r.must_keep_added ? ` (${r.must_keep_added} preserved from open positions)` : ""),
      );
      refetch();
    } catch (e: any) {
      toast.error(e.message || "Trim failed");
    } finally {
      setBusy(false);
    }
  }

  async function subscribeAllFromExchange() {
    const exchange = SEGMENT_TO_EXCHANGE[searchSeg];
    if (!exchange) return;
    if (
      !confirm(
        `Fetch every instrument on ${exchange} and subscribe (skipping any already subscribed)?\n\nThis can be a large number of instruments.`
      )
    ) {
      return;
    }
    setBusy(true);
    try {
      const list = await ZerodhaAPI.listForExchange(exchange);
      const subs = settings?.subscribedInstruments ?? [];
      const knownTokens = new Set(subs.map((s: any) => s.token));
      const fresh = list.filter((i: any) => !knownTokens.has(i.token));
      if (fresh.length === 0) {
        toast.info(`Already subscribed to all ${list.length} instruments on ${exchange}`);
        setBusy(false);
        return;
      }

      let added = 0;
      const batchSize = 200;
      for (let i = 0; i < fresh.length; i += batchSize) {
        const batch = fresh.slice(i, i + batchSize);
        const r = await ZerodhaAPI.subscribeBulk(batch);
        added += r.count || 0;
        toast.message(`Subscribing ${Math.min(i + batchSize, fresh.length)} / ${fresh.length}…`);
      }
      toast.success(`Subscribed ${added} instruments from ${exchange}`);
      refetch();
    } catch (e: any) {
      toast.error(e.message || "Bulk subscribe failed");
    } finally {
      setBusy(false);
    }
  }

  // ── Diagnostics ─────────────────────────────────────────────
  const [diagBusy, setDiagBusy] = useState(false);
  const [diag, setDiag] = useState<any | null>(null);

  async function runDiagnostics() {
    setDiagBusy(true);
    setDiag(null);
    try {
      const r = await ZerodhaAPI.diagnose();
      setDiag(r);
      const ok =
        r?.credentials?.ok &&
        r?.auth?.isConnected &&
        !r?.auth?.isTokenExpired &&
        r?.restQuote?.ok;
      if (ok) toast.success("Zerodha pipeline healthy — live data is reachable.");
      else toast.message("Diagnostics done — see report below.");
    } catch (e: any) {
      toast.error(e.message || "Diagnostics failed");
    } finally {
      setDiagBusy(false);
    }
  }

  // ── Manual token (fallback when OAuth callback can't reach the server) ─
  const [manualToken, setManualToken] = useState("");
  const [manualBusy, setManualBusy] = useState(false);
  async function connectWithToken() {
    if (!manualToken.trim()) {
      toast.info("Paste the request_token from the Kite redirect URL");
      return;
    }
    setManualBusy(true);
    try {
      await ZerodhaAPI.connectWithToken(manualToken.trim(), activeAccount);
      toast.success("Connected to Zerodha");
      setManualToken("");
      refetch();
    } catch (e: any) {
      toast.error(e.message || "Token exchange failed");
    } finally {
      setManualBusy(false);
    }
  }

  const subCols: Column<any>[] = [
    { key: "symbol", header: "Symbol" },
    { key: "exchange", header: "Exch" },
    { key: "segment", header: "Segment" },
    { key: "instrumentType", header: "Type" },
    { key: "lotSize", header: "Lot", align: "right" },
    { key: "expiry", header: "Expiry", render: (r) => r.expiry || "—" },
    {
      key: "actions",
      header: "",
      align: "right",
      render: (r) => (
        <Button variant="ghost" size="icon" aria-label="Unsubscribe" onClick={() => unsubscribe(r.token)}>
          <Trash2 className="size-4 text-destructive" />
        </Button>
      ),
    },
  ];

  if (!settings) return <div className="text-sm text-muted-foreground">Loading…</div>;

  const accountLabel = activeAccount === 1 ? "Account B" : "Account A";

  return (
    <div className="space-y-6">
      <PageHeader
        title="Zerodha Kite Connect"
        description="Connect your Kite Connect app to stream live exchange data into the platform."
        actions={
          <div className="flex flex-wrap gap-2">
            {settings.isConnected && !settings.isTokenExpired ? (
              <Button variant="destructive" onClick={disconnectZerodha}>
                <Unlink className="size-4" /> Disconnect {accountLabel}
              </Button>
            ) : (
              <Button onClick={connectZerodha} disabled={!settings.apiKey || !settings.apiSecretConfigured}>
                <LinkIcon className="size-4" /> {settings.isTokenExpired ? `Reconnect ${accountLabel}` : `Login ${accountLabel}`}
              </Button>
            )}
            {settings.wsStatus === "connected" && activeAccount === 0 && (
              <Button variant="outline" onClick={disconnectWs}>
                <Plug className="size-4" /> Stop ticker
              </Button>
            )}
            {settings.isConnected
              && !settings.isTokenExpired
              && settings.wsStatus !== "connected"
              && activeAccount === 0 && (
                <Button
                  variant="outline"
                  onClick={forceReconnectWs}
                  disabled={reconnecting}
                  title="Reset the heal-failure counter and reconnect the ticker"
                >
                  <RefreshCw className={cn("size-4", reconnecting && "animate-spin")} />
                  {reconnecting ? "Reconnecting…" : "Force reconnect ticker"}
                </Button>
              )}
          </div>
        }
      />

      {/* ── Account A / Account B tab switcher ──────────────────── */}
      <div className="flex w-full flex-col gap-2 rounded-xl border border-border bg-muted/30 p-1.5 sm:w-fit sm:flex-row sm:items-stretch">
        {[
          {
            idx: 0,
            label: "Account A",
            title: "NSE + BSE",
            desc: "Equity + F&O feed",
            tag: "Equity",
          },
          {
            idx: 1,
            label: "Account B",
            title: "MCX",
            desc: "Commodities feed",
            tag: "Commodity",
          },
        ].map(({ idx, label, title, desc, tag }) => (
          <button
            key={idx}
            onClick={() => setActiveAccount(idx)}
            className={cn(
              "flex flex-1 flex-col items-start rounded-lg px-4 py-3 text-left transition-all",
              activeAccount === idx
                ? "bg-background shadow-sm ring-1 ring-emerald-500/40"
                : "text-muted-foreground hover:bg-background/50 hover:text-foreground"
            )}
          >
            <div className="flex w-full items-center justify-between gap-3">
              <span
                className={cn(
                  "text-base font-bold",
                  activeAccount === idx ? "text-foreground" : "",
                )}
              >
                {label}
              </span>
              <span
                className={cn(
                  "rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
                  idx === 0
                    ? "bg-emerald-500/15 text-emerald-400"
                    : "bg-amber-500/15 text-amber-400",
                )}
              >
                {tag}
              </span>
            </div>
            <span className="mt-0.5 text-sm font-semibold text-foreground/90">{title}</span>
            <span className="text-xs text-muted-foreground">{desc}</span>
          </button>
        ))}
      </div>

      {settings.isTokenExpired && settings.apiKey && (
        <div className="flex items-start gap-3 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-destructive" />
          <div>
            <div className="font-semibold text-destructive">{accountLabel} token expired</div>
            <div className="text-xs text-muted-foreground">
              Kite access tokens are treated as expired at 07:00 IST every day. Click <strong>Login {accountLabel}</strong> above to refresh.
            </div>
          </div>
        </div>
      )}

      <AutoLoginPanel account={activeAccount} />

      <FeedRoutingCard />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Credentials — {accountLabel}</CardTitle>
            <CardDescription>
              Get these from{" "}
              <a className="text-primary underline" href="https://developers.kite.trade/apps/" target="_blank" rel="noreferrer">
                developers.kite.trade
              </a>
              . {activeAccount === 1 && "Create a separate Kite Connect app under Account B. "}
              Set the redirect URL on the Kite app to match the one shown below.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="space-y-1.5">
              <Label>API key</Label>
              {/* A text field followed by a password field reads to Chrome as a
                  login form, so it autofilled the saved admin email into
                  "API key". These are Kite credentials, not a sign-in. */}
              <Input
                name="kite-api-key"
                autoComplete="off"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="Kite API key"
              />
            </div>
            <div className="space-y-1.5">
              <Label>API secret</Label>
              <Input
                type="password"
                name="kite-api-secret"
                autoComplete="new-password"
                value={apiSecret}
                onChange={(e) => setApiSecret(e.target.value)}
                placeholder={settings.apiSecretConfigured ? "•••••• (saved — leave blank to keep)" : "Kite API secret"}
              />
            </div>
            <div className="space-y-1.5">
              <div className="flex items-center justify-between gap-2">
                <Label>Redirect URL (set this in your Kite app)</Label>
                {settings.defaultRedirectUrl && redirectUrl !== settings.defaultRedirectUrl && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => setRedirectUrl(settings.defaultRedirectUrl)}
                  >
                    Use backend default
                  </Button>
                )}
              </div>
              <Input
                value={redirectUrl}
                onChange={(e) => setRedirectUrl(e.target.value)}
                placeholder={settings.defaultRedirectUrl || "http://localhost:8000/api/v1/admin/zerodha/callback"}
              />
              {redirectUrl && settings.defaultRedirectUrl && redirectUrl !== settings.defaultRedirectUrl && (
                <div className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 p-2 text-[11px] text-destructive">
                  <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
                  <div>
                    <div className="font-semibold">Redirect URL doesn't point at this backend.</div>
                    <div className="text-muted-foreground">
                      Kite must hit <code className="rounded bg-muted px-1">{settings.defaultRedirectUrl}</code>
                      &nbsp;— that's where the OAuth callback handler lives. The frontend ports
                      (3000/3001) have no callback route and the connection will fail silently.
                    </div>
                  </div>
                </div>
              )}

              <div className="rounded-md border border-info/40 bg-info/10 p-3 text-xs">
                <div className="mb-1 font-semibold text-foreground">
                  Copy this exact URL into your Kite Connect app on{" "}
                  <a
                    href="https://developers.kite.trade/apps/"
                    target="_blank"
                    rel="noreferrer"
                    className="text-info underline"
                  >
                    developers.kite.trade
                  </a>
                </div>
                <div className="flex items-center gap-2">
                  <code className="flex-1 truncate rounded bg-muted px-2 py-1 font-mono text-[11px] text-foreground">
                    {redirectUrl || settings.defaultRedirectUrl}
                  </code>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      const v = redirectUrl || settings.defaultRedirectUrl;
                      navigator.clipboard.writeText(v).then(
                        () => toast.success("Redirect URL copied"),
                        () => toast.error("Couldn't copy — select & copy manually")
                      );
                    }}
                  >
                    Copy
                  </Button>
                </div>
                <div className="mt-2 text-muted-foreground">
                  This URL must match in <strong>both</strong> places: here, and the
                  &ldquo;Redirect URL&rdquo; field of your Kite app on developers.kite.trade. After
                  changing it on Kite's side, click <strong>Login to Zerodha</strong> again.
                </div>
                <div className="mt-1 text-muted-foreground">
                  <strong>Tip:</strong> the frontends now also proxy this callback path, so even if
                  your Kite app currently points at <code>localhost:3000</code> or
                  <code>localhost:3001</code>, the connect flow will still complete — but pointing
                  it directly at the backend (<code>:8000</code>) is faster and avoids one redirect hop.
                </div>
              </div>
            </div>
            <div className="space-y-1.5">
              <Label>Enabled segments</Label>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                {SEGMENTS.map((s) => (
                  <label key={s.key} className="flex items-center gap-2 rounded-md border border-border bg-muted/20 px-2 py-1.5 text-xs">
                    <input
                      type="checkbox"
                      checked={!!enabled[s.key]}
                      onChange={(e) => setEnabled((prev) => ({ ...prev, [s.key]: e.target.checked }))}
                      className="size-4 accent-primary"
                    />
                    {s.label}
                  </label>
                ))}
              </div>
            </div>
            <div className="flex justify-end">
              <Button onClick={save} loading={saving}>
                Save
              </Button>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Status</CardTitle>
            <CardDescription>Token rotates daily at 07:00 IST.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            <Row
              label="Authentication"
              value={
                settings.isConnected ? (
                  <span className="inline-flex items-center gap-1 text-primary">
                    <CheckCircle2 className="size-3.5" /> Connected
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-1 text-muted-foreground">
                    <XCircle className="size-3.5" /> Disconnected
                  </span>
                )
              }
            />
            <Row label="Ticker (WebSocket)" value={<StatusPill status={settings.wsStatus} />} />
            <Row label="Last connected" value={settings.lastConnected ? new Date(settings.lastConnected).toLocaleString() : "—"} />
            <Row label="Token expires" value={settings.tokenExpiry ? new Date(settings.tokenExpiry).toLocaleString() : "—"} />
            <Row label="Subscribed" value={`${settings.subscribedInstruments.length} instruments`} />
            {settings.wsLastError && (
              <div className="space-y-2 rounded-md border border-destructive/40 bg-destructive/10 p-2.5 text-xs text-destructive">
                <div>{settings.wsLastError}</div>
                {settings.isConnected
                  && !settings.isTokenExpired
                  && settings.wsStatus !== "connected" && (
                    <Button
                      size="sm"
                      onClick={forceReconnectWs}
                      disabled={reconnecting}
                      className="h-7 gap-1 bg-destructive text-destructive-foreground hover:bg-destructive/90"
                    >
                      <RefreshCw className={cn("size-3.5", reconnecting && "animate-spin")} />
                      {reconnecting ? "Reconnecting…" : "Fix it — force reconnect"}
                    </Button>
                  )}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {activeAccount === 1 && (
        <div className="rounded-lg border border-border bg-muted/20 p-4 text-sm text-muted-foreground">
          <div className="font-semibold text-foreground mb-1">Account B — token-only setup</div>
          Account B's access token is used to open a second Zerodha WebSocket connection for up to 3000
          additional instrument subscriptions. Instruments, diagnostics, and WebSocket management are
          handled via Account A. Save API credentials + auto-login above, then click <strong>Login Account B</strong> to authenticate.
        </div>
      )}

      {activeAccount === 0 && <Card>
        <CardHeader>
          <CardTitle>Pipeline diagnostics</CardTitle>
          <CardDescription>
            Pings Kite end-to-end (auth → instruments → REST quote → ticker). Use this when the user
            terminal isn't showing live prices to find out exactly which step is failing.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <Button onClick={runDiagnostics} loading={diagBusy} variant="outline">
            <Stethoscope className="size-4" /> Run diagnostics
          </Button>

          {diag && (
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              <DiagRow
                label="Credentials saved"
                ok={!!diag.credentials?.ok}
                detail={
                  diag.credentials?.apiKeySet
                    ? diag.credentials?.apiSecretSet
                      ? "API key & secret set"
                      : "API key set, secret missing"
                    : "API key missing"
                }
              />
              <DiagRow
                label="Auth (token + profile call)"
                ok={!!diag.auth?.isConnected && !diag.auth?.isTokenExpired && diag.auth?.profileCall === "ok"}
                detail={
                  diag.auth?.isTokenExpired
                    ? "Token expired — click Reconnect to Zerodha"
                    : !diag.auth?.isConnected
                    ? "Not authenticated — click Login to Zerodha"
                    : diag.auth?.profileCall === "ok"
                    ? "Profile call succeeded"
                    : `Profile call ${diag.auth?.profileCall ?? "skipped"}`
                }
              />
              <DiagRow
                label="Instruments fetch (NSE)"
                ok={!!diag.instrumentsFetch?.ok}
                detail={
                  diag.instrumentsFetch?.ok
                    ? `Got ${diag.instrumentsFetch?.count?.toLocaleString?.("en-IN") ?? 0} rows`
                    : diag.instrumentsFetch?.error || "Failed"
                }
              />
              <DiagRow
                label="REST quote"
                ok={!!diag.restQuote?.ok}
                detail={
                  diag.restQuote?.ok
                    ? `OK · ${diag.restQuote?.key} → LTP ₹${
                        diag.restQuote?.sample?.last_price ?? "—"
                      }`
                    : diag.restQuote?.error || "No quote returned"
                }
              />
              <DiagRow
                label="Subscriptions"
                ok={(diag.subscriptions?.count ?? 0) > 0}
                detail={
                  diag.subscriptions?.count > 0
                    ? `${diag.subscriptions.count} subscribed${
                        diag.subscriptions?.sample?.length
                          ? ` (e.g. ${diag.subscriptions.sample.join(", ")})`
                          : ""
                      }`
                    : "Empty — search & subscribe at least one instrument below"
                }
              />
              <DiagRow
                label="WebSocket ticker"
                ok={diag.ticker?.status === "connected"}
                detail={
                  diag.ticker?.status === "connected"
                    ? `Streaming · ${diag.ticker?.liveTicksHeld ?? 0} live ticks held`
                    : `${diag.ticker?.status ?? "disconnected"}${
                        diag.ticker?.lastError ? ` — ${diag.ticker.lastError}` : ""
                      }`
                }
              />
              <div className="sm:col-span-2 rounded-md border border-border bg-muted/10 p-2 text-[11px] text-muted-foreground">
                <strong>Note:</strong> Indian markets are closed on weekends and outside 09:15–15:30 IST.
                The WebSocket ticker stays idle then, but REST quotes still return the last close — the
                user terminal will show those last-traded prices automatically (we added a 10s REST fallback).
              </div>
            </div>
          )}
        </CardContent>
      </Card>}

      {activeAccount === 0 && settings.isConnected && !settings.isTokenExpired && (
        <Card>
          <CardHeader>
            <CardTitle>Instrument management</CardTitle>
            <CardDescription>
              On-demand: instruments are pulled from Kite when you search. Sync if Zerodha's CSV
              changed today (rare — only when lot sizes or contracts get added).
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-wrap items-center gap-2">
            <Button variant="outline" onClick={syncCache} loading={busy}>
              <RefreshCw className="size-4" /> Sync cache & remove expired
            </Button>
            <Button variant="outline" onClick={trimSubscriptions} loading={busy}>
              <Eraser className="size-4" /> Trim to N (LRU)
            </Button>
            <Button variant="destructive" onClick={clearAll} loading={busy}>
              <Eraser className="size-4" /> Clear all subscriptions
            </Button>
            <Button variant="outline" onClick={subscribeAllFromExchange} loading={busy}>
              <DownloadCloud className="size-4" /> Subscribe every {SEGMENT_TO_EXCHANGE[searchSeg]} instrument
            </Button>
          </CardContent>
        </Card>
      )}

      {!settings.isConnected && settings.apiKey && settings.apiSecretConfigured && (
        <Card>
          <CardHeader>
            <CardTitle>Stuck on the redirect?</CardTitle>
            <CardDescription>
              If Kite redirected you to a URL that couldn't reach this server, copy the
              <code className="mx-1 rounded bg-muted px-1 text-[11px]">request_token</code>
              from the URL bar and paste it here — same effect as a successful callback.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-wrap items-end gap-2">
            <div className="flex-1 min-w-[260px] space-y-1.5">
              <Label>Request token</Label>
              <Input
                value={manualToken}
                onChange={(e) => setManualToken(e.target.value)}
                placeholder="Paste the request_token query param"
              />
            </div>
            <Button onClick={connectWithToken} loading={manualBusy}>
              <KeyRound className="size-4" /> Connect with token
            </Button>
          </CardContent>
        </Card>
      )}

      {activeAccount === 0 && <Card>
        <CardHeader>
          <CardTitle>Search & subscribe instruments</CardTitle>
          <CardDescription>
            Subscribed instruments are streamed live via Kite WebSocket and published to user terminals.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <select
              value={searchSeg}
              onChange={(e) => setSearchSeg(e.target.value)}
              className="h-10 rounded-md border border-border bg-background px-3 text-sm"
            >
              {SEGMENTS.map((s) => (
                <option key={s.key} value={s.key}>
                  {s.label}
                </option>
              ))}
            </select>
            <div className="relative flex-1 min-w-[220px]">
              <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && runSearch()}
                placeholder="Symbol or company name (min 2 chars)"
                className="pl-9"
              />
            </div>
            <Button variant="outline" onClick={runSearch} loading={searching}>
              <RefreshCw className="size-4" /> Search
            </Button>
            {searchResults.length > 0 && (
              <Button onClick={subscribeAll}>
                <Plus className="size-4" /> Subscribe all ({searchResults.length})
              </Button>
            )}
          </div>

          {searchResults.length > 0 && (
            <div className="max-h-80 overflow-y-auto rounded-md border border-border bg-muted/10 scrollbar-thin">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-card text-muted-foreground">
                  <tr>
                    <th className="px-3 py-2 text-left">Symbol</th>
                    <th className="px-3 py-2 text-left">Name</th>
                    <th className="px-3 py-2 text-left">Type</th>
                    <th className="px-3 py-2 text-left">Expiry</th>
                    <th className="px-3 py-2 text-right">Lot</th>
                    <th className="px-3 py-2 text-right">Strike</th>
                    <th className="px-3 py-2"></th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {searchResults.map((r: any) => {
                    const subs = settings.subscribedInstruments ?? [];
                    const isSub = subs.some((s: any) => s.token === r.token);
                    return (
                      <tr key={r.token} className="hover:bg-muted/30">
                        <td className="px-3 py-1.5 font-medium">{r.symbol}</td>
                        <td className="max-w-[260px] truncate px-3 py-1.5">{r.name}</td>
                        <td className="px-3 py-1.5">{r.instrumentType || r.exchange}</td>
                        <td className="px-3 py-1.5">{r.expiry || "—"}</td>
                        <td className="px-3 py-1.5 text-right">{r.lotSize}</td>
                        <td className="px-3 py-1.5 text-right">{r.strike ?? "—"}</td>
                        <td className="px-3 py-1.5 text-right">
                          {isSub ? (
                            <span className="text-[10px] uppercase text-primary">subscribed</span>
                          ) : (
                            <Button size="sm" variant="outline" onClick={() => subscribe(r)}>
                              <Plus className="size-3" /> Subscribe
                            </Button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>}

      {activeAccount === 0 && <Card>
        <CardHeader>
          <CardTitle>Subscribed instruments</CardTitle>
          <CardDescription>{settings.subscribedInstruments.length} streaming via Kite WebSocket</CardDescription>
        </CardHeader>
        <CardContent>
          <DataTable
            columns={subCols}
            rows={settings.subscribedInstruments}
            keyExtractor={(r) => String(r.token)}
            empty="No subscriptions yet — search above and click subscribe."
          />
        </CardContent>
      </Card>}
    </div>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between border-b border-border/40 py-1.5 last:border-b-0">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-sm">{value}</span>
    </div>
  );
}

function DiagRow({ label, ok, detail }: { label: string; ok: boolean; detail: string }) {
  return (
    <div className="flex items-start gap-2 rounded-md border border-border bg-muted/10 p-2 text-xs">
      {ok ? (
        <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-primary" />
      ) : (
        <XCircle className="mt-0.5 size-4 shrink-0 text-destructive" />
      )}
      <div className="min-w-0 flex-1">
        <div className="font-medium">{label}</div>
        <div className="break-words text-muted-foreground">{detail}</div>
      </div>
    </div>
  );
}
