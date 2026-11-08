"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { OtpStep } from "@/components/checkout/OtpStep";
import { JourneySteps } from "@/components/JourneySteps";
import {
  ApiError,
  DOC_SLOTS,
  type DocSlot,
  type GstinPreview,
  type KycApplication,
  type Order,
  createKycApplication,
  editOnRecord,
  fetchFromDigilocker,
  getKycApplication,
  getOrder,
  getToken,
  lookupGstinOnRecord,
  lookupUdyamOnRecord,
  manualEntry,
  previewGstin,
  requestKycVisit,
  setToken,
  submitKyc,
  uploadKycDocument,
} from "@/lib/api";
import { type Locale, type Messages, fill } from "@/lib/i18n";

const APP_KEY = "udyam.kyc.application";
const ACCEPT = "image/png,image/jpeg,application/pdf";
/** Slots where the applicant may type the number if the photo can't be read (N2 / K04). */
const MANUAL_SLOTS: DocSlot[] = ["GST_CERTIFICATE", "UDYAM_CERTIFICATE"];
/** Documents DigiLocker can hand over, issuer-signed, without a photo. */
const DIGILOCKER_SLOTS = ["SIGNATORY_ID", "UDYAM_CERTIFICATE"] as const;
type LockerSlot = (typeof DIGILOCKER_SLOTS)[number];
const isLockerSlot = (slot: DocSlot): slot is LockerSlot => (DIGILOCKER_SLOTS as readonly string[]).includes(slot);
const GSTIN_LENGTH = 15;

type Phase = "signin" | "loading" | "onRecord" | "documents";

/** A form label without its trailing hint: "GSTIN (if you have one)" → "GSTIN". */
const plain = (label: string) => label.replace(/\s*\([^)]*\)\s*$/, "");

type Editable = "contact_name" | "gstin" | "udyam_number";
const EMPTY_EDIT: Record<Editable, string> = { contact_name: "", gstin: "", udyam_number: "" };

interface SlotState {
  busy: boolean;
  /** Immediate problem with this upload (blur, wrong document, too big…). */
  fix: string | null;
}

/**
 * Smart KYC: declare what's on record → upload the four documents → check → fix.
 * Images go to our server only (never to the AI helper); the verdict comes back as
 * plain-language fixes per document.
 */
