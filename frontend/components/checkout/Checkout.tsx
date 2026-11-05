"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ConsentForm } from "@/components/checkout/ConsentForm";
import { OtpStep } from "@/components/checkout/OtpStep";
import {
  ApiError,
  type ConsentState,
  type Order,
  type PaymentMethod,
  type QuoteView,
  getConsents,
  getOrder,
  getQuote,
  getToken,
  newKey,
  payOrder,
  placeOrder,
  setToken,
} from "@/lib/api";
import { formatPaise } from "@/lib/format";
import { type Locale, type Messages, fill } from "@/lib/i18n";

type Step = "loading" | "otp" | "consent" | "confirm" | "order";

const METHODS: PaymentMethod[] = ["upi", "card", "netbanking"];
const orderKeyFor = (quoteId: string) => `udyam.orderKey.${quoteId}`;
const orderIdFor = (quoteId: string) => `udyam.order.${quoteId}`;

/** OTP → consent → confirm order (idempotent) → mock payment (retryable). */
export function Checkout({ locale, t }: { locale: Locale; t: Messages }) {
  const quoteId = useSearchParams().get("q");
  const [step, setStep] = useState<Step>("loading");
  const [quote, setQuote] = useState<QuoteView | null>(null);
  const [consent, setConsent] = useState<ConsentState | null>(null);
  const [order, setOrder] = useState<Order | null>(null);
  const [method, setMethod] = useState<PaymentMethod>("upi");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [lastPayment, setLastPayment] = useState<"FAILED" | "SUCCEEDED" | null>(null);

  const explain = useCallback(
    (e: unknown): string => {
      const code = e instanceof ApiError ? e.code : null;
      switch (code) {
        case "QUOTE_EXPIRED":
          return t.quote.expired;
        case "QUOTE_STALE":
        case "QUOTE_SUPERSEDED":
          return t.checkout.stale;
        case "QUOTE_TAMPERED":
          return t.checkout.tampered;
        case "KYC_NOT_VERIFIED":
          return t.checkout.kycRequired;
        case "CONSENT_REQUIRED":
          return t.checkout.consentRequired;
        default:
          return t.advisor.error;
      }
    },
    [t],
  );

  const afterSignIn = useCallback(async () => {
    if (!quoteId) return;
    try {
      const savedOrder = sessionStorage.getItem(orderIdFor(quoteId));
      if (savedOrder) {
        setOrder(await getOrder(savedOrder));
        setStep("order");
        return;
      }
      const state = await getConsents();
      setConsent(state);
      setStep(state.ready_for_checkout ? "confirm" : "consent");
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setToken(null); // expired token: sign in again
        setStep("otp");
      } else {
        setProblem(explain(e));
      }
    }
  }, [quoteId, explain]);

  useEffect(() => {
    if (!quoteId) return;
    getQuote(quoteId)
      .then(setQuote)
      .catch(() => undefined); // an accepted quote is owner-only; the order view covers it
    if (getToken()) void afterSignIn();
    else setStep("otp");
  }, [quoteId, afterSignIn]);

  const confirm = async () => {
    if (!quoteId) return;
    setBusy(true);
    setProblem(null);
    // Same key for every retry of this quote ⇒ a double tap can never create two orders.
    const key = sessionStorage.getItem(orderKeyFor(quoteId)) ?? newKey();
    sessionStorage.setItem(orderKeyFor(quoteId), key);
    try {
      const placed = await placeOrder(quoteId, key);
      sessionStorage.setItem(orderIdFor(quoteId), placed.order_id);
      setOrder(placed);
      setStep("order");
    } catch (e) {
      if (e instanceof ApiError && e.code === "CONSENT_REQUIRED") setStep("consent");
      setProblem(explain(e));
    } finally {
      setBusy(false);
    }
  };

  const pay = async () => {
    if (!order) return;
    setBusy(true);
    setProblem(null);
    try {
      const res = await payOrder(order.order_id, method, newKey());
      setOrder(res.order);
      setLastPayment(res.payment.status);
    } catch (e) {
      setProblem(explain(e));
    } finally {
      setBusy(false);
    }
  };

  const refreshOrder = async () => {
    if (!order) return;
    setBusy(true);
    try {
      setOrder(await getOrder(order.order_id));
    } catch (e) {
      setProblem(explain(e));
    } finally {
      setBusy(false);
    }
  };

  if (!quoteId) {
    return (
      <p role="alert">
        {t.quote.notFound}{" "}
        <Link href={`/${locale}/advisor`} className="underline">
          {t.quote.backToAdvisor}
        </Link>
      </p>
    );
  }

  const backToPlan = (
    <Link href={`/${locale}/quote?q=${encodeURIComponent(quoteId)}`} className="self-start text-sm underline">
      {t.checkout.backToPlan}
    </Link>
  );

  return (
    <div className="flex flex-col gap-5">
      {quote && step !== "order" && (
        <section
          data-testid="checkout-summary"
          className="flex flex-col gap-1 rounded-2xl bg-white p-4 shadow-sm ring-1 ring-neutral-200 dark:bg-neutral-900 dark:ring-neutral-700"
        >
          <h2 className="font-semibold">{t.checkout.summary}</h2>
          <ul className="text-sm text-neutral-600 dark:text-neutral-300">
            {quote.lines.map((l) => (
              <li key={l.product_id}>
                {l.product_name}
                {l.qty > 1 ? ` × ${l.qty}` : ""}
              </li>
            ))}
          </ul>
          <p className="font-semibold tabular-nums">
            {formatPaise(quote.totals.monthly_total_paise)}
            {t.quote.perMonth} · {t.quote.setup} {formatPaise(quote.totals.setup_total_paise)}
          </p>
        </section>
      )}

      {step === "loading" && <p className="text-neutral-500">{t.advisor.thinking}</p>}
      {step === "otp" && (
        <>
          <p className="text-sm font-medium">{t.checkout.signIn}</p>
          <OtpStep t={t} onVerified={() => void afterSignIn()} />
        </>
      )}
      {step === "consent" && (
        <ConsentForm
          locale={locale}
          t={t}
          current={consent}
          onSaved={(state) => {
            setConsent(state);
            if (state.ready_for_checkout) {
              setProblem(null);
              setStep("confirm");
            }
          }}
        />
      )}
      {step === "confirm" && (
        <button
          type="button"
          disabled={busy}
          onClick={() => void confirm()}
          className="h-12 rounded-xl bg-red-700 font-medium text-white disabled:opacity-50"
        >
          {busy ? t.checkout.placing : t.checkout.placeOrder}
        </button>
      )}

      {step === "order" && order && (
        <section data-testid="order" className="flex flex-col gap-3">
          <h2 className="text-lg font-semibold">{t.checkout.orderPlaced}</h2>
          <p className="text-sm">
            {t.checkout.orderId}: <span className="font-mono" data-testid="order-id">{order.order_id.slice(0, 8).toUpperCase()}</span>
          </p>
          <dl className="grid grid-cols-[1fr_auto] gap-x-3 text-sm">
            <dt>{t.checkout.dueNow}</dt>
            <dd className="text-right font-semibold tabular-nums" data-testid="due-now">
              {formatPaise(order.amount_due_now_paise)}
            </dd>
          </dl>

          {(order.next_action === "KYC" || order.next_action === "FIX_KYC") && (
            <div className="flex flex-col gap-2 rounded-xl bg-neutral-100 p-3 text-sm dark:bg-neutral-800">
              <p>{t.checkout.kycNext}</p>
              <Link
                href={`/${locale}/kyc?o=${encodeURIComponent(order.order_id)}`}
                data-testid="kyc-link"
                className="self-start font-medium underline"
              >
                {t.kyc.link}
              </Link>
              <button type="button" onClick={() => void refreshOrder()} disabled={busy} className="self-start underline">
                {t.checkout.retry}
              </button>
            </div>
          )}

          {(order.next_action === "PAY" || order.next_action === "RETRY_PAYMENT") && (
            <div className="flex flex-col gap-3">
              {lastPayment === "FAILED" && (
                <p role="alert" className="rounded-xl bg-red-50 p-3 text-sm text-red-800 dark:bg-red-950 dark:text-red-100">
                  {t.checkout.paymentFailed}
                </p>
              )}
              <fieldset className="flex flex-wrap gap-2">
                <legend className="mb-1 text-sm font-medium">{t.checkout.method}</legend>
                {METHODS.map((m) => (
                  <label key={m} className="flex items-center gap-2 rounded-lg px-3 py-2 ring-1 ring-neutral-300 dark:ring-neutral-600">
                    <input type="radio" name="method" value={m} checked={method === m} onChange={() => setMethod(m)} className="accent-red-700" />
                    {t.checkout[m]}
                  </label>
                ))}
              </fieldset>
              <button
                type="button"
                disabled={busy}
                onClick={() => void pay()}
                className="h-12 rounded-xl bg-red-700 font-medium text-white disabled:opacity-50"
              >
                {busy
                  ? t.checkout.paying
                  : lastPayment === "FAILED"
                    ? t.checkout.retry
                    : fill(t.checkout.pay, { amount: formatPaise(order.amount_due_now_paise) })}
              </button>
            </div>
          )}

          {order.state === "PAYMENT_CONFIRMED" && (
            <p role="status" data-testid="paid" className="rounded-xl bg-green-50 p-3 text-green-900 dark:bg-green-950 dark:text-green-100">
              {t.checkout.paid}
            </p>
          )}
          <Link
            href={`/${locale}/track/${encodeURIComponent(order.order_id)}`}
            data-testid="track-link"
            className="self-start font-medium underline"
          >
            {t.checkout.trackLink}
          </Link>
        </section>
      )}

      {problem && (
        <p role="alert" className="text-sm text-red-700">
          {problem}
        </p>
      )}
      {step !== "order" && backToPlan}
    </div>
  );
}
