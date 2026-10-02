"use client";

import { LogOut, Menu, ShieldAlert, RefreshCw } from "lucide-react";
import { useQueryClient, useIsFetching } from "@tanstack/react-query";
import { useAdminAuthStore } from "@/stores/authStore";
import { Button } from "@/components/ui/button";
import { ThemeToggle } from "@/components/common/ThemeToggle";
import { NotificationBell } from "@/components/layout/NotificationBell";
import { useMobileNav } from "@/components/layout/MobileNavContext";
import { BrandLogo } from "@/components/layout/BrandLogo";

export function AdminTopBar() {
  const admin = useAdminAuthStore((s) => s.admin);
  const logout = useAdminAuthStore((s) => s.logout);
  const { setOpen } = useMobileNav();
  const qc = useQueryClient();
  // Count of in-flight queries across the app — drives the spin animation
  // so the admin gets immediate feedback that a refresh is running.
  const fetching = useIsFetching();

  return (
    <header className="sticky top-0 z-20 flex h-14 items-center gap-2 border-b border-border bg-background/85 px-3 backdrop-blur sm:gap-3 sm:px-4">
      {/* Mobile-only hamburger. Desktop sidebar is always visible so no
          trigger is needed there. */}
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="tap-target -ml-1 inline-flex items-center justify-center rounded-md text-foreground/80 hover:bg-accent hover:text-foreground md:hidden"
        aria-label="Open navigation menu"
      >
        <Menu className="size-5" />
      </button>

      {/* Compact brand on mobile only (desktop has the brand in the sidebar). */}
      <div className="md:hidden">
        <BrandLogo size="sm" />
      </div>

      <div className="hidden text-xs text-muted-foreground md:block">
        Signed in as{" "}
        <span className="text-foreground">{admin?.full_name ?? "Admin"}</span>
        {" · "}
        <span className="text-primary">
          {admin?.role === "BROKER" && admin?.assigned_broker_id
            ? "SUB-BROKER"
            : admin?.role}
        </span>
      </div>

      {/* Audit banner: full text on sm+, icon only on phones to save space. */}
      <div className="ml-auto inline-flex items-center gap-1.5 rounded-md border border-destructive/30 bg-destructive/10 px-2 py-1 text-xs text-destructive sm:gap-2 sm:px-2.5">
        <ShieldAlert className="size-3.5" />
        <span className="hidden sm:inline">Live system — actions are audited</span>
        <span className="sr-only sm:hidden">Live system — actions are audited</span>
      </div>

      {/* Global refresh — invalidates every query so the current page
          refetches immediately and the rest refresh on next visit.
          keepPreviousData + refetchOnMount keep the existing data on screen
          while it reloads, so this never flashes a skeleton / empty state.
          The icon spins while any fetch is in flight. */}
      <Button
        variant="ghost"
        size="icon"
        aria-label="Refresh data"
        title="Refresh"
        onClick={() => void qc.invalidateQueries()}
      >
        <RefreshCw className={`size-4 ${fetching > 0 ? "animate-spin" : ""}`} />
      </Button>
      <NotificationBell />
      <ThemeToggle />
      {/* Desktop sign-out (mobile uses the one inside the drawer). */}
      <Button
        variant="ghost"
        size="icon"
        aria-label="Sign out"
        className="hidden md:inline-flex"
        onClick={() => logout().then(() => (window.location.href = "/login"))}
      >
        <LogOut className="size-4" />
      </Button>
    </header>
  );
}
