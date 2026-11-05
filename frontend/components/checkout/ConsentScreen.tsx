"use client";

import { useEffect, useState } from "react";

import { ConsentForm } from "@/components/checkout/ConsentForm";
import { OtpStep } from "@/components/checkout/OtpStep";
import { ApiError, type ConsentState, getConsents, getToken, setToken } from "@/lib/api";
import type { Locale, Messages } from "@/lib/i18n";

/** Stand-alone consent management: sign in, then review or change each purpose. */
export function ConsentScreen({ locale, t }: { locale: Locale; t: Messages }) {
  const [signedIn, setSignedIn] = useState(false);
  const [state, setState] = useState<ConsentState | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    setSignedIn(Boolean(getToken()));
  }, []);

  useEffect(() => {
    if (!signedIn) return;
    getConsents()
      .then(setState)
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 401) {
          setToken(null);
          setSignedIn(false);
        } else {
          setFailed(true);
        }
      });
  }, [signedIn]);

  if (!signedIn) return <OtpStep t={t} onVerified={() => setSignedIn(true)} />;
  if (failed) return <p role="alert">{t.advisor.error}</p>;
  if (!state) return <p className="text-neutral-500">{t.advisor.thinking}</p>;
  return <ConsentForm key={state.notice_version} locale={locale} t={t} current={state} onSaved={setState} />;
}
