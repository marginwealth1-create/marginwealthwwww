"use client";

import Link from "next/link";
import { FlaskConical } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

interface DemoUpgradeDialogProps {
  open: boolean;
  onClose: () => void;
}

export function DemoUpgradeDialog({ open, onClose }: DemoUpgradeDialogProps) {
  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-sm text-center">
        <DialogHeader className="items-center gap-3 pb-2">
          <div className="grid size-14 place-items-center rounded-2xl bg-amber-500/10 ring-1 ring-amber-500/20">
            <FlaskConical className="size-7 text-amber-500" />
          </div>
          <DialogTitle className="text-lg">Demo Account</DialogTitle>
          <DialogDescription className="text-sm leading-relaxed">
            You&apos;re trading with <span className="font-semibold text-foreground">₹50,00,000 virtual money</span>.
            Deposits and withdrawals are not available on demo accounts.
          </DialogDescription>
        </DialogHeader>

        <p className="text-xs text-muted-foreground">
          Open a real account to deposit funds and trade with real money.
        </p>

        <DialogFooter className="flex-col gap-2 sm:flex-col">
          <Button asChild className="w-full" onClick={onClose}>
            <Link href="/register">Open Real Account</Link>
          </Button>
          <Button variant="ghost" className="w-full text-muted-foreground" onClick={onClose}>
            Continue with Demo
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
