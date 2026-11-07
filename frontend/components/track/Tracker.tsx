"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { OtpStep } from "@/components/checkout/OtpStep";
import {
  ApiError,
  type Order,
  type OrderState,
  type SlotWindow,
  type StepStatus,
  type Tracker as TrackerView,
  bookSlot,
  cancelOrder,
  getOrder,
  getToken,
  requestOrderCallback,
  setToken,
  simulateFulfilment,
  simulateLate,
} from "@/lib/api";
import { formatPaise } from "@/lib/format";
import { type Locale, type Messages, bcp47, fill } from "@/lib/i18n";

export function formatDay(iso: string, locale: Locale): string {
  // Date-only ISO strings are calendar days in India; pin the zone so they never shift.
  const d = iso.length === 10 ? new Date(`${iso}T00:00:00+05:30`) : new Date(iso);
  return new Intl.DateTimeFormat(`${bcp47(locale)}-u-nu-latn`, {
    day: "numeric",
    month: "short",
    timeZone: "Asia/Kolkata",
  }).format(d);
}

/** Demo only: the next step operations would take from each fulfilment state. */
const NEXT_STEP: Partial<Record<OrderState, OrderState>> = {
  PAYMENT_CONFIRMED: "FEASIBILITY_CHECK",
  FEASIBILITY_CHECK: "INSTALL_SCHEDULED",
  INSTALL_SCHEDULED: "INSTALLED",
  INSTALLED: "ACTIVATED",
};

const DOT: Record<StepStatus, string> = {
  done: "bg-green-600 ring-green-600",
  current: "bg-white ring-red-700 dark:bg-neutral-900",
  attention: "bg-amber-500 ring-amber-500",
  todo: "bg-white ring-neutral-300 dark:bg-neutral-900 dark:ring-neutral-600",
  skipped: "bg-neutral-200 ring-neutral-300 dark:bg-neutral-800 dark:ring-neutral-600",
};

