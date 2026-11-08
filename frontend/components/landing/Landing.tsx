import Link from "next/link";

import { LanguagePicker } from "@/components/LanguagePicker";
import { backofficeMessages } from "@/lib/backoffice-i18n";
import { type Locale, type Messages, bcp47 } from "@/lib/i18n";

/** The front door: what this is, one "Start" button, and the way back to an order. */
export function Landing({ locale, t }: { locale: Locale; t: Messages }) {
  const l = t.landing;
  return (
    <main lang={bcp47(locale)} className="mx-auto grid min-h-dvh w-full max-w-md grid-cols-1 content-center gap-6 px-6 py-8 sm:max-w-lg lg:max-w-5xl lg:grid-cols-2 lg:items-center lg:gap-16 lg:px-8">
      <div className="flex flex-col gap-6">
        <header className="flex flex-col gap-2">
          <h1 className="text-3xl font-semibold sm:text-4xl lg:text-5xl tracking-tight">Udyam Digital</h1>
          <p className="text-lg leading-snug lg:text-xl text-neutral-700 dark:text-neutral-200" data-testid="tagline">
            {l.tagline}
          </p>
        </header>
        <ul className="flex flex-col gap-2 text-neutral-700 dark:text-neutral-300">
          {[l.point1, l.point2, l.point3].map((point) => (
            <li key={point} className="flex gap-2">
              <span aria-hidden="true" className="text-red-700">
                ✓
              </span>
              {point}
            </li>
          ))}
        </ul>
      </div>
      <div className="flex flex-col gap-6 lg:rounded-2xl lg:p-8 lg:shadow-sm lg:ring-1 lg:ring-neutral-200 dark:lg:ring-neutral-700">
        <div className="flex flex-col gap-3">
          <Link
            href={`/${locale}/advisor`}
            data-testid="start"
            className="flex h-14 items-center justify-center rounded-xl bg-red-700 text-lg font-medium text-white"
          >
            {l.start}
          </Link>
          <Link href={`/${locale}/track`} data-testid="track" className="self-center text-red-700 underline">
            {l.track}
          </Link>
        </div>
        <Link href={`/${locale}/privacy`} data-testid="my-data" className="self-center text-sm text-neutral-600 underline dark:text-neutral-300">
          {t.privacy.link}
        </Link>
        <section className="flex flex-col gap-1">
          <h2 className="text-sm font-medium text-neutral-600 dark:text-neutral-300">{l.language}</h2>
          <LanguagePicker current={locale} autoNote={l.autoPicked} />
        </section>
        <Link
          href={`/${locale}/backoffice`}
          data-testid="staff-sign-in"
          className="flex min-h-11 items-center justify-center self-center rounded-xl px-4 text-sm font-medium ring-1 ring-neutral-300 dark:ring-neutral-600"
        >
          {backofficeMessages(locale).signIn}
        </Link>
      </div>
    </main>
  );
}
