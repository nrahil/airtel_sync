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
  type ProductOption,
  type QuoteView,
  changeItems,
  createQuote,
  getQuote,
} from "@/lib/api";
import { formatPaise } from "@/lib/format";
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
      show(await createQuote(quote.session_id, items, chosenContract));
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

  return (
    <div className="flex flex-col gap-5">
      <JourneySteps t={t} current={1} />
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
            {t.quote.setup} <span className="text-neutral-500">({t.quote.setupNote})</span>
          </dt>
          <dd className="text-right tabular-nums" data-testid="setup-total">
            {formatPaise(totals.setup_total_paise)}
          </dd>
          <dt>{t.quote.contract}</dt>
          <dd className="text-right">
            {quote.contract_options.length > 1 && editable ? (
              <label className="inline-flex items-center gap-1">
                <span className="sr-only">{t.quote.contractChoice}</span>
                <select
                  value={quote.contract_months}
                  disabled={busy}
                  onChange={(e) => void send(items, Number(e.target.value))}
                  className="rounded border border-neutral-300 bg-transparent px-1 dark:border-neutral-600"
                >
                  {quote.contract_options.map((m) => (
                    <option key={m} value={m}>
                      {m} {t.quote.months}
                    </option>
                  ))}
                </select>
              </label>
            ) : (
              <>
                {quote.contract_months} {t.quote.months}
              </>
            )}
          </dd>
        </dl>
        {totals.savings_paise > 0 && (
          <p className="text-sm text-green-700 dark:text-green-400">
            {fill(t.quote.savings, { amount: formatPaise(totals.savings_paise) })}
          </p>
        )}
        <p className="text-xs text-neutral-500">{fill(t.quote.validUntil, { date: validUntil })}</p>
      </article>

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
          <OptionList title={t.quote.inPlan} options={inPlan} t={t} busy={busy} onToggle={toggle} onTry={() => {}} canRemove={inPlan.length > 1} />
          <OptionList
            options={others}
            t={t}
            busy={busy}
            onToggle={toggle}
            onTry={(alt, qty) => void send([...items.filter((i) => i.product_id !== alt), { product_id: alt, qty }], chosenContract)}
            canRemove
          />
        </section>
      )}

      <div className="sticky bottom-0 -mx-4 flex flex-col gap-1 bg-neutral-50/95 px-4 py-3 dark:bg-neutral-950/95">
        <button
          type="button"
          disabled={busy || expired || !editable || cascade !== null}
          onClick={() => router.push(`/${locale}/checkout?q=${encodeURIComponent(quote.quote_id)}`)}
          className="h-12 rounded-xl bg-red-700 font-medium text-white disabled:opacity-50"
        >
          {t.quote.continue} · {formatPaise(totals.monthly_total_paise)}
          {t.quote.perMonth}
        </button>
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
        {options.map((o) => (
          <li key={o.product_id} data-testid={`option-${o.product_id}`} className="flex items-start justify-between gap-3 p-3">
            <div className="flex min-w-0 flex-col">
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
            </div>
            {o.eligible ? (
              <button
                type="button"
                disabled={busy || (o.in_bundle && !props.canRemove)}
                aria-pressed={o.in_bundle}
                aria-label={`${o.in_bundle ? t.quote.remove : t.quote.add} ${o.product_name}`}
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
                  className="shrink-0 rounded-lg px-3 py-2 text-sm font-medium text-red-800 ring-1 ring-red-700 disabled:opacity-50 dark:text-red-200"
                >
                  {fill(t.quote.fixReplace, { product: o.alternative_name })}
                </button>
              )
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
