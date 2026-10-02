"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Plus, Trash2 } from "lucide-react";
import { SettingsAPI } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { PageHeader } from "@/components/common/PageHeader";
import { DataTable, type Column } from "@/components/common/DataTable";

export default function HolidaysPage() {
  const qc = useQueryClient();
  const [year, setYear] = useState<number | undefined>(new Date().getFullYear());
  const { data, isFetching } = useQuery({
    queryKey: ["admin", "holidays", year],
    queryFn: () => SettingsAPI.holidays(year),
  });

  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState({ holiday_date: "", description: "", exchange: "NSE", is_full_day: true });

  async function add() {
    try {
      await SettingsAPI.createHoliday(form);
      toast.success("Added");
      setAdding(false);
      qc.invalidateQueries({ queryKey: ["admin", "holidays"] });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function remove(id: string) {
    if (!confirm("Delete this holiday?")) return;
    try {
      await SettingsAPI.deleteHoliday(id);
      toast.success("Deleted");
      qc.invalidateQueries({ queryKey: ["admin", "holidays"] });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  const cols: Column<any>[] = [
    { key: "holiday_date", header: "Date" },
    { key: "exchange", header: "Exchange" },
    { key: "description", header: "Description" },
    { key: "is_full_day", header: "Full day", render: (r) => (r.is_full_day ? "Yes" : "No") },
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
        title="Trading holidays"
        actions={
          <div className="flex gap-2">
            <Input
              type="number"
              value={year ?? ""}
              onChange={(e) => setYear(e.target.value ? Number(e.target.value) : undefined)}
              className="h-10 w-24"
              placeholder="Year"
            />
            <Dialog open={adding} onOpenChange={setAdding}>
              <DialogTrigger asChild>
                <Button>
                  <Plus className="size-4" /> Add holiday
                </Button>
              </DialogTrigger>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>New holiday</DialogTitle>
                </DialogHeader>
                <div className="space-y-3">
                  <div className="space-y-1.5">
                    <Label>Date</Label>
                    <Input type="date" value={form.holiday_date} onChange={(e) => setForm((f) => ({ ...f, holiday_date: e.target.value }))} />
                  </div>
                  <div className="space-y-1.5">
                    <Label>Exchange</Label>
                    <select
                      value={form.exchange}
                      onChange={(e) => setForm((f) => ({ ...f, exchange: e.target.value }))}
                      className="h-10 w-full rounded-md border border-border bg-background px-3 text-sm"
                    >
                      <option value="NSE">NSE</option>
                      <option value="BSE">BSE</option>
                      <option value="MCX">MCX</option>
                    </select>
                  </div>
                  <div className="space-y-1.5">
                    <Label>Description</Label>
                    <Input value={form.description} onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))} />
                  </div>
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={form.is_full_day}
                      onChange={(e) => setForm((f) => ({ ...f, is_full_day: e.target.checked }))}
                      className="size-4 accent-primary"
                    />
                    Full-day holiday
                  </label>
                </div>
                <DialogFooter>
                  <Button variant="outline" onClick={() => setAdding(false)}>
                    Cancel
                  </Button>
                  <Button onClick={add}>Add</Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>
          </div>
        }
      />
      <DataTable columns={cols} rows={data} keyExtractor={(r) => r.id} loading={isFetching && !data} />
    </div>
  );
}
