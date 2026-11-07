"use client";

import { useCallback, useEffect, useState } from "react";

import { ApiError } from "@/lib/api";
import {
  CALL_OUTCOMES,
  type CallOutcome,
  type Factor,
  type LastCall,
  type LeadRow,
  type ModelView,
  REVEAL_REASONS,
  type ReviewItem,
  type RevealKind,
  type RevealReason,
  type SessionRow,
  type SlaRow,
  type StaffLang,
  type StaffMe,
  bo,
  getStaffToken,
  setStaffToken,
} from "@/lib/backoffice";
import type { BackofficeMessages } from "@/lib/backoffice-i18n";
import { formatPaise } from "@/lib/format";
import { type Locale, fill } from "@/lib/i18n";

type T = BackofficeMessages;
type Tab = "leads" | "sessions" | "review" | "sla" | "model";

/** Which tab each permission unlocks (the server enforces the same rules). */
const TAB_PERMISSION: Record<Tab, string[]> = {
  leads: ["leads:read"],
  sessions: ["leads:read"],
  review: ["review:read", "handoff:read"],
  sla: ["sla:read"],
  model: ["model:read"],
};

const NEXT_STEP: Record<string, string> = {
  PAYMENT_CONFIRMED: "FEASIBILITY_CHECK",
  FEASIBILITY_CHECK: "INSTALL_SCHEDULED",
  INSTALL_SCHEDULED: "INSTALLED",
  INSTALLED: "ACTIVATED",
};

const card = "rounded-2xl bg-white p-4 shadow-sm ring-1 ring-neutral-200 dark:bg-neutral-900 dark:ring-neutral-700";
const button = "rounded-lg px-3 py-1.5 text-sm font-medium disabled:opacity-50";

/** Staff console: sign-in, then the tabs this role may use. PII stays masked until revealed. */
export function Backoffice({ locale, t }: { locale: Locale; t: T }) {
  const lang: StaffLang = locale === "hi" ? "hi" : "en";
  const [me, setMe] = useState<StaffMe | null>(null);
  const [checking, setChecking] = useState(true);
  const [tab, setTab] = useState<Tab | null>(null);

  const signedIn = (who: StaffMe) => {
    setMe(who);
    const first = (Object.keys(TAB_PERMISSION) as Tab[]).find((k) => can(who, k));
    setTab(first ?? null);
  };

  useEffect(() => {
    if (!getStaffToken()) {
      setChecking(false);
      return;
    }
    bo.me()
      .then(signedIn)
      .catch(() => setStaffToken(null))
      .finally(() => setChecking(false));
  }, []);

  if (checking) return <p className="text-neutral-500">{t.loading}</p>;
  if (!me) return <Login t={t} onSignedIn={signedIn} />;

  const tabs = (Object.keys(TAB_PERMISSION) as Tab[]).filter((k) => can(me, k));
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-neutral-600 dark:text-neutral-300" data-testid="staff-who">
          {fill(t.signedInAs, { user: me.username, role: t.roles[me.role] })}
          {me.partner_id ? ` · ${me.partner_id}` : ""}
        </p>
        <button
          type="button"
          className={`${button} border border-neutral-300`}
          onClick={() => {
            setStaffToken(null);
            setMe(null);
          }}
        >
          {t.signOut}
        </button>
      </div>
      <nav className="flex flex-wrap gap-2" role="tablist">
        {tabs.map((k) => (
          <button
            key={k}
            type="button"
            role="tab"
            aria-selected={tab === k}
            onClick={() => setTab(k)}
            className={`${button} ${tab === k ? "bg-red-700 text-white" : "border border-neutral-300"}`}
          >
            {t.tabs[k]}
          </button>
        ))}
      </nav>
      {tab === "leads" && <Leads t={t} me={me} lang={lang} />}
      {tab === "sessions" && <Sessions t={t} me={me} lang={lang} />}
      {tab === "review" && <Review t={t} />}
      {tab === "sla" && <Sla t={t} me={me} />}
      {tab === "model" && <Model t={t} />}
    </div>
  );
}

