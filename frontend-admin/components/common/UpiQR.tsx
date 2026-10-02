"use client";

import { QRCodeSVG } from "qrcode.react";

/** Builds a standard UPI deep-link payable URI per the NPCI spec.
 * Most UPI apps (PhonePe, Google Pay, Paytm, BHIM) recognise this. */
export function buildUpiUri({
  upiId,
  payeeName,
  amount,
  note,
}: {
  upiId: string;
  payeeName?: string;
  amount?: number;
  note?: string;
}) {
  const params = new URLSearchParams({ pa: upiId.trim() });
  if (payeeName) params.set("pn", payeeName);
  if (amount && amount > 0) params.set("am", amount.toFixed(2));
  if (note) params.set("tn", note);
  params.set("cu", "INR");
  return `upi://pay?${params.toString()}`;
}

export function UpiQR({
  upiId,
  payeeName,
  amount,
  size = 192,
  className,
}: {
  upiId?: string | null;
  payeeName?: string;
  amount?: number;
  size?: number;
  className?: string;
}) {
  if (!upiId || !upiId.trim()) {
    return (
      <div
        className={"flex items-center justify-center rounded-md border border-dashed border-border bg-muted/20 text-[10px] text-muted-foreground " + (className ?? "")}
        style={{ width: size, height: size }}
      >
        Enter UPI ID
      </div>
    );
  }
  const uri = buildUpiUri({ upiId, payeeName, amount });
  return (
    <div className={"inline-block rounded-md bg-white p-2 " + (className ?? "")}>
      <QRCodeSVG value={uri} size={size} level="M" includeMargin={false} />
    </div>
  );
}
