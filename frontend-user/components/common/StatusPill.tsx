import { cn } from "@/lib/utils";

const PALETTES: Record<string, string> = {
  ACTIVE: "bg-primary/15 text-primary",
  EXECUTED: "bg-primary/15 text-primary",
  APPROVED: "bg-primary/15 text-primary",
  COMPLETED: "bg-primary/15 text-primary",
  OPEN: "bg-blue-500/15 text-blue-400",
  PENDING: "bg-amber-500/15 text-amber-400",
  PARTIAL: "bg-amber-500/15 text-amber-400",
  PROCESSING: "bg-amber-500/15 text-amber-400",
  REJECTED: "bg-destructive/15 text-destructive",
  CANCELLED: "bg-muted text-muted-foreground",
  CLOSED: "bg-muted text-muted-foreground",
  EXPIRED: "bg-muted text-muted-foreground",
  BLOCKED: "bg-destructive/15 text-destructive",
  BUY: "bg-buy/15 text-buy",
  SELL: "bg-sell/15 text-sell",
  MARKET: "bg-muted text-foreground",
  LIMIT: "bg-blue-500/15 text-blue-400",
  SL: "bg-amber-500/15 text-amber-400",
  SL_M: "bg-amber-500/15 text-amber-400",
  MIS: "bg-blue-500/15 text-blue-400",
  CNC: "bg-primary/15 text-primary",
  NRML: "bg-muted text-foreground",
};

export function StatusPill({ status, className }: { status: string; className?: string }) {
  const c = PALETTES[status?.toUpperCase()] ?? "bg-muted text-muted-foreground";
  return (
    <span className={cn("inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium uppercase tracking-wider", c, className)}>
      {status}
    </span>
  );
}
