"use client";

import * as React from "react";
import { Download, CheckCircle2, Share, Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * "Install web app" button used on the admin login page.
 *
 * Two install flows are handled here because the platforms expose
 * them very differently:
 *
 *   1. Chromium (Android Chrome, Desktop Chrome / Edge / Brave)
 *      The browser fires `beforeinstallprompt` when the manifest +
 *      icon criteria are met. We intercept the event, stash it, and
 *      let the admin trigger the native install dialog with a click.
 *
 *   2. iOS Safari + iPadOS Safari
 *      Apple does NOT expose any install API. The only way is the
 *      manual "Share → Add to Home Screen" gesture. So on iOS we
 *      show the button anyway and, on click, open a small inline
 *      sheet that walks the admin through the two taps.
 *
 * If the app is already running as a PWA (standalone display mode)
 * we render a muted "Installed" pill so the admin doesn't see a
 * stale "Install" CTA every time they reopen the panel.
 */
export function InstallPWAButton({ className }: { className?: string }) {
  const [deferred, setDeferred] = React.useState<BeforeInstallPromptEvent | null>(null);
  const [installed, setInstalled] = React.useState(false);
  const [isIOS, setIsIOS] = React.useState(false);
  const [showIOSHelp, setShowIOSHelp] = React.useState(false);

  React.useEffect(() => {
    if (typeof window === "undefined") return;

    // Detect "already installed" via standalone display-mode.
    const mql = window.matchMedia("(display-mode: standalone)");
    const updateStandalone = () =>
      setInstalled(mql.matches || (window.navigator as any).standalone === true);
    updateStandalone();
    mql.addEventListener?.("change", updateStandalone);

    // iOS Safari detection — used to decide whether to show the
    // "Add to Home Screen" walkthrough. We deliberately treat both
    // iPhone and iPad-on-Safari (which now reports as Mac).
    const ua = window.navigator.userAgent;
    const iPad = /iPad|Macintosh/i.test(ua) && "ontouchend" in document;
    setIsIOS(/iPhone|iPod/.test(ua) || iPad);

    function onBeforeInstall(e: Event) {
      // Stop the browser's mini-info-bar so we can offer the install
      // from inside our UI instead.
      e.preventDefault();
      setDeferred(e as BeforeInstallPromptEvent);
    }
    function onInstalled() {
      setDeferred(null);
      setInstalled(true);
    }
    window.addEventListener("beforeinstallprompt", onBeforeInstall);
    window.addEventListener("appinstalled", onInstalled);
    return () => {
      window.removeEventListener("beforeinstallprompt", onBeforeInstall);
      window.removeEventListener("appinstalled", onInstalled);
      mql.removeEventListener?.("change", updateStandalone);
    };
  }, []);

  // ── States ────────────────────────────────────────────────────────
  if (installed) {
    return (
      <div
        className={cn(
          "inline-flex items-center gap-1.5 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-1.5 text-xs font-medium text-emerald-600 dark:text-emerald-300",
          className,
        )}
      >
        <CheckCircle2 className="size-3.5" />
        App installed
      </div>
    );
  }

  // Chromium happy path — native prompt
  if (deferred) {
    return (
      <Button
        type="button"
        variant="outline"
        size="sm"
        className={cn(
          "h-9 w-full gap-1.5 border-primary/40 bg-primary/10 text-primary hover:bg-primary hover:text-primary-foreground",
          className,
        )}
        onClick={async () => {
          try {
            await deferred.prompt();
            // Always clear; even on dismiss the event is single-use.
            setDeferred(null);
          } catch {
            setDeferred(null);
          }
        }}
      >
        <Download className="size-4" />
        Install web app
      </Button>
    );
  }

  // iOS — manual walkthrough (no install API exists on Safari).
  if (isIOS) {
    return (
      <>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className={cn(
            "h-9 w-full gap-1.5 border-primary/40 bg-primary/10 text-primary hover:bg-primary hover:text-primary-foreground",
            className,
          )}
          onClick={() => setShowIOSHelp(true)}
        >
          <Download className="size-4" />
          Install on iPhone
        </Button>
        {showIOSHelp && (
          <IOSInstallSheet onClose={() => setShowIOSHelp(false)} />
        )}
      </>
    );
  }

  // Everything else (e.g. Firefox desktop, in-app browsers) — the
  // browser hasn't told us we can install, so we hide the button
  // rather than show a CTA that does nothing on click.
  return null;
}

/**
 * Inline help sheet for iOS Safari users. iOS doesn't fire
 * beforeinstallprompt — the user must tap the Share icon at the
 * bottom of Safari and choose "Add to Home Screen". We illustrate
 * those two taps so a non-technical admin can follow without
 * leaving the page.
 */
function IOSInstallSheet({ onClose }: { onClose: () => void }) {
  return (
    <div
      role="dialog"
      aria-modal="true"
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/50 p-3 sm:items-center"
      onClick={onClose}
    >
      <div
        className="w-full max-w-sm rounded-2xl border border-border bg-card p-4 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold">Install on iPhone</h3>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="grid size-7 place-items-center rounded-md text-muted-foreground hover:bg-muted"
          >
            <X className="size-4" />
          </button>
        </div>
        <ol className="space-y-3 text-sm">
          <li className="flex items-start gap-3">
            <span className="grid size-7 shrink-0 place-items-center rounded-full bg-primary/15 text-xs font-semibold text-primary">
              1
            </span>
            <span className="leading-snug">
              Tap the <Share className="inline size-4 align-text-bottom text-primary" />{" "}
              <span className="font-medium">Share</span> icon at the bottom of Safari.
            </span>
          </li>
          <li className="flex items-start gap-3">
            <span className="grid size-7 shrink-0 place-items-center rounded-full bg-primary/15 text-xs font-semibold text-primary">
              2
            </span>
            <span className="leading-snug">
              Scroll and choose{" "}
              <span className="inline-flex items-center gap-1 rounded-md border border-border bg-muted/40 px-1.5 py-0.5 font-medium">
                Add to Home Screen <Plus className="size-3.5" />
              </span>
              .
            </span>
          </li>
          <li className="flex items-start gap-3">
            <span className="grid size-7 shrink-0 place-items-center rounded-full bg-primary/15 text-xs font-semibold text-primary">
              3
            </span>
            <span className="leading-snug">
              Tap <span className="font-medium">Add</span> in the top-right. The MP Admin icon
              will appear on your home screen and open like a native app.
            </span>
          </li>
        </ol>
        <Button
          type="button"
          variant="outline"
          className="mt-4 w-full"
          onClick={onClose}
        >
          Got it
        </Button>
      </div>
    </div>
  );
}

/** Chromium-specific extension to the WindowEventMap. */
interface BeforeInstallPromptEvent extends Event {
  readonly platforms: string[];
  readonly userChoice: Promise<{ outcome: "accepted" | "dismissed"; platform: string }>;
  prompt: () => Promise<void>;
}
