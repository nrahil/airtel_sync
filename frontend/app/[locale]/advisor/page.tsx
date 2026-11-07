import Link from "next/link";
import { notFound } from "next/navigation";

import { Advisor } from "@/components/advisor/Advisor";
import { LanguagePicker } from "@/components/LanguagePicker";
import { LOCALES, bcp47, isLocale, messagesFor } from "@/lib/i18n";

export function generateStaticParams() {
  return LOCALES.map((locale) => ({ locale }));
}

export const dynamicParams = false;

export default async function AdvisorPage({ params }: { params: Promise<{ locale: string }> }) {
  const { locale } = await params;
  if (!isLocale(locale)) notFound();
  const t = messagesFor(locale);
  return (
    <main lang={bcp47(locale)} className="mx-auto flex min-h-dvh max-w-md flex-col gap-4 px-4 py-6">
      <Link href={`/${locale}`} className="self-start text-sm text-neutral-500 underline" data-testid="home">
        ← {t.advisor.home}
      </Link>
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">{t.advisor.title}</h1>
        <p className="text-neutral-600 dark:text-neutral-300">{t.advisor.subtitle}</p>
        <p className="text-xs text-neutral-500">{t.advisor.privacy}</p>
      </header>
      <LanguagePicker current={locale} path="/advisor" />
      <Advisor locale={locale} t={t} />
    </main>
  );
}
