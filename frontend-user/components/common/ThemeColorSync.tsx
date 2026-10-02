"use client";

import { useEffect } from "react";
import { useTheme } from "next-themes";

/**
 * Keeps the Android status-bar / PWA chrome colour in sync with the
 * current app theme. The static `<meta name="theme-color">` injected by
 * Next.js can only react to `prefers-color-scheme`, so when the user
 * forces light/dark from Profile → Preferences (overriding the OS) the
 * status bar would otherwise stay on the system-default colour — leading
 * to a green or near-black band above a white app surface (the user
 * flagged this as "mere sabse top me green color a rha hai esko theme se
 * match karo").
 *
 * Implementation note — DO NOT remove existing theme-color meta nodes.
 * The previous version called `parentElement.removeChild(el)` on every
 * matching tag and then appended a fresh one, which tripped React's
 * reconciler with "Cannot read properties of null (reading 'removeChild')"
 * during the next commit phase (the SSR-emitted metas were still tracked
 * by React's HMR / head manager). Updating attributes in place leaves
 * the nodes where React expects them, and our own appended tag wins the
 * cascade because the browser picks the first applicable theme-color
 * (we strip `media` from all of them so every tag matches every
 * scheme).
 */
export function ThemeColorSync() {
  const { resolvedTheme } = useTheme();

  useEffect(() => {
    if (typeof document === "undefined") return;
    const colour = resolvedTheme === "light" ? "#ffffff" : "#0a0a0a";

    const metas = document.querySelectorAll('meta[name="theme-color"]');
    if (metas.length === 0) {
      const m = document.createElement("meta");
      m.setAttribute("name", "theme-color");
      m.setAttribute("content", colour);
      document.head.appendChild(m);
      return;
    }
    metas.forEach((m) => {
      m.setAttribute("content", colour);
      // Drop any media query so this tag applies regardless of the OS
      // colour scheme — the app's resolvedTheme is the source of truth.
      if (m.hasAttribute("media")) m.removeAttribute("media");
    });
  }, [resolvedTheme]);

  return null;
}
