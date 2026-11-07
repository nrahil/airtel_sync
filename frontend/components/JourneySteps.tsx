import Link from "next/link";

import { type Messages, fill } from "@/lib/i18n";

/** Where the owner is in the whole journey: plan → number → documents → pay. */
export type JourneyStep = 1 | 2 | 3 | 4;

/**
 * `links`: earlier steps the owner may go back to (only steps already done are linked).
 */
export function JourneySteps({
  t,
  current,
  links = {},
}: {
  t: Messages;
  current: JourneyStep;
  links?: Partial<Record<JourneyStep, string>>;
}) {
  const labels = [t.journey.plan, t.journey.signIn, t.journey.documents, t.journey.pay];
  return (
    <nav aria-label={t.journey.label} data-testid="journey-steps" data-step={current} className="flex flex-col gap-1.5">
      <ol className="grid grid-cols-4 gap-1.5">
        {labels.map((label, i) => {
          const step = i + 1;
          const state = step < current ? "done" : step === current ? "current" : "todo";
          const href = state === "done" ? links[step as JourneyStep] : undefined;
          const bar = (
            <span
              className={`h-1.5 rounded-full ${state === "todo" ? "bg-neutral-200 dark:bg-neutral-700" : "bg-red-700"}`}
            />
          );
          return (
            <li key={label} aria-current={state === "current" ? "step" : undefined} className="flex flex-col gap-1">
              {href ? (
                // A step already done: tap it to go back (a full-height target, not just the label).
                <Link href={href} data-testid={`journey-back-${step}`} className="flex flex-col gap-1 rounded py-1 -my-1">
                  {bar}
                  <span className="truncate text-xs text-red-800 underline dark:text-red-200">
                    ← {label}
                  </span>
                </Link>
              ) : (
                <>
                  {bar}
                  <span className={`truncate text-xs ${state === "current" ? "font-semibold" : "text-neutral-500"}`}>
                    {label}
                  </span>
                </>
              )}
            </li>
          );
        })}
      </ol>
      <p className="sr-only">{fill(t.journey.step, { step: current, total: labels.length })}</p>
    </nav>
  );
}
