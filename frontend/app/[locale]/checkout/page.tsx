import { notFound } from "next/navigation";
import { Suspense } from "react";

import { Checkout } from "@/components/checkout/Checkout";
import { LOCALES, bcp47, isLocale, messagesFor } from "@/lib/i18n";

export function generateStaticParams() {
  return LOCALES.map((locale) => ({ locale }));
}

export const dynamicParams = false;

export default async function CheckoutPage({ params }: { params: Promise<{ locale: string }> }) {
  const { locale } = await params;
  if (!isLocale(locale)) notFound();
  const t = messagesFor(locale);
  return (
    <main lang={bcp47(locale)} className="mx-auto flex min-h-dvh max-w-md flex-col gap-4 px-4 py-6">
      <h1 className="text-2xl font-semibold tracking-tight">{t.checkout.title}</h1>
      <Suspense fallback={<p className="text-neutral-500">{t.advisor.thinking}</p>}>
        <Checkout locale={locale} t={t} />
      </Suspense>
    </main>
  );
}