function can(me: StaffMe, tab: Tab): boolean {
  return TAB_PERMISSION[tab].some((p) => me.permissions.includes(p));
}

function useLoad<V>(fetcher: () => Promise<V>, t: T) {
  const [data, setData] = useState<V | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const load = useCallback(async () => {
    setProblem(null);
    try {
      setData(await fetcher());
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setStaffToken(null);
        window.location.reload();
        return;
      }
      setProblem(t.error);
    }
  }, [fetcher, t]);
  useEffect(() => {
    void load();
  }, [load]);
  return { data, problem, reload: load };
}

function Status({ problem, t }: { problem: string | null; t: T }) {
  return problem ? (
    <p role="alert" className="text-sm text-red-700">
      {problem}
    </p>
  ) : (
    <p className="text-neutral-500">{t.loading}</p>
  );
}

// ------------------------------------------------------------------ sign-in

function Login({ t, onSignedIn }: { t: T; onSignedIn: (me: StaffMe) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      const res = await bo.login(username, password);
      setStaffToken(res.token);
      setPassword("");
      onSignedIn(res);
    } catch (e) {
      setProblem(e instanceof ApiError && e.status === 401 ? t.badLogin : t.error);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className={`${card} flex max-w-sm flex-col gap-3`} data-testid="staff-login">
      <h2 className="font-semibold">{t.signIn}</h2>
      <label className="flex flex-col gap-1 text-sm">
        {t.username}
        <input
          className="rounded-lg border border-neutral-300 px-3 py-2"
          autoComplete="username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          required
        />
      </label>
      <label className="flex flex-col gap-1 text-sm">
        {t.password}
        <input
          className="rounded-lg border border-neutral-300 px-3 py-2"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
      </label>
      {problem && (
        <p role="alert" className="text-sm text-red-700">
          {problem}
        </p>
      )}
      <button type="submit" disabled={busy} className={`${button} bg-red-700 py-2 text-white`}>
        {t.signInButton}
      </button>
    </form>
  );
}

// ------------------------------------------------------------------ shared pieces

/** "GSTIN active", "Came via Referral", "12 staff": a factor as a caller would say it. */
function factorText(f: Factor, t: T): string {
  const x = t.factors;
  const pick = (table: Record<string, string>) => table[f.value] ?? f.value;
  switch (f.feature) {
    case "segment":
      return pick(x.segment);
    case "gstin_status":
      return pick(x.gstin_status);
    case "has_udyam":
      return pick(x.has_udyam);
    case "journey_stage":
      return pick(x.journey_stage);
    case "current_internet":
      return f.value === "None" ? x.noInternet : fill(x.current_internet, { v: f.value });
    case "current_provider":
      return f.value === "None" ? x.noProvider : fill(x.current_provider, { v: f.value });
    default: {
      const template = (x as unknown as Record<string, unknown>)[f.feature];
      return typeof template === "string" ? fill(template, { v: f.value }) : `${f.feature}: ${f.value}`;
    }
  }
}

function Factors({ factors, t }: { factors: Factor[]; t: T }) {
  if (!factors.length) return null;
  return (
    <p className="text-xs text-neutral-600 dark:text-neutral-300" data-testid="factors">
      {t.leads.why}:{" "}
      {factors.map((f, i) => {
        const up = f.effect > 0;
        return (
          <span key={f.feature} className={up ? "text-green-700" : "text-red-700"} title={up ? t.factors.up : t.factors.down}>
            {i > 0 ? " · " : ""}
            <span aria-hidden="true">{up ? "▲" : "▼"}</span>
            <span className="sr-only">{up ? t.factors.up : t.factors.down}: </span> {factorText(f, t)}
          </span>
        );
      })}
    </p>
  );
}

function LastCallLine({ call, t, lang }: { call?: LastCall | null; t: T; lang: StaffLang }) {
  if (!call) return null;
  const when = new Intl.DateTimeFormat(lang === "hi" ? "hi-IN-u-nu-latn" : "en-IN", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(call.at));
  return (
    <p className="text-xs text-neutral-600 dark:text-neutral-300" data-testid="last-call">
      {fill(t.calls.last, { outcome: t.calls.outcomes[call.outcome] ?? call.outcome, when, by: call.by })}
    </p>
  );
}

