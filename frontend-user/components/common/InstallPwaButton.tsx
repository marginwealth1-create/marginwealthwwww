"use client";

import { useEffect, useState } from "react";
import { Download, Share2, Smartphone, MoreVertical, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface InstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

/**
 * "Install App" button. Works across all browsers:
 *
 *   Chrome/Edge/Samsung: captures `beforeinstallprompt`, shows native prompt
 *   iOS Safari: shows manual "Share → Add to Home Screen" instructions
 *   Fallback: if `beforeinstallprompt` was consumed or never fired, shows
 *             a manual-install dialog instead of silently doing nothing
 *
 * The button is ALWAYS visible (not gated behind `beforeinstallprompt`).
 * Previously it was hidden until the browser event fired, which meant:
 *   • First-time visitors on custom domains saw nothing (event fires
 *     only after manifest + SW are validated — race with React hydration)
 *   • If the user dismissed the native prompt once, the button vanished
 *     forever (event consumed, Chrome doesn't re-fire for weeks)
 *
 * Now the button is always rendered. Click behaviour:
 *   1. If native prompt available → use it (best UX)
 *   2. If not → show a fallback dialog with browser-specific instructions
 */
export function InstallPwaButton({
  variant = "default",
  className,
  label = "Install app",
}: {
  variant?: "default" | "compact";
  className?: string;
  /** Button text — defaults to "Install app"; the branded /download page
   *  passes "Download App" to match the shared link's wording. */
  label?: string;
}) {
  const [installed, setInstalled] = useState(false);
  const [hasNativePrompt, setHasNativePrompt] = useState(false);
  const [showFallback, setShowFallback] = useState(false);
  const [isIos, setIsIos] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined") return;

    const standalone =
      window.matchMedia?.("(display-mode: standalone)").matches ||
      (window.navigator as any).standalone === true;
    if (standalone) {
      setInstalled(true);
      return;
    }

    const ua = window.navigator.userAgent;
    setIsIos(/iPad|iPhone|iPod/.test(ua) && !(window as any).MSStream);

    if ((window as any).__mpInstallPrompt) setHasNativePrompt(true);
    const onAvail = () => setHasNativePrompt(true);
    const onInstalled = () => {
      setHasNativePrompt(false);
      setInstalled(true);
    };
    window.addEventListener("mp:install-available", onAvail);
    window.addEventListener("mp:installed", onInstalled);
    return () => {
      window.removeEventListener("mp:install-available", onAvail);
      window.removeEventListener("mp:installed", onInstalled);
    };
  }, []);

  if (installed) return null;

  async function handleClick() {
    const evt = (window as any).__mpInstallPrompt as
      | InstallPromptEvent
      | undefined;
    if (evt) {
      try {
        await evt.prompt();
        const choice = await evt.userChoice;
        if (choice?.outcome === "accepted") {
          (window as any).__mpInstallPrompt = null;
          setHasNativePrompt(false);
          setInstalled(true);
          return;
        }
        // User DISMISSED the native prompt — show manual instructions
        // as fallback (Chrome won't re-fire beforeinstallprompt for weeks)
        (window as any).__mpInstallPrompt = null;
        setHasNativePrompt(false);
        setShowFallback(true);
        return;
      } catch {
        // Browser rejected the prompt (expired, already used, etc.)
      }
    }
    // Native prompt not available OR prompt() threw — always show fallback
    setShowFallback(true);
  }

  const isAndroid =
    typeof navigator !== "undefined" && /android/i.test(navigator.userAgent);

  if (variant === "compact") {
    return (
      <>
        <button
          type="button"
          onClick={handleClick}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full border border-primary/30 bg-primary/10 px-3 py-1.5 text-xs font-semibold text-primary hover:bg-primary/20 transition-colors",
            className,
          )}
        >
          <Download className="size-3.5" />
          {label}
        </button>
        {showFallback && (
          <FallbackDialog
            isIos={isIos}
            isAndroid={isAndroid}
            onClose={() => setShowFallback(false)}
          />
        )}
      </>
    );
  }

  return (
    <>
      <Button
        onClick={handleClick}
        className={cn("h-11 gap-2 px-5 text-sm font-semibold", className)}
      >
        <Download className="size-4" /> {label}
      </Button>
      {showFallback && (
        <FallbackDialog
          isIos={isIos}
          isAndroid={isAndroid}
          onClose={() => setShowFallback(false)}
        />
      )}
    </>
  );
}

/**
 * Fallback dialog when the native `beforeinstallprompt` isn't available.
 * Shows step-by-step instructions specific to the user's browser/OS.
 */
function FallbackDialog({
  isIos,
  isAndroid,
  onClose,
}: {
  isIos: boolean;
  isAndroid: boolean;
  onClose: () => void;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 p-4 sm:items-center">
      <div className="relative w-full max-w-sm rounded-2xl border border-border bg-card p-5 shadow-xl">
        <button
          type="button"
          onClick={onClose}
          className="absolute right-3 top-3 text-muted-foreground hover:text-foreground"
        >
          <X className="size-4" />
        </button>

        <div className="flex items-center gap-2.5 text-base font-semibold">
          <Smartphone className="size-5 text-primary" />
          Install this app
        </div>

        <p className="mt-2 text-sm text-muted-foreground">
          Add this app to your home screen for quick access — works like a native app, no app store needed.
        </p>

        <div className="mt-4 space-y-3">
          {isIos ? (
            <>
              <Step
                num={1}
                icon={<Share2 className="size-4" />}
                text={
                  <>
                    Tap the <strong>Share</strong> button in Safari
                    (bottom bar)
                  </>
                }
              />
              <Step
                num={2}
                icon={<Download className="size-4" />}
                text={
                  <>
                    Scroll down and tap{" "}
                    <strong>Add to Home Screen</strong>
                  </>
                }
              />
              <Step num={3} text={<>Tap <strong>Add</strong> to confirm</>} />
            </>
          ) : (
            <>
              <div className="rounded-lg bg-orange-500/10 border border-orange-500/20 p-2.5 text-xs text-orange-300">
                <strong>Already have an older version installed?</strong> Uninstall it first from your phone settings, then come back and tap Install again.
              </div>
              <Step
                num={1}
                icon={<MoreVertical className="size-4" />}
                text={
                  <>
                    Tap the <strong>⋮ menu</strong> (top-right corner in Chrome)
                  </>
                }
              />
              <Step
                num={2}
                icon={<Download className="size-4" />}
                text={
                  isAndroid ? (
                    <>
                      Tap <strong>Add to Home screen</strong> or{" "}
                      <strong>Install app</strong>
                    </>
                  ) : (
                    <>
                      Tap <strong>Install app</strong> or{" "}
                      <strong>Create shortcut</strong>
                    </>
                  )
                }
              />
              <Step
                num={3}
                text={<>Tap <strong>Install</strong> to confirm</>}
              />
            </>
          )}
        </div>

        <button
          type="button"
          onClick={onClose}
          className="mt-4 w-full rounded-lg bg-primary/10 py-2 text-sm font-semibold text-primary hover:bg-primary/20 transition-colors"
        >
          Got it
        </button>
      </div>
    </div>
  );
}

function Step({
  num,
  icon,
  text,
}: {
  num: number;
  icon?: React.ReactNode;
  text: React.ReactNode;
}) {
  return (
    <div className="flex items-start gap-2.5">
      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary/15 text-xs font-bold text-primary">
        {num}
      </span>
      <div className="flex items-center gap-1.5 text-sm">
        {icon && <span className="text-muted-foreground">{icon}</span>}
        <span>{text}</span>
      </div>
    </div>
  );
}
