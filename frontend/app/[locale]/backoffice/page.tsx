import { notFound } from "next/navigation";

import { Backoffice } from "@/components/backoffice/Backoffice";
import { backofficeMessages } from "@/lib/backoffice-i18n";
import { LOCALES, bcp47, isLocale } from "@/lib/i18n";

export function generateStaticParams() {
  return LOCALES.map((locale) => ({ locale }));
}

export const dynamicParams = false;

export const metadata = { robots: { index: false, follow: false } };

export default async function BackofficePage({ params }: { params: Promise<{ locale: string }> }) {
  const { locale } = await params;
  if (!isLocale(locale)) notFound();
  const t = backofficeMessages(locale);
  return (
    <main lang={bcp47(locale)} className="page-wide">
      <h1 className="text-2xl font-semibold tracking-tight">{t.title}</h1>
      <Backoffice locale={locale} t={t} />
    </main>
  );
}
