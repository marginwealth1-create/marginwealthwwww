"use client";

import { useState } from "react";
import { Check, X } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { useAcceptTerms, useUserTerms } from "@/lib/useSupport";
import { Button } from "@/components/ui/button";

/**
 * Renders a modal Terms & Conditions popup once per user, gated by
 * the admin-side toggle. Visibility logic:
 *
 *   - GET /user/support/terms returns `needs_accept=true` when the
 *     resolved (cascaded) admin has enabled T&C AND the user hasn't
 *     accepted the current version yet.
 *   - On Accept: POST /user/support/terms/accept stamps the user's
 *     `terms_accepted_at`; modal closes and never reappears until
 *     the admin updates the text.
 *   - On Close (Skip): modal is hidden for this session only — the
 *     next page load will show it again because the backend still
 *     reports `needs_accept=true`. Operator choice: hard-stop vs
 *     soft-prompt. The user wanted both options, so we honour the
 *     skip locally without stamping the server.
 *
 * Rendered once at the dashboard layout level so every authenticated
 * route gets the gate without each page wiring it manually.
 */
export function TermsGate() {
  const qc = useQueryClient();
  const { data, isLoading } = useUserTerms();
  const acceptMut = useAcceptTerms();
  const [skipped, setSkipped] = useState(false);

  if (isLoading || !data) return null;
  if (!data.needs_accept) return null;
  if (skipped) return null;

  async function handleAccept() {
    try {
      await acceptMut.mutateAsync();
      await qc.invalidateQueries({ queryKey: ["user", "terms"] });
      toast.success("Thanks — terms accepted");
    } catch (e: any) {
      toast.error(e?.message || "Could not save your acceptance");
    }
  }

  return (
    <div className="fixed inset-0 z-[100] flex items-end justify-center bg-black/60 p-3 backdrop-blur-sm sm:items-center sm:p-6">
      <div className="relative w-full max-w-2xl overflow-hidden rounded-2xl border border-border bg-card shadow-2xl">
        <button
          type="button"
          onClick={() => setSkipped(true)}
          aria-label="Close"
          title="Skip for now"
          className="absolute right-3 top-3 grid size-8 place-items-center rounded-full bg-muted/40 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          <X className="size-4" />
        </button>

        <div className="border-b border-border bg-gradient-to-br from-primary/10 via-primary/5 to-transparent px-5 py-4">
          <h2 className="text-lg font-bold tracking-tight sm:text-xl">
            Terms &amp; Conditions
          </h2>
          <p className="mt-0.5 text-xs text-muted-foreground sm:text-sm">
            Please read and accept to continue using your account.
          </p>
        </div>

        <div className="max-h-[55vh] overflow-y-auto whitespace-pre-wrap px-5 py-4 text-sm leading-relaxed text-foreground/90 sm:text-[15px]">
          {data.text || "—"}
        </div>

        <div className="flex flex-col-reverse gap-2 border-t border-border bg-muted/20 px-5 py-3 sm:flex-row sm:items-center sm:justify-end">
          <Button
            type="button"
            variant="outline"
            onClick={() => setSkipped(true)}
            disabled={acceptMut.isPending}
            className="w-full sm:w-auto"
          >
            Close
          </Button>
          <Button
            type="button"
            onClick={handleAccept}
            loading={acceptMut.isPending}
            className="w-full gap-1.5 sm:w-auto"
          >
            <Check className="size-4" />
            I Accept
          </Button>
        </div>
      </div>
    </div>
  );
}
