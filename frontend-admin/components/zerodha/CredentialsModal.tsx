"use client";

import { useState } from "react";
import { Eye, EyeOff, ShieldAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";

interface Props {
  open: boolean;
  onClose: () => void;
  /**
   * True when the saved row already has encrypted creds. We tweak the
   * copy ("Update credentials" vs "Save credentials") and the warning
   * banner so the operator knows they're overwriting an existing set.
   */
  hasExisting: boolean;
  /** Bubble up errors so the parent can toast.error() them. */
  onSubmit: (body: {
    username: string;
    password: string;
    totp_secret: string;
  }) => Promise<void>;
}

/**
 * Modal that captures the three Kite Connect credentials the daily
 * auto-login scheduler needs:
 *   • Kite Client ID (e.g. "ZK1234")
 *   • Kite login password
 *   • TOTP secret (base32 string from the Kite TOTP setup screen)
 *
 * Backend AES-256-GCM-encrypts all three at rest. The modal never reads
 * existing values back — for security the API always returns masked
 * snapshots, so editing means re-entering all three fields fresh.
 */
export function CredentialsModal({
  open,
  onClose,
  hasExisting,
  onSubmit,
}: Props) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [totpSecret, setTotpSecret] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [showTotpSecret, setShowTotpSecret] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function resetAndClose() {
    setUsername("");
    setPassword("");
    setTotpSecret("");
    setShowPassword(false);
    setShowTotpSecret(false);
    setError(null);
    setSubmitting(false);
    onClose();
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    const u = username.trim();
    const p = password;
    const t = totpSecret.trim().replace(/\s+/g, "").toUpperCase();
    if (!u || !p || !t) {
      setError("All three fields are required.");
      return;
    }
    if (t.length < 8) {
      setError("TOTP secret looks too short — paste the full base32 string.");
      return;
    }
    setSubmitting(true);
    try {
      await onSubmit({ username: u, password: p, totp_secret: t });
      resetAndClose();
    } catch (err: unknown) {
      const msg =
        err instanceof Error ? err.message : "Failed to save credentials";
      setError(msg);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && !submitting && resetAndClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {hasExisting ? "Update Kite Credentials" : "Save Kite Credentials"}
          </DialogTitle>
        </DialogHeader>

        <div className="mb-3 flex items-start gap-2 rounded-md border border-yellow-500/30 bg-yellow-500/5 px-3 py-2 text-xs text-yellow-200">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
          <div>
            These credentials enable automated daily login on your behalf.
            They&apos;re AES-256-GCM encrypted at rest, but anyone who saves
            them effectively grants the platform full Kite read access. Use a
            no-trading Kite API key for defense in depth.
          </div>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="kite-username">Kite Client ID</Label>
            <Input
              id="kite-username"
              autoComplete="off"
              spellCheck={false}
              value={username}
              placeholder="ZK1234"
              onChange={(e) => setUsername(e.target.value)}
            />
            {hasExisting && (
              <p className="text-[11px] text-muted-foreground">
                Existing credentials are stored — re-enter all three fields
                to overwrite.
              </p>
            )}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="kite-password">Password</Label>
            <div className="relative">
              <Input
                id="kite-password"
                type={showPassword ? "text" : "password"}
                autoComplete="new-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
              <button
                type="button"
                onClick={() => setShowPassword((v) => !v)}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                tabIndex={-1}
                aria-label={showPassword ? "Hide password" : "Show password"}
              >
                {showPassword ? (
                  <EyeOff className="h-4 w-4" />
                ) : (
                  <Eye className="h-4 w-4" />
                )}
              </button>
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="kite-totp">TOTP Secret (base32)</Label>
            <div className="relative">
              <Input
                id="kite-totp"
                type={showTotpSecret ? "text" : "password"}
                autoComplete="off"
                spellCheck={false}
                placeholder="JBSWY3DPEHPK3PXP6X7K…"
                value={totpSecret}
                onChange={(e) => setTotpSecret(e.target.value)}
              />
              <button
                type="button"
                onClick={() => setShowTotpSecret((v) => !v)}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                tabIndex={-1}
                aria-label={
                  showTotpSecret ? "Hide TOTP secret" : "Show TOTP secret"
                }
              >
                {showTotpSecret ? (
                  <EyeOff className="h-4 w-4" />
                ) : (
                  <Eye className="h-4 w-4" />
                )}
              </button>
            </div>
            <p className="text-[11px] text-muted-foreground">
              Find this under Kite profile → Password &amp; Security →
              External TOTP → Reset → &quot;Can&apos;t scan?&quot;. Same secret
              must also be in your Authy app.
            </p>
          </div>

          {error && (
            <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {error}
            </div>
          )}

          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              onClick={resetAndClose}
              disabled={submitting}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={submitting}>
              {submitting ? "Saving…" : "Save credentials"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
