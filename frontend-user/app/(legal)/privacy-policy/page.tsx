import type { Metadata } from "next";
import { MpPageHero, MpProse, MpSection } from "@/components/marketing/mp-ui";

export const metadata: Metadata = {
  title: "Privacy Policy | MarginWealth",
  description:
    "How MarginWealth collects, uses, protects and shares your personal and financial information across its trading and investing services.",
  alternates: { canonical: "/privacy" },
};

const LAST_UPDATED = "24 June 2026";

const SECTIONS: { title: string; body: string }[] = [
  {
    title: "1. Introduction",
    body: "MarginWealth (“MarginWealth”, “we”, “us” or “our”) is committed to protecting your privacy. This Privacy Policy explains what information we collect when you use our website, mobile apps and trading services, how we use and safeguard it, and the choices you have. By using MarginWealth you agree to the practices described here.",
  },
  {
    title: "2. Information we collect",
    body: "Account & identity details you provide when registering or verifying your account — name, date of birth, email, mobile number, address and government-issued identifiers required by law. Financial & transaction data such as bank account details, deposits, withdrawals, orders, positions and trading history. Technical & usage data including device type, browser, IP address, operating system and how you interact with the platform. Communications you send us through support, chat or email.",
  },
  {
    title: "3. How we use your information",
    body: "We use your information to open and operate your account; execute, settle and report your trades; process deposits and withdrawals; verify your identity and meet regulatory obligations; secure the platform and detect fraud; provide customer support; and improve our products. We may send you service and transactional messages; marketing messages are only sent with your consent and you can opt out anytime.",
  },
  {
    title: "4. Legal basis & regulatory compliance",
    body: "As a financial services platform we process certain data to comply with applicable Indian laws and regulator requirements, including identity verification, record-keeping and anti-money-laundering rules. Where processing is not required by law, we rely on your consent or our legitimate interest in operating a secure, reliable service.",
  },
  {
    title: "5. How we share information",
    body: "We do NOT sell your personal data. We share it only when necessary: with regulators, exchanges, depositories and law-enforcement when required by law; with payment partners and banks to process your deposits and withdrawals; and with trusted service providers (e.g. cloud hosting, KYC/identity, analytics and communication vendors) who act on our instructions under confidentiality obligations. If MarginWealth is involved in a merger or acquisition, data may transfer to the successor entity under this policy.",
  },
  {
    title: "6. Cookies & tracking",
    body: "We use cookies and similar technologies to keep you logged in, remember your preferences, secure your session and understand how the platform is used. You can control cookies through your browser settings; disabling some cookies may affect how the platform works.",
  },
  {
    title: "7. Data security",
    body: "We protect your data with industry-standard safeguards — encryption in transit, access controls, segregated environments and continuous monitoring. No system is perfectly secure, so we also ask you to keep your login credentials confidential and to enable available security features such as two-factor authentication and biometric/PIN locks on the mobile app.",
  },
  {
    title: "8. Data retention",
    body: "We retain your information for as long as your account is active and thereafter for the period required to meet legal, regulatory, tax and accounting obligations, to resolve disputes and to enforce our agreements. When data is no longer required, it is securely deleted or anonymised.",
  },
  {
    title: "9. Your rights & choices",
    body: "Subject to applicable law, you can access and review your account information, request corrections to inaccurate data, withdraw consent for marketing communications, and request deletion of data we are not legally required to retain. To exercise these rights, contact us using the details below; we may need to verify your identity before acting on a request.",
  },
  {
    title: "10. Third-party links",
    body: "Our website and apps may link to third-party sites or services that we do not control. This policy does not cover their practices — please review the privacy policies of any third party before sharing information with them.",
  },
  {
    title: "11. Children",
    body: "MarginWealth is intended for users who are 18 years or older. We do not knowingly collect personal information from minors. If you believe a minor has provided us data, please contact us and we will delete it.",
  },
  {
    title: "12. Changes to this policy",
    body: "We may update this Privacy Policy from time to time to reflect changes in our practices or the law. We will post the revised version on this page and update the “last updated” date above. Significant changes may be communicated to you directly.",
  },
];

export default function PrivacyPage() {
  return (
    <>
      <MpPageHero
        eyebrow="Legal"
        title="Privacy Policy"
        lead="Your trust matters. This policy explains how MarginWealth collects, uses, protects and shares your personal and financial information."
      />
      <MpSection>
        <div className="flex flex-col gap-8">
          <p className="text-sm font-medium text-mp-text-mut">
            Last updated: {LAST_UPDATED}
          </p>

          {SECTIONS.map((s) => (
            <div key={s.title}>
              <h2 className="font-display text-xl font-semibold text-mp-text">
                {s.title}
              </h2>
              <MpProse className="mt-3">{s.body}</MpProse>
            </div>
          ))}

          {/* Contact card */}
          <div className="rounded-2xl border border-mp-border bg-mp-surface/60 p-6 sm:p-8">
            <h2 className="font-display text-xl font-semibold text-mp-text">
              13. Contact us
            </h2>
            <MpProse className="mt-3">
              Questions about this Privacy Policy or how your data is handled?
              Reach our privacy team and we&apos;ll be glad to help.
            </MpProse>
            <div className="mt-4 flex flex-col gap-1 text-sm text-mp-text">
              <span>
                Email:{" "}
                <a
                  href="mailto:privacy@marginwealth.live"
                  className="font-medium text-mp-accent hover:underline"
                >
                  privacy@marginwealth.live
                </a>
              </span>
              <span className="text-mp-text-mut">
                Support:{" "}
                <a href="/contact" className="text-mp-accent hover:underline">
                  marginwealth.live/contact
                </a>
              </span>
            </div>
          </div>

          <p className="text-sm text-mp-text-mut">
            This Privacy Policy is provided for general information. The final,
            legally binding policy should be reviewed and confirmed by your legal
            team before publishing.
          </p>
        </div>
      </MpSection>
    </>
  );
}
