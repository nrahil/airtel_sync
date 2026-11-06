"use client";

import { useState } from "react";

import { OtpStep } from "@/components/checkout/OtpStep";
import { ApiError, askForHelp, getToken, setToken } from "@/lib/api";
import type { Messages } from "@/lib/i18n";

type Kind = "callback" | "resume_link";

/**
 * "Need a hand?": have someone call me, or text me a link to finish later. Both need a
 * confirmed mobile number (OTP) so nobody can make us call or text someone else.
 */
export function HelpPanel({ sessionId, t }: { sessionId: string; t: Messages }) {
  const [kind, setKind] = useState<Kind | null>(null);
  const [needsOtp, setNeedsOtp] = useState(false);
  const [done, setDone] = useState<Kind | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);

  const send = async (which: Kind) => {
    setBusy(true);
    setFailed(false);
    try {
      const res = await askForHelp(sessionId, which);
      if (res.status === "FAILED") setFailed(true);
      else {
        setDone(which);
        setKind(null);
        setNeedsOtp(false);
      }
    } catch (e) {
      if (e instanceof ApiError && (e.status === 401 || e.code === "NO_CONTACT")) {
        setToken(null);
        setNeedsOtp(true); // confirm the number first, then send
      } else setFailed(true);
    } finally {
      setBusy(false);
    }
  };

  const choose = (which: Kind) => {
    setKind(which);
    setDone(null);
    setFailed(false);
    if (getToken()) void send(which);
    else setNeedsOtp(true);
  };

  const link = "text-left text-sm font-medium text-red-700 underline disabled:opacity-50";
  return (
    <section
      data-testid="help-panel"
      className="flex flex-col gap-2 rounded-xl bg-neutral-50 p-3 ring-1 ring-neutral-200 dark:bg-neutral-900 dark:ring-neutral-700"
    >
      <h2 className="text-sm font-semibold">{t.advisor.help.title}</h2>
      {done && (
        <p role="status" data-testid="help-done" className="text-sm text-green-800 dark:text-green-200">
          {done === "callback" ? t.advisor.help.callDone : t.advisor.help.textDone}
        </p>
      )}
      {!kind && (
        <div className="flex flex-col gap-2">
          {done !== "callback" && (
            <button type="button" disabled={busy} className={link} onClick={() => choose("callback")}>
              {t.advisor.help.callMe}
            </button>
          )}
          {done !== "resume_link" && (
            <button type="button" disabled={busy} className={link} onClick={() => choose("resume_link")}>
              {t.advisor.help.textMe}
            </button>
          )}
        </div>
      )}
      {kind && needsOtp && (
        <div className="flex flex-col gap-2">
          <p className="text-sm text-neutral-600 dark:text-neutral-300">
            {kind === "callback" ? t.advisor.help.callIntro : t.advisor.help.textIntro}
          </p>
          <OtpStep t={t} onVerified={() => void send(kind)} />
          <button type="button" className="self-start text-sm text-neutral-500 underline" onClick={() => setKind(null)}>
            {t.advisor.help.cancel}
          </button>
        </div>
      )}
      {failed && (
        <p role="alert" className="text-sm text-red-700">
          {t.advisor.help.failed}
        </p>
      )}
    </section>
  );
}
