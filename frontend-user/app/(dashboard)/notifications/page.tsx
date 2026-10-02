"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Check } from "lucide-react";
import { NotificationsAPI } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/common/PageHeader";
import { Card, CardContent } from "@/components/ui/card";
import { StatusPill } from "@/components/common/StatusPill";
import { cn } from "@/lib/utils";

export default function NotificationsPage() {
  const qc = useQueryClient();
  const { data } = useQuery({ queryKey: ["notifications"], queryFn: () => NotificationsAPI.list() });

  async function markAll() {
    try {
      await NotificationsAPI.markAllRead();
      toast.success("All marked read");
      qc.invalidateQueries({ queryKey: ["notifications"] });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function markOne(id: string) {
    try {
      await NotificationsAPI.markRead(id);
      qc.invalidateQueries({ queryKey: ["notifications"] });
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Notifications"
        description={`${data?.length ?? 0} total`}
        actions={
          <Button variant="outline" onClick={markAll}>
            <Check className="size-4" /> Mark all read
          </Button>
        }
      />

      <div className="space-y-2">
        {data?.length ? (
          data.map((n: any) => (
            <Card key={n.id} className={cn(!n.is_read && "border-primary/40")}>
              <CardContent className="flex items-start justify-between gap-3 p-3">
                <div className="space-y-1">
                  <div className="flex items-center gap-2 text-sm">
                    <StatusPill status={n.type} />
                    <span className="font-medium">{n.title}</span>
                    {!n.is_read && <span className="size-1.5 rounded-full bg-primary" />}
                  </div>
                  <div className="text-sm text-muted-foreground">{n.message}</div>
                  {n.data?.link && (
                    <a
                      href={n.data.link}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
                    >
                      Open link ↗
                    </a>
                  )}
                  <div className="text-xs text-muted-foreground">{new Date(n.created_at).toLocaleString()}</div>
                </div>
                {!n.is_read && (
                  <Button size="sm" variant="ghost" onClick={() => markOne(n.id)}>
                    <Check className="size-4" />
                  </Button>
                )}
              </CardContent>
            </Card>
          ))
        ) : (
          <div className="rounded-md border border-dashed border-border p-12 text-center text-sm text-muted-foreground">
            No notifications.
          </div>
        )}
      </div>
    </div>
  );
}