/** Owner-only order tracker: OTP sign-in, timeline, per-line ETAs, next action. */
export function Tracker({ locale, t, orderId }: { locale: Locale; t: Messages; orderId: string }) {
  const [order, setOrder] = useState<Order | null>(null);
  const [needsSignIn, setNeedsSignIn] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmCancel, setConfirmCancel] = useState(false);

  const load = useCallback(async () => {
    setProblem(null);
    try {
      setOrder(await getOrder(orderId));
      setNeedsSignIn(false);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setToken(null);
        setNeedsSignIn(true);
      } else if (e instanceof ApiError && e.status === 404) {
        setProblem(t.track.notFound);
      } else {
        setProblem(t.advisor.error);
      }
    }
  }, [orderId, t]);

  useEffect(() => {
    if (getToken()) void load();
    else setNeedsSignIn(true);
  }, [load]);

  const act = async (fn: () => Promise<Order>) => {
    setBusy(true);
    setProblem(null);
    try {
      setOrder(await fn());
    } catch {
      setProblem(t.advisor.error);
    } finally {
      setBusy(false);
      setConfirmCancel(false);
    }
  };

  if (needsSignIn) {
    return (
      <>
        <p className="text-sm font-medium">{t.track.signIn}</p>
        <OtpStep t={t} onVerified={() => void load()} />
      </>
    );
  }
  if (problem && !order) {
    return (
      <p role="alert" className="text-sm text-red-700">
        {problem}
      </p>
    );
  }
  if (!order?.tracker) return <p className="text-neutral-500">{t.advisor.thinking}</p>;

  const tr = order.tracker;
  const ref = order.order_id.slice(0, 8).toUpperCase();
  const label = (s: OrderState) => t.track.states[s];
  const nextStep = NEXT_STEP[order.state];
  // Demo: show the late path (day 21+) for any order still open.
  const canAge = !tr.late && !["ACTIVATED", "CANCELLED", "NOT_FEASIBLE"].includes(order.state);

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-col gap-1">
        <p className="font-mono text-sm text-neutral-500" data-testid="order-ref">
          {fill(t.track.orderNumber, { ref })}
        </p>
        <p className="text-xl font-semibold" data-testid="order-state">
          {label(order.state)}
        </p>
        {tr.eta && (
          <p className="text-sm" data-testid="order-eta">
            {fill(tr.eta_estimated ? t.track.etaEstimated : t.track.eta, { date: formatDay(tr.eta, locale) })}
          </p>
        )}
      </header>

      {tr.late && (
        <section role="alert" data-testid="sla" className="flex flex-col gap-1 rounded-xl bg-amber-50 p-3 text-sm text-amber-950 dark:bg-amber-950 dark:text-amber-50">
          <p className="font-semibold">{t.track.lateTitle}</p>
          <p data-testid="late-date">{fill(t.track.lateDate, { date: formatDay(tr.late.new_date, locale) })}</p>
          {tr.callback_requested ? (
            <p role="status" data-testid="late-called">
              {t.track.lateCalled}
            </p>
          ) : (
            <button
              type="button"
              data-testid="late-call"
              disabled={busy}
              onClick={() => void act(() => requestOrderCallback(order.order_id))}
              className="self-start rounded-lg bg-amber-700 px-3 py-2 font-medium text-white disabled:opacity-50"
            >
              {t.track.lateCall}
            </button>
          )}
        </section>
      )}

      <section data-testid="next-action" className="flex flex-col gap-2 rounded-xl bg-neutral-100 p-3 text-sm dark:bg-neutral-800">
        <h2 className="font-semibold">{t.track.nextTitle}</h2>
        <p data-testid="next-sentence">{nextSentence(order, tr, t, locale)}</p>
        {order.state === "INSTALL_SCHEDULED" && (
          <VisitPicker
            tracker={tr}
            t={t}
            locale={locale}
            busy={busy}
            onBook={(day, window) => act(() => bookSlot(order.order_id, day, window))}
          />
        )}
        {order.next_action === "FIX_KYC" && (
          <ul className="list-disc ps-5" data-testid="kyc-fixes">
            {tr.kyc.fixes.map((f) => (
              <li key={`${f.reason}-${f.slot ?? ""}`}>{f.message}</li>
            ))}
          </ul>
        )}
        {tr.kyc.handed_off && order.next_action === "FIX_KYC" && <p>{t.track.handedOff}</p>}
        {(order.next_action === "KYC" || order.next_action === "FIX_KYC") && !tr.kyc.handed_off && (
          <Link
            href={`/${locale}/kyc?o=${encodeURIComponent(order.order_id)}`}
            data-testid="kyc-link"
            className="self-start font-medium underline"
          >
            {t.kyc.link}
          </Link>
        )}
        {order.next_action === "SIGN" && (
          <Link href={`/${locale}/checkout?q=${encodeURIComponent(order.quote_id)}`} data-testid="sign-link" className="self-start font-medium underline">
            {t.checkout.signTitle}
          </Link>
        )}
        {(order.next_action === "PAY" || order.next_action === "RETRY_PAYMENT") && (
          <Link href={`/${locale}/checkout?q=${encodeURIComponent(order.quote_id)}`} className="self-start font-medium underline">
            {t.track.goToPayment}
          </Link>
        )}
        {tr.alternative && (
          <div data-testid="alternative" className="flex flex-col gap-1">
            <p>
              {fill(t.track.alternative, {
                name: tr.alternative.product_name,
                from: formatPaise(tr.alternative.monthly_from_paise),
                to: formatPaise(tr.alternative.monthly_to_paise),
                days: tr.alternative.install_days,
              })}
            </p>
            {tr.alternative.replaces.length > 0 && (
              <p>{fill(t.track.alternativeReplaces, { items: names(order, tr.alternative.replaces) })}</p>
            )}
            {tr.alternative.drops.length > 0 && (
              <p>{fill(t.track.alternativeDrops, { items: names(order, tr.alternative.drops) })}</p>
            )}
            <Link href={`/${locale}/advisor`} className="self-start font-medium underline">
              {fill(t.track.startAlternative, { name: tr.alternative.product_name })}
            </Link>
          </div>
        )}
        {tr.refund_due_paise > 0 && (
          <p data-testid="refund">{fill(t.track.refund, { amount: formatPaise(tr.refund_due_paise) })}</p>
        )}
      </section>

      <ol data-testid="timeline" className="flex flex-col">
        {tr.steps.map((s, i) => {
          // An off-path state (document fix, failed payment) shows on its step.
          const shown: OrderState = s.status === "attention" ? order.state : s.state;
          return (
            <li key={s.state} data-status={s.status} className="flex gap-3">
              <div className="flex flex-col items-center">
                <span className={`mt-1 h-3 w-3 rounded-full ring-2 ${DOT[s.status]}`} aria-hidden />
                {i < tr.steps.length - 1 && <span className="w-px flex-1 bg-neutral-300 dark:bg-neutral-600" aria-hidden />}
              </div>
              <div className={`pb-4 text-sm ${s.status === "todo" || s.status === "skipped" ? "text-neutral-500" : ""} ${s.status === "skipped" ? "line-through" : ""}`}>
                <p className={s.status === "current" || s.status === "attention" ? "font-semibold" : ""}>{label(shown)}</p>
                {s.at && <p className="text-xs text-neutral-500">{formatDay(s.at, locale)}</p>}
              </div>
            </li>
          );
        })}
        {(order.state === "CANCELLED" || order.state === "NOT_FEASIBLE") && (
          <li data-status="terminal" className="flex gap-3 text-sm font-semibold text-red-800 dark:text-red-200">
            <span className="mt-1 h-3 w-3 rounded-full bg-red-700" aria-hidden />
            {label(order.state)}
          </li>
        )}
      </ol>

      <section className="flex flex-col gap-1">
        <ul data-testid="lines" className="flex flex-col gap-2 text-sm">
          {tr.lines.map((l) => (
            <li key={l.product_id} className="flex justify-between gap-3">
              <span>
                {l.product_name}
                {l.qty > 1 ? ` × ${l.qty}` : ""}
              </span>
              {l.eta && (
                <span className="shrink-0 text-neutral-500 tabular-nums">
                  {fill(t.track.lineEta, { days: l.install_days, date: formatDay(l.eta, locale) })}
                </span>
              )}
            </li>
          ))}
        </ul>
      </section>

      <section className="flex flex-col gap-1 text-sm">
        <h2 className="font-semibold">{t.track.updates}</h2>
        {tr.notifications.some((n) => n.status === "SENT") ? (
          <ul data-testid="notifications" className="flex flex-col gap-1 text-neutral-600 dark:text-neutral-300">
            {tr.notifications
              .filter((n) => n.status === "SENT")
              .map((n, i) => (
                <li key={`${n.at}-${i}`}>
                  {fill(t.track.sentAt, { channel: t.track.channel[n.channel], when: formatWhen(n.at, locale) })}
                  {" — "}
                  {n.template === "INSTALL_SLOT" ? t.track.slotNotice : label(n.state)}
                </li>
              ))}
          </ul>
        ) : (
          <p className="text-neutral-500">{t.track.noContact}</p>
        )}
      </section>

      <div className="flex flex-wrap gap-3">
        <button type="button" disabled={busy} onClick={() => void load()} className="h-11 rounded-xl px-4 ring-1 ring-neutral-300 disabled:opacity-50 dark:ring-neutral-600">
          {t.track.refresh}
        </button>
        {tr.can_cancel && !confirmCancel && (
          <button type="button" disabled={busy} onClick={() => setConfirmCancel(true)} className="h-11 rounded-xl px-4 text-red-700 ring-1 ring-red-300 disabled:opacity-50">
            {t.track.cancel}
          </button>
        )}
        {confirmCancel && (
          <>
            <button type="button" disabled={busy} onClick={() => void act(() => cancelOrder(order.order_id))} className="h-11 rounded-xl bg-red-700 px-4 text-white disabled:opacity-50">
              {t.track.cancelConfirm}
            </button>
            <button type="button" disabled={busy} onClick={() => setConfirmCancel(false)} className="h-11 rounded-xl px-4 ring-1 ring-neutral-300">
              {t.track.keep}
            </button>
          </>
        )}
      </div>

      {tr.can_simulate && (nextStep || canAge) && (
        <section data-testid="demo" className="flex flex-col gap-2 rounded-xl border border-dashed border-neutral-400 p-3 text-sm">
          <p className="font-medium">{t.track.demo}</p>
          <p className="text-xs text-neutral-500">{t.track.demoNote}</p>
          <div className="flex flex-wrap gap-2">
            {nextStep && (
              <button type="button" disabled={busy} onClick={() => void act(() => simulateFulfilment(order.order_id, nextStep))} className="rounded-lg px-3 py-2 ring-1 ring-neutral-300 disabled:opacity-50">
                → {label(nextStep)}
              </button>
            )}
            {order.state === "FEASIBILITY_CHECK" && (
              <button type="button" disabled={busy} onClick={() => void act(() => simulateFulfilment(order.order_id, "NOT_FEASIBLE"))} className="rounded-lg px-3 py-2 ring-1 ring-neutral-300 disabled:opacity-50">
                → {label("NOT_FEASIBLE")}
              </button>
            )}
            {canAge && (
              <button
                type="button"
                data-testid="demo-late"
                disabled={busy}
                onClick={() => void act(() => simulateLate(order.order_id))}
                className="rounded-lg px-3 py-2 ring-1 ring-neutral-300 disabled:opacity-50"
              >
                {t.track.demoLate}
              </button>
            )}
          </div>
        </section>
      )}

      {problem && (
        <p role="alert" className="text-sm text-red-700">
          {problem}
        </p>
      )}
      <Link href={`/${locale}/privacy`} data-testid="tracker-my-data" className="self-start text-sm text-neutral-600 underline dark:text-neutral-300">
        {t.privacy.link}
      </Link>
    </div>
  );
}

