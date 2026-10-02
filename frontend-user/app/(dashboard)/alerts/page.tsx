"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Plus, Trash2 } from "lucide-react";
import { AlertsAPI, InstrumentAPI } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { PageHeader } from "@/components/common/PageHeader";
import { DataTable, type Column } from "@/components/common/DataTable";
import { StatusPill } from "@/components/common/StatusPill";

export default function AlertsPage() {
  const qc = useQueryClient();
  const { data } = useQuery({ queryKey: ["alerts"], queryFn: () => AlertsAPI.list() });
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const { data: results } = useQuery({
    queryKey: ["instr-search-alerts", search],
    queryFn: () => InstrumentAPI.search(search, undefined, undefined, 8),
    enabled: search.length > 0,
  });
  const [token, setToken] = useState("");
  const [alertType, setAlertType] = useState("LTP_ABOVE");
  const [target, setTarget] = useState("");
  const [note, setNote] = useState("");

  async function create() {
    if (!token || !target) {
      toast.error("Instrument + target required");
      return;
    }
    try {
      await AlertsAPI.create({ token, alert_type: alertType, target_price: Number(target), note });
      toast.success("Alert created");
      setOpen(false);
      setToken("");
      setTarget("");
      setNote("");
      qc.invalidateQueries({ queryKey: ["alerts"] });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function remove(id: string) {
    try {
      await AlertsAPI.delete(id);
      toast.success("Deleted");
      qc.invalidateQueries({ queryKey: ["alerts"] });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  const cols: Column<any>[] = [
    { key: "symbol", header: "Symbol" },
    { key: "alert_type", header: "Type", render: (r) => <StatusPill status={r.alert_type.replace("_", " ")} /> },
    { key: "target_price", header: "Target", align: "right" },
    { key: "is_triggered", header: "Triggered", render: (r) => (r.is_triggered ? "Yes" : "No") },
    { key: "note", header: "Note" },
    { key: "created_at", header: "Created", render: (r) => new Date(r.created_at).toLocaleDateString() },
    {
      key: "actions",
      header: "",
      align: "right",
      render: (r) => (
        <Button variant="ghost" size="icon" onClick={() => remove(r.id)} aria-label="Delete">
          <Trash2 className="size-4 text-destructive" />
        </Button>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader
        title="Price alerts"
        actions={
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger asChild>
              <Button>
                <Plus className="size-4" /> New alert
              </Button>
            </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>New price alert</DialogTitle>
              </DialogHeader>
              <div className="space-y-3">
                <div className="space-y-1.5">
                  <Label>Instrument</Label>
                  <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search symbol…" />
                  {search && results?.length ? (
                    <div className="max-h-40 space-y-1 overflow-y-auto rounded-md border border-border p-1">
                      {results.map((r: any) => (
                        <button
                          key={r.token}
                          onClick={() => {
                            setToken(r.token);
                            setSearch(r.symbol);
                          }}
                          className="flex w-full items-center justify-between rounded px-2 py-1 text-left text-xs hover:bg-accent"
                        >
                          <span>{r.symbol}</span>
                          <span className="text-muted-foreground">{r.exchange}</span>
                        </button>
                      ))}
                    </div>
                  ) : null}
                </div>
                <div className="space-y-1.5">
                  <Label>Alert type</Label>
                  <select
                    value={alertType}
                    onChange={(e) => setAlertType(e.target.value)}
                    className="h-10 w-full rounded-md border border-border bg-background px-3 text-sm"
                  >
                    <option value="LTP_ABOVE">LTP rises above</option>
                    <option value="LTP_BELOW">LTP falls below</option>
                  </select>
                </div>
                <div className="space-y-1.5">
                  <Label>Target price (₹)</Label>
                  <Input type="number" step="0.05" value={target} onChange={(e) => setTarget(e.target.value)} />
                </div>
                <div className="space-y-1.5">
                  <Label>Note</Label>
                  <Input value={note} onChange={(e) => setNote(e.target.value)} />
                </div>
              </div>
              <DialogFooter>
                <Button variant="outline" onClick={() => setOpen(false)}>
                  Cancel
                </Button>
                <Button onClick={create}>Create</Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        }
      />
      <DataTable columns={cols} rows={data} keyExtractor={(r) => r.id} />
    </div>
  );
}