export function KycScreen({ locale, t }: { locale: Locale; t: Messages }) {
  const orderId = useSearchParams().get("o");
  const [phase, setPhase] = useState<Phase>("loading");
  const [order, setOrder] = useState<Order | null>(null);
  const [app, setApp] = useState<KycApplication | null>(null);
  const [slots, setSlots] = useState<Partial<Record<DocSlot, SlotState>>>({});
  const [manual, setManual] = useState<Partial<Record<DocSlot, string>>>({});
  const [onRecord, setOnRecord] = useState({ contact_name: "", gstin: "", udyam_number: "" });
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [needsConsent, setNeedsConsent] = useState(false);
  // "Is this your business?" — masked registry preview of the GSTIN typed in.
  const [gstPreview, setGstPreview] = useState<GstinPreview | "none" | null>(null);
  // DigiLocker's own consent screen, open for one document at a time.
  const [locker, setLocker] = useState<LockerSlot | null>(null);
  // Fixing the details given at the start (a wrong GSTIN...), without starting over.
  const [editing, setEditing] = useState(false);
  const [edits, setEdits] = useState<Record<Editable, string>>(EMPTY_EDIT);
  const [removed, setRemoved] = useState<Partial<Record<Editable, boolean>>>({});
  const [notice, setNotice] = useState<string | null>(null);

  const fail = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) {
        setToken(null);
        setPhase("signin");
        return;
      }
      if (e instanceof ApiError && e.status === 403) {
        setNeedsConsent(true);
        return;
      }
      if (e instanceof ApiError && e.status === 429) {
        setProblem(t.otp.tooMany);
        return;
      }
      setProblem(t.advisor.error);
    },
    [t],
  );

  const load = useCallback(async () => {
    setPhase("loading");
    setProblem(null);
    // A different order may be a different business: drop the previous one's state.
    setApp(null);
    setSlots({});
    setManual({});
    setOnRecord({ contact_name: "", gstin: "", udyam_number: "" });
    setGstPreview(null);
    try {
      let known: string | null;
      if (orderId) {
        // Only this order's own KYC. Never fall back to an application remembered from
        // an earlier order: that may be a different business (GSTIN / Udyam).
        const o = await getOrder(orderId);
        setOrder(o);
        known = o.tracker?.kyc.application_id ?? null;
      } else {
        known = sessionStorage.getItem(APP_KEY);
      }
      if (known) {
        try {
          const found = await getKycApplication(known);
          if (!orderId) sessionStorage.setItem(APP_KEY, found.application_id);
          setApp(found);
          setPhase("documents");
          return;
        } catch (e) {
          if (!(e instanceof ApiError && e.status === 404)) throw e;
          if (!orderId) sessionStorage.removeItem(APP_KEY); // someone else's / erased: start fresh
        }
      }
      setPhase("onRecord");
    } catch (e) {
      setPhase("onRecord");
      fail(e);
    }
  }, [orderId, fail]);

  useEffect(() => {
    if (getToken()) void load();
    else setPhase("signin");
  }, [load]);

  const start = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      const body = Object.fromEntries(
        Object.entries(onRecord)
          .map(([k, v]) => [k, v.trim()])
          .filter(([, v]) => v),
      );
      const created = await createKycApplication(orderId ? { ...body, order_id: orderId } : body);
      if (!orderId) sessionStorage.setItem(APP_KEY, created.application_id);
      setApp(created);
      setPhase("documents");
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  const setSlot = (slot: DocSlot, state: SlotState) => setSlots((s) => ({ ...s, [slot]: state }));

  const upload = async (slot: DocSlot, file: File | undefined) => {
    if (!app || !file) return;
    setSlot(slot, { busy: true, fix: null });
    setProblem(null);
    try {
      const res = await uploadKycDocument(app.application_id, slot, file, locale);
      setSlot(slot, { busy: false, fix: res.accepted ? null : res.fix });
      setApp(await getKycApplication(app.application_id));
    } catch (e) {
      const code = e instanceof ApiError ? e.code : null;
      const known: Record<string, string> = {
        FILE_TOO_LARGE: t.kyc.tooLarge,
        UNSUPPORTED_TYPE: t.kyc.wrongType,
        IMAGE_DIMENSIONS_REJECTED: t.kyc.wrongType,
      };
      if (code && code in known) setSlot(slot, { busy: false, fix: known[code] });
      else if (e instanceof ApiError && e.status === 415) setSlot(slot, { busy: false, fix: t.kyc.wrongType });
      else {
        setSlot(slot, { busy: false, fix: null });
        fail(e);
      }
    }
  };

  const saveManual = async (slot: DocSlot) => {
    if (!app) return;
    const value = (manual[slot] ?? "").trim();
    if (!value) return;
    setBusy(true);
    try {
      setApp(await manualEntry(app.application_id, slot, value));
      setManual((m) => ({ ...m, [slot]: "" }));
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  const lookup = async () => {
    if (!app) return;
    setBusy(true);
    try {
      setApp(await lookupUdyamOnRecord(app.application_id));
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  /** Check a GSTIN online in place of a certificate photo (or the Udyam number in place of its certificate). */
  const verifyOnline = async (slot: DocSlot) => {
    if (!app) return;
    setBusy(true);
    setProblem(null);
    try {
      const call = slot === "GST_CERTIFICATE" ? lookupGstinOnRecord : lookupUdyamOnRecord;
      setApp(await call(app.application_id));
      setSlot(slot, { busy: false, fix: null });
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  const saveDetails = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!app) return;
    const changes: Partial<Record<Editable, string | null>> = {};
    for (const key of ["contact_name", "gstin", "udyam_number"] as const) {
      if (removed[key]) changes[key] = null;
      else if (edits[key].trim()) changes[key] = edits[key].trim();
    }
    if (Object.keys(changes).length === 0) {
      setEditing(false);
      return;
    }
    setBusy(true);
    setProblem(null);
    try {
      const res = await editOnRecord(app.application_id, changes);
      setApp(res);
      setSlots({});
      setEditing(false);
      setEdits(EMPTY_EDIT);
      setRemoved({});
      const docs = res.documents_dropped.map((slot) => t.kyc.slots[slot]).join(", ");
      setNotice(docs ? `${t.kyc.detailsSaved} ${fill(t.kyc.droppedNote, { docs })}` : t.kyc.detailsSaved);
      if (changes.gstin) void checkGstin(changes.gstin);
      else setGstPreview(null);
    } catch (e) {
      if (e instanceof ApiError && e.code === "KYC_VERIFIED_LOCKED") setProblem(t.kyc.detailsLocked);
      else fail(e);
    } finally {
      setBusy(false);
    }
  };

  /** The owner's answer on DigiLocker's consent screen (both are sent: a "no" is audited too). */
  const answerLocker = async (slot: LockerSlot, allow: boolean) => {
    if (!app) return;
    setBusy(true);
    setProblem(null);
    try {
      setApp(await fetchFromDigilocker(app.application_id, slot, allow));
      setSlot(slot, { busy: false, fix: null });
    } catch (e) {
      const code = e instanceof ApiError ? e.code : null;
      if (code === "DIGILOCKER_NOT_LINKED") setSlot(slot, { busy: false, fix: t.kyc.digilockerNotLinked });
      else if (code === "DIGILOCKER_DECLINED") setSlot(slot, { busy: false, fix: t.kyc.digilockerDeclined });
      else fail(e);
    } finally {
      setLocker(null);
      setBusy(false);
    }
  };

  const askVisit = async () => {
    if (!app) return;
    setBusy(true);
    setProblem(null);
    try {
      setApp(await requestKycVisit(app.application_id));
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  const checkGstin = async (value: string) => {
    const gstin = value.replace(/\s/g, "").toUpperCase();
    if (gstin.length !== GSTIN_LENGTH) {
      setGstPreview(null);
      return;
    }
    try {
      setGstPreview(await previewGstin(gstin));
    } catch (e) {
      // Not found / malformed: say so. Anything else (rate limit, outage): stay quiet.
      setGstPreview(e instanceof ApiError && (e.status === 404 || e.status === 422) ? "none" : null);
    }
  };

  const check = async () => {
    if (!app) return;
    setBusy(true);
    setProblem(null);
    try {
      const checked = await submitKyc(app.application_id, locale);
      setApp(checked);
      setSlots({});
      if (orderId) setOrder(await getOrder(orderId).catch(() => order));
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  const consentLink = (
    <p role="alert" className="rounded-xl bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100">
      {t.kyc.consentNeeded}{" "}
      <Link href={`/${locale}/consent`} className="font-medium underline">
        {t.consent.title}
      </Link>
    </p>
  );

  const steps = (
    <JourneySteps
      t={t}
      current={app?.status === "PASS" ? 4 : 3}
      links={order ? { 2: `/${locale}/checkout?q=${encodeURIComponent(order.quote_id)}` } : {}}
    />
  );

  if (phase === "signin") {
    return (
      <>
        {steps}
        <p className="text-sm font-medium">{t.kyc.signIn}</p>
        <OtpStep t={t} onVerified={() => void load()} />
      </>
    );
  }
  if (phase === "loading") return <p className="text-neutral-500">{t.advisor.thinking}</p>;

  if (phase === "onRecord") {
    return (
      <form onSubmit={(e) => void start(e)} className="flex flex-col gap-3" data-testid="kyc-on-record">
        {steps}
        <p className="text-sm text-neutral-600 dark:text-neutral-300">{t.kyc.onRecordIntro}</p>
        {(
          [
            ["contact_name", t.kyc.contactName, "name"],
            ["gstin", t.kyc.gstin, "off"],
            ["udyam_number", t.kyc.udyam, "off"],
          ] as const
        ).map(([key, label, autoComplete]) => (
          <label key={key} className="flex flex-col gap-1 text-sm font-medium">
            {label}
            <input
              name={key}
              value={onRecord[key]}
              autoComplete={autoComplete}
              maxLength={key === "contact_name" ? 120 : 25}
              onChange={(e) => setOnRecord((r) => ({ ...r, [key]: e.target.value }))}
              onBlur={key === "gstin" ? (e) => void checkGstin(e.target.value) : undefined}
              className={`h-11 rounded-lg px-3 font-normal ring-1 ring-neutral-300 dark:bg-neutral-900 dark:ring-neutral-600 ${
                key === "contact_name" ? "" : "uppercase"
              }`}
            />
          </label>
        ))}
        {gstPreview && (
          <p
            data-testid="gst-preview"
            className={`rounded-lg p-2 text-sm ${
              gstPreview === "none"
                ? "bg-amber-50 text-amber-900 dark:bg-amber-950 dark:text-amber-100"
                : "bg-green-50 text-green-900 dark:bg-green-950 dark:text-green-100"
            }`}
          >
            {gstPreview === "none"
              ? t.kyc.gstNotFound
              : fill(t.kyc.gstFound, { name: gstPreview.trade_name, state: gstPreview.state })}
          </p>
        )}
        <p className="text-xs text-neutral-500">{t.kyc.privacy}</p>
        <button type="submit" disabled={busy} className="h-12 rounded-xl bg-red-700 font-medium text-white disabled:opacity-50">
          {t.kyc.start}
        </button>
        {needsConsent && consentLink}
        {problem && (
          <p role="alert" className="text-sm text-red-700">
            {problem}
          </p>
        )}
        {orderId && (
          <Link href={`/${locale}/track/${encodeURIComponent(orderId)}`} data-testid="kyc-back" className="self-start text-sm underline">
            ← {t.kyc.toTracker}
          </Link>
        )}
      </form>
    );
  }

  if (!app) return null;
  // A new upload puts the application back to DRAFT; the last verdict's fixes stay
  // visible for the documents not replaced since.
  const verdict = app.status === "DRAFT" ? null : app.verdict;
  const uploaded = new Map(app.documents.map((d) => [d.slot, d]));
  const fixesFor = (slot: DocSlot | null) =>
    slot && slots[slot] ? [] : (app.verdict?.fixes ?? []).filter((f) => f.slot === slot);
  const passed = app.status === "PASS";
  // A case in review may still list things only the owner can correct.
  const canFix = app.status === "ACTION_NEEDED" || app.status === "FAIL" || app.status === "REVIEW";

  // Numbers given on record can be checked online instead of photographing a certificate.
  const onlineFor = (slot: DocSlot) =>
    (slot === "GST_CERTIFICATE" && app.on_record?.gstin) ||
    (slot === "UDYAM_CERTIFICATE" && app.on_record?.udyam);

  return (
    <div className="flex flex-col gap-5">
      {steps}
      {verdict && (
        <section
          role="status"
          data-testid="kyc-verdict"
          data-decision={app.status}
          className={`flex flex-col gap-2 rounded-xl p-3 text-sm ${
            passed
              ? "bg-green-50 text-green-900 dark:bg-green-950 dark:text-green-100"
              : "bg-amber-50 text-amber-900 dark:bg-amber-950 dark:text-amber-100"
          }`}
        >
          <p className="font-semibold">{t.kyc.status[verdict.decision]}</p>
          {fixesFor(null).length > 0 && (
            <ul className="list-disc ps-5" data-testid="kyc-general-fixes">
              {fixesFor(null).map((f) => (
                <li key={f.reason}>{f.message}</li>
              ))}
            </ul>
          )}
          {app.handed_off && <p>{t.track.handedOff}</p>}
          {verdict.lookup_offered && (
            <button type="button" disabled={busy} onClick={() => void lookup()} className="self-start font-medium underline">
              {t.kyc.lookup}
            </button>
          )}
          {passed && order && (
            <Link
              href={`/${locale}/checkout?q=${encodeURIComponent(order.quote_id)}`}
              data-testid="kyc-to-payment"
              className="self-start font-medium underline"
            >
              {order.next_action === "SIGN" ? t.checkout.signTitle : t.kyc.toPayment}
            </Link>
          )}
        </section>
      )}

      {app.on_record_masked && (
        <section data-testid="kyc-details" className="flex flex-col gap-2 rounded-xl bg-white p-3 text-sm ring-1 ring-neutral-200 dark:bg-neutral-900 dark:ring-neutral-700">
          <div className="flex items-center justify-between gap-3">
            <h2 className="font-semibold">{t.kyc.detailsTitle}</h2>
            {!passed && !editing && (
              <button
                type="button"
                data-testid="kyc-details-edit"
                onClick={() => {
                  setEditing(true);
                  setNotice(null);
                }}
                className="rounded-lg px-3 py-1.5 font-medium text-red-800 ring-1 ring-red-700 dark:text-red-200"
              >
                {t.kyc.edit}
              </button>
            )}
          </div>
          {!editing ? (
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
              {(
                [
                  [t.kyc.contactName, app.on_record_masked.contact_name],
                  [t.kyc.gstin, app.on_record_masked.gstin],
                  [t.kyc.udyam, app.on_record_masked.udyam_number],
                ] as const
              ).map(([label, value]) => (
                <div key={label} className="contents">
                  <dt className="text-neutral-500">{plain(label)}</dt>
                  <dd className="font-mono">{value ?? "—"}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <form onSubmit={(e) => void saveDetails(e)} data-testid="kyc-details-form" className="flex flex-col gap-3">
              <p className="text-xs text-neutral-500">{t.kyc.editHint}</p>
              {(
                [
                  ["contact_name", t.kyc.contactName, app.on_record_masked.contact_name],
                  ["gstin", t.kyc.gstin, app.on_record_masked.gstin],
                  ["udyam_number", t.kyc.udyam, app.on_record_masked.udyam_number],
                ] as const
              ).map(([key, label, now]) => (
                <div key={key} className="flex flex-col gap-1">
                  <label className="flex flex-col gap-1 font-medium">
                    {plain(label)}
                    <input
                      name={key}
                      value={edits[key]}
                      disabled={removed[key]}
                      maxLength={key === "contact_name" ? 120 : 25}
                      placeholder={fill(t.kyc.current, { value: now ?? "—" })}
                      onChange={(e) => setEdits((v) => ({ ...v, [key]: e.target.value }))}
                      className={`h-11 rounded-lg px-3 font-normal ring-1 ring-neutral-300 disabled:opacity-50 dark:bg-neutral-900 dark:ring-neutral-600 ${
                        key === "contact_name" ? "" : "uppercase placeholder:normal-case"
                      }`}
                    />
                  </label>
                  {key !== "contact_name" && now && (
                    <label className="flex items-center gap-2 text-xs">
                      <input
                        type="checkbox"
                        checked={!!removed[key]}
                        onChange={(e) => setRemoved((r) => ({ ...r, [key]: e.target.checked }))}
                        className="h-4 w-4 accent-red-700"
                      />
                      {t.kyc.removeField}
                    </label>
                  )}
                </div>
              ))}
              <div className="flex gap-2">
                <button type="submit" disabled={busy} className="h-11 rounded-xl bg-red-700 px-4 font-medium text-white disabled:opacity-50">
                  {t.kyc.saveDetails}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setEditing(false);
                    setEdits(EMPTY_EDIT);
                    setRemoved({});
                  }}
                  className="h-11 rounded-xl px-4 ring-1 ring-neutral-300 dark:ring-neutral-600"
                >
                  {t.kyc.cancelEdit}
                </button>
              </div>
            </form>
          )}
          {passed && <p className="text-xs text-neutral-500">{t.kyc.detailsLocked}</p>}
          {notice && (
            <p role="status" data-testid="kyc-details-notice" className="rounded-lg bg-green-50 p-2 text-green-900 dark:bg-green-950 dark:text-green-100">
              {notice}
            </p>
          )}
          {gstPreview && !editing && (
            <p data-testid="gst-preview" className="rounded-lg bg-neutral-100 p-2 dark:bg-neutral-800">
              {gstPreview === "none"
                ? t.kyc.gstNotFound
                : fill(passed ? t.kyc.gstVerified : t.kyc.gstFound, { name: gstPreview.trade_name, state: gstPreview.state })}
            </p>
          )}
        </section>
      )}

      {!passed && (
        <section data-testid="kyc-fastest" className="rounded-xl bg-green-50 p-3 text-sm text-green-950 dark:bg-green-950 dark:text-green-50">
          <p className="font-semibold">{t.kyc.fastestTitle}</p>
          <p>{t.kyc.fastestBody}</p>
        </section>
      )}

      {!passed && (
        <ul className="flex flex-col gap-3" data-testid="kyc-slots">
          {DOC_SLOTS.map((slot) => {
            const doc = uploaded.get(slot);
            const state = slots[slot];
            const fixes = fixesFor(slot);
            const inputId = `kyc-file-${slot}`;
            return (
              <li
                key={slot}
                data-testid={`kyc-slot-${slot}`}
                className="flex flex-col gap-2 rounded-2xl bg-white p-4 shadow-sm ring-1 ring-neutral-200 dark:bg-neutral-900 dark:ring-neutral-700"
              >
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="font-medium">{t.kyc.slots[slot]}</p>
                    <p className="text-xs text-neutral-500">{t.kyc.hints[slot]}</p>
                  </div>
                  {doc && !state?.fix && (
                    <span data-testid="kyc-slot-done" className="shrink-0 rounded-full bg-green-100 px-2 py-0.5 text-xs text-green-900 dark:bg-green-900 dark:text-green-100">
                      {doc.source === "MANUAL"
                        ? t.kyc.typed
                        : doc.source === "LOOKUP"
                          ? t.kyc.lookedUp
                          : doc.source === "DIGILOCKER"
                            ? t.kyc.fetched
                            : t.kyc.uploaded}
                    </span>
                  )}
                </div>
                {state?.fix && (
                  <p role="alert" data-testid="kyc-upload-fix" className="text-sm text-red-700">
                    {state.fix}
                  </p>
                )}
                {fixes.length > 0 && (
                  <ul className="list-disc ps-5 text-sm text-amber-900 dark:text-amber-100" data-testid="kyc-fixes">
                    {fixes.map((f) => (
                      <li key={f.reason}>{f.message}</li>
                    ))}
                  </ul>
                )}
                {!doc && onlineFor(slot) && (
                  <button
                    type="button"
                    data-testid={`kyc-online-${slot}`}
                    disabled={busy}
                    onClick={() => void verifyOnline(slot)}
                    className="self-start rounded-lg bg-red-700 px-3 py-2 text-left text-sm font-medium text-white disabled:opacity-50"
                  >
                    {slot === "GST_CERTIFICATE" ? t.kyc.verifyGst : t.kyc.verifyUdyam}
                    {" "}
                    <span className="font-normal opacity-90">· {t.kyc.timeInstant}</span>
                  </button>
                )}
                {!doc && isLockerSlot(slot) && locker !== slot && (
                  <button
                    type="button"
                    data-testid={`kyc-digilocker-${slot}`}
                    disabled={busy}
                    onClick={() => setLocker(slot)}
                    className="self-start rounded-lg px-3 py-2 text-left text-sm font-medium text-red-800 ring-1 ring-red-700 disabled:opacity-50 dark:text-red-200"
                  >
                    {t.kyc.digilocker}
                    {" "}
                    <span className="font-normal">· {t.kyc.timeDigilocker}</span>
                  </button>
                )}
                {locker === slot && isLockerSlot(slot) && (
                  <div
                    role="dialog"
                    aria-label={t.kyc.digilockerTitle}
                    data-testid="digilocker-consent"
                    className="flex flex-col gap-2 rounded-xl bg-indigo-50 p-3 text-sm text-indigo-950 ring-1 ring-indigo-200 dark:bg-indigo-950 dark:text-indigo-50 dark:ring-indigo-800"
                  >
                    <p className="font-semibold">{t.kyc.digilockerTitle}</p>
                    <p>
                      {fill(t.kyc.digilockerAsk, {
                        doc: slot === "SIGNATORY_ID" ? t.kyc.digilockerIdDoc : t.kyc.digilockerUdyamDoc,
                      })}
                    </p>
                    <div className="flex gap-2">
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => void answerLocker(slot, true)}
                        className="rounded-lg bg-indigo-700 px-3 py-2 font-medium text-white disabled:opacity-50"
                      >
                        {t.kyc.digilockerAllow}
                      </button>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => void answerLocker(slot, false)}
                        className="rounded-lg px-3 py-2 font-medium ring-1 ring-indigo-300 disabled:opacity-50"
                      >
                        {t.kyc.digilockerDeny}
                      </button>
                    </div>
                  </div>
                )}
                <label
                  htmlFor={inputId}
                  className="self-start rounded-lg px-3 py-2 text-sm font-medium ring-1 ring-neutral-300 dark:ring-neutral-600"
                >
                  {state?.busy ? t.kyc.uploading : doc ? t.kyc.replace : t.kyc.choose}
                  {!doc && !state?.busy && <span className="font-normal text-neutral-500"> · {t.kyc.timePhoto}</span>}
                </label>
                <input
                  id={inputId}
                  data-testid={`kyc-file-${slot}`}
                  type="file"
                  accept={ACCEPT}
                  capture="environment"
                  disabled={state?.busy || busy}
                  className="sr-only"
                  onChange={(e) => {
                    void upload(slot, e.target.files?.[0]);
                    e.target.value = "";
                  }}
                />
                {canFix && MANUAL_SLOTS.includes(slot) && fixes.length > 0 && (
                  <div className="flex gap-2">
                    <input
                      aria-label={t.kyc.manualLabel}
                      placeholder={t.kyc.manualLabel}
                      value={manual[slot] ?? ""}
                      maxLength={25}
                      onChange={(e) => setManual((m) => ({ ...m, [slot]: e.target.value }))}
                      className="h-10 min-w-0 flex-1 rounded-lg px-3 text-sm uppercase ring-1 ring-neutral-300 dark:bg-neutral-900 dark:ring-neutral-600"
                    />
                    <button
                      type="button"
                      disabled={busy || !(manual[slot] ?? "").trim()}
                      onClick={() => void saveManual(slot)}
                      className="rounded-lg px-3 text-sm font-medium ring-1 ring-neutral-300 disabled:opacity-50 dark:ring-neutral-600"
                    >
                      {t.kyc.manualSave}
                    </button>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {!passed && (
        <>
          <p className="text-xs text-neutral-500">{t.kyc.privacy}</p>
          <button
            type="button"
            data-testid="kyc-submit"
            disabled={busy || app.documents.length === 0}
            onClick={() => void check()}
            className="h-12 rounded-xl bg-red-700 font-medium text-white disabled:opacity-50"
          >
            {busy ? t.kyc.checking : t.kyc.submit}
          </button>
          <section
            data-testid="kyc-visit"
            className="flex flex-col gap-1 rounded-xl bg-neutral-50 p-3 text-sm ring-1 ring-neutral-200 dark:bg-neutral-900 dark:ring-neutral-700"
          >
            {app.handed_off ? (
              <p role="status">{t.kyc.visitDone}</p>
            ) : (
              <>
                <p className="font-medium">{t.kyc.visitTitle}</p>
                <p className="text-neutral-600 dark:text-neutral-300">{t.kyc.visitBody}</p>
                <button
                  type="button"
                  data-testid="kyc-visit-ask"
                  disabled={busy}
                  onClick={() => void askVisit()}
                  className="self-start font-medium text-red-700 underline disabled:opacity-50"
                >
                  {t.kyc.visitAsk}
                </button>
              </>
            )}
          </section>
        </>
      )}

      {needsConsent && consentLink}
      {problem && (
        <p role="alert" className="text-sm text-red-700">
          {problem}
        </p>
      )}
      {orderId && (
        <Link href={`/${locale}/track/${encodeURIComponent(orderId)}`} className="self-start text-sm underline">
          {t.kyc.toTracker}
        </Link>
      )}
    </div>
  );
}
