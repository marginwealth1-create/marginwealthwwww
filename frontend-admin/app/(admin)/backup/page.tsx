"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { DatabaseBackup, Power } from "lucide-react";
import { SettingsAPI } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { PageHeader } from "@/components/common/PageHeader";

export default function BackupPage() {
  const qc = useQueryClient();
  const { data } = useQuery({ queryKey: ["admin", "backups"], queryFn: () => SettingsAPI.backupList() });

  async function runBackup() {
    if (!confirm("Run manual backup now?")) return;
    try {
      await SettingsAPI.runBackup();
      toast.success("Backup queued");
      qc.invalidateQueries({ queryKey: ["admin", "backups"] });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function eod() {
    if (!confirm("⚠ Run End-of-Day reset? This squares off MIS, settles holdings, clears day counters.")) return;
    try {
      await SettingsAPI.eodReset();
      toast.success("EOD reset triggered");
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader title="Backup & EOD" description="Manual backup + end-of-day reset trigger" />

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Database backup</CardTitle>
            <CardDescription>Snapshot Mongo to S3 (configured in env). Phase 7 wires the actual S3 dump.</CardDescription>
          </CardHeader>
          <CardContent>
            <Button onClick={runBackup}>
              <DatabaseBackup className="size-4" /> Run backup now
            </Button>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>End-of-Day reset</CardTitle>
            <CardDescription>Square off MIS, settle holdings, update day counters, clear in-day caches.</CardDescription>
          </CardHeader>
          <CardContent>
            <Button variant="destructive" onClick={eod}>
              <Power className="size-4" /> Trigger EOD
            </Button>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Recent backup events</CardTitle>
        </CardHeader>
        <CardContent className="space-y-1 text-xs">
          {data?.length ? (
            data.map((b: any) => (
              <div key={b.id} className="flex justify-between border-b border-border/50 py-1.5 last:border-b-0">
                <span>{new Date(b.created_at).toLocaleString()}</span>
                <code className="text-muted-foreground">{JSON.stringify(b.metadata)}</code>
              </div>
            ))
          ) : (
            <p className="text-muted-foreground">No backup events yet.</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
