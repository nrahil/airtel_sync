"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { OtpStep } from "@/components/checkout/OtpStep";
import {
  ApiError,
  type DocSource,
  PURPOSES,
  type PrivacySummary,
  type Purpose,
  eraseMyData,
  exportMyData,
  getPrivacySummary,
  getToken,
  setToken,
  withdrawConsent,
} from "@/lib/api";
import { type Locale, type Messages, bcp47, fill } from "@/lib/i18n";

const REQUIRED: readonly Purpose[] = ["NEEDS_ASSESSMENT", "KYC_VERIFICATION"];

/** A form label without its trailing hint: "GSTIN (if you have one)" → "GSTIN". */
const plain = (label: string) => label.replace(/\s*\([^)]*\)\s*$/, "");

type Phase = "signin" | "loading" | "ready" | "erased";

/**
 * The owner's privacy centre (DPDP rights): what we hold, masked; each consent with a
 * way to turn it off; a full download; erasure; and the grievance contact. Everything
 * is the server's view — nothing here is computed from data the browser kept.
 */
export function PrivacyCentre({ locale, t }: { locale: Locale; t: Messages }) {
  const p = t.privacy;
  const [phase, setPhase] = useState<Phase>("loading");
  const [data, setData] = useState<PrivacySummary | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [sure, setSure] = useState(false);

  const day = useCallback(
    (iso: string) => new Intl.DateTimeFormat(bcp47(locale), { dateStyle: "medium" }).format(new Date(iso)),
    [locale],
  );

  const load = useCallback(async () => {
    setPhase("loading");
    try {
      setData(await getPrivacySummary());
      setPhase("ready");
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setToken(null);
        setPhase("signin");
      } else {
        setProblem(t.advisor.error);
        setPhase("ready");
      }
    }
  }, [t]);

  useEffect(() => {
    if (getToken()) void load();
    else setPhase("signin");
  }, [load]);

  const act = async (work: () => Promise<void>) => {
    setBusy(true);
    setProblem(null);
    setNotice(null);
    try {
      await work();
    } catch (e) {
      if (e instanceof ApiError && e.code === "ERASURE_BLOCKED") setProblem(p.deleteBlocked);
      else if (e instanceof ApiError && e.status === 401) {
        setToken(null);
        setPhase("signin");
      } else setProblem(t.advisor.error);
    } finally {
      setBusy(false);
    }
  };

  const withdraw = (purpose: Purpose) =>
    act(async () => {
      const res = await withdrawConsent(purpose, locale);
      setData(await getPrivacySummary());
      setNotice(
        res.deletion_scheduled_for
          ? fill(p.withdrawn, { date: day(res.deletion_scheduled_for) })
          : `${t.consent[purpose].title}: ${p.off}`,
      );
    });

  const download = () =>
    act(async () => {
      const blob = await exportMyData();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "my-data.json";
      a.click();
      URL.revokeObjectURL(url);
    });

  const erase = () =>
    act(async () => {
      await eraseMyData(locale);
      setToken(null); // the server already revoked it
      setData(null);
      setPhase("erased");
    });

  const grievance = (
    <section className="flex flex-col gap-1 text-sm">
      <h2 className="font-semibold">{p.grievanceTitle}</h2>
      <p className="text-neutral-600 dark:text-neutral-300">{p.grievance}</p>
    </section>
  );

  if (phase === "signin") {
    return (
      <div className="flex flex-col gap-4">
        <p className="text-sm text-neutral-600 dark:text-neutral-300">{p.intro}</p>
        <p className="text-sm font-medium">{p.signIn}</p>
        <OtpStep t={t} onVerified={() => void load()} />
        {grievance}
      </div>
    );
  }
  if (phase === "erased") {
    return (
      <div className="flex flex-col gap-4">
        <p role="status" data-testid="erased" className="rounded-xl bg-green-50 p-3 text-green-900 dark:bg-green-950 dark:text-green-100">
          {p.deletedDone}
        </p>
        <Link href={`/${locale}`} className="self-start font-medium underline">
          {t.landing.start}
        </Link>
        {grievance}
      </div>
    );
  }
  if (phase === "loading" || !data) return <p className="text-neutral-500">{t.advisor.thinking}</p>;

  const source: Record<DocSource, string> = {
    OCR: p.srcOCR,
    LOOKUP: p.srcLOOKUP,
    DIGILOCKER: p.srcDIGILOCKER,
    MANUAL: p.srcMANUAL,
  };
  const card = "flex flex-col gap-2 rounded-2xl bg-white p-4 text-sm shadow-sm ring-1 ring-neutral-200 dark:bg-neutral-900 dark:ring-neutral-700";

  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-neutral-600 dark:text-neutral-300">{p.intro}</p>
      {notice && (
        <p role="status" data-testid="privacy-notice" className="rounded-xl bg-green-50 p-3 text-sm text-green-900 dark:bg-green-950 dark:text-green-100">
          {notice}
        </p>
      )}
      {problem && (
        <p role="alert" data-testid="privacy-problem" className="rounded-xl bg-amber-50 p-3 text-sm text-amber-950 dark:bg-amber-950 dark:text-amber-50">
          {problem}
        </p>
      )}

      {data.phone && (
        <p className="text-sm">
          {p.phone}: <span className="font-mono" data-testid="privacy-phone">{data.phone}</span>
        </p>
      )}

      <section className={card} data-testid="privacy-consents">
        <h2 className="text-base font-semibold">{p.consentsTitle}</h2>
        <ul className="flex flex-col divide-y divide-neutral-100 dark:divide-neutral-800">
          {PURPOSES.map((purpose) => {
            const c = data.consents[purpose];
            return (
              <li key={purpose} data-testid={`consent-${purpose}`} className="flex flex-col gap-1 py-2">
                <div className="flex items-center justify-between gap-3">
                  <span className="font-medium">{t.consent[purpose].title}</span>
                  <span className={c.granted ? "text-green-800 dark:text-green-300" : "text-neutral-500"}>
                    {c.granted ? p.on : p.off}
                  </span>
                </div>
                <span className="text-xs text-neutral-500">{t.consent[purpose].keep}</span>
                {c.granted && (
                  <>
                    {REQUIRED.includes(purpose) && <span className="text-xs text-amber-800 dark:text-amber-200">{p.withdrawWarn}</span>}
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void withdraw(purpose)}
                      className="self-start rounded-lg px-3 py-1.5 text-sm ring-1 ring-neutral-300 disabled:opacity-50 dark:ring-neutral-600"
                    >
                      {p.withdraw}
                    </button>
                  </>
                )}
              </li>
            );
          })}
        </ul>
        <p className="text-xs text-neutral-500">
          {p.turnOnHint}{" "}
          <Link href={`/${locale}/consent`} className="underline">
            {t.consent.title}
          </Link>
        </p>
      </section>

      <section className={card} data-testid="privacy-kyc">
        <h2 className="text-base font-semibold">{p.kycTitle}</h2>
        {data.kyc_applications.length === 0 && <p className="text-neutral-500">{p.none}</p>}
        {data.kyc_applications.map((a) => (
          <div key={a.application_id} className="flex flex-col gap-1">
            {a.on_record && (
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
                {(
                  [
                    [plain(t.kyc.contactName), a.on_record.contact_name],
                    [plain(t.kyc.gstin), a.on_record.gstin],
                    [plain(t.kyc.udyam), a.on_record.udyam_number],
                  ] as const
                )
                  .filter(([, v]) => v)
                  .map(([label, value]) => (
                    <div key={label} className="contents">
                      <dt className="text-neutral-500">{label}</dt>
                      <dd className="font-mono">{value}</dd>
                    </div>
                  ))}
              </dl>
            )}
            {a.documents.length > 0 && (
              <>
                <h3 className="mt-1 font-medium">{p.docsTitle}</h3>
                <ul className="flex flex-col gap-0.5" data-testid="privacy-documents">
                  {a.documents.map((d) => (
                    <li key={d.slot}>
                      {t.kyc.slots[d.slot]} — {source[d.source]}
                      {d.source === "OCR" && (
                        <span className="block text-xs text-neutral-500">
                          {!d.image_retained
                            ? p.deleted
                            : d.image_delete_after
                              ? fill(p.kept, { date: day(d.image_delete_after) })
                              : p.keptWhile}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>
        ))}
      </section>

      <section className={card} data-testid="privacy-orders">
        <h2 className="text-base font-semibold">{p.ordersTitle}</h2>
        {data.orders.length === 0 && <p className="text-neutral-500">{p.none}</p>}
        <ul className="flex flex-col gap-1">
          {data.orders.map((o) => (
            <li key={o.order_id}>
              <Link href={`/${locale}/track/${encodeURIComponent(o.order_id)}`} className="underline">
                {o.products.join(", ")}
              </Link>{" "}
              <span className="text-neutral-500">· {t.track.states[o.state]}</span>
            </li>
          ))}
        </ul>
      </section>

      <section className={card}>
        <h2 className="text-base font-semibold">{p.download}</h2>
        <p className="text-neutral-600 dark:text-neutral-300">{p.downloadNote}</p>
        <button
          type="button"
          data-testid="privacy-download"
          disabled={busy}
          onClick={() => void download()}
          className="self-start rounded-lg px-3 py-2 font-medium ring-1 ring-neutral-300 disabled:opacity-50 dark:ring-neutral-600"
        >
          {p.download}
        </button>
      </section>

      <section className={`${card} ring-red-200 dark:ring-red-900`} data-testid="privacy-delete">
        <h2 className="text-base font-semibold text-red-800 dark:text-red-200">{p.deleteTitle}</h2>
        <p className="text-neutral-600 dark:text-neutral-300">{p.deleteBody}</p>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={sure} onChange={(e) => setSure(e.target.checked)} className="h-5 w-5 accent-red-700" />
          {p.deleteCheck}
        </label>
        <button
          type="button"
          disabled={busy || !sure}
          onClick={() => void erase()}
          className="h-11 self-start rounded-lg bg-red-700 px-4 font-medium text-white disabled:opacity-50"
        >
          {p.deleteButton}
        </button>
      </section>

      {grievance}
    </div>
  );
}
