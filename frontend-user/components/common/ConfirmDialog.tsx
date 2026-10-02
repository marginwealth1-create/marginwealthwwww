"use client";

import { useState } from "react";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * App-themed confirmation dialog — replacement for `window.confirm()`.
 *
 * Drop-in for one-shot destructive prompts ("Square off ALL positions?",
 * "Cancel this order?"). Caller controls open state and gets a Promise-less
 * onConfirm/onCancel pair so it composes cleanly with React Query mutations
 * (no awaiting a global confirm modal).
 */
interface Props {
  open: boolean;
  title: string;
  description?: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** When true (default) the confirm button gets the destructive red palette. */
  destructive?: boolean;
  /** Fired BEFORE the dialog closes; if it throws/rejects the dialog stays open. */
  onConfirm: () => void | Promise<void>;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  destructive = true,
  onConfirm,
  onCancel,
}: Props) {
  const [pending, setPending] = useState(false);

  async function handleConfirm() {
    if (pending) return;
    setPending(true);
    try {
      await onConfirm();
    } finally {
      setPending(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o && !pending) onCancel(); }}>
      <DialogContent className="max-w-sm gap-3 p-5">
        <DialogTitle className="text-base font-semibold">{title}</DialogTitle>
        {description && (
          <div className="text-sm text-muted-foreground">{description}</div>
        )}
        <div className="mt-1 grid grid-cols-2 gap-2">
          <Button variant="outline" onClick={onCancel} disabled={pending}>
            {cancelLabel}
          </Button>
          <Button
            onClick={handleConfirm}
            disabled={pending}
            className={cn(
              destructive &&
                "bg-destructive text-destructive-foreground hover:bg-destructive/90",
            )}
          >
            {pending ? "Working…" : confirmLabel}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
