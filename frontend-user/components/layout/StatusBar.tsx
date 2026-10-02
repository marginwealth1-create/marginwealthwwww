"use client";

import { useEffect, useState } from "react";
import { Circle } from "lucide-react";
import { cn } from "@/lib/utils";

function isMarketOpen(date = new Date()): boolean {
  // IST is UTC+5:30
  const ist = new Date(date.getTime() + (5 * 60 + 30) * 60_000);
  const day = ist.getUTCDay();
  if (day === 0 || day === 6) return false;
  const minutes = ist.getUTCHours() * 60 + ist.getUTCMinutes();
  return minutes >= 9 * 60 + 15 && minutes <= 15 * 60 + 30;
}

function formatIST(date: Date) {
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

export function StatusBar() {
  const [now, setNow] = useState(() => new Date());
  const [wsStatus, _setWsStatus] = useState<"connected" | "connecting" | "disconnected">("disconnected");

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  const open = isMarketOpen(now);

  return (
    <footer className="sticky bottom-0 z-10 hidden h-7 items-center gap-4 border-t border-border bg-card/70 px-3 text-xs text-muted-foreground backdrop-blur md:flex">
      <div className="flex items-center gap-1.5">
        <Circle className={cn("size-2 fill-current", open ? "text-profit" : "text-loss")} />
        <span>Market {open ? "open" : "closed"}</span>
      </div>
      <div className="font-tabular">{formatIST(now)} IST</div>
      <div className="ml-auto flex items-center gap-1.5">
        <Circle
          className={cn(
            "size-2 fill-current",
            wsStatus === "connected" && "text-profit",
            wsStatus === "connecting" && "text-amber-400",
            wsStatus === "disconnected" && "text-muted-foreground"
          )}
        />
        <span>Feed {wsStatus}</span>
      </div>
    </footer>
  );
}
