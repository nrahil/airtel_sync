"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { LOCALE_KEY, localeFromBrowser } from "@/components/LanguagePicker";
import { isLocale } from "@/lib/i18n";

/**
 * On `/` (English): open the owner's language instead — the one they picked before,
 * else the phone's. A phone-picked language is flagged (`?auto=1`) so the landing
 * page can say so and show how to change it.
 */
export function LocaleAutoPick() {
  const router = useRouter();
  useEffect(() => {
    const stored = localStorage.getItem(LOCALE_KEY);
    if (stored && isLocale(stored)) {
      if (stored !== "en") router.replace(`/${stored}`);
      return;
    }
    const fromPhone = localeFromBrowser(navigator.languages ?? [navigator.language]);
    if (fromPhone && fromPhone !== "en") router.replace(`/${fromPhone}?auto=1`);
  }, [router]);
  return null;
}
