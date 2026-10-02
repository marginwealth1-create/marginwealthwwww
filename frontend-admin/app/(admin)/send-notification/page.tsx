"use client";

import { useState } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { Send, Users, Link2 } from "lucide-react";
import { NotificationsAPI } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent } from "@/components/ui/card";
import { PageHeader } from "@/components/common/PageHeader";

const LEVELS = [
  { k: "INFO", label: "Info", tone: "border-blue-500/40 text-blue-500" },
  { k: "SUCCESS", label: "Success", tone: "border-emerald-500/40 text-emerald-500" },
  { k: "WARNING", label: "Warning", tone: "border-amber-500/40 text-amber-500" },
  { k: "DANGER", label: "Alert", tone: "border-destructive/40 text-destructive" },
];

export default function SendNotificationPage() {
  const [title, setTitle] = useState("");
  const [message, setMessage] = useState("");
  const [link, setLink] = useState("");
  const [level, setLevel] = useState("INFO");

  // How many users this actor's broadcast reaches (their own pool).
  const { data: rec } = useQuery({
    queryKey: ["admin", "broadcast-recipients"],
    queryFn: () => NotificationsAPI.broadcastRecipients(),
  });
  const count = rec?.count ?? 0;

  const mut = useMutation({
    mutationFn: () =>
      NotificationsAPI.broadcast({
        title: title.trim(),
        message: message.trim(),
        link: link.trim() || undefined,
        level,
      }),
    onSuccess: (r) => {
      toast.success(`Sent to ${r.count} user${r.count === 1 ? "" : "s"}`);
      setTitle("");
      setMessage("");
      setLink("");
      setLevel("INFO");
    },
    onError: (e: any) => toast.error(e?.message || "Failed to send"),
  });

  const canSend = title.trim().length > 0 && message.trim().length > 0 && !mut.isPending;

  function submit() {
    if (!canSend) return;
    if (
      !window.confirm(
        `Send this notification to all ${count} of your users? This cannot be undone.`,
      )
    )
      return;
    mut.mutate();
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Send Notification"
        description="Broadcast a message (with an optional link) to all your users at once."
      />

      <Card className="max-w-2xl">
        <CardContent className="space-y-5 p-5">
          {/* Recipient count */}
          <div className="flex items-center gap-2 rounded-md border border-border bg-muted/30 px-3 py-2 text-sm">
            <Users className="size-4 text-primary" />
            <span className="text-muted-foreground">
              Goes to <span className="font-semibold text-foreground">{count}</span> user
              {count === 1 ? "" : "s"} in your pool
            </span>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="n-title">Title</Label>
            <Input
              id="n-title"
              value={title}
              maxLength={120}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="e.g. Platform maintenance tonight"
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="n-msg">Message</Label>
            <textarea
              id="n-msg"
              value={message}
              maxLength={1000}
              onChange={(e) => setMessage(e.target.value)}
              rows={4}
              placeholder="Type the message your users will see…"
              className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/40"
            />
            <div className="text-right text-[11px] text-muted-foreground">{message.length}/1000</div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="n-link" className="flex items-center gap-1.5">
              <Link2 className="size-3.5" /> Link <span className="text-muted-foreground">(optional)</span>
            </Label>
            <Input
              id="n-link"
              value={link}
              maxLength={500}
              onChange={(e) => setLink(e.target.value)}
              placeholder="https://…  (users can tap to open)"
            />
          </div>

          <div className="space-y-1.5">
            <Label>Level</Label>
            <div className="flex flex-wrap gap-2">
              {LEVELS.map((l) => (
                <button
                  key={l.k}
                  type="button"
                  onClick={() => setLevel(l.k)}
                  className={`rounded-md border px-3 py-1.5 text-xs font-semibold transition-colors ${
                    level === l.k ? l.tone + " bg-accent" : "border-border text-muted-foreground hover:bg-accent/50"
                  }`}
                >
                  {l.label}
                </button>
              ))}
            </div>
          </div>

          <div className="flex justify-end pt-1">
            <Button onClick={submit} disabled={!canSend}>
              <Send className="size-4" /> {mut.isPending ? "Sending…" : `Send to ${count} users`}
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
