"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { QuestionForm } from "@/components/advisor/QuestionForm";
import { RecommendationCard } from "@/components/advisor/RecommendationCard";
import { VoiceButton } from "@/components/advisor/VoiceButton";
import {
  type AssistantMessage,
  type Question,
  type Recommendation,
  type TurnResponse,
  sendAnswer,
  sendText,
  startSession,
} from "@/lib/api";
import { type Locale, type Messages, bcp47 } from "@/lib/i18n";

type Entry = AssistantMessage | { role: "user"; kind: "text"; text: string };

interface Snapshot {
  sessionId: string;
  mode: "chat" | "form";
  entries: AssistantMessage[]; // assistant turns only: the owner's own words are never stored
  question: Question | null;
  recommendation: Recommendation | null;
}

const storageKey = (locale: Locale) => `udyam.advisor.${locale}`;

export function Advisor({ locale, t }: { locale: Locale; t: Messages }) {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [mode, setMode] = useState<"chat" | "form">("chat");
  const [entries, setEntries] = useState<Entry[]>([]);
  const [question, setQuestion] = useState<Question | null>(null);
  const [recommendation, setRecommendation] = useState<Recommendation | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  const apply = useCallback(
    (res: TurnResponse, userText?: string) => {
      setSessionId(res.session_id);
      setMode(res.mode);
      setQuestion(res.question);
      setRecommendation(res.recommendation);
      setEntries((prev) => {
        const next: Entry[] = [...prev];
        if (userText) next.push({ role: "user", kind: "text", text: userText });
        next.push(...res.messages);
        const snapshot: Snapshot = {
          sessionId: res.session_id,
          mode: res.mode,
          entries: next.filter((e): e is AssistantMessage => e.role === "assistant"),
          question: res.question,
          recommendation: res.recommendation,
        };
        sessionStorage.setItem(storageKey(locale), JSON.stringify(snapshot));
        return next;
      });
    },
    [locale],
  );

  const run = useCallback(async (call: () => Promise<TurnResponse>, userText?: string) => {
    setBusy(true);
    setError(false);
    try {
      apply(await call(), userText);
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  }, [apply]);

  const start = useCallback(
    (startMode: "chat" | "form") => {
      setEntries([]);
      setRecommendation(null);
      sessionStorage.removeItem(storageKey(locale));
      void run(() => startSession(locale, startMode));
    },
    [locale, run],
  );

  useEffect(() => {
    const saved = sessionStorage.getItem(storageKey(locale));
    if (saved) {
      const snap = JSON.parse(saved) as Snapshot;
      setSessionId(snap.sessionId);
      setMode(snap.mode);
      setEntries(snap.entries);
      setQuestion(snap.question);
      setRecommendation(snap.recommendation);
      return;
    }
    start("chat");
  }, [locale, start]);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ behavior: "smooth", block: "end" });
  }, [entries, recommendation]);

  const submitText = (event: React.FormEvent) => {
    event.preventDefault();
    const text = draft.trim();
    if (!text || !sessionId || busy) return;
    setDraft("");
    void run(() => sendText(sessionId, text), text);
  };

  const answer = (slot: string, value: unknown, label: string) => {
    if (!sessionId || busy) return;
    void run(() => sendAnswer(sessionId, slot, value), label);
  };

  return (
    <section className="flex flex-col gap-4" aria-live="polite">
      <ol className="flex flex-col gap-2" data-testid="transcript">
        {entries.map((entry, i) => (
          <li
            key={i}
            data-kind={entry.kind}
            className={
              entry.role === "user"
                ? "ml-auto max-w-[85%] rounded-2xl rounded-br-sm bg-red-700 px-4 py-2 text-white"
                : entry.kind === "notice"
                  ? "max-w-[90%] rounded-xl bg-neutral-100 px-3 py-2 text-sm text-neutral-700 dark:bg-neutral-800 dark:text-neutral-200"
                  : "max-w-[90%] rounded-2xl rounded-bl-sm bg-white px-4 py-2 shadow-sm ring-1 ring-neutral-200 dark:bg-neutral-900 dark:ring-neutral-700"
            }
          >
            {/* Plain text only: React escapes it; LLM output is never rendered as HTML. */}
            {entry.text}
          </li>
        ))}
      </ol>

      {busy && <p className="text-sm text-neutral-500">{t.advisor.thinking}</p>}
      {error && (
        <p role="alert" className="text-sm text-red-700">
          {t.advisor.error}
        </p>
      )}

      {recommendation && (
        <RecommendationCard recommendation={recommendation} locale={locale} t={t} />
      )}

      {question && !recommendation && (
        <>
          {mode === "form" ? (
            <QuestionForm key={question.slot} question={question} t={t} disabled={busy} onAnswer={answer} />
          ) : (
            <>
              <form onSubmit={submitText} className="flex items-end gap-2">
                <label className="sr-only" htmlFor="advisor-input">
                  {t.advisor.placeholder}
                </label>
                <textarea
                  id="advisor-input"
                  rows={2}
                  maxLength={500}
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  placeholder={t.advisor.placeholder}
                  lang={bcp47(locale)}
                  className="min-h-12 flex-1 resize-none rounded-xl border border-neutral-300 px-3 py-2 text-base dark:border-neutral-600 dark:bg-neutral-900"
                />
                <VoiceButton locale={locale} t={t} onText={(text) => setDraft((d) => `${d} ${text}`.trim())} />
                <button
                  type="submit"
                  disabled={busy || !draft.trim()}
                  className="h-12 rounded-xl bg-red-700 px-4 font-medium text-white disabled:opacity-50"
                >
                  {t.advisor.send}
                </button>
              </form>
              <QuestionShortcut question={question} t={t} disabled={busy} onAnswer={answer} />
            </>
          )}
        </>
      )}

      <button
        type="button"
        onClick={() => start(mode === "form" ? "form" : "chat")}
        className="self-start text-sm text-neutral-500 underline"
      >
        {t.advisor.restart}
      </button>
      <div ref={endRef} />
    </section>
  );
}

/** In chat mode, offer the structured options for the current question too. */
function QuestionShortcut(props: {
  question: Question;
  t: Messages;
  disabled: boolean;
  onAnswer: (slot: string, value: unknown, label: string) => void;
}) {
  const [open, setOpen] = useState(false);
  if (!open) {
    return (
      <button type="button" onClick={() => setOpen(true)} className="self-start text-sm text-red-700 underline">
        {props.t.advisor.useForm}
      </button>
    );
  }
  return <QuestionForm key={props.question.slot} {...props} />;
}
