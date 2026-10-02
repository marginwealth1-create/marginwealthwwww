import type { Metadata } from "next";
import { MpPageHero, MpProse, MpSection } from "@/components/marketing/mp-ui";

export const metadata: Metadata = {
  title: "Terms & Conditions",
  description:
    "The terms and conditions governing the use of MarginWealth's trading and investing services across NSE, BSE & MCX.",
};

const SECTIONS = [
  {
    title: "1. Acceptance of terms",
    body: "By opening an account or using MarginWealth's website, apps and services, you agree to these Terms & Conditions. If you do not agree, please do not use the platform.",
  },
  {
    title: "2. Eligibility",
    body: "You must be at least 18 years old, a resident of India (unless otherwise permitted), and legally able to enter into a binding agreement. You are responsible for providing accurate KYC information.",
  },
  {
    title: "3. Your account",
    body: "You are responsible for keeping your login credentials confidential and for all activity under your account. Notify us immediately of any unauthorised use. Securities are held in your own demat account with the depository.",
  },
  {
    title: "4. Trading & market risk",
    body: "Investments in the securities market are subject to market risks. Read all related documents carefully before investing. Past performance does not guarantee future results, and nothing on the platform is investment advice.",
  },
  {
    title: "5. Charges",
    body: "Applicable brokerage and statutory charges (STT, exchange transaction charges, GST, SEBI turnover fees and stamp duty) are displayed before you place an order and are deducted as per prevailing regulations.",
  },
  {
    title: "6. Limitation of liability",
    body: "We strive for reliable, uninterrupted service but do not guarantee that the platform will always be available or error-free. To the extent permitted by law, we are not liable for losses arising from market movements, third-party outages, or events beyond our reasonable control.",
  },
  {
    title: "7. Changes to these terms",
    body: "We may update these terms from time to time. Continued use of the platform after changes take effect constitutes acceptance of the revised terms.",
  },
];

export default function TermsPage() {
  return (
    <>
      <MpPageHero
        eyebrow="Legal"
        title="Terms & Conditions"
        lead="Please read these terms carefully. They govern your use of MarginWealth's trading and investing services."
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
            This is a general summary. The final, legally binding terms should be
            reviewed and confirmed by your legal team before publishing.
          </p>
        </div>
      </MpSection>
    </>
  );
}
