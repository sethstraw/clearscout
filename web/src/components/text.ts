export function titleCase(value: string): string {
  return value.replaceAll(/[-_]/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

const EMPLOYMENT_TYPE_LABELS: Record<string, string> = {
  "full time": "Full time",
  "part time": "Part time",
  contract: "Contract",
  contractor: "Contract",
  temporary: "Temporary",
  temp: "Temporary",
  intern: "Internship",
  internship: "Internship",
};

/** A stored employment type in words, whatever its case, separators, or run-together form. */
export function employmentTypeLabel(value: string): string {
  const words = value
    .trim()
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/[\s_-]+/g, " ")
    .toLowerCase();
  const known = EMPLOYMENT_TYPE_LABELS[words];
  if (known) return known;
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** The phrase for an unstatable amount in a table cell, where the column is already labelled. */
const NO_PRICE = "no price";

/** The same fact in a sentence, where "no price" would attach to the wrong words. */
export const UNKNOWN_AMOUNT = "unknown";

/** Format a recorded USD amount, or say why there is no figure. */
export function formatUsd(
  amount: number | string | null | undefined,
  {
    places = 4,
    unknown = NO_PRICE,
    cents = false,
  }: { places?: number; unknown?: string; cents?: boolean } = {},
): string {
  if (amount === null || amount === undefined) return unknown;
  return `$${(cents ? Number(amount) / 100 : Number(amount)).toFixed(places)}`;
}