/** Log how a call went (audited; "not interested" / "wrong number" take the row off the list). */
function CallLog({ kind, refId, t, onDone }: { kind: "LEAD" | "SESSION"; refId: string; t: T; onDone: () => void }) {
  const [outcome, setOutcome] = useState<CallOutcome>("REACHED_INTERESTED");
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    setMessage(null);
    try {
      const res = await bo.logCall(kind, refId, outcome);
      setMessage(res.closed ? t.calls.closed : fill(t.calls.logged, { outcome: t.calls.outcomes[outcome] }));
      onDone();
    } catch {
      setMessage(t.error);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm" data-testid="call-log">
      <label className="flex items-center gap-1">
        {t.calls.label}
        <select
          className="rounded-lg border border-neutral-300 px-2 py-1"
          value={outcome}
          onChange={(e) => setOutcome(e.target.value as CallOutcome)}
        >
          {CALL_OUTCOMES.map((o) => (
            <option key={o} value={o}>
              {t.calls.outcomes[o]}
            </option>
          ))}
        </select>
      </label>
      <button type="button" disabled={busy} className={`${button} border border-neutral-300`} onClick={() => void save()}>
        {t.calls.log}
      </button>
      {message && (
        <span role="status" className="text-neutral-600">
          {message}
        </span>
      )}
    </div>
  );
}

function Score({ score, t }: { score: number | null; t: T }) {
  if (score === null) return null;
  return (
    <span className="rounded-full bg-neutral-100 px-2 py-0.5 text-xs font-semibold dark:bg-neutral-800" title={t.leads.score}>
      {Math.round(score * 100)}%
    </span>
  );
}

/** Masked by default; opening it needs a reason and is written to the audit log. */
function Reveal({ kind, refId, t, defaultReason = "CALL_BACK" }: { kind: RevealKind; refId: string; t: T; defaultReason?: RevealReason }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState<RevealReason>(defaultReason);
  const [fields, setFields] = useState<Record<string, string> | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  if (fields) {
    return (
      <div className="rounded-lg bg-amber-50 p-2 text-sm dark:bg-amber-950" data-testid="revealed">
        <dl className="grid grid-cols-[auto_1fr] gap-x-3">
          {Object.entries(fields).map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-neutral-500">{k.replaceAll("_", " ")}</dt>
              <dd className="font-mono">{v}</dd>
            </div>
          ))}
        </dl>
        <p className="mt-1 text-xs text-neutral-500">{t.reveal.logged}</p>
      </div>
    );
  }
  if (!open) {
    return (
      <button type="button" className={`${button} border border-neutral-300`} onClick={() => setOpen(true)}>
        {t.reveal.button}
      </button>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <label className="flex items-center gap-1">
        {t.reveal.reason}
        <select
          className="rounded-lg border border-neutral-300 px-2 py-1"
          value={reason}
          onChange={(e) => setReason(e.target.value as RevealReason)}
        >
          {REVEAL_REASONS.map((r) => (
            <option key={r} value={r}>
              {t.reveal.reasons[r]}
            </option>
          ))}
        </select>
      </label>
      <button
        type="button"
        className={`${button} bg-amber-600 text-white`}
        onClick={async () => {
          setProblem(null);
          try {
            setFields((await bo.reveal(kind, refId, reason)).fields);
          } catch (e) {
            setProblem(e instanceof ApiError && e.code === "NOTHING_TO_REVEAL" ? t.reveal.nothing : t.error);
          }
        }}
      >
        {t.reveal.confirm}
      </button>
      {problem && <span className="text-red-700">{problem}</span>}
    </div>
  );
}