function names(order: Order, ids: string[]): string {
  return ids.map((id) => order.lines.find((l) => l.product_id === id)?.product_name ?? id).join(", ");
}

/** "15 Jul, 10:42" in India time: when a WhatsApp/SMS update went out. */
function formatWhen(iso: string, locale: Locale): string {
  return new Intl.DateTimeFormat(`${bcp47(locale)}-u-nu-latn`, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone: "Asia/Kolkata",
  }).format(new Date(iso));
}

/** "Thu 16 Jul": a visit day the owner can recognise at a glance. */
function formatVisitDay(iso: string, locale: Locale): string {
  return new Intl.DateTimeFormat(`${bcp47(locale)}-u-nu-latn`, {
    weekday: "short",
    day: "numeric",
    month: "short",
    timeZone: "Asia/Kolkata",
  }).format(new Date(`${iso}T00:00:00+05:30`));
}

/** One sentence: what happens next, and when (or what the owner needs to do). */
function nextSentence(order: Order, tr: TrackerView, t: Messages, locale: Locale): string {
  if (order.next_action !== "NONE") return t.track.next[order.next_action];
  const by = tr.eta ? formatDay(tr.eta, locale) : "";
  switch (order.state) {
    case "PAYMENT_CONFIRMED":
    case "FEASIBILITY_CHECK":
      return fill(t.track.nextCheck, { date: by });
    case "INSTALL_SCHEDULED":
      return tr.slot
        ? fill(t.track.nextVisit, { date: formatVisitDay(tr.slot.date, locale), window: t.track.windows[tr.slot.window] })
        : t.track.nextPick;
    case "INSTALLED":
      return fill(t.track.nextSwitchOn, { date: by });
    case "ACTIVATED":
      return t.track.nextLive;
    default:
      return t.track.next.NONE;
  }
}

