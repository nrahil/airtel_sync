"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { LOCALES, type Locale, bcp47, isLocale, messagesFor } from "@/lib/i18n";

/** The owner's own pick, remembered so `/` opens in their language next time. */
export const LOCALE_KEY = "udyam.locale";

/** First supported language in the phone's preferences (`hi-IN` → `hi`), if any. */
export function localeFromBrowser(languages: readonly string[]): Locale | null {
  for (const tag of languages) {
    const code = tag.toLowerCase().split("-")[0];
    if (isLocale(code)) return code;
  }
  return null;
}

/** All 10 languages, each in its own script. `path` is the page after the locale ("/advisor"). */
export function LanguagePicker({
  current,
  path = "",
  autoNote,
  className = "flex flex-wrap gap-x-3 gap-y-1 text-sm",
}: {
  current: Locale;
  path?: string;
  autoNote?: string;
  className?: string;
}) {
  const [auto, setAuto] = useState(false);
  useEffect(() => {
    // `?auto=1`: we picked this language from the phone; say so, and how to change it.
    setAuto(new URLSearchParams(window.location.search).get("auto") === "1");
  }, []);
  return (
    <div className="flex flex-col gap-1">
      <nav aria-label="Language" className={className}>
        {LOCALES.map((code) => (
          <Link
            key={code}
            href={`/${code}${path}`}
            lang={bcp47(code)}
            aria-current={code === current ? "page" : undefined}
            onClick={() => localStorage.setItem(LOCALE_KEY, code)}
            className={code === current ? "font-semibold text-red-700" : "text-neutral-500 underline"}
          >
            {messagesFor(code).languageName}
          </Link>
        ))}
      </nav>
      {auto && autoNote && (
        <p className="text-xs text-neutral-500" data-testid="auto-picked">
          {autoNote}
        </p>
      )}
    </div>
  );
}