function Assign({ kind, refId, current, t, onDone }: { kind: "LEAD" | "SESSION"; refId: string; current: string | null; t: T; onDone: () => void }) {
  const [partner, setPartner] = useState(current ?? "");
  const [message, setMessage] = useState<string | null>(null);
  const save = async (value: string | null) => {
    try {
      await bo.assign(kind, refId, value);
      setMessage(value ? fill(t.assign.saved, { partner: value }) : t.assign.cleared);
      onDone();
    } catch (e) {
      setMessage(e instanceof ApiError && e.code ? e.code : t.error);
    }
  };
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <input
        aria-label={t.assign.label}
        placeholder={t.assign.label}
        className="w-36 rounded-lg border border-neutral-300 px-2 py-1"
        value={partner}
        onChange={(e) => setPartner(e.target.value.toUpperCase())}
      />
      <button type="button" className={`${button} border border-neutral-300`} disabled={!partner} onClick={() => void save(partner)}>
        {t.assign.button}
      </button>
      {current && (
        <button type="button" className={`${button} border border-neutral-300`} onClick={() => void save(null)}>
          {t.assign.clear}
        </button>
      )}
      {message && <span className="text-neutral-600">{message}</span>}
    </div>
  );
}

// ------------------------------------------------------------------ tabs

type CallRow =
  | { kind: "lead"; key: string; score: number; lead: LeadRow }
  | { kind: "session"; key: string; score: number; session: SessionRow };

/**
 * One call list: owners who asked for a call first, then historic leads and the app's
 * own unfinished journeys together, by likelihood.
 */
function Leads({ t, me, lang }: { t: T; me: StaffMe; lang: StaffLang }) {
  const [unassigned, setUnassigned] = useState(false);
  const [limit, setLimit] = useState(25);
  const fetcher = useCallback(() => bo.leads({ unassigned, limit, lang }), [unassigned, limit, lang]);
  const { data, problem, reload } = useLoad(fetcher, t);
  const canAssign = me.permissions.includes("leads:assign");

  if (!data) return <Status problem={problem} t={t} />;
  const live = data.sessions ?? [];
  const asRow = (s: SessionRow): CallRow => ({ kind: "session", key: s.session_id, score: s.score ?? 0, session: s });
  const ranked: CallRow[] = [
    ...live.filter((s) => !s.callback_requested_at).map(asRow),
    ...data.leads.map((lead): CallRow => ({ kind: "lead", key: lead.lead_id, score: lead.score ?? 0, lead })),
  ].sort((a, b) => b.score - a.score);
  const rows: CallRow[] = [...live.filter((s) => s.callback_requested_at).map(asRow), ...ranked];
  return (
    <section className="flex flex-col gap-3" data-testid="leads">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-medium">
          {live.length ? fill(t.leads.totalWithApp, { n: data.total, m: live.length }) : fill(t.leads.total, { n: data.total })}
        </p>
        {canAssign && (
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={unassigned} onChange={(e) => setUnassigned(e.target.checked)} />
            {t.leads.unassignedOnly}
          </label>
        )}
      </div>
      {!data.model_available && <p className="text-sm text-amber-700">{t.leads.noModel}</p>}
      {rows.length === 0 && <p>{t.empty}</p>}
      <ul className="flex flex-col gap-3">
        {rows.map((row) =>
          row.kind === "session" ? (
            <SessionCard key={row.key} s={row.session} t={t} lang={lang} canAssign={canAssign} fromApp onChange={() => void reload()} />
          ) : (
            <LeadCard key={row.key} lead={row.lead} t={t} lang={lang} canAssign={canAssign} onChange={() => void reload()} />
          ),
        )}
      </ul>
      {data.total > data.leads.length && (
        <button type="button" className={`${button} self-start border border-neutral-300`} onClick={() => setLimit((n) => Math.min(100, n + 25))}>
          {t.leads.more}
        </button>
      )}
    </section>
  );
}

