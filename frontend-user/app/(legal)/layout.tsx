import { SiteFooter, SiteHeader } from "@/components/landing/site";
import "@/components/landing/landing.css";

/**
 * Chrome for the legal pages (terms, privacy, refund). They keep the
 * `mp-scope` palette their prose components were written against, but
 * wear the public site's header and footer so the whole site reads as
 * one thing.
 */
export default function LegalLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-white">
      <SiteHeader />
      <main className="mp-scope bg-mp-bg text-mp-text">{children}</main>
      <SiteFooter />
    </div>
  );
}
