import type { Metadata } from "next";
import { MpPageHero, MpProse, MpSection } from "@/components/marketing/mp-ui";

export const metadata: Metadata = {
  title: "Refund Policy",
  description:
    "How refunds work at MarginWealth for account charges, subscriptions and added funds.",
};

const SECTIONS = [
  {
    title: "Added funds",
    body: "Money you add to your trading account remains yours. Unused funds can be withdrawn to your registered bank account at any time, subject to applicable settlement timelines and regulatory holds.",
  },
  {
    title: "Account opening & maintenance charges",
    body: "Account opening is free. Annual maintenance and any one-time charges, once levied, are generally non-refundable unless required by law or in the case of a billing error.",
  },
  {
    title: "Subscriptions & add-ons",
    body: "Any optional paid plans or add-ons are billed in advance. Cancellations stop future renewals; the current paid period is non-refundable unless stated otherwise at purchase.",
  },
  {
    title: "Billing errors",
    body: "If you believe you were charged incorrectly, contact support within a reasonable period with your account ID and the transaction details. Verified errors are corrected or refunded.",
  },
  {
    title: "How to request",
    body: "Raise a request through your dashboard or by emailing support. We will acknowledge it and respond with the outcome and timeline.",
  },
];

export default function RefundPage() {
  return (
    <>
      <MpPageHero
        eyebrow="Legal"
        title="Refund Policy"
        lead="How refunds work for funds, account charges and any optional plans at MarginWealth."
      />
      <MpSection>
        <div className="flex flex-col gap-8">
          {SECTIONS.map((s) => (
            <div key={s.title}>
              <h2 className="font-display text-xl font-semibold text-mp-text">
                {s.title}
              </h2>
              <MpProse className="mt-3">{s.body}</MpProse>
            </div>
          ))}
          <p className="text-sm text-mp-text-mut">
            This is a general summary. The final, legally binding refund policy
            should be reviewed and confirmed by your legal team before publishing.
          </p>
        </div>
      </MpSection>
    </>
  );
}
