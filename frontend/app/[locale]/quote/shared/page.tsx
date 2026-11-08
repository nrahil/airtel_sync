import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { Suspense } from "react";

import { SharedQuoteView } from "@/components/quote/SharedQuote";
import { LOCALES, bcp47, isLocale, messagesFor } from "@/lib/i18n";

export function generateStaticParams() {
  return LOCALES.map((locale) => ({ locale }));
}

export const dynamicParams = false;

// A personal price link: keep it out of search engines.
export const metadata: Metadata = { robots: { index: false, follow: false } };

/** A quote someone was sent ("send this to my accountant"): read-only, no checkout. */
export default async function SharedQuotePage({ params }: { params: Promise<{ locale: string }> }) {
  const { locale } = await params;
  if (!isLocale(locale)) notFound();
  const t = messagesFor(locale);
  return (
    <main lang={bcp47(locale)} className="page-column">
      <h1 className="text-2xl font-semibold tracking-tight">{t.shared.title}</h1>
      <Suspense fallback={<p className="text-neutral-500">{t.advisor.thinking}</p>}>
        <SharedQuoteView locale={locale} t={t} />
      </Suspense>
    </main>
  );
}
