import type { Recommendation } from "@/lib/api";
import { formatPaise } from "@/lib/format";
import type { Locale, Messages } from "@/lib/i18n";

/** Every amount here comes from the pricing engine's integer paise — never from LLM text. */
export function RecommendationCard({
  recommendation,
  t,
}: {
  recommendation: Recommendation;
  locale: Locale;
  t: Messages;
}) {
  const { pricing, blocked, explanation } = recommendation;
  const extras = (recommendation.addons ?? []).filter((a) => a.product_name);
  return (
    <article
      data-testid="recommendation"
      className="flex flex-col gap-4 rounded-2xl bg-white p-4 shadow-sm ring-1 ring-neutral-200 dark:bg-neutral-900 dark:ring-neutral-700"
    >
      <h2 className="text-lg font-semibold">{t.quote.title}</h2>

      <ul className="flex flex-col divide-y divide-neutral-100 dark:divide-neutral-800">
        {pricing.lines.map((line) => (
          <li key={line.product_id} className="flex items-baseline justify-between gap-3 py-2">
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

      <dl className="grid grid-cols-[1fr_auto] gap-x-3 gap-y-1">
        <dt className="font-semibold">{t.quote.monthlyTotal}</dt>
        <dd className="text-right text-xl font-semibold tabular-nums" data-testid="monthly-total">
          {formatPaise(pricing.totals.monthly_total_paise)}
          <span className="text-sm font-normal text-neutral-500">{t.quote.perMonth}</span>
        </dd>
        <dt className="text-sm text-neutral-500" />
        <dd className="text-right text-sm text-neutral-500">{t.quote.gstIncluded}</dd>
        <dt>{t.quote.setup}</dt>
        <dd className="text-right tabular-nums" data-testid="setup-total">
          {formatPaise(pricing.totals.setup_total_paise)}
        </dd>
        <dt>{t.quote.contract}</dt>
        <dd className="text-right">
          {pricing.contract_months} {t.quote.months}
        </dd>
      </dl>

      {extras.length > 0 && (
        <section>
          <h3 className="text-sm font-semibold">{t.quote.suggestedExtras}</h3>
          <ul className="mt-1 flex flex-col gap-1 text-sm text-neutral-700 dark:text-neutral-300" data-testid="suggested-extras">
            {extras.map((a) => (
              <li key={a.product_id}>+ {a.product_name}</li>
            ))}
          </ul>
        </section>
      )}

      {blocked.length > 0 && (
        <section>
          <h3 className="text-sm font-semibold text-neutral-600 dark:text-neutral-300">{t.quote.notIncluded}</h3>
          <ul className="mt-1 flex flex-col gap-1 text-sm text-neutral-600 dark:text-neutral-300" data-testid="blocked">
            {blocked.map((b) => (
              <li key={b.product_id}>
                {b.product_name} — {b.reason_text}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section>
        <h3 className="text-sm font-semibold">{t.quote.why}</h3>
        <p className="mt-1 text-sm leading-relaxed" data-testid="explanation">
          {explanation.text}
        </p>
        <p className="mt-1 text-xs text-neutral-500">
          {explanation.source === "llm" ? t.quote.aiNote : t.quote.templateNote}
        </p>
      </section>
    </article>
  );
}
