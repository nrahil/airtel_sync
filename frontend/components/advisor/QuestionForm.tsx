"use client";

import { useState } from "react";

import type { Question } from "@/lib/api";
import type { Messages } from "@/lib/i18n";

/** Guided-form input for one slot. Labels come from the locale catalogue, keyed by option code. */
export function QuestionForm({
  question,
  t,
  disabled,
  onAnswer,
}: {
  question: Question;
  t: Messages;
  disabled: boolean;
  onAnswer: (slot: string, value: unknown, label: string) => void;
}) {
  const [text, setText] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const labels = labelsFor(question.slot, t);
  const label = (code: string) => labels[code] ?? code;
  const button =
    "min-h-12 rounded-xl border border-neutral-300 px-4 py-2 text-left text-base hover:border-red-700 disabled:opacity-50 dark:border-neutral-600";

  switch (question.kind) {
    case "choice":
      return (
        <div role="group" className="grid gap-2" data-testid={`form-${question.slot}`}>
          {question.options.map((code) => (
            <button key={code} type="button" disabled={disabled} className={button} onClick={() => onAnswer(question.slot, code, label(code))}>
              {label(code)}
            </button>
          ))}
        </div>
      );
    case "yes_no":
      return (
        <div role="group" className="grid grid-cols-2 gap-2" data-testid={`form-${question.slot}`}>
          <button type="button" disabled={disabled} className={button} onClick={() => onAnswer(question.slot, true, t.advisor.yes)}>
            {t.advisor.yes}
          </button>
          <button type="button" disabled={disabled} className={button} onClick={() => onAnswer(question.slot, false, t.advisor.no)}>
            {t.advisor.no}
          </button>
        </div>
      );
    case "multi":
      return (
        <div className="grid gap-2" data-testid={`form-${question.slot}`}>
          <p className="text-sm text-neutral-500">{t.advisor.selectAny}</p>
          <div className="flex flex-wrap gap-2">
            {question.options.map((code) => {
              const on = picked.includes(code);
              return (
                <button
                  key={code}
                  type="button"
                  aria-pressed={on}
                  disabled={disabled}
                  onClick={() => setPicked((p) => (on ? p.filter((c) => c !== code) : [...p, code]))}
                  className={`rounded-full border px-3 py-1.5 text-sm ${on ? "border-red-700 bg-red-50 text-red-800 dark:bg-red-950 dark:text-red-200" : "border-neutral-300 dark:border-neutral-600"}`}
                >
                  {label(code)}
                </button>
              );
            })}
          </div>
          <button
            type="button"
            disabled={disabled}
            className="h-12 rounded-xl bg-red-700 font-medium text-white disabled:opacity-50"
            onClick={() =>
              onAnswer(question.slot, picked, picked.length ? picked.map(label).join(", ") : t.advisor.noneOfThese)
            }
          >
            {picked.length ? t.advisor.next : t.advisor.noneOfThese}
          </button>
        </div>
      );
    case "number":
    case "text": {
      const isNumber = question.kind === "number";
      const value = text.trim();
      const parsed = Number(value);
      const valid = isNumber
        ? Number.isInteger(parsed) && parsed >= (question.min ?? 1) && parsed <= (question.max ?? 1e6)
        : value.length >= 2;
      return (
        <form
          className="flex gap-2"
          data-testid={`form-${question.slot}`}
          onSubmit={(e) => {
            e.preventDefault();
            if (valid) onAnswer(question.slot, isNumber ? parsed : value, value);
          }}
        >
          <input
            aria-label={isNumber ? t.advisor.numberPlaceholder : t.advisor.cityPlaceholder}
            inputMode={isNumber ? "numeric" : "text"}
            type={isNumber ? "number" : "text"}
            min={question.min ?? undefined}
            max={question.max ?? undefined}
            list={question.suggestions ? `suggest-${question.slot}` : undefined}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={isNumber ? t.advisor.numberPlaceholder : t.advisor.cityPlaceholder}
            className="h-12 flex-1 rounded-xl border border-neutral-300 px-3 text-base dark:border-neutral-600 dark:bg-neutral-900"
          />
          {question.suggestions && (
            <datalist id={`suggest-${question.slot}`}>
              {question.suggestions.map((s) => (
                <option key={s} value={s} />
              ))}
            </datalist>
          )}
          <button type="submit" disabled={disabled || !valid} className="h-12 rounded-xl bg-red-700 px-4 font-medium text-white disabled:opacity-50">
            {t.advisor.next}
          </button>
        </form>
      );
    }
  }
}

function labelsFor(slot: string, t: Messages): Record<string, string> {
  if (slot === "segment") return t.segment;
  if (slot === "current_internet") return t.internet;
  if (slot === "needs") return t.needs;
  return {};
}
