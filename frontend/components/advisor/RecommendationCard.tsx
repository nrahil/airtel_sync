import type { Recommendation } from "@/lib/api";
import { formatPaise } from "@/lib/format";
import { type Locale, type Messages, fill } from "@/lib/i18n";

/** What an edit changed, against the plan shown before it. */
export interface PlanChange {
  added: string[];
  removed: string[];
  qty: { name: string; from: number; to: number }[];
  total: { from: number; to: number } | null;
  /** Product IDs to highlight on the card. */
  ids: string[];
}

export function diffPlans(before: Recommendation, after: Recommendation): PlanChange | null {
  const old = new Map(before.pricing.lines.map((l) => [l.product_id, l]));
  const now = new Map(after.pricing.lines.map((l) => [l.product_id, l]));
  const change: PlanChange = { added: [], removed: [], qty: [], total: null, ids: [] };
  for (const line of after.pricing.lines) {
    const was = old.get(line.product_id);
    if (!was) {
      change.added.push(line.product_name);
      change.ids.push(line.product_id);
    } else if (was.qty !== line.qty) {
      change.qty.push({ name: line.product_name, from: was.qty, to: line.qty });
      change.ids.push(line.product_id);
    }
  }
  for (const line of before.pricing.lines) {
    if (!now.has(line.product_id)) change.removed.push(line.product_name);
  }
  const from = before.pricing.totals.monthly_total_paise;
  const to = after.pricing.totals.monthly_total_paise;
  if (from !== to) change.total = { from, to };
  const empty = !change.added.length && !change.removed.length && !change.qty.length && !change.total;
  return empty ? null : change;
}

/** Every amount here comes from the pricing engine's integer paise — never from LLM text. */
export function RecommendationCard({
  recommendation,
  t,
  changed,
}: {
  recommendation: Recommendation;
  locale: Locale;
  t: Messages;
  changed?: PlanChange | null;
}) {
  const { pricing, blocked, explanation } = recommendation;
  const why = recommendation.why ?? {};
  const extras = (recommendation.addons ?? []).filter((a) => a.product_name);
  const highlight = new Set(changed?.ids ?? []);
  return (
    <article
      data-testid="recommendation"
      className="flex flex-col gap-4 rounded-2xl bg-white p-4 shadow-sm ring-1 ring-neutral-200 dark:bg-neutral-900 dark:ring-neutral-700"
    >
      <h2 className="text-lg font-semibold">{t.quote.title}</h2>

      {changed && <ChangeSummary change={changed} t={t} />}

      <ul className="flex flex-col divide-y divide-neutral-100 dark:divide-neutral-800">
        {pricing.lines.map((line) => (
          <li
            key={line.product_id}
            data-testid={`line-${line.product_id}`}
            data-changed={highlight.has(line.product_id) ? "true" : undefined}
            className={`flex flex-col gap-0.5 py-2 ${highlight.has(line.product_id) ? "-mx-2 rounded-lg bg-amber-50 px-2 dark:bg-amber-950" : ""}`}
          >
            <div className="flex items-baseline justify-between gap-3">
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
            </div>
            {why[line.product_id] && (
              <p className="text-xs text-neutral-600 dark:text-neutral-400" data-testid={`why-${line.product_id}`}>
                {why[line.product_id]}
              </p>
            )}
          </li>
        ))}
      </ul>

      <dl className="grid grid-cols-[1fr_auto] gap-x-3 gap-y-1">
        <dt className="font-semibold">{t.quote.monthlyTotal}</dt>
        <dd
          className={`text-right text-xl font-semibold tabular-nums ${changed?.total ? "rounded bg-amber-50 px-1 dark:bg-amber-950" : ""}`}
          data-testid="monthly-total"
        >
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
              <li key={a.product_id}>
                + {a.product_name}
                {why[a.product_id] && (
                  <span className="block ps-3 text-xs text-neutral-500" data-testid={`why-${a.product_id}`}>
                    {why[a.product_id]}
                  </span>
                )}
              </li>
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

function ChangeSummary({ change, t }: { change: PlanChange; t: Messages }) {
  const a = t.advisor;
  return (
    <section
      data-testid="plan-changes"
      className="rounded-xl bg-amber-50 p-3 text-sm ring-1 ring-amber-200 dark:bg-amber-950 dark:ring-amber-800"
    >
      <h3 className="font-semibold">{a.changedTitle}</h3>
      <ul className="mt-1 flex flex-col gap-0.5">
        {change.added.map((name) => (
          <li key={`+${name}`}>{fill(a.changedAdded, { name })}</li>
        ))}
        {change.removed.map((name) => (
          <li key={`-${name}`}>{fill(a.changedRemoved, { name })}</li>
        ))}
        {change.qty.map((q) => (
          <li key={`q${q.name}`}>{fill(a.changedQty, { name: q.name, from: q.from, to: q.to })}</li>
        ))}
        {change.total && (
          <li className="font-medium">
            {fill(a.changedTotal, { from: formatPaise(change.total.from), to: formatPaise(change.total.to) })}
          </li>
        )}
      </ul>
    </section>
  );
}
