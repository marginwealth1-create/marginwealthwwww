"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Download, Pencil, Trash2, Upload } from "lucide-react";
import { ChartLevelsAPI, type ChartLevelRow } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { PageHeader } from "@/components/common/PageHeader";
import { DataTable, type Column } from "@/components/common/DataTable";

/**
 * Chart lines — Excel round-trip, plus an inline editor for one-off fixes.
 *
 * Pick a segment, download the template (pre-filled with whatever is already
 * saved), fill in up to six line / colour / price sets per instrument, upload
 * it back. Each price is drawn as a horizontal line on that instrument's
 * chart, in the colour given, for the users under this admin.
 */

// Mirrors MAX_LEVELS / DEFAULT_COLORS in chart_level_service.py. The server
// truncates anything beyond MAX_LINES, so drift here can only ever show fewer
// boxes — it can never save more lines than the sheet allows.
const MAX_LINES = 6;
const DEFAULT_COLORS = [
  "#E31E24",
  "#0EA5E9",
  "#16A34A",
  "#F59E0B",
  "#7C3AED",
  "#EC4899",
];

type Draft = { price: string; color: string; label: string };

/** A level this far from the live price is drawn where nobody will scroll —
 *  the whole of the "I uploaded prices and no line appeared" report. */
const OFFSCREEN_RATIO = 10;
function offscreen(price: string | number, ltp: number | null | undefined) {
  const p = Number(price);
  if (!ltp || ltp <= 0 || !Number.isFinite(p) || p <= 0) return false;
  return p > ltp * OFFSCREEN_RATIO || p * OFFSCREEN_RATIO < ltp;
}

