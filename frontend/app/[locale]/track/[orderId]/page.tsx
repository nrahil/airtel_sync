import Link from "next/link";
import { notFound } from "next/navigation";

import { Tracker } from "@/components/track/Tracker";
import { bcp47, isLocale, messagesFor } from "@/lib/i18n";

// Order ids are random UUIDs; anything else is not an order (and never reaches the API).
const ORDER_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export default async function TrackOrderPage({
  params,
}: {
  params: Promise<{ locale: string; orderId: string }>;
}) {
  const { locale, orderId } = await params;
  if (!isLocale(locale) || !ORDER_ID.test(orderId)) notFound();
  const t = messagesFor(locale);
  return (
    <main lang={bcp47(locale)} className="mx-auto flex min-h-dvh max-w-md flex-col gap-4 px-4 py-6">
      <h1 className="text-2xl font-semibold tracking-tight">{t.track.title}</h1>
      <Tracker locale={locale} t={t} orderId={orderId} />
      <Link href={`/${locale}/track`} className="self-start text-sm underline">
        {t.track.listTitle}
      </Link>
    </main>
  );
}