/** Pick, or move, the installation visit from the free slots the server offers. */
function VisitPicker(props: {
  tracker: TrackerView;
  t: Messages;
  locale: Locale;
  busy: boolean;
  /** Resolves once the server has answered (the picker stays open until then). */
  onBook: (day: string, window: SlotWindow) => Promise<void>;
}) {
  const { tracker: tr, t, locale, busy } = props;
  const [open, setOpen] = useState(false);
  const [day, setDay] = useState<string | null>(null);
  const [slotWindow, setSlotWindow] = useState<SlotWindow | null>(null);

  if (tr.slot && !tr.slot.can_change) {
    return (
      <p className="text-neutral-600 dark:text-neutral-300" data-testid="visit-locked">
        {tr.slot.why_not === "TOO_MANY_CHANGES" ? t.track.nextTooMany : t.track.nextTooLate}
      </p>
    );
  }
  if (tr.calendar.length === 0) return null;
  if (tr.slot && !open) {
    return (
      <button
        type="button"
        data-testid="visit-change"
        onClick={() => setOpen(true)}
        className="self-start rounded-lg px-3 py-2 font-medium ring-1 ring-neutral-300 dark:ring-neutral-600"
      >
        {t.track.nextChange}
      </button>
    );
  }
  const free = tr.calendar.find((d) => d.date === day)?.free ?? [];
  return (
    <div data-testid="visit-picker" className="flex flex-col gap-2">
      <p className="font-medium">{t.track.pickTitle}</p>
      <p className="text-xs text-neutral-500">{t.track.pickHint}</p>
      <div className="flex flex-wrap gap-2" role="group" aria-label={t.track.pickTitle}>
        {tr.calendar.map((d) => (
          <button
            key={d.date}
            type="button"
            data-testid={`visit-day-${d.date}`}
            aria-pressed={day === d.date}
            disabled={d.free.length === 0}
            onClick={() => {
              setDay(d.date);
              setSlotWindow(null);
            }}
            className={`rounded-lg px-3 py-2 ring-1 disabled:opacity-40 ${
              day === d.date
                ? "bg-red-700 text-white ring-red-700"
                : "bg-white ring-neutral-300 dark:bg-neutral-900 dark:ring-neutral-600"
            }`}
          >
            {formatVisitDay(d.date, locale)}
          </button>
        ))}
      </div>
      {day && (
        <div className="flex flex-wrap gap-2" role="group">
          {(["MORNING", "AFTERNOON", "EVENING"] as const).map((w) => (
            <button
              key={w}
              type="button"
              data-testid={`visit-window-${w}`}
              aria-pressed={slotWindow === w}
              disabled={!free.includes(w)}
              onClick={() => setSlotWindow(w)}
              className={`rounded-lg px-3 py-2 ring-1 disabled:line-through disabled:opacity-40 ${
                slotWindow === w
                  ? "bg-red-700 text-white ring-red-700"
                  : "bg-white ring-neutral-300 dark:bg-neutral-900 dark:ring-neutral-600"
              }`}
            >
              {t.track.windows[w]}
            </button>
          ))}
        </div>
      )}
      {day && slotWindow && (
        <button
          type="button"
          data-testid="visit-book"
          disabled={busy}
          onClick={async () => {
            await props.onBook(day, slotWindow);
            setOpen(false);
          }}
          className="h-11 self-start rounded-xl bg-red-700 px-4 font-medium text-white disabled:opacity-50"
        >
          {fill(t.track.book, { day: formatVisitDay(day, locale), window: t.track.windows[slotWindow] })}
        </button>
      )}
    </div>
  );
}
