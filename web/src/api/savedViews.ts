/**
 * What a saved view is allowed to say about the jobs ledger.
 *
 * Saved-view JSON is interpreted against the ledger's filter and sort vocabulary.
 */
import type { SavedView } from "./client";

/** Every column the ledger will sort by, and so every key a view may name. */
const sortKeys = [
  "employer",
  "title",
  "decision",
  "fit_score",
  "compensation_score",
  "eligibility_score",
  "evidence_score",
  "opportunity_score",
  "recorded_score",
  "overall_score",
  "application_status",
  "completeness",
  "first_seen_at",
  "evaluated_at",
  "manual_priority",
  "manual_fit",
] as const;

const legacyScoreSorts = {
  recorded_fit_score: "fit_score",
  recorded_compensation_score: "compensation_score",
  tracker_composite_score: "recorded_score",
} as const;

export type JobSort = (typeof sortKeys)[number];

/** The filter keys a view may set, named for the reader of the tooltip. */
export const VIEW_KEYS =
  "search, decision, status, completeness, place, employer, posting_status, include_archived";

/** The filters of a view, or nothing when it stored a shape with no keys. */
export function viewFilters(view: SavedView): Record<string, unknown> {
  const filters = view.configuration.filters;
  return filters && !Array.isArray(filters) ? filters : {};
}

/** One filter as text. A key that is absent or non-text clears its filter. */
export function viewText(filters: Record<string, unknown>, key: string): string {
  const value = filters[key];
  return typeof value === "string" ? value : "";
}

/** One filter as a list of text. A lone text value is a list of one; anything else clears it. */
export function viewTextList(filters: Record<string, unknown>, key: string): string[] {
  const value = filters[key];
  if (typeof value === "string") return value ? [value] : [];
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && item !== "")
    : [];
}

/** The sort a view asks for, or null when it names none the ledger can obey. */
export function viewSort(
  sort: SavedView["configuration"]["sort"],
): { key: JobSort; descending: boolean } | null {
  // The form may store a list, but the ledger supports one sort column.
  const clause = Array.isArray(sort) ? sort[0] : sort;
  if (!clause || typeof clause !== "object") return null;
  const record = clause as Record<string, unknown>;
  const key = record.key;
  if (typeof key !== "string") return null;
  const normalized =
    key in legacyScoreSorts ? legacyScoreSorts[key as keyof typeof legacyScoreSorts] : key;
  if (!(sortKeys as readonly string[]).includes(normalized)) return null;
  return { key: normalized as JobSort, descending: record.descending !== false };
}
