"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { OtpStep } from "@/components/checkout/OtpStep";
import { formatDay } from "@/components/track/Tracker";
import { ApiError, type OrderSummary, getToken, listOrders, setToken } from "@/lib/api";
import { type Locale, type Messages, fill } from "@/lib/i18n";

/** "Where is my order?" — sign in with the phone used at checkout and pick an order. */
export function OrderList({ locale, t }: { locale: Locale; t: Messages }) {
  const [orders, setOrders] = useState<OrderSummary[] | null>(null);
  const [needsSignIn, setNeedsSignIn] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setOrders((await listOrders()).orders);
      setNeedsSignIn(false);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setToken(null);
        setNeedsSignIn(true);
      } else {
        setProblem(t.advisor.error);
      }
    }
  }, [t]);

  useEffect(() => {
    if (getToken()) void load();
    else setNeedsSignIn(true);
  }, [load]);

  if (needsSignIn) {
    return (
      <>
        <p className="text-sm font-medium">{t.track.signIn}</p>
        <OtpStep t={t} onVerified={() => void load()} />
      </>
    );
  }
  if (problem) return <p role="alert" className="text-sm text-red-700">{problem}</p>;
  if (!orders) return <p className="text-neutral-500">{t.advisor.thinking}</p>;
  if (orders.length === 0) return <p data-testid="no-orders">{t.track.noOrders}</p>;

  return (
    <ul data-testid="orders" className="flex flex-col gap-3">
      {orders.map((o) => (
        <li key={o.order_id}>
          <Link
            href={`/${locale}/track/${encodeURIComponent(o.order_id)}`}
            className="flex flex-col gap-1 rounded-2xl bg-white p-4 shadow-sm ring-1 ring-neutral-200 dark:bg-neutral-900 dark:ring-neutral-700"
          >
            <span className="font-mono text-xs text-neutral-500">
              {fill(t.track.orderNumber, { ref: o.order_id.slice(0, 8).toUpperCase() })} · {formatDay(o.created_at, locale)}
            </span>
            <span className="font-semibold">{t.track.states[o.state]}</span>
            <span className="text-sm text-neutral-600 dark:text-neutral-300">{o.products.join(", ")}</span>
            {o.eta && <span className="text-sm">{fill(t.track.eta, { date: formatDay(o.eta, locale) })}</span>}
          </Link>
        </li>
      ))}
    </ul>
  );
}
