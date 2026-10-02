"use client";

import { useState } from "react";
import { Download } from "lucide-react";
import { Button } from "@/components/ui/button";
import { API_URL, STORAGE_KEYS } from "@/lib/constants";

export type ReportKind = "pnl" | "tradebook" | "brokerage" | "tax" | "margin" | "tradebook/full";

interface Props {
  kind: ReportKind;
  /** Optional query params (e.g. { from_date, to_date }). */
  params?: Record<string, string | number | undefined>;
  label?: string;
}

/**
 * Click → fetch the PDF via the existing JWT, take the suggested filename
 * from the `Content-Disposition` header the server exposes via
 * Access-Control-Expose-Headers, and trigger the browser download.
 *
 * We use fetch + blob (rather than just an <a download>) so the bearer
 * token can ride along on the Authorization header. A raw anchor click
 * would hit the endpoint anonymously and return 401.
 */
export function ReportPdfButton({ kind, params, label = "Download PDF" }: Props) {
  const [downloading, setDownloading] = useState(false);

  async function onDownload() {
    setDownloading(true);
    try {
      const qs = params
        ? "?" +
          Object.entries(params)
            .filter(([, v]) => v !== undefined && v !== "")
            .map(
              ([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`,
            )
            .join("&")
        : "";
      const token =
        typeof window !== "undefined"
          ? window.localStorage.getItem(STORAGE_KEYS.accessToken)
          : null;
      const pdfPath = kind === "tradebook/full" ? "tradebook/full-pdf" : `${kind}/pdf`;
      const res = await fetch(`${API_URL}/api/v1/user/reports/${pdfPath}${qs}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      });
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(text || `HTTP ${res.status}`);
      }
      const blob = await res.blob();

      // Prefer the server-suggested filename. Browsers expose it only when
      // the backend explicitly opts in via Access-Control-Expose-Headers,
      // which the FastAPI route does.
      const disp = res.headers.get("Content-Disposition") || "";
      const match = /filename="?([^"]+)"?/i.exec(disp);
      const stamp = new Date().toISOString().slice(0, 10).replace(/-/g, "");
      const filename = match?.[1] ?? `marginwealth_${kind}_${stamp}.pdf`;

      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      // Microtask delay so Firefox finalises the download before the URL
      // is revoked. Chromium is fine either way.
      setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (e) {
      // eslint-disable-next-line no-console
      console.error("PDF download failed", e);
      if (typeof window !== "undefined") {
        window.alert(
          e instanceof Error ? e.message : "Couldn't download the PDF.",
        );
      }
    } finally {
      setDownloading(false);
    }
  }

  return (
    <Button
      variant="outline"
      size="sm"
      onClick={onDownload}
      disabled={downloading}
      loading={downloading}
    >
      {!downloading && <Download className="size-4" />}
      {downloading ? "Building…" : label}
    </Button>
  );
}
