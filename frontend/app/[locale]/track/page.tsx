import { notFound } from "next/navigation";

import { OrderList } from "@/components/track/OrderList";
import { LOCALES, bcp47, isLocale, messagesFor } from "@/lib/i18n";

export function generateStaticParams() {
  return LOCALES.map((locale) => ({ locale }));
}

export const dynamicParams = false;

export default async function TrackListPage({ params }: { params: Promise<{ locale: string }> }) {
  const { locale } = await params;
  if (!isLocale(locale)) notFound();
  const t = messagesFor(locale);
  return (
    <main lang={bcp47(locale)} className="mx-auto flex min-h-dvh max-w-md flex-col gap-4 px-4 py-6">
      <h1 className="text-2xl font-semibold tracking-tight">{t.track.listTitle}</h1>
      <OrderList locale={locale} t={t} />
    </main>
  );
}
