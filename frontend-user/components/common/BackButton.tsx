"use client";

import { useRouter } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Round back-arrow. Defaults to browser-history back (`router.back()`); pass
 * `href` to send the user to a specific route instead (used when a page can be
 * opened directly and has no history to pop). Sized as a 36px touch target so
 * it's comfortable on mobile.
 */
export function BackButton({
  href,
  className,
}: {
  href?: string;
  className?: string;
}) {
  const router = useRouter();
  return (
    <button
      type="button"
      aria-label="Go back"
      onClick={() => (href ? router.push(href) : router.back())}
      className={cn(
        "grid size-9 shrink-0 place-items-center rounded-full border border-border text-muted-foreground transition-colors hover:bg-muted/40 hover:text-foreground",
        className,
      )}
    >
      <ArrowLeft className="size-5" />
    </button>
  );
}
