"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { ApiError, type SharedQuote, getSharedQuote } from "@/lib/api";
import { formatPaise } from "@/lib/format";
import { type Locale, type Messages, bcp47, fill } from "@/lib/i18n";

type Problem = "notFound" | "EXPIRED" | "CHANGED" | "error";

/**
 * Read-only view of a shared quote. Nothing here can change the owner's plan: the
 * server returns products and prices only, never the quote or session id.
 */
export function SharedQuoteView({ locale, t }: { locale: Locale; t: Messages }) {
  const token = useSearchParams().get("t");
  const [quote, setQuote] = useState<SharedQuote | null>(null);
  const [problem, setProblem] = useState<Problem | null>(token ? null : "notFound");

  useEffect(() => {
    if (!token) return;
    getSharedQuote(token)
      .then(setQuote)
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 410) {
          setProblem(e.detail?.reason === "EXPIRED" ? "EXPIRED" : "CHANGED");
        } else if (e instanceof ApiError && (e.status === 404 || e.status === 422)) {
          setProblem("notFound");
        } else {
          setProblem("error");
        }
      });
  }, [token]);

  const ownPlan = (
    <Link href={`/${locale}`} className="text-red-800 underline dark:text-red-200">
      {t.shared.ownPlan}
    </Link>
  );

  if (problem) {
    const text =
      problem === "EXPIRED" || problem === "CHANGED"
        ? `${t.shared.ended} ${problem === "EXPIRED" ? t.shared.expired : t.shared.changed} ${t.shared.askNew}`
        : problem === "notFound"
          ? t.shared.notFound
          : t.advisor.error;
    return (
      <div className="flex flex-col gap-3">
        <p role="alert" data-testid="shared-problem" className="rounded-xl bg-amber-50 p-3 text-amber-950 dark:bg-amber-950 dark:text-amber-50">
          {text}
        </p>
        {ownPlan}
      </div>
    );
  }
  if (!quote) return <p className="text-neutral-500">{t.advisor.thinking}</p>;

  const date = new Intl.DateTimeFormat(bcp47(locale), { dateStyle: "medium" }).format(new Date(quote.valid_until));
  const { totals } = quote;
  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-neutral-600 dark:text-neutral-300">{fill(t.shared.readOnly, { date })}</p>
      <article
        data-testid="shared-quote"
        className="flex flex-col gap-3 rounded-2xl bg-white p-4 shadow-sm ring-1 ring-neutral-200 dark:bg-neutral-900 dark:ring-neutral-700"
      >
        <ul className="flex flex-col divide-y divide-neutral-100 dark:divide-neutral-800">
          {quote.lines.map((line) => (
            <li key={line.product_name} className="flex items-baseline justify-between gap-3 py-2">
              <span>
                {line.product_name}
                {line.qty > 1 && (
                  <span className="text-sm text-neutral-500">
                    {" "}
                    · {t.quote.qty} {line.qty}
                  </span>
                )}
              </span>
              <span className="whitespace-nowrap tabular-nums">{formatPaise(line.line_total_paise)}</span>
            </li>
          ))}
        </ul>
        <dl className="grid grid-cols-[1fr_auto] gap-x-3 gap-y-1 text-sm">
          <dt>{t.quote.subtotal}</dt>
          <dd className="text-right tabular-nums">{formatPaise(totals.monthly_subtotal_paise)}</dd>
          <dt>{fill(t.quote.gst, { rate: (quote.gst_rate_bps / 100).toString() })}</dt>
          <dd className="text-right tabular-nums">{formatPaise(totals.monthly_gst_paise)}</dd>
          <dt className="text-base font-semibold">{t.quote.monthlyTotal}</dt>
          <dd className="text-right text-xl font-semibold tabular-nums" data-testid="shared-monthly-total">
            {formatPaise(totals.monthly_total_paise)}
            <span className="text-sm font-normal text-neutral-500">{t.quote.perMonth}</span>
          </dd>
          <dt>
            {t.quote.payToday}
            <span className="block text-xs text-neutral-500">
              {fill(t.quote.payTodayNote, {
                setup: formatPaise(totals.setup_total_paise),
                month: formatPaise(totals.monthly_total_paise),
              })}
            </span>
          </dt>
          <dd className="text-right tabular-nums" data-testid="shared-pay-today">
            {formatPaise(quote.due_today_paise)}
          </dd>
          <dt>{t.quote.contract}</dt>
          <dd className="text-right">
            {quote.contract_months} {t.quote.months}
          </dd>
        </dl>
        <p className="text-xs text-neutral-600 dark:text-neutral-300">
          {fill(t.quote.gstPlain, {
            rate: (quote.gst_rate_bps / 100).toString(),
            amount: formatPaise(totals.monthly_gst_paise),
          })}
        </p>
      </article>
      {ownPlan}
    </div>
  );
}
