import { notFound } from "next/navigation";

import { Landing } from "@/components/landing/Landing";
import { LOCALES, isLocale, messagesFor } from "@/lib/i18n";

export function generateStaticParams() {
  return LOCALES.map((locale) => ({ locale }));
}

export const dynamicParams = false;

export default async function LocaleHome({ params }: { params: Promise<{ locale: string }> }) {
  const { locale } = await params;
  if (!isLocale(locale)) notFound();
  return <Landing locale={locale} t={messagesFor(locale)} />;
}