function LeadCard(props: { lead: LeadRow; t: T; lang: StaffLang; canAssign: boolean; onChange: () => void }) {
  const { lead, t, lang, canAssign, onChange } = props;
  return (
    <li className={`${card} flex flex-col gap-2`} data-testid="lead">
      <div className="flex flex-wrap items-center gap-2">
        <Score score={lead.score} t={t} />
        <span className="font-mono text-xs text-neutral-500">{lead.lead_id}</span>
        <span className="font-semibold">{lead.business_name}</span>
        <span className="text-sm text-neutral-600 dark:text-neutral-300">
          {lead.business_type} · {lead.city} · {lead.lead_source}
        </span>
      </div>
      <p className="text-sm">
        {lead.contact_name} ({lead.contact_role}) · {lead.preferred_language} · {lead.employees} / {lead.sites} · GSTIN{" "}
        {lead.gstin ?? "—"} ({lead.gstin_status})
      </p>
      <p className="text-xs text-neutral-500">
        {fill(t.leads.stage, { stage: lead.stage_reached })}
        {lead.drop_reason ? ` — ${lead.drop_reason}` : ""}
        {lead.assigned_partner ? ` · ${fill(t.leads.assigned, { partner: lead.assigned_partner })}` : ""}
      </p>
      <Factors factors={lead.factors} t={t} />
      <LastCallLine call={lead.last_call} t={t} lang={lang} />
      <div className="flex flex-wrap gap-3">
        <Reveal kind="LEAD" refId={lead.lead_id} t={t} />
        <CallLog kind="LEAD" refId={lead.lead_id} t={t} onDone={onChange} />
        {canAssign && <Assign kind="LEAD" refId={lead.lead_id} current={lead.assigned_partner} t={t} onDone={onChange} />}
      </div>
    </li>
  );
}

function SessionCard(props: {
  s: SessionRow;
  t: T;
  lang: StaffLang;
  canAssign: boolean;
  fromApp?: boolean;
  onChange: () => void;
}) {
  const { s, t, lang, canAssign, fromApp, onChange } = props;
  const reason = (code: string) => t.sessions.reasons[code] ?? code;
  const detail = s.detail ?? {};
  const profile = [
    s.profile.business_type,
    s.profile.city,
    s.profile.employee_count ? fill(t.factors.employees, { v: String(s.profile.employee_count) }) : null,
  ].filter(Boolean);
  return (
    <li
      className={`${card} flex flex-col gap-2 ${s.callback_requested_at ? "ring-2 ring-red-700" : ""}`}
      data-testid="session"
      data-callback={s.callback_requested_at ? "true" : undefined}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Score score={s.score} t={t} />
        {fromApp && <span className="rounded-full bg-red-50 px-2 py-0.5 text-xs font-medium text-red-800 dark:bg-red-950 dark:text-red-100">{t.leads.fromApp}</span>}
        <span className="font-semibold" data-testid="stall">{reason(s.stall_reason)}</span>
        <span className="text-xs text-neutral-500">
          {fill(t.sessions.idle, { n: s.idle_minutes })} · {s.locale}
        </span>
      </div>
      {s.callback_requested_at && s.stuck_at && (
        <p className="text-sm">{fill(t.sessions.stuckAt, { reason: reason(s.stuck_at) })}</p>
      )}
      {profile.length > 0 && <p className="text-sm text-neutral-600 dark:text-neutral-300">{profile.join(" · ")}</p>}
      {s.monthly_total_paise !== null && (
        <p className="text-sm" data-testid="saw-price">
          {fill(t.sessions.sawPrice, { amount: formatPaise(s.monthly_total_paise) })}
        </p>
      )}
      {s.at_question && (
        <p className="text-xs text-neutral-500">{fill(t.sessions.atQuestion, { slot: t.slots[s.at_question] ?? s.at_question })}</p>
      )}
      {detail.kyc_fixes && detail.kyc_fixes.length > 0 && (
        <div className="rounded-lg bg-amber-50 p-2 text-sm text-amber-950 dark:bg-amber-950 dark:text-amber-50" data-testid="kyc-detail">
          <ul className="list-disc ps-5">
            {detail.kyc_fixes.map((fix) => (
              <li key={fix}>{fix}</li>
            ))}
          </ul>
          {detail.kyc_attempts ? <p className="text-xs">{fill(t.sessions.kycAttempts, { n: detail.kyc_attempts })}</p> : null}
        </div>
      )}
      {detail.failed_payments ? (
        <p className="text-sm" data-testid="payment-detail">
          {fill(t.sessions.failedPayments, { n: detail.failed_payments })}
        </p>
      ) : null}
      <Factors factors={s.factors} t={t} />
      {s.basis && (
        <p className="text-xs text-neutral-500" data-testid="basis">
          {fill(t.sessions.basis, { n: s.basis.n, pct: Math.round(s.basis.rate * 100) })}
        </p>
      )}
      <LastCallLine call={s.last_call} t={t} lang={lang} />
      <div className="flex flex-wrap items-center gap-3">
        {s.contactable ? (
          <>
            <Reveal kind="SESSION" refId={s.session_id} t={t} />
            <CallLog kind="SESSION" refId={s.session_id} t={t} onDone={onChange} />
          </>
        ) : (
          <span className="text-xs text-neutral-500">{t.sessions.anonymous}</span>
        )}
        {canAssign && <Assign kind="SESSION" refId={s.session_id} current={s.assigned_partner} t={t} onDone={onChange} />}
      </div>
    </li>
  );
}

