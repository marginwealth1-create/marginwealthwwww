"use client";

import { useState, useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import { UsersAPI, ReportsAdminAPI } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { PageHeader } from "@/components/common/PageHeader";
import { Download, Search, FileText, Loader2 } from "lucide-react";
import { toast } from "sonner";

export default function TradebookPage() {
  const [search, setSearch] = useState("");
  const [selectedUser, setSelectedUser] = useState<any>(null);
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [downloading, setDownloading] = useState(false);
  const [showDropdown, setShowDropdown] = useState(false);

  const { data: usersData } = useQuery({
    queryKey: ["admin", "users", "tradebook-search", search],
    // NOTE: /admin/users takes `q` + `page_size` — NOT `search` / `limit`.
    // Sending the wrong names meant FastAPI silently ignored both: the box
    // never filtered (it returned the same default first page whatever the
    // admin typed), so the user could never be found/selected and the PDF
    // could never be generated. Same param shape the audit page already uses.
    queryFn: () => UsersAPI.list({ q: search, page_size: 10, page: 1 }),
    enabled: search.length >= 2,
  });

  const users = usersData?.items ?? [];

  const handleSelectUser = useCallback((user: any) => {
    setSelectedUser(user);
    setSearch("");
    setShowDropdown(false);
  }, []);

  const handleDownload = useCallback(async () => {
    if (!selectedUser) {
      toast.error("Please select a user first");
      return;
    }

    setDownloading(true);
    try {
      const blob = await ReportsAdminAPI.tradebookPdf(
        selectedUser._id || selectedUser.id,
        fromDate || undefined,
        toDate || undefined,
      );

      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      const code = selectedUser.user_code || "user";
      const stamp = new Date().toISOString().slice(0, 10).replace(/-/g, "");
      a.href = url;
      a.download = `tradebook_${code}_${stamp}.pdf`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      toast.success("Tradebook PDF downloaded");
    } catch (err: any) {
      toast.error(err?.message || "Failed to generate tradebook PDF");
    } finally {
      setDownloading(false);
    }
  }, [selectedUser, fromDate, toDate]);

  return (
    <div className="space-y-4">
      <PageHeader title="Tradebook PDF" description="Generate ARK Trader-style tradebook PDF per user" />

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <FileText className="h-4 w-4 text-emerald-500" />
            Generate Tradebook
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* User search */}
          <div className="space-y-1.5">
            <Label>Select User</Label>
            {selectedUser ? (
              <div className="flex items-center gap-3 rounded-md border border-emerald-500/30 bg-emerald-500/5 px-3 py-2">
                <div className="flex-1">
                  <span className="font-medium">{selectedUser.full_name || "—"}</span>
                  <span className="ml-2 text-xs text-muted-foreground">
                    {selectedUser.user_code} &middot; {selectedUser.email || selectedUser.mobile}
                  </span>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setSelectedUser(null)}
                  className="text-xs"
                >
                  Change
                </Button>
              </div>
            ) : (
              <div className="relative">
                <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  placeholder="Search by name, code, email, or mobile..."
                  value={search}
                  onChange={(e) => {
                    setSearch(e.target.value);
                    setShowDropdown(true);
                  }}
                  onFocus={() => setShowDropdown(true)}
                  className="pl-9"
                />
                {showDropdown && users.length > 0 && (
                  <div className="absolute z-50 mt-1 max-h-60 w-full overflow-auto rounded-md border bg-popover shadow-lg">
                    {users.map((u: any) => (
                      <button
                        key={u._id || u.id}
                        onClick={() => handleSelectUser(u)}
                        className="flex w-full items-center gap-3 px-3 py-2 text-left text-sm hover:bg-accent"
                      >
                        <div className="flex-1">
                          <span className="font-medium">{u.full_name || "—"}</span>
                          <span className="ml-2 text-xs text-muted-foreground">{u.user_code}</span>
                        </div>
                        <span className="text-xs text-muted-foreground">{u.email || u.mobile}</span>
                      </button>
                    ))}
                  </div>
                )}
                {showDropdown && search.length >= 2 && users.length === 0 && (
                  <div className="absolute z-50 mt-1 w-full rounded-md border bg-popover p-3 text-center text-sm text-muted-foreground shadow-lg">
                    No users found
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Date range */}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label>From Date (optional)</Label>
              <Input
                type="date"
                value={fromDate}
                onChange={(e) => setFromDate(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label>To Date (optional)</Label>
              <Input
                type="date"
                value={toDate}
                onChange={(e) => setToDate(e.target.value)}
              />
            </div>
          </div>

          {/* Download button */}
          <Button
            onClick={handleDownload}
            disabled={!selectedUser || downloading}
            className="w-full bg-emerald-600 hover:bg-emerald-700 sm:w-auto"
          >
            {downloading ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Generating PDF...
              </>
            ) : (
              <>
                <Download className="mr-2 h-4 w-4" />
                Download Tradebook PDF
              </>
            )}
          </Button>
        </CardContent>
      </Card>

      {/* Info */}
      <Card>
        <CardContent className="pt-6">
          <div className="text-sm text-muted-foreground space-y-2">
            <p className="font-medium text-foreground">PDF includes:</p>
            <ul className="list-disc pl-5 space-y-1">
              <li>Closed Transactions (trades + SL/TP triggers + deposits/withdrawals)</li>
              <li>Money Totals (Deposit, Withdrawal, Adjustment, Bonus)</li>
              <li>Opened Deals (current open positions with unrealized P&L)</li>
              <li>Pending Orders (LIMIT, SL, SL-M orders waiting to trigger)</li>
              <li>Financial Standings (Balance, Equity, Margin, Free Margin, Margin Level %)</li>
            </ul>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
