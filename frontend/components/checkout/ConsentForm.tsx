"use client";

import Link from "next/link";
import { useState } from "react";

import { ApiError, type ConsentState, PURPOSES, type Purpose, submitConsents } from "@/lib/api";
import type { Locale, Messages } from "@/lib/i18n";

const REQUIRED: readonly Purpose[] = ["NEEDS_ASSESSMENT", "KYC_VERIFICATION"];

/**
 * DPDP consent screen: plain-language notice, one separate choice per purpose, none
 * pre-ticked. The notice version shown is sent with the choices and recorded in the
 * server's hash-chained ledger.
 */
export function ConsentForm({
  locale,
  t,
  current,
  onSaved,
}: {
  locale: Locale;
  t: Messages;
  current: ConsentState | null;
  onSaved: (state: ConsentState) => void;
}) {
  const [choices, setChoices] = useState<Record<Purpose, boolean>>(() => ({
    NEEDS_ASSESSMENT: current?.purposes.NEEDS_ASSESSMENT.granted ?? false,
    KYC_VERIFICATION: current?.purposes.KYC_VERIFICATION.granted ?? false,
    MARKETING: current?.purposes.MARKETING.granted ?? false,
  }));
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  // A newer notice was published since this page loaded: show it before asking again.
  const [outdated, setOutdated] = useState(false);
  const missingRequired = REQUIRED.some((p) => !choices[p]);

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setMessage(null);
    setOutdated(false);
    try {
      const state = await submitConsents(t.consent.noticeVersion, locale, choices);
      setMessage(t.consent.saved);
      onSaved(state);
    } catch (e) {
      const old = e instanceof ApiError && e.code === "NOTICE_OUTDATED";
      setOutdated(old);
      setMessage(old ? t.consent.outdated : t.advisor.error);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={(e) => void save(e)} className="flex flex-col gap-4" data-testid="consent">
      <header className="flex flex-col gap-1">
        <h2 className="text-lg font-semibold">{t.consent.title}</h2>
        <p className="text-sm text-neutral-600 dark:text-neutral-300">{t.consent.intro}</p>
      </header>

      <fieldset className="flex flex-col gap-2">
        {PURPOSES.map((purpose) => (
          <label
            key={purpose}
            className="flex gap-3 rounded-xl bg-white p-3 ring-1 ring-neutral-200 dark:bg-neutral-900 dark:ring-neutral-700"
          >
            <input
              type="checkbox"
              name={purpose}
              checked={choices[purpose]}
              onChange={(e) => setChoices((c) => ({ ...c, [purpose]: e.target.checked }))}
              className="mt-1 h-5 w-5 shrink-0 accent-red-700"
            />
            <span className="flex flex-col gap-0.5">
              <span className="font-medium">
                {t.consent[purpose].title}{" "}
                <span className="text-xs font-normal text-neutral-500">
                  · {REQUIRED.includes(purpose) ? t.consent.required : t.consent.optional}
                </span>
              </span>
              <span className="text-sm text-neutral-600 dark:text-neutral-300">{t.consent[purpose].body}</span>
              <span className="text-xs text-neutral-500" data-testid={`keep-${purpose}`}>
                {t.consent[purpose].keep}
              </span>
            </span>
          </label>
        ))}
      </fieldset>

      <dl className="flex flex-col gap-2 text-sm">
        {(
          [
            ["processorsTitle", "processors"],
            ["retentionTitle", "retention"],
            ["rightsTitle", "rights"],
          ] as const
        ).map(([title, body]) => (
          <div key={title}>
            <dt className="font-semibold">{t.consent[title]}</dt>
            <dd className="text-neutral-600 dark:text-neutral-300">{t.consent[body]}</dd>
          </div>
        ))}
      </dl>

      <Link href={`/${locale}/privacy`} data-testid="consent-my-data" className="self-start text-sm underline">
        {t.consent.myData}
      </Link>

      {missingRequired && <p className="text-sm text-neutral-600 dark:text-neutral-300">{t.consent.needRequired}</p>}
      <button type="submit" disabled={busy} className="min-h-12 rounded-xl bg-red-700 px-4 py-2 font-medium text-white disabled:opacity-50">
        {t.consent.save}
      </button>
      {message && (
        <p role="status" className="text-sm">
          {message}
        </p>
      )}
      {outdated && (
        <button
          type="button"
          data-testid="consent-reload"
          onClick={() => window.location.reload()}
          className="self-start rounded-lg px-3 py-2 text-sm font-medium ring-1 ring-neutral-300 dark:ring-neutral-600"
        >
          {t.consent.reload}
        </button>
      )}
    </form>
  );
}
