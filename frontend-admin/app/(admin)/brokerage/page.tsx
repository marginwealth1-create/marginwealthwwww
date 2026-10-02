"use client";

import { useQuery } from "@tanstack/react-query";
import { BrokerageAPI } from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { PageHeader } from "@/components/common/PageHeader";

export default function BrokerageAdminPage() {
  const { data, isLoading } = useQuery({ queryKey: ["admin", "brokerage"], queryFn: () => BrokerageAPI.list() });

  return (
    <div className="space-y-4">
      <PageHeader title="Brokerage plans" description="Per-segment platform brokerage rate. No statutory charges are passed through to the user." />

      {isLoading && <div className="text-sm text-muted-foreground">Loading…</div>}

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {data?.map((p: any) => (
          <Card key={p.id}>
            <CardHeader className="flex flex-row items-start justify-between">
              <div>
                <CardTitle>{p.plan_name}</CardTitle>
                <CardDescription>{p.description || "—"}</CardDescription>
              </div>
              {p.is_default && (
                <span className="rounded bg-primary/15 px-2 py-0.5 text-[10px] uppercase tracking-wider text-primary">
                  Default
                </span>
              )}
            </CardHeader>
            <CardContent className="space-y-1 text-xs">
              <div className="text-muted-foreground">{p.details_count} segment row(s)</div>
              <div className="max-h-48 overflow-y-auto rounded-md border border-border bg-muted/20 p-2 scrollbar-thin">
                <table className="w-full font-tabular">
                  <thead className="text-muted-foreground">
                    <tr>
                      <th className="text-left">Segment</th>
                      <th className="text-right">Brokerage</th>
                      <th className="text-right">Min</th>
                      <th className="text-right">Max</th>
                    </tr>
                  </thead>
                  <tbody>
                    {p.details?.map((d: any) => (
                      <tr key={d.segment_type}>
                        <td className="font-mono text-[10px]">{d.segment_type}</td>
                        <td className="text-right">
                          {d.value} {d.brokerage_type}
                        </td>
                        <td className="text-right">{d.min_brokerage}</td>
                        <td className="text-right">
                          {d.max_brokerage > 0 ? d.max_brokerage : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
