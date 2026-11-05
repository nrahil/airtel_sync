import bn from "@/messages/bn.json";
import en from "@/messages/en.json";
import gu from "@/messages/gu.json";
import hi from "@/messages/hi.json";
import kn from "@/messages/kn.json";
import ml from "@/messages/ml.json";
import mr from "@/messages/mr.json";
import pa from "@/messages/pa.json";
import ta from "@/messages/ta.json";
import te from "@/messages/te.json";

/** The 10 languages owners prefer in the leads data (ARCHITECTURE §2). */
export const LOCALES = ["hi", "en", "mr", "kn", "gu", "te", "ta", "bn", "pa", "ml"] as const;
export type Locale = (typeof LOCALES)[number];

export type Messages = typeof en;

// `satisfies` makes the compiler reject a catalogue that is missing a key.
const CATALOGUES = { en, hi, mr, kn, gu, te, ta, bn, pa, ml } satisfies Record<Locale, Messages>;

export function isLocale(value: string): value is Locale {
  return (LOCALES as readonly string[]).includes(value);
}

export function messagesFor(locale: Locale): Messages {
  return CATALOGUES[locale];
}

/** BCP-47 tag for `Intl` and the `lang` attribute. */
export function bcp47(locale: Locale): string {
  return `${locale}-IN`;
}

/** Fill `{name}` placeholders. Values are plain text; React escapes them on render. */
export function fill(template: string, values: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (match, key: string) =>
    key in values ? String(values[key]) : match,
  );
}