function Sessions({ t, me, lang }: { t: T; me: StaffMe; lang: StaffLang }) {
  const fetcher = useCallback(() => bo.sessions(undefined, lang), [lang]);
  const { data, problem, reload } = useLoad(fetcher, t);
  const canAssign = me.permissions.includes("leads:assign");
  if (!data) return <Status problem={problem} t={t} />;
  return (
    <section className="flex flex-col gap-3" data-testid="sessions">
      {data.sessions.length === 0 && <p>{t.empty}</p>}
      <ul className="flex flex-col gap-3">
        {data.sessions.map((s: SessionRow) => (
          <SessionCard key={s.session_id} s={s} t={t} lang={lang} canAssign={canAssign} onChange={() => void reload()} />
        ))}
      </ul>
    </section>
  );
}

function Review({ t }: { t: T }) {
  const fetcher = useCallback(() => bo.reviewQueue(), []);
  const { data, problem, reload } = useLoad(fetcher, t);
  if (!data) return <Status problem={problem} t={t} />;
  return (
    <section className="flex flex-col gap-3" data-testid="review">
      {data.items.length === 0 && <p>{t.empty}</p>}
      <ul className="flex flex-col gap-3">
        {data.items.map((item) => (
          <ReviewCard key={item.item_id} item={item} t={t} onDone={() => void reload()} />
        ))}
      </ul>
    </section>
  );
}

function ReviewCard({ item, t, onDone }: { item: ReviewItem; t: T; onDone: () => void }) {
  const [note, setNote] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const decide = async (approve: boolean) => {
    setBusy(true);
    try {
      const res = await bo.decide(item.item_id, approve, note.trim());
      setMessage(fill(t.review.done, { status: res.application_status }));
      setTimeout(onDone, 800);
    } catch {
      setMessage(t.error);
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className={`${card} flex flex-col gap-2`} data-testid="review-item">
      <p className="font-semibold">{t.review.kinds[item.kind]}</p>
      <p className="text-sm">{item.reasons.join(", ")}</p>
      <p className="text-xs text-neutral-500">
        {fill(t.review.attempts, { n: item.failed_attempts })} · {item.application_status}
        {item.order_state ? ` · ${item.order_state}` : ""}
      </p>
      {item.checks.length > 0 && (
        <ul className="text-xs text-neutral-600 dark:text-neutral-300">
          {item.checks.map((c) => (
            <li key={c.check_id}>
              {c.status} · {c.reason ?? c.check_id}
              {Object.keys(c.evidence).length ? ` · ${Object.entries(c.evidence).map(([k, v]) => `${k}: ${v}`).join(", ")}` : ""}
            </li>
          ))}
        </ul>
      )}
      <Reveal kind="KYC" refId={item.application_id} t={t} defaultReason="KYC_REVIEW" />
      <label className="flex flex-col gap-1 text-sm">
        {t.review.note}
        <textarea className="rounded-lg border border-neutral-300 px-2 py-1" rows={2} maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} />
      </label>
      <div className="flex gap-2">
        {item.kind === "REVIEW" ? (
          <>
            <button type="button" disabled={busy || note.trim().length < 3} className={`${button} bg-green-700 text-white`} onClick={() => void decide(true)}>
              {t.review.approve}
            </button>
            <button type="button" disabled={busy || note.trim().length < 3} className={`${button} bg-red-700 text-white`} onClick={() => void decide(false)}>
              {t.review.reject}
            </button>
          </>
        ) : (
          <button type="button" disabled={busy || note.trim().length < 3} className={`${button} bg-neutral-800 text-white`} onClick={() => void decide(true)}>
            {t.review.handled}
          </button>
        )}
      </div>
      {message && <p className="text-sm" role="status">{message}</p>}
    </li>
  );
}

