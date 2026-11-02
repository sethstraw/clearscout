import type {
  PendingDiscovery,
  PendingDiscoveryFacets,
  RelevanceReview,
  RelevanceReviewRejection,
} from "../../api/client";

type RelevanceReviewRecommendation = NonNullable<RelevanceReview["recommendation"]>;
type RelevanceReviewNonVerdict = Exclude<RelevanceReview["outcome"], "judged">;
export type DiscoveryRecommendation = PendingDiscovery["recommendation"];

const RELEVANCE_REVIEW_LABELS: Record<RelevanceReviewRecommendation, string> = {
  review: "High relevance",
  investigate: "Needs closer look",
  unlikely: "Low relevance",
  insufficient_evidence: "Insufficient evidence",
};

const DISCOVERY_RECOMMENDATION_LABELS: Record<DiscoveryRecommendation, string> = {
  review: "Review",
  investigate: "Investigate",
  unlikely: "Unlikely",
};

// A row is reviewed with a verdict, reviewed without one, disputed by the
// person, rejected after a paid call, or not yet reviewed; all are one triage
// vocabulary so the queue can badge, order, and filter them.
export type RelevanceKey = PendingDiscoveryFacets["relevance"][number]["value"];

// Queue order: strong matches, then the ones needing a look, then verdicts the
// person set aside, then the rows a paid review left without one, then the
// unreviewed backlog, then the weak verdicts.
export const RELEVANCE_ORDER: readonly RelevanceKey[] = [
  "review",
  "investigate",
  "disputed",
  "review_rejected",
  "not_reviewed",
  "insufficient_posting_text",
  "insufficient_evidence",
  "unlikely",
];

// Each tone is the suffix of a shared .status--tone-* pill class.
const RELEVANCE_TONE: Record<RelevanceKey, string> = {
  review: "positive",
  investigate: "warning",
  disputed: "warning",
  review_rejected: "warning",
  not_reviewed: "muted",
  insufficient_posting_text: "neutral",
  insufficient_evidence: "neutral",
  unlikely: "muted",
};

type RelevanceNonVerdictKey =
  RelevanceReviewNonVerdict | "not_reviewed" | "disputed" | "review_rejected";

const NON_VERDICT_LABELS: Record<RelevanceNonVerdictKey, string> = {
  not_reviewed: "Not yet reviewed",
  disputed: "Disputed by you",
  review_rejected: "Paid review returned nothing usable",
  insufficient_posting_text: "Posting text too short to review",
};

const REJECTION_REASON_LABELS: Record<RelevanceReviewRejection["failure_code"], string> = {
  missing_result: "the model answered nothing for this row",
  duplicate_result: "the model answered this row more than once",
  wrong_posting_span: "the answer cited posting text this row was never sent",
  invalid_cv_evidence: "the answer cited CV evidence outside your selected CV",
  inconsistent_verdict: "the answer's classes and recommendation contradict each other",
  leading_excerpt_only: "the answer read only the opening of the posting",
};

/** Why the paid review recorded nothing for this row, in a person's words. */
export function rejectionReasonLabel(rejection: RelevanceReviewRejection): string {
  return REJECTION_REASON_LABELS[rejection.failure_code];
}

/** Whether the newest paid review was rejected after the stored verdict was recorded. */
export function rejectionFollowsReview(
  review: RelevanceReview | undefined,
  rejection: RelevanceReviewRejection,
): boolean {
  if (!review) return true;
  return Date.parse(rejection.rejected_at) > Date.parse(review.reviewed_at);
}

/** What the model recorded, read without regard to a dispute of it. */
export function modelRelevanceKey(review: RelevanceReview): RelevanceKey {
  return review.outcome === "judged" && review.recommendation
    ? review.recommendation
    : "insufficient_posting_text";
}

export function discoveryRelevanceKey(
  review: RelevanceReview | undefined,
  rejection?: RelevanceReviewRejection | null,
): RelevanceKey {
  if (!review) return rejection ? "review_rejected" : "not_reviewed";
  return review.disputed_at ? "disputed" : modelRelevanceKey(review);
}

export function relevanceLabel(key: RelevanceKey): string {
  return key === "not_reviewed" ||
    key === "disputed" ||
    key === "review_rejected" ||
    key === "insufficient_posting_text"
    ? NON_VERDICT_LABELS[key]
    : RELEVANCE_REVIEW_LABELS[key];
}

export function relevanceTone(key: RelevanceKey): string {
  return RELEVANCE_TONE[key];
}

export function relevanceRank(key: RelevanceKey): number {
  return RELEVANCE_ORDER.indexOf(key);
}

export type StaleInput = RelevanceReview["out_of_date_because"][number];

export const STALE_INPUT_ORDER: readonly StaleInput[] = [
  "master_cv",
  "search_parameters",
  "review_instructions",
  "keep_note",
];

const STALE_INPUT_LABELS: Record<StaleInput, string> = {
  master_cv: "Master CV changed",
  search_parameters: "Search parameters changed",
  review_instructions: "Review instructions changed",
  keep_note: "Your keep note changed",
};

// Each tone is the suffix of a shared .status--tone-* pill class.
const STALE_INPUT_TONE: Record<StaleInput, string> = {
  master_cv: "accent",
  search_parameters: "info",
  review_instructions: "neutral",
  keep_note: "warning",
};

/** Whether the server named any input that changed since the review ran. */
export function relevanceReviewIsOutOfDate(review: RelevanceReview): boolean {
  return review.out_of_date_because.length > 0;
}

export function staleInputLabel(input: StaleInput): string {
  return STALE_INPUT_LABELS[input];
}

export function staleInputTone(input: StaleInput): string {
  return STALE_INPUT_TONE[input];
}

export function discoveryRecommendationLabel(recommendation: DiscoveryRecommendation): string {
  return DISCOVERY_RECOMMENDATION_LABELS[recommendation];
}

export function relevanceReviewsByPending(
  reviews: readonly RelevanceReview[],
): Map<string, RelevanceReview> {
  return new Map(reviews.map((review) => [review.pending_discovery_id, review]));
}
