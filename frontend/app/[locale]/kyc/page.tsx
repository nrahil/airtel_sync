import { notFound } from "next/navigation";
import { Suspense } from "react";

import { KycScreen } from "@/components/kyc/KycScreen";
import { LOCALES, bcp47, isLocale, messagesFor } from "@/lib/i18n";

export function generateStaticParams() {
  return LOCALES.map((locale) => ({ locale }));
}

export const dynamicParams = false;

export default async function KycPage({ params }: { params: Promise<{ locale: string }> }) {
  const { locale } = await params;
  if (!isLocale(locale)) notFound();
  const t = messagesFor(locale);
  return (
    <main lang={bcp47(locale)} className="mx-auto flex min-h-dvh max-w-md flex-col gap-4 px-4 py-6">
      <h1 className="text-2xl font-semibold tracking-tight">{t.kyc.title}</h1>
      <Suspense fallback={<p className="text-neutral-500">{t.advisor.thinking}</p>}>
        <KycScreen locale={locale} t={t} />
      </Suspense>
    </main>
  );
}