function Sla({ t, me }: { t: T; me: StaffMe }) {
  const fetcher = useCallback(() => bo.sla(), []);
  const { data, problem, reload } = useLoad(fetcher, t);
  const canFulfil = me.permissions.includes("orders:fulfil");
  if (!data) return <Status problem={problem} t={t} />;
  return (
    <section className="flex flex-col gap-3" data-testid="sla">
      {data.orders.length === 0 && <p>{t.empty}</p>}
      <ul className="flex flex-col gap-3">
        {data.orders.map((o: SlaRow) => (
          <li key={o.order_id} className={`${card} flex flex-col gap-2`} data-testid="sla-order">
            <p className="font-semibold">
              <span className="font-mono text-xs text-neutral-500">{o.order_id.slice(0, 8).toUpperCase()}</span> {o.state} ·{" "}
              <span className="text-red-700">{fill(t.sla.age, { n: o.age_days })}</span>
            </p>
            {o.callback_requested_at && (
              <p className="self-start rounded bg-amber-100 px-2 py-0.5 text-sm font-medium text-amber-900 dark:bg-amber-900 dark:text-amber-100" data-testid="sla-call-me">
                {t.sla.callMe}
              </p>
            )}
            <p className="text-sm">{o.products.join(", ")}</p>
            <p className="text-xs text-neutral-500">{fill(t.sla.due, { date: o.sla_due })}</p>
            <div className="flex flex-wrap gap-3">
              <Reveal kind="ORDER" refId={o.order_id} t={t} defaultReason="FULFILMENT" />
              {canFulfil && NEXT_STEP[o.state] && (
                <button
                  type="button"
                  className={`${button} border border-neutral-300`}
                  onClick={async () => {
                    await bo.fulfil(o.order_id, NEXT_STEP[o.state]);
                    void reload();
                  }}
                >
                  {fill(t.sla.advance, { state: NEXT_STEP[o.state] })}
                </button>
              )}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Model({ t }: { t: T }) {
  const fetcher = useCallback(() => bo.model(), []);
  const { data, problem } = useLoad<ModelView>(fetcher, t);
  if (!data) return <Status problem={problem} t={t} />;
  if (!data.available || !data.metrics) return <p>{t.leads.noModel}</p>;
  const m = data.metrics;
  return (
    <section className={`${card} flex flex-col gap-3`} data-testid="model">
      <div className="grid grid-cols-2 gap-3">
        <div>
          <p className="text-xs text-neutral-500">{t.model.auc}</p>
          <p className="text-2xl font-semibold" data-testid="model-auc">{m.auc.toFixed(3)}</p>
          <p className="text-xs text-neutral-500">
            {fill(t.model.baselines, { random: m.baseline_random_auc.toFixed(2), gstin: m.baseline_gstin_auc.toFixed(3) })}
          </p>
        </div>
        <div>
          <p className="text-xs text-neutral-500">{t.model.lift}</p>
          <p className="text-2xl font-semibold">{m.lift_top_decile.toFixed(2)}×</p>
        </div>
      </div>
      <p className="text-xs text-neutral-500">{fill(t.model.rows, { train: m.train_rows, test: m.test_rows })}</p>
      <h3 className="text-sm font-semibold">{t.model.coefficients}</h3>
      <ul className="text-sm">
        {(data.top_coefficients ?? []).slice(0, 10).map((c) => (
          <li key={c.column} className="flex justify-between gap-3">
            <span>{c.column.replace("=", " = ").replaceAll("_", " ")}</span>
            <span className={`font-mono ${c.coefficient > 0 ? "text-green-700" : "text-red-700"}`}>
              {c.coefficient > 0 ? "+" : ""}
              {c.coefficient.toFixed(2)}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
