"use client";

import {
  Camera,
  CandlestickChart,
  ChevronDown,
  LayoutGrid,
  Maximize2,
  Minimize2,
  Redo2,
  Settings,
  Undo2,
  Zap,
} from "lucide-react";
import { cn } from "@/lib/utils";

export type Timeframe = { label: string; interval: string; days: number };

export const TIMEFRAMES: Timeframe[] = [
  { label: "1m", interval: "minute", days: 1 },
  { label: "5m", interval: "5minute", days: 5 },
  { label: "15m", interval: "15minute", days: 15 },
  { label: "1h", interval: "60minute", days: 30 },
  { label: "1D", interval: "day", days: 365 },
];

interface Props {
  tf: Timeframe;
  onTfChange: (t: Timeframe) => void;
  fullscreen?: boolean;
  onToggleFullscreen?: () => void;
  onScreenshot?: () => void;
}

export function ChartToolbar({ tf, onTfChange, fullscreen, onToggleFullscreen, onScreenshot }: Props) {
  return (
    <div className="flex items-center gap-0.5 border-b border-border bg-card px-2 py-1.5 text-muted-foreground">
      {/* Timeframe */}
      <div className="relative">
        <select
          value={tf.label}
          onChange={(e) => {
            const next = TIMEFRAMES.find((x) => x.label === e.target.value);
            if (next) onTfChange(next);
          }}
          className="h-7 cursor-pointer appearance-none rounded-md bg-transparent pl-2 pr-2 text-xs font-medium text-foreground outline-none hover:bg-muted/30"
        >
          {TIMEFRAMES.map((t) => (
            <option key={t.label} value={t.label} className="bg-popover text-foreground">
              {t.label}
            </option>
          ))}
        </select>
      </div>

      <Sep />

      <ToolbarBtn title="Chart type">
        <CandlestickChart className="size-4" />
      </ToolbarBtn>
      <ToolbarBtn title="Indicators">
        <span className="px-0.5 font-serif italic">fx</span>
        <span className="hidden text-xs font-medium not-italic md:inline">Indicators</span>
      </ToolbarBtn>

      <Sep />

      <ToolbarBtn title="Layouts">
        <LayoutGrid className="size-4" />
      </ToolbarBtn>

      <Sep />

      <ToolbarBtn title="Undo">
        <Undo2 className="size-4" />
      </ToolbarBtn>
      <ToolbarBtn title="Redo">
        <Redo2 className="size-4" />
      </ToolbarBtn>

      <div className="ml-auto flex items-center gap-0.5">
        <button
          type="button"
          className="flex h-7 items-center gap-1 rounded-md px-2 text-xs hover:bg-muted/30 hover:text-foreground"
        >
          Save <ChevronDown className="size-3" />
        </button>
        <ToolbarBtn title="Quick actions">
          <Zap className="size-4" />
        </ToolbarBtn>
        <ToolbarBtn title="Settings">
          <Settings className="size-4" />
        </ToolbarBtn>
        <ToolbarBtn title={fullscreen ? "Exit fullscreen" : "Fullscreen"} onClick={onToggleFullscreen}>
          {fullscreen ? <Minimize2 className="size-4" /> : <Maximize2 className="size-4" />}
        </ToolbarBtn>
        <ToolbarBtn title="Screenshot" onClick={onScreenshot}>
          <Camera className="size-4" />
        </ToolbarBtn>
      </div>
    </div>
  );
}

function Sep() {
  return <span className="mx-1 h-4 w-px bg-border" />;
}

function ToolbarBtn({
  children,
  title,
  onClick,
  className,
}: {
  children: React.ReactNode;
  title?: string;
  onClick?: () => void;
  className?: string;
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className={cn(
        "flex h-7 items-center gap-1 rounded-md px-1.5 text-xs hover:bg-muted/30 hover:text-foreground",
        className
      )}
    >
      {children}
    </button>
  );
}
