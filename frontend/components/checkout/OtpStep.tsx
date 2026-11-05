"use client";

import { useState } from "react";

import { ApiError, requestOtp, setToken, verifyOtp } from "@/lib/api";
import { type Messages, fill } from "@/lib/i18n";

/** Mock phone OTP. The number goes to the server only; it is never stored in the browser. */
export function OtpStep({ t, onVerified }: { t: Messages; onVerified: () => void }) {
  const [phone, setPhone] = useState("");
  const [challenge, setChallenge] = useState<{ id: string; devCode?: string } | null>(null);
  const [code, setCode] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const send = async (event?: React.FormEvent) => {
    event?.preventDefault();
    setBusy(true);
    setMessage(null);
    try {
      const res = await requestOtp(phone);
      setChallenge({ id: res.challenge_id, devCode: res.dev_code });
      setCode("");
    } catch (e) {
      setMessage(
        e instanceof ApiError && e.code === "INVALID_PHONE"
          ? t.otp.invalidPhone
          : e instanceof ApiError && e.status === 429
            ? t.otp.tooMany
            : t.advisor.error,
      );
    } finally {
      setBusy(false);
    }
  };

  const verify = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!challenge) return;
    setBusy(true);
    setMessage(null);
    try {
      const res = await verifyOtp(challenge.id, code.trim());
      setToken(res.token);
      onVerified();
    } catch (e) {
      const err = e instanceof ApiError ? e : null;
      if (err?.code === "OTP_INVALID") {
        setMessage(fill(t.otp.wrongCode, { left: Number(err.detail?.attempts_left ?? 0) }));
      } else if (err?.code === "OTP_LOCKED" || err?.code === "OTP_EXPIRED") {
        setMessage(err.code === "OTP_LOCKED" ? t.otp.locked : t.otp.expired);
        setChallenge(null);
      } else {
        setMessage(t.advisor.error);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="flex flex-col gap-3" data-testid="otp">
      <h2 className="text-lg font-semibold">{t.otp.title}</h2>
      <p className="text-sm text-neutral-600 dark:text-neutral-300">{t.otp.intro}</p>
      {!challenge ? (
        <form onSubmit={(e) => void send(e)} className="flex items-end gap-2">
          <label className="flex flex-1 flex-col gap-1 text-sm">
            {t.otp.phone}
            <input
              type="tel"
              inputMode="tel"
              autoComplete="tel-national"
              maxLength={16}
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              className="h-12 rounded-xl border border-neutral-300 px-3 text-base dark:border-neutral-600 dark:bg-neutral-900"
            />
          </label>
          <button
            type="submit"
            disabled={busy || phone.replace(/\D/g, "").length < 10}
            className="h-12 rounded-xl bg-red-700 px-4 font-medium text-white disabled:opacity-50"
          >
            {t.otp.send}
          </button>
        </form>
      ) : (
        <form onSubmit={(e) => void verify(e)} className="flex flex-col gap-2">
          <div className="flex items-end gap-2">
            <label className="flex flex-1 flex-col gap-1 text-sm">
              {t.otp.code}
              <input
                inputMode="numeric"
                autoComplete="one-time-code"
                pattern="[0-9]{6}"
                maxLength={6}
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
                className="h-12 rounded-xl border border-neutral-300 px-3 text-base tracking-widest dark:border-neutral-600 dark:bg-neutral-900"
              />
            </label>
            <button
              type="submit"
              disabled={busy || code.length !== 6}
              className="h-12 rounded-xl bg-red-700 px-4 font-medium text-white disabled:opacity-50"
            >
              {t.otp.verify}
            </button>
          </div>
          {challenge.devCode && (
            <p className="text-xs text-neutral-500" data-testid="dev-code">
              {fill(t.otp.devCode, { code: challenge.devCode })}
            </p>
          )}
          <button type="button" onClick={() => void send()} disabled={busy} className="self-start text-sm underline">
            {t.otp.resend}
          </button>
        </form>
      )}
      {message && (
        <p role="alert" className="text-sm text-red-700">
          {message}
        </p>
      )}
    </section>
  );
}
