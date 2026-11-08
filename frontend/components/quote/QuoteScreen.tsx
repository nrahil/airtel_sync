"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { JourneySteps } from "@/components/JourneySteps";
import {
  ApiError,
  type Fix,
  type Ineligible,
  type ItemRef,
  type PlanChange,
  type PlanChoice,
  type ProductOption,
  type QuoteView,
  changeItems,
  createQuote,
  getQuote,
  shareQuote,
} from "@/lib/api";
import { formatPaise, rupeeDifference } from "@/lib/format";
import { type Locale, type Messages, bcp47, fill } from "@/lib/i18n";

/**
 * The quote screen. Every number comes from the server's signed quote (integer paise);
 * the client only says which items it wants. A toggle the engines reject comes back as
 * a cascade with one-tap fixes, and the current quote stays as it was.
 */
export function QuoteScreen({ locale, t }: { locale: Locale; t: Messages }) {
  const router = useRouter();
  const params = useSearchParams();
  const quoteId = params.get("q");
  const [quote, setQuote] = useState<QuoteView | null>(null);
  const [cascade, setCascade] = useState<Ineligible | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<"notFound" | "error" | null>(null);
  // After "Get today's price": the monthly total before the refresh, to say what moved.
  const [refreshedFrom, setRefreshedFrom] = useState<number | null>(null);

  // Id of the quote on screen; a URL change to it (after a toggle) needs no refetch.
  const shownId = useRef<string | null>(null);

  useEffect(() => {
    if (!quoteId) {
      setFailed("notFound");
      return;
    }
    if (shownId.current === quoteId) return;
    getQuote(quoteId)
      .then((q) => {
        shownId.current = q.quote_id;
        setQuote(q);
      })
      .catch((e: unknown) => setFailed(e instanceof ApiError && e.status === 404 ? "notFound" : "error"));
  }, [quoteId]);

  const show = useCallback(
    (next: QuoteView) => {
      shownId.current = next.quote_id;
      setQuote(next);
      setCascade(null);
      router.replace(`/${locale}/quote?q=${encodeURIComponent(next.quote_id)}`);
    },
    [locale, router],
  );

  const send = useCallback(
    async (items: ItemRef[], contract: number | null) => {
      if (!quote || busy) return;
      setBusy(true);
      setFailed(null);
      setRefreshedFrom(null);
      try {
        show(await changeItems(quote.quote_id, items, contract));
      } catch (e) {
        if (e instanceof ApiError && e.code === "BUNDLE_INELIGIBLE") {
          setCascade(e.detail as unknown as Ineligible);
        } else {
          setFailed("error");
        }
      } finally {
        setBusy(false);
      }
    },
    [quote, busy, show],
  );

  if (failed === "notFound") {
    return (
      <p role="alert" className="text-red-700">
        {t.quote.notFound}{" "}
        <Link href={`/${locale}/advisor`} className="underline">
          {t.quote.backToAdvisor}
        </Link>
      </p>
    );
  }
  if (!quote) {
    return failed ? <p role="alert">{t.advisor.error}</p> : <p className="text-neutral-500">{t.advisor.thinking}</p>;
  }

  const items: ItemRef[] = quote.lines.map((l) => ({ product_id: l.product_id, qty: l.qty }));
  const chosenContract = quote.contract_months > quote.required_contract_months ? quote.contract_months : null;
  const expired = quote.expired ?? Date.now() >= Date.parse(quote.valid_until);
  const editable = quote.status === "ISSUED";

  const toggle = (option: ProductOption) => {
    const next = option.in_bundle
      ? items.filter((i) => i.product_id !== option.product_id)
      : [...items, { product_id: option.product_id, qty: option.qty }];
    if (next.length > 0) void send(next, chosenContract);
  };

  const refresh = async () => {
    setBusy(true);
    try {
      const before = quote.totals.monthly_total_paise;
      show(await createQuote(quote.session_id, items, chosenContract));
      setRefreshedFrom(before);
    } catch {
      setFailed("error");
    } finally {
      setBusy(false);
    }
  };

  const validUntil = new Intl.DateTimeFormat(bcp47(locale), { dateStyle: "medium" }).format(
    new Date(quote.valid_until),
  );
  const gstRate = (quote.gst_rate_bps / 100).toString();
  const { totals } = quote;
  const inPlan = quote.options.filter((o) => o.in_bundle);
  const others = quote.options.filter((o) => !o.in_bundle);

  const continueButton = (
    <button
      type="button"
      disabled={busy || expired || !editable || cascade !== null}
      onClick={() => router.push(`/${locale}/checkout?q=${encodeURIComponent(quote.quote_id)}`)}
      className="min-h-12 w-full rounded-xl bg-red-700 px-4 py-2 font-medium text-white disabled:opacity-50"
    >
      {t.quote.continue} · {formatPaise(totals.monthly_total_paise)}
      {t.quote.perMonth}
    </button>
  );

  return (
    <div className="flex flex-col gap-5">
      <JourneySteps t={t} current={1} />
      {editable && (
        <Link
          href={`/${locale}/advisor?s=${encodeURIComponent(quote.session_id)}`}
          data-testid="change-answers"
          className="self-start text-sm text-red-800 underline dark:text-red-200"
        >
          {t.quote.changeAnswers}
        </Link>
      )}
      <div className="grid grid-cols-1 gap-5 md:grid-cols-[minmax(0,1fr)_20rem] md:grid-rows-[auto_1fr] md:gap-x-6 md:gap-y-0 lg:grid-cols-[minmax(0,1fr)_26rem] lg:gap-x-8">
        <div className="flex flex-col gap-5 empty:hidden md:col-start-1 md:mb-5">
          {refreshedFrom !== null && (
            <p role="status" data-testid="refreshed" className="rounded-xl bg-green-50 p-3 text-green-900 dark:bg-green-950 dark:text-green-100">
              {rupeeDifference(refreshedFrom, totals.monthly_total_paise) === 0
                ? fill(t.quote.refreshedSame, { amount: formatPaise(totals.monthly_total_paise) })
                : fill(t.quote.refreshedChanged, { old: formatPaise(refreshedFrom), new: formatPaise(totals.monthly_total_paise) })}
            </p>
          )}
          {editable && !expired && quote.plans && quote.plans.length > 0 && (
            <PlanChooser plans={quote.plans} t={t} busy={busy} onPick={(plan) => void send(plan.items, chosenContract)} />
          )}
        </div>
        {/* Side column from tablet up: the price stays beside the add-ons being toggled. */}
        <div className="flex flex-col gap-3 md:col-start-2 md:row-span-2 md:row-start-1 md:self-start md:[@media(min-height:56rem)]:sticky md:[@media(min-height:56rem)]:top-6">
          <article
            data-testid="quote"
            className="flex flex-col gap-3 rounded-2xl bg-white p-4 shadow-sm ring-1 ring-neutral-200 dark:bg-neutral-900 dark:ring-neutral-700"
          >
            <ul className="flex flex-col divide-y divide-neutral-100 dark:divide-neutral-800" data-testid="quote-lines">
              {quote.lines.map((line) => (
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
            <dl className="grid grid-cols-[1fr_auto] gap-x-3 gap-y-1 text-sm">
              <dt>{t.quote.subtotal}</dt>
              <dd className="text-right tabular-nums">{formatPaise(totals.monthly_subtotal_paise)}</dd>
              <dt>{fill(t.quote.gst, { rate: gstRate })}</dt>
              <dd className="text-right tabular-nums" data-testid="gst">
                {formatPaise(totals.monthly_gst_paise)}
              </dd>
              <dt className="text-base font-semibold">{t.quote.monthlyTotal}</dt>
              <dd className="text-right text-xl font-semibold tabular-nums" data-testid="monthly-total">
                {formatPaise(totals.monthly_total_paise)}
                <span className="text-sm font-normal text-neutral-500">{t.quote.perMonth}</span>
              </dd>
              <dt>
                {t.quote.setup} <span className="text-neutral-500">({t.quote.gstIncluded})</span>
              </dt>
              <dd className="text-right tabular-nums" data-testid="setup-total">
                {formatPaise(totals.setup_total_paise)}
              </dd>
            </dl>
            <ContractChoice quote={quote} t={t} busy={busy} editable={editable && !expired} onPick={(m) => void send(items, m)} />
            {totals.savings_paise > 0 && (
              <p className="text-sm text-green-700 dark:text-green-400" data-testid="bundle-discount">
                {fill(t.quote.savings, { amount: formatPaise(totals.savings_paise) })}
              </p>
            )}
            <PayBox quote={quote} t={t} />
            <p className="text-xs text-neutral-500">{fill(t.quote.validUntil, { date: validUntil })}</p>
          </article>
          <div className="hidden md:block">{continueButton}</div>
        </div>
        <div className="flex flex-col gap-5 empty:hidden md:col-start-1 md:row-start-2">
          {busy && <p className="text-sm text-neutral-500">{t.quote.updating}</p>}
          {failed === "error" && (
            <p role="alert" className="text-sm text-red-700">
              {t.advisor.error}
            </p>
          )}

          {expired && (
            <div role="alert" className="flex flex-col gap-2 rounded-xl bg-amber-50 p-3 text-amber-900 dark:bg-amber-950 dark:text-amber-100">
              <p>{t.quote.expired}</p>
              <button type="button" onClick={() => void refresh()} disabled={busy} className="self-start rounded-lg bg-amber-700 px-3 py-2 text-white">
                {t.quote.refresh}
              </button>
            </div>
          )}

          {cascade && <Cascade cascade={cascade} t={t} busy={busy} onFix={(fix) => void send(fix.items, chosenContract)} onKeep={() => setCascade(null)} />}

          {editable && (
            <section className="flex flex-col gap-2">
              <h2 className="text-lg font-semibold">{t.quote.addons}</h2>
              <OptionList title={t.quote.inPlan} options={inPlan} totals={totals} t={t} busy={busy} onToggle={toggle} onTry={() => {}} canRemove={inPlan.length > 1} />
              <OptionList
                options={others}
                totals={totals}
                t={t}
                busy={busy}
                onToggle={toggle}
                onTry={(alt, qty) => void send([...items.filter((i) => i.product_id !== alt), { product_id: alt, qty }], chosenContract)}
                canRemove
              />
            </section>
          )}

          {editable && !expired && <SharePanel quote={quote} locale={locale} t={t} />}
        </div>
      </div>

      {/* Phones keep the action pinned to the thumb; wider layouts have it under the price. */}
      <div className="sticky bottom-0 -mx-4 flex flex-col gap-1 bg-neutral-50/95 px-4 py-3 sm:-mx-6 sm:px-6 md:hidden dark:bg-neutral-950/95">
        {continueButton}
      </div>
    </div>
  );
}

function fixLabel(fix: Fix, t: Messages): string {
  switch (fix.action) {
    case "ADD":
      return fill(t.quote.fixAdd, { product: fix.product_name ?? "" });
    case "REPLACE":
      return fill(t.quote.fixReplace, { product: fix.product_name ?? "" });
    case "REMOVE":
      return fill(t.quote.fixRemove, { product: fix.product_name ?? "" });
    case "SET_QTY":
      return fill(t.quote.fixSetQty, { product: fix.product_name ?? "", qty: fix.qty ?? 1 });
    case "REMOVE_BLOCKED":
      return fill(t.quote.fixRemoveBlocked, { products: (fix.products ?? []).join(", ") });
  }
}

function Cascade(props: {
  cascade: Ineligible;
  t: Messages;
  busy: boolean;
  onFix: (fix: Fix) => void;
  onKeep: () => void;
}) {
  const { cascade, t, busy } = props;
  return (
    <section
      role="alert"
      data-testid="cascade"
      className="flex flex-col gap-2 rounded-xl bg-amber-50 p-3 text-amber-950 ring-1 ring-amber-200 dark:bg-amber-950 dark:text-amber-50 dark:ring-amber-800"
    >
      <h2 className="font-semibold">{t.quote.cascadeTitle}</h2>
      <ul className="flex flex-col gap-1 text-sm">
        {cascade.blocked.map((b) => (
          <li key={b.product_id}>
            <span className="font-medium">{b.product_name}</span> — {b.reason_text}
          </li>
        ))}
      </ul>
      <div className="flex flex-wrap gap-2">
        {cascade.fixes.map((fix, i) => (
          <button
            key={`${fix.action}-${fix.product_id ?? i}`}
            type="button"
            disabled={busy}
            onClick={() => props.onFix(fix)}
            className={
              i === 0
                ? "rounded-lg bg-red-700 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
                : "rounded-lg px-3 py-2 text-sm font-medium ring-1 ring-red-700 text-red-800 disabled:opacity-50 dark:text-red-200"
            }
          >
            {fixLabel(fix, t)}
          </button>
        ))}
        <button type="button" onClick={props.onKeep} className="px-2 py-2 text-sm underline">
          {t.quote.keep}
        </button>
      </div>
    </section>
  );
}

function OptionList(props: {
  title?: string;
  options: ProductOption[];
  totals: QuoteView["totals"];
  t: Messages;
  busy: boolean;
  canRemove: boolean;
  onToggle: (o: ProductOption) => void;
  onTry: (alternative: string, qty: number) => void;
}) {
  const { options, t, busy } = props;
  if (options.length === 0) return null;
  return (
    <div className="flex flex-col gap-1">
      {props.title && <h3 className="text-sm font-semibold text-neutral-600 dark:text-neutral-300">{props.title}</h3>}
      <ul className="flex flex-col divide-y divide-neutral-100 rounded-xl bg-white ring-1 ring-neutral-200 dark:divide-neutral-800 dark:bg-neutral-900 dark:ring-neutral-700">
        {options.map((o) => {
          const delta = o.eligible ? deltaLabel(o, props.totals, t) : null;
          return (
          <li key={o.product_id} data-testid={`option-${o.product_id}`} className="flex flex-wrap items-start justify-between gap-3 p-3">
            <div className="flex min-w-0 flex-1 basis-48 flex-col">
              <span className="font-medium">
                {o.product_name}
                {o.recommended && !o.in_bundle && (
                  <span className="ml-2 rounded bg-green-100 px-1.5 text-xs text-green-800 dark:bg-green-900 dark:text-green-100">
                    {t.quote.suggested}
                  </span>
                )}
              </span>
              <span className="text-xs text-neutral-500">{o.description}</span>
              <span className="text-xs tabular-nums text-neutral-600 dark:text-neutral-300">
                {fill(t.quote.eachMonth, { price: formatPaise(o.list_unit_paise) })}
              </span>
              {!o.eligible && (
                <span className="text-sm text-neutral-700 dark:text-neutral-200" data-testid="unavailable-reason">
                  {t.quote.unavailable} — {o.reason_text}
                </span>
              )}
              {o.eligible && !o.in_bundle && !o.requires_met && (
                <span className="text-xs text-amber-800 dark:text-amber-200">{t.quote.needsBase}</span>
              )}
              {delta && (
                <span
                  data-testid="delta"
                  className={`text-sm font-medium tabular-nums ${
                    o.in_bundle ? "text-green-700 dark:text-green-400" : "text-neutral-800 dark:text-neutral-100"
                  }`}
                >
                  {o.in_bundle ? t.quote.remove : t.quote.add}: {delta}
                </span>
              )}
            </div>
            {o.eligible ? (
              <button
                type="button"
                disabled={busy || (o.in_bundle && !props.canRemove)}
                aria-pressed={o.in_bundle}
                aria-label={`${o.in_bundle ? t.quote.remove : t.quote.add} ${o.product_name}${delta ? ` (${delta})` : ""}`}
                onClick={() => props.onToggle(o)}
                className={
                  o.in_bundle
                    ? "shrink-0 rounded-lg px-3 py-2 text-sm ring-1 ring-neutral-300 disabled:opacity-50 dark:ring-neutral-600"
                    : "shrink-0 rounded-lg bg-red-700 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
                }
              >
                {o.in_bundle ? t.quote.remove : t.quote.add}
              </button>
            ) : (
              o.alternative &&
              o.alternative_name && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => props.onTry(o.alternative as string, o.qty)}
                  className="max-w-full rounded-lg px-3 py-2 text-sm font-medium text-red-800 ring-1 ring-red-700 disabled:opacity-50 dark:text-red-200"
                >
                  {fill(t.quote.fixReplace, { product: o.alternative_name })}
                </button>
              )
            )}
          </li>
          );
        })}
      </ul>
    </div>
  );
}

/** "+₹353/month" / "−₹501/month · −₹1,180 one-time", from the engine's delta, as rounded on screen. */
function deltaLabel(
  option: ProductOption,
  current: QuoteView["totals"],
  t: Messages,
): string | null {
  if (!option.delta) return null;
  const monthly = rupeeDifference(
    current.monthly_total_paise,
    current.monthly_total_paise + option.delta.monthly_total_paise,
  );
  const setup = rupeeDifference(
    current.setup_total_paise,
    current.setup_total_paise + option.delta.setup_total_paise,
  );
  const amount = (r: number) => formatPaise(Math.abs(r) * 100);
  const parts = [
    monthly === 0
      ? t.quote.deltaNone
      : fill(monthly > 0 ? t.quote.deltaMore : t.quote.deltaLess, {
          amount: amount(monthly),
        }),
  ];
  if (setup !== 0)
    parts.push(
      fill(setup > 0 ? t.quote.setupMore : t.quote.setupLess, {
        amount: amount(setup),
      }),
    );
  return parts.join(" · ");
}

function changeText(change: PlanChange, t: Messages): string {
  switch (change.kind) {
    case "swapped":
      return fill(t.quote.changeSwapped, { from: change.from, to: change.to });
    case "removed":
      return fill(t.quote.changeRemoved, { product: change.product });
    case "added":
      return fill(t.quote.changeAdded, { product: change.product });
    case "fewer":
      return fill(t.quote.changeFewer, {
        product: change.product,
        from: change.from,
        to: change.to,
      });
  }
}

/** Starter vs Recommended (micro owners): both engine-priced, with what the starter gives up. */
function PlanChooser(props: {
  plans: PlanChoice[];
  t: Messages;
  busy: boolean;
  onPick: (plan: PlanChoice) => void;
}) {
  const { plans, t, busy } = props;
  const label = (key: PlanChoice["key"]) =>
    key === "starter" ? t.quote.planStarter : t.quote.planRecommended;
  return (
    <section data-testid="plans" className="flex flex-col gap-2">
      <h2 className="text-lg font-semibold">{t.quote.plansTitle}</h2>
      <div className="grid grid-cols-2 gap-2">
        {plans.map((plan) => (
          <div
            key={plan.key}
            data-testid={`plan-${plan.key}`}
            className={`flex flex-col gap-1 rounded-xl p-3 ${
              plan.current
                ? "bg-red-50 ring-2 ring-red-700 dark:bg-red-950"
                : "bg-white ring-1 ring-neutral-200 dark:bg-neutral-900 dark:ring-neutral-700"
            }`}
          >
            <span className="font-semibold">{label(plan.key)}</span>
            <span
              className="text-lg font-semibold tabular-nums"
              data-testid={`plan-${plan.key}-price`}
            >
              {formatPaise(plan.monthly_total_paise)}
              <span className="text-sm font-normal text-neutral-500">
                {t.quote.perMonth}
              </span>
            </span>
            <ul className="text-xs text-neutral-600 dark:text-neutral-300">
              {plan.products.map((p) => (
                <li key={p.product_name}>
                  {p.product_name}
                  {p.qty > 1 ? ` × ${p.qty}` : ""}
                </li>
              ))}
            </ul>
            {plan.current ? (
              <span className="mt-auto pt-1 text-sm font-medium text-red-800 dark:text-red-200">
                ✓ {t.quote.planCurrent}
              </span>
            ) : (
              <button
                type="button"
                disabled={busy}
                onClick={() => props.onPick(plan)}
                className="mt-auto rounded-lg bg-red-700 px-2 py-2 text-sm font-medium text-white disabled:opacity-50"
              >
                {fill(t.quote.planSwitch, { plan: label(plan.key) })}
              </button>
            )}
          </div>
        ))}
      </div>
      {plans
        .filter((p) => p.changes.length > 0)
        .map((plan) => (
          <div
            key={plan.key}
            data-testid="plan-changes"
            className="rounded-xl bg-neutral-100 p-3 text-sm dark:bg-neutral-800"
          >
            <p className="font-medium">
              {label(plan.key)} — {t.quote.planChanges}
            </p>
            <ul className="list-disc pl-5">
              {plan.changes.map((c) => (
                <li key={changeText(c, t)}>{changeText(c, t)}</li>
              ))}
            </ul>
          </div>
        ))}
    </section>
  );
}

/** 12 vs 24 months as two buttons, each with its monthly price; a longer one says the saving in rupees. */
function ContractChoice(props: {
  quote: QuoteView;
  t: Messages;
  busy: boolean;
  editable: boolean;
  onPick: (months: number) => void;
}) {
  const { quote, t, busy } = props;
  const prices = quote.contract_prices ?? [];
  if (!props.editable || prices.length < 2) {
    return (
      <p className="text-sm" data-testid="contract">
        {t.quote.contract}: {quote.contract_months} {t.quote.months}
      </p>
    );
  }
  const shortest = prices[0];
  return (
    <fieldset className="flex flex-col gap-1" data-testid="contract">
      <legend className="mb-1 text-sm">{t.quote.contractChoice}</legend>
      <div className="grid grid-cols-2 gap-2">
        {prices.map((p) => {
          const saves = -rupeeDifference(
            shortest.monthly_total_paise,
            p.monthly_total_paise,
          );
          const chosen = p.months === quote.contract_months;
          return (
            <button
              key={p.months}
              type="button"
              aria-pressed={chosen}
              disabled={busy}
              onClick={() => {
                if (!chosen) props.onPick(p.months);
              }}
              className={`flex flex-col items-start rounded-lg px-3 py-2 text-left text-sm disabled:opacity-50 ${
                chosen
                  ? "bg-red-50 ring-2 ring-red-700 dark:bg-red-950"
                  : "ring-1 ring-neutral-300 dark:ring-neutral-600"
              }`}
            >
              <span className="font-medium">
                {p.months} {t.quote.months}
              </span>
              <span className="tabular-nums">
                {formatPaise(p.monthly_total_paise)}
                {t.quote.perMonth}
              </span>
              {saves > 0 && (
                <span
                  className="text-green-700 dark:text-green-400"
                  data-testid={`contract-saves-${p.months}`}
                >
                  {fill(t.quote.contractSaves, {
                    amount: formatPaise(saves * 100),
                  })}
                </span>
              )}
            </button>
          );
        })}
      </div>
    </fieldset>
  );
}

/** What leaves the owner's account today, what from next month, and GST in plain words. */
function PayBox({ quote, t }: { quote: QuoteView; t: Messages }) {
  const { totals } = quote;
  const today =
    quote.due_today_paise ??
    totals.setup_total_paise + totals.monthly_total_paise;
  return (
    <section
      data-testid="pay-box"
      className="flex flex-col gap-2 rounded-xl bg-neutral-100 p-3 text-sm dark:bg-neutral-800"
    >
      <h3 className="font-semibold">{t.quote.payTitle}</h3>
      <div className="grid grid-cols-[1fr_auto] gap-x-3 gap-y-2">
        <span>
          {t.quote.payToday}
          <span className="block text-xs text-neutral-500">
            {fill(t.quote.payTodayNote, {
              setup: formatPaise(totals.setup_total_paise),
              month: formatPaise(totals.monthly_total_paise),
            })}
          </span>
        </span>
        <span
          className="text-right text-base font-semibold tabular-nums"
          data-testid="pay-today"
        >
          {formatPaise(today)}
        </span>
        <span>
          {t.quote.payLater}
          <span className="block text-xs text-neutral-500">
            {fill(t.quote.payLaterNote, { months: quote.contract_months })}
          </span>
        </span>
        <span
          className="text-right text-base font-semibold tabular-nums"
          data-testid="pay-monthly"
        >
          {formatPaise(totals.monthly_total_paise)}
        </span>
      </div>
      <p
        className="text-xs text-neutral-600 dark:text-neutral-300"
        data-testid="gst-plain"
      >
        {fill(t.quote.gstPlain, {
          rate: (quote.gst_rate_bps / 100).toString(),
          amount: formatPaise(totals.monthly_gst_paise),
        })}
      </p>
    </section>
  );
}

/** "Send this to my partner/accountant": a read-only link that ends with the quote. */
function SharePanel({
  quote,
  locale,
  t,
}: {
  quote: QuoteView;
  locale: Locale;
  t: Messages;
}) {
  const [link, setLink] = useState<{
    url: string;
    validUntil: string;
    quoteId: string;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState(false);
  const [copied, setCopied] = useState(false);
  // A toggle issues a new quote and ends the old link, so never offer a stale one.
  const current = link && link.quoteId === quote.quote_id ? link : null;

  const make = async () => {
    setBusy(true);
    setProblem(false);
    setCopied(false);
    try {
      const { token, valid_until } = await shareQuote(quote.quote_id);
      const url = `${window.location.origin}/${locale}/quote/shared?t=${encodeURIComponent(token)}`;
      setLink({ url, validUntil: valid_until, quoteId: quote.quote_id });
    } catch {
      setProblem(true);
    } finally {
      setBusy(false);
    }
  };

  const copy = async () => {
    if (!current) return;
    try {
      await navigator.clipboard.writeText(current.url);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  if (!current) {
    return (
      <div className="flex flex-col gap-1">
        <button
          type="button"
          disabled={busy}
          onClick={() => void make()}
          className="min-h-11 rounded-xl py-2 px-3 text-sm font-medium text-red-800 ring-1 ring-red-700 disabled:opacity-50 dark:text-red-200"
        >
          {t.quote.share}
        </button>
        {problem && (
          <p role="alert" className="text-sm text-red-700">
            {t.advisor.error}
          </p>
        )}
      </div>
    );
  }
  const date = new Intl.DateTimeFormat(bcp47(locale), {
    dateStyle: "medium",
  }).format(new Date(current.validUntil));
  const message = fill(t.quote.shareMessage, {
    amount: formatPaise(quote.totals.monthly_total_paise),
    url: current.url,
  });
  return (
    <section
      data-testid="share"
      className="flex flex-col gap-2 rounded-xl bg-white p-3 ring-1 ring-neutral-200 dark:bg-neutral-900 dark:ring-neutral-700"
    >
      <p className="text-sm">{fill(t.quote.shareNote, { date })}</p>
      <div className="flex flex-wrap gap-2">
        <a
          href={`https://wa.me/?text=${encodeURIComponent(message)}`}
          target="_blank"
          rel="noopener noreferrer"
          data-testid="share-whatsapp"
          className="rounded-lg bg-green-700 px-3 py-2 text-sm font-medium text-white"
        >
          {t.quote.shareWhatsApp}
        </a>
        <button
          type="button"
          onClick={() => void copy()}
          className="rounded-lg px-3 py-2 text-sm ring-1 ring-neutral-300 dark:ring-neutral-600"
        >
          {copied ? t.quote.shareCopied : t.quote.shareCopy}
        </button>
      </div>
      <input
        readOnly
        value={current.url}
        aria-label={t.quote.shareCopy}
        data-testid="share-url"
        onFocus={(e) => e.currentTarget.select()}
        className="w-full rounded border border-neutral-200 bg-transparent px-2 py-1 text-xs dark:border-neutral-700"
      />
    </section>
  );
}
