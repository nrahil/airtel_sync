/**
 * Money is integer paise everywhere (backend pricing engine). This is the only place
 * it becomes text, always with Indian digit grouping (₹1,23,456). Never format a
 * number that came from LLM text — prices come from the priced data only.
 */
export function formatPaise(paise: number, locale = "en-IN"): string {
  if (!Number.isSafeInteger(paise)) throw new Error("paise must be an integer");
  return new Intl.NumberFormat(locale, {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
    minimumFractionDigits: 0,
    numberingSystem: "latn",
  }).format(Math.round(paise / 100));
}

/**
 * `to - from` in whole rupees, as the owner sees them on screen (each total rounded
 * first). So "+₹353/month" next to a toggle always matches the two totals shown.
 */
export function rupeeDifference(fromPaise: number, toPaise: number): number {
  return Math.round(toPaise / 100) - Math.round(fromPaise / 100);
}
