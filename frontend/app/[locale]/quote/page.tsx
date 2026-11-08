import { notFound } from "next/navigation";
import { Suspense } from "react";

import { QuoteScreen } from "@/components/quote/QuoteScreen";
import { LOCALES, bcp47, isLocale, messagesFor } from "@/lib/i18n";

export function generateStaticParams() {
  return LOCALES.map((locale) => ({ locale }));
}

export const dynamicParams = false;

export default async function QuotePage({ params }: { params: Promise<{ locale: string }> }) {
  const { locale } = await params;
  if (!isLocale(locale)) notFound();
  const t = messagesFor(locale);
  return (
    <main lang={bcp47(locale)} className="page-wide">
      <h1 className="text-2xl font-semibold tracking-tight">{t.quote.screenTitle}</h1>
      <Suspense fallback={<p className="text-neutral-500">{t.advisor.thinking}</p>}>
        <QuoteScreen locale={locale} t={t} />
      </Suspense>
    </main>
  );
}
