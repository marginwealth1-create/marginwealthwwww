"use client";

/**
 * Shared open/close state for the mobile nav drawer.
 *
 * Lives in React Context (not zustand) on purpose — it's pure UI state,
 * never persisted, and only the topbar trigger + the drawer/bottom-bar
 * consume it. Keeping it out of the global auth/zustand stores avoids
 * coupling UI chrome to business state.
 */

import * as React from "react";

type MobileNavCtx = {
  open: boolean;
  setOpen: (v: boolean) => void;
  toggle: () => void;
};

const Ctx = React.createContext<MobileNavCtx | null>(null);

export function MobileNavProvider({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = React.useState(false);
  const value = React.useMemo<MobileNavCtx>(
    () => ({ open, setOpen, toggle: () => setOpen((o) => !o) }),
    [open]
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useMobileNav(): MobileNavCtx {
  const v = React.useContext(Ctx);
  // Tolerate being called outside the provider (e.g. server-rendered
  // fragments before hydration) — return a no-op so nothing crashes.
  if (!v) return { open: false, setOpen: () => {}, toggle: () => {} };
  return v;
}