export default function ChartLevelsPage() {
  const qc = useQueryClient();
  // Empty until the segment list lands — hardcoding a default would show a
  // segment this deployment may not even have instruments in.
  const [segment, setSegment] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<ChartLevelRow | null>(null);
  const [draft, setDraft] = useState<Draft[]>([]);
  const fileRef = useRef<HTMLInputElement>(null);

  const { data: segments } = useQuery({
    queryKey: ["admin", "chart-levels", "segments"],
    queryFn: () => ChartLevelsAPI.segments(),
  });

  // Default to the biggest segment once the list arrives.
  useEffect(() => {
    if (!segment && segments?.length) setSegment(segments[0].value);
  }, [segments, segment]);

  const { data: rows, isFetching } = useQuery({
    queryKey: ["admin", "chart-levels", segment],
    queryFn: () => ChartLevelsAPI.list(segment),
    enabled: !!segment,
  });

  const configured = useMemo(() => (rows ?? []).length, [rows]);

  async function download() {
    setBusy(true);
    try {
      const blob = await ChartLevelsAPI.template(segment);
      // Object URL + synthetic click — the request needs the auth header, so
      // a plain <a href> to the endpoint would 401.
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `chart-levels-${segment}.xlsx`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch {
      toast.error("Could not build the template");
    } finally {
      setBusy(false);
    }
  }

  async function upload(file: File) {
    setBusy(true);
    try {
      const res = await ChartLevelsAPI.import(file);
      const bits = [`${res.updated} updated`];
      if (res.cleared) bits.push(`${res.cleared} cleared`);
      toast.success(`Saved — ${bits.join(", ")}`);
      // Show every rejected row rather than a single generic failure: with
      // forty rows the operator needs to know WHICH ones to fix.
      for (const e of (res.errors ?? []).slice(0, 8)) toast.error(e);
      if ((res.errors ?? []).length > 8) {
        toast.error(`…and ${res.errors.length - 8} more problems`);
      }
      // Saved, but drawn off-screen — a warning, not a rejection.
      for (const w of (res.warnings ?? []).slice(0, 8)) {
        toast.warning(w, { duration: 10000 });
      }
      qc.invalidateQueries({ queryKey: ["admin", "chart-levels"] });
    } catch (e: any) {
      toast.error(e?.message || "Upload failed");
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  function openEdit(r: ChartLevelRow) {
    setEditing(r);
    setDraft(
      Array.from({ length: MAX_LINES }, (_, i) => {
        const lv = r.levels[i];
        return {
          price: lv ? String(lv.price) : "",
          color: lv?.color ?? DEFAULT_COLORS[i],
          label: lv?.label ?? "",
        };
      }),
    );
  }

  async function saveEdit() {
    if (!editing) return;
    setBusy(true);
    try {
      const levels = draft.filter((d) => d.price.trim() !== "");
      await ChartLevelsAPI.save(editing.token, levels);
      toast.success(
        levels.length
          ? `${editing.symbol} — ${levels.length} line${levels.length === 1 ? "" : "s"} saved`
          : "Lines removed",
      );
      setEditing(null);
      qc.invalidateQueries({ queryKey: ["admin", "chart-levels"] });
    } catch (e: any) {
      toast.error(e?.message || "Could not save");
    } finally {
      setBusy(false);
    }
  }

  async function clearRow(token: string) {
    try {
      await ChartLevelsAPI.clear(token);
      toast.success("Lines removed");
      qc.invalidateQueries({ queryKey: ["admin", "chart-levels"] });
    } catch {
      toast.error("Could not remove");
    }
  }

  const columns: Column<ChartLevelRow>[] = [
    { key: "symbol", header: "Symbol", render: (r) => <span className="font-medium">{r.symbol}</span> },
    { key: "token", header: "Token", render: (r) => <span className="text-xs text-muted-foreground">{r.token}</span> },
    {
      key: "ltp",
      header: "Live",
      align: "right",
      render: (r) => (
        <span className="text-xs tabular-nums text-muted-foreground">{r.ltp ? r.ltp : "—"}</span>
      ),
    },
    {
      key: "levels",
      header: "Lines",
      render: (r) => (
        <div className="flex flex-wrap gap-2">
          {r.levels.map((lv, i) => {
            const off = offscreen(lv.price, r.ltp);
            return (
              <span
                key={i}
                className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs tabular-nums ${
                  off ? "border-red-500/60 text-red-500" : "border-border"
                }`}
                title={
                  off
                    ? "Far from the live price — this line is drawn off-screen"
                    : lv.label || undefined
                }
              >
                <span
                  className="size-2.5 rounded-full ring-1 ring-inset ring-black/20"
                  style={{ backgroundColor: lv.color }}
                />
                {lv.price}
                {lv.label ? <span className="text-muted-foreground">· {lv.label}</span> : null}
              </span>
            );
          })}
        </div>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      render: (r) => (
        <div className="flex justify-end gap-1">
          <Button size="sm" variant="ghost" onClick={() => openEdit(r)} title="Edit these lines">
            <Pencil className="size-4" />
          </Button>
          <Button size="sm" variant="ghost" onClick={() => clearRow(r.token)} title="Remove all lines">
            <Trash2 className="size-4" />
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-5">
      <PageHeader
        title="Chart lines"
        description="Draw horizontal price lines on your users' charts. Download the segment's sheet, fill in prices and colours, upload it back — or edit one instrument right here."
      />

      <div className="flex flex-wrap items-end gap-3 rounded-xl border border-border bg-card p-4">
        <div className="flex flex-col gap-1.5">
          <label className="text-xs font-medium text-muted-foreground">Segment</label>
          <select
            value={segment}
            onChange={(e) => setSegment(e.target.value)}
            className="h-9 rounded-md border border-input bg-background px-3 text-sm"
          >
            {(segments ?? []).map((s) => (
              <option key={s.value} value={s.value}>
                {s.label} ({s.count})
              </option>
            ))}
          </select>
        </div>

        <Button onClick={download} disabled={busy || !segment} className="gap-2">
          <Download className="size-4" /> Download sheet
        </Button>

        <Button
          variant="secondary"
          disabled={busy}
          className="gap-2"
          onClick={() => fileRef.current?.click()}
        >
          <Upload className="size-4" /> Upload filled sheet
        </Button>
        <input
          ref={fileRef}
          type="file"
          accept=".xlsx,.xlsm"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void upload(f);
          }}
        />

        <span className="ml-auto text-sm text-muted-foreground">
          {configured} instrument{configured === 1 ? "" : "s"} with lines in this segment
        </span>
      </div>

      <DataTable
        columns={columns}
        rows={rows}
        keyExtractor={(r) => r.token}
        loading={isFetching}
        empty="No lines set for this segment yet."
      />

      <p className="text-xs leading-relaxed text-muted-foreground">
        The sheet carries {MAX_LINES} sets of <b>Line · Colour · Price</b> per instrument.
        Re-uploading replaces the lines for every instrument listed in it; a row with all prices
        blank clears that instrument. Colours accept hex (<code>#E31E24</code>) or names (red,
        green, blue, orange, purple…). A price far from the live price is drawn off-screen — those
        are flagged in red.
      </p>

      <Dialog open={!!editing} onOpenChange={(o) => !o && setEditing(null)}>
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle>
              {editing?.symbol}
              {editing?.ltp ? (
                <span className="ml-2 text-sm font-normal tabular-nums text-muted-foreground">
                  live {editing.ltp}
                </span>
              ) : null}
            </DialogTitle>
          </DialogHeader>

          <div className="space-y-2">
            <div className="grid grid-cols-[1.5rem_1fr_3rem_1fr] items-center gap-2 text-xs text-muted-foreground">
              <span>#</span>
              <span>Line</span>
              <span>Colour</span>
              <span>Price</span>
            </div>
            {draft.map((d, i) => (
              <div key={i} className="grid grid-cols-[1.5rem_1fr_3rem_1fr] items-center gap-2">
                <span className="text-xs text-muted-foreground">{i + 1}</span>
                <Input
                  value={d.label}
                  placeholder="R1 / Support"
                  maxLength={40}
                  onChange={(e) =>
                    setDraft((p) => p.map((x, j) => (j === i ? { ...x, label: e.target.value } : x)))
                  }
                />
                <input
                  type="color"
                  value={d.color}
                  className="h-9 w-full cursor-pointer rounded-md border border-input bg-background p-1"
                  onChange={(e) =>
                    setDraft((p) => p.map((x, j) => (j === i ? { ...x, color: e.target.value } : x)))
                  }
                />
                <Input
                  value={d.price}
                  inputMode="decimal"
                  placeholder="blank = no line"
                  className={offscreen(d.price, editing?.ltp) ? "border-red-500 text-red-500" : ""}
                  onChange={(e) =>
                    setDraft((p) => p.map((x, j) => (j === i ? { ...x, price: e.target.value } : x)))
                  }
                />
              </div>
            ))}
          </div>

          <DialogFooter>
            <Button variant="ghost" onClick={() => setEditing(null)} disabled={busy}>
              Cancel
            </Button>
            <Button onClick={saveEdit} disabled={busy}>
              Save lines
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
