"use client";

import { useState } from "react";

import { ApiError, type EsignStart, type Order, confirmEsign, startEsign } from "@/lib/api";
import { formatPaise } from "@/lib/format";
import { type Messages, fill } from "@/lib/i18n";

/**
 * Sign the order form (mock Aadhaar e-sign) after KYC passes, before payment. The
 * owner sees exactly what they agree to, asks for a code and types it; the server
 * keeps the SHA-256 of that form as the signature.
 */
export function EsignStep({ order, t, onSigned }: { order: Order; t: Messages; onSigned: (order: Order) => void }) {
  const [started, setStarted] = useState<EsignStart | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [closed, setClosed] = useState(false);

  const send = async () => {
    setBusy(true);
    setMessage(null);
    setClosed(false);
    setCode("");
    try {
      setStarted(await startEsign(order.order_id));
    } catch {
      setMessage(t.advisor.error);
    } finally {
      setBusy(false);
    }
  };

  const sign = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!started) return;
    setBusy(true);
    setMessage(null);
    try {
      onSigned(await confirmEsign(order.order_id, started.request_id, code.trim()));
    } catch (e) {
      const err = e instanceof ApiError ? e : null;
      if (err?.code === "ESIGN_CODE_WRONG") {
        const left = Number(err.detail?.attempts_left ?? 0);
        setMessage(left > 0 ? fill(t.otp.wrongCode, { left }) : t.otp.locked);
        if (left <= 0) setClosed(true);
      } else if (err?.code === "ESIGN_EXPIRED" || err?.code === "ESIGN_REQUEST_CLOSED") {
        setMessage(err.code === "ESIGN_EXPIRED" ? t.otp.expired : t.otp.locked);
        setClosed(true);
      } else {
        setMessage(t.advisor.error);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <section data-testid="esign" className="flex flex-col gap-3 rounded-xl bg-white p-4 ring-1 ring-neutral-200 dark:bg-neutral-900 dark:ring-neutral-700">
      <h3 className="font-semibold">{t.checkout.signTitle}</h3>
      <p className="text-sm text-neutral-600 dark:text-neutral-300">{t.checkout.signIntro}</p>
      <div data-testid="esign-form" className="rounded-lg bg-neutral-100 p-3 text-sm dark:bg-neutral-800">
        <ul>
          {order.lines.map((l) => (
            <li key={l.product_id}>
              {l.product_name}
              {l.qty > 1 ? ` × ${l.qty}` : ""}
            </li>
          ))}
        </ul>
        <p className="mt-1 font-medium tabular-nums">
          {formatPaise(order.monthly_total_paise)}
          {t.quote.perMonth} · {t.quote.setup} {formatPaise(order.setup_total_paise)} · {order.contract_months} {t.quote.months}
        </p>
      </div>
      {!started || closed ? (
        <button
          type="button"
          disabled={busy}
          onClick={() => void send()}
          className="h-12 rounded-xl bg-red-700 font-medium text-white disabled:opacity-50"
        >
          {closed ? t.otp.resend : t.checkout.signSend}
        </button>
      ) : (
        <form onSubmit={(e) => void sign(e)} className="flex flex-col gap-2">
          {started.dev_code && (
            <p className="text-xs text-neutral-500" data-testid="esign-dev-code">
              {fill(t.otp.devCode, { code: started.dev_code })}
            </p>
          )}
          <label className="flex flex-col gap-1 text-sm font-medium">
            {t.otp.code}
            <input
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={6}
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
              className="h-11 rounded-lg px-3 font-normal tracking-widest ring-1 ring-neutral-300 dark:bg-neutral-900 dark:ring-neutral-600"
            />
          </label>
          <button
            type="submit"
            disabled={busy || code.length !== 6}
            className="h-12 rounded-xl bg-red-700 font-medium text-white disabled:opacity-50"
          >
            {t.checkout.signConfirm}
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
