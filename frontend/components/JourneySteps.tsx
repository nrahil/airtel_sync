import { type Messages, fill } from "@/lib/i18n";

/** Where the owner is in the whole journey: plan → number → documents → pay. */
export type JourneyStep = 1 | 2 | 3 | 4;

export function JourneySteps({ t, current }: { t: Messages; current: JourneyStep }) {
  const labels = [t.journey.plan, t.journey.signIn, t.journey.documents, t.journey.pay];
  return (
    <nav aria-label={t.journey.label} data-testid="journey-steps" data-step={current} className="flex flex-col gap-1.5">
      <ol className="grid grid-cols-4 gap-1.5">
        {labels.map((label, i) => {
          const step = i + 1;
          const state = step < current ? "done" : step === current ? "current" : "todo";
          return (
            <li key={label} aria-current={state === "current" ? "step" : undefined} className="flex flex-col gap-1">
              <span
                className={`h-1.5 rounded-full ${
                  state === "todo" ? "bg-neutral-200 dark:bg-neutral-700" : "bg-red-700"
                }`}
              />
              <span
                className={`truncate text-xs ${
                  state === "current" ? "font-semibold" : "text-neutral-500"
                }`}
              >
                {label}
              </span>
            </li>
          );
        })}
      </ol>
      <p className="sr-only">{fill(t.journey.step, { step: current, total: labels.length })}</p>
    </nav>
  );
}
