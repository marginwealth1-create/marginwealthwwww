"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { ShieldCheck, Copy, CheckCircle2 } from "lucide-react";
import { AuthAPI, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export default function TwoFAEnrollPage() {
  const router = useRouter();
  const [secret, setSecret] = useState<string | null>(null);
  const [uri, setUri] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const r = await AuthAPI.twoFASetup();
        setSecret(r.secret);
        setUri(r.provisioning_uri);
      } catch (err) {
        toast.error(err instanceof ApiError ? err.message : "Could not start 2FA setup");
      }
    })();
  }, []);

  async function enable() {
    setBusy(true);
    try {
      await AuthAPI.twoFAEnable(code);
      toast.success("Two-factor authentication enabled");
      router.push("/profile");
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Verification failed");
    } finally {
      setBusy(false);
    }
  }

  function copySecret() {
    if (!secret) return;
    navigator.clipboard.writeText(secret);
    setCopied(true);
    toast.success("Secret copied to clipboard");
    setTimeout(() => setCopied(false), 2000);
  }

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="space-y-2">
        <div className="mb-4 grid size-12 place-items-center rounded-2xl bg-primary/10 text-primary ring-1 ring-primary/15">
          <ShieldCheck className="size-5" />
        </div>
        <h2 className="text-3xl font-bold tracking-tight">
          Enable two-factor authentication
        </h2>
        <p className="text-sm text-muted-foreground">
          Scan the secret with Google Authenticator, Authy, or 1Password and enter the 6-digit code.
        </p>
      </div>

      {secret ? (
        <div className="space-y-6">
          {/* Secret display */}
          <div className="space-y-3">
            <div className="rounded-xl border border-border/40 bg-muted/20 p-4">
              <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                Secret Key
              </div>
              <div className="flex items-center justify-between gap-2">
                <code className="break-all font-mono text-xs text-foreground">
                  {secret}
                </code>
                <button
                  onClick={copySecret}
                  className="grid size-8 shrink-0 place-items-center rounded-lg bg-primary/10 text-primary transition-colors hover:bg-primary/20"
                  aria-label="Copy secret"
                >
                  {copied ? (
                    <CheckCircle2 className="size-3.5" />
                  ) : (
                    <Copy className="size-3.5" />
                  )}
                </button>
              </div>
            </div>

            {uri && (
              <div className="rounded-xl border border-border/40 bg-muted/20 p-4">
                <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                  Provisioning URI
                </div>
                <code className="break-all font-mono text-[11px] text-muted-foreground">
                  {uri}
                </code>
              </div>
            )}
          </div>

          {/* Code input */}
          <div className="space-y-2">
            <Label htmlFor="code" className="text-sm font-medium">
              6-digit verification code
            </Label>
            <div className="relative">
              <ShieldCheck className="absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                id="code"
                inputMode="numeric"
                maxLength={6}
                placeholder="000000"
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
                className="h-12 rounded-xl border-border/60 bg-muted/30 pl-10 text-center font-mono text-lg tracking-[0.5em] transition-colors focus:border-primary/50 focus:bg-background"
              />
            </div>
          </div>

          <Button
            onClick={enable}
            className="h-12 w-full rounded-xl text-sm font-semibold shadow-lg shadow-primary/20"
            loading={busy}
            disabled={code.length !== 6}
          >
            Verify &amp; enable
          </Button>
        </div>
      ) : (
        <div className="flex min-h-[200px] items-center justify-center">
          <div className="text-center">
            <div className="mx-auto mb-3 size-8 animate-spin rounded-full border-2 border-primary border-t-transparent" />
            <p className="text-sm text-muted-foreground">Generating secret…</p>
          </div>
        </div>
      )}
    </div>
  );
}
