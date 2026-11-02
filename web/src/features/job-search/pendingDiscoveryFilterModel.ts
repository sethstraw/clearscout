import type {
  PendingDiscovery,
  PendingDiscoveryFacets,
  PendingDiscoveryReadiness,
  PendingQueueQuery,
} from "../../api/client";
import { CONSTRAINT_CONFIDENCE_ROUTES, type ConstraintConfidence } from "./constraintConfidence";
import {
  STALE_INPUT_ORDER,
  type DiscoveryRecommendation,
  type RelevanceKey,
} from "./relevanceReviewModel";

export type Category = "all" | DiscoveryRecommendation;
export type RelevanceReviewFilter = "all" | RelevanceKey;
export type ReviewStaleness = PendingDiscoveryFacets["review_status"][number]["value"];
export type ReviewStalenessFilter = "all" | ReviewStaleness;
export type QueueSortMode = "relevance" | "rank";
export type QueuePageSize = "50" | "100" | "all";
export type ConstraintFilter = "all" | NonNullable<PendingQueueQuery["constraint"]>;
export type ContentReadiness = PendingDiscoveryReadiness;
export type ContentReadinessFilter = "all" | ContentReadiness;
export type ArrangementCategory = PendingDiscovery["arrangement_categories"][number];
export type ArrangementFilter = "all" | ArrangementCategory;
export type StagingProvenance = PendingDiscovery["staging_provenance"];
export type StagingProvenanceFilter = "all" | StagingProvenance;
export type Marker = "favorite" | "kept";
/** The marker chips a person has pressed; none pressed shows every row. */
type MarkerFilter = Readonly<Record<Marker, boolean>>;
const NO_MARKER_FILTER: MarkerFilter = { favorite: false, kept: false };

/** What the queue toolbar has chosen; the server applies all of it before paging. */
export type PendingQueueFilters = Readonly<{
  arrangement: ArrangementFilter;
  category: Category;
  constraint: ConstraintFilter;
  employers: readonly string[];
  markers: MarkerFilter;
  provenance: StagingProvenanceFilter;
  readiness: ContentReadinessFilter;
  relevance: RelevanceReviewFilter;
  reviewStatus: ReviewStalenessFilter;
  search: string;
}>;

export const UNFILTERED_QUEUE: PendingQueueFilters = {
  arrangement: "all",
  category: "all",
  constraint: "all",
  employers: [],
  markers: NO_MARKER_FILTER,
  provenance: "all",
  readiness: "all",
  relevance: "all",
  reviewStatus: "all",
  search: "",
};

export const DEFAULT_QUEUE_FILTERS: PendingQueueFilters = {
  ...UNFILTERED_QUEUE,
  constraint: "not_excluded",
};

function chosen<Value extends string>(value: "all" | Value): Value | undefined {
  return value === "all" ? undefined : value;
}

/** The page route's query for these filters, leaving out every unnarrowed one. */
export function pendingQueueQuery(filters: PendingQueueFilters): PendingQueueQuery {
  return {
    arrangement: chosen(filters.arrangement),
    category: chosen(filters.category),
    constraint: chosen(filters.constraint),
    employer: filters.employers.length ? [...filters.employers] : undefined,
    favorite: filters.markers.favorite || undefined,
    kept: filters.markers.kept || undefined,
    provenance: chosen(filters.provenance),
    readiness: chosen(filters.readiness),
    relevance: chosen(filters.relevance),
    review_status: chosen(filters.reviewStatus),
    search: filters.search.trim() || undefined,
  };
}

// Favorites lead, then kept rows, then everything else, under every sort.
export function markerRank(item: Pick<PendingDiscovery, "favorite" | "kept">): number {
  return item.favorite ? 0 : item.kept ? 1 : 2;
}

export const STAGING_PROVENANCE_LABELS: Record<StagingProvenance, string> = {
  guided_campaign: "Guided campaign (paid)",
  board_scan: "Board scan",
  person_added: "Added by you",
  unrecorded: "Origin not recorded",
};

// Filter dropdown and badge order; what the product paid for leads.
export const STAGING_PROVENANCE_ORDER: StagingProvenance[] = [
  "guided_campaign",
  "board_scan",
  "person_added",
  "unrecorded",
];

export const REVIEW_STALENESS_LABELS: Record<ReviewStaleness, string> = {
  out_of_date: "Review out of date",
  out_of_date_master_cv: "Out of date: master CV",
  out_of_date_search_parameters: "Out of date: search parameters",
  out_of_date_review_instructions: "Out of date: review instructions",
  out_of_date_keep_note: "Out of date: your keep note",
  current: "Review current",
};

export const REVIEW_STALENESS_ORDER: readonly ReviewStaleness[] = [
  "out_of_date",
  ...STALE_INPUT_ORDER.map((input) => `out_of_date_${input}` as const),
  "current",
];

export const CONTENT_READINESS_LABELS: Record<ContentReadiness, string> = {
  ready: "Ready to evaluate",
  screening_refused: "Screened out: review your rules",
  no_body: "No retained posting body",
  access_blocked: "Publisher blocked access",
  fetch_failed: "Posting read failed",
  policy_refused: "Publisher policy refused automation",
  render_required: "Rendered recovery required",
  session_required: "Publisher session required",
  identity_not_verified: "Posting identity is not verified",
  identity_ambiguous: "Posting identity is ambiguous",
  identity_contradicted: "Posting identity contradicts the staged listing",
  url_mismatch: "Retained evidence belongs to another URL",
  evidence_incomplete: "Retained evidence is incomplete",
  partial_text: "Only part of the posting was read",
  withdrawn: "No longer listed",
};

// Filter dropdown and attention-summary order; "ready" leads, attention states follow.
export const CONTENT_READINESS_ORDER: ContentReadiness[] = [
  "ready",
  "screening_refused",
  "no_body",
  "access_blocked",
  "fetch_failed",
  "policy_refused",
  "render_required",
  "session_required",
  "identity_not_verified",
  "identity_ambiguous",
  "identity_contradicted",
  "url_mismatch",
  "evidence_incomplete",
  "partial_text",
  "withdrawn",
];

// A withdrawn row stays listed only for its marker; no recovery step can make it ready.
export const ATTENTION_READINESS = CONTENT_READINESS_ORDER.filter(
  (readiness) => readiness !== "ready" && readiness !== "withdrawn",
);

export function contentReadiness(item: PendingDiscovery): ContentReadiness {
  if (item.readiness) return item.readiness;
  if (item.withdrawn_at) return "withdrawn";
  if (item.content_status === "readable" && item.retained_content_id) return "ready";
  if (item.content_status === "access_blocked") return "access_blocked";
  if (item.content_status === "fetch_failed") return "fetch_failed";
  if (item.content_status === "render_required") return "render_required";
  if (item.content_status === "session_required") return "session_required";
  if (item.content_status === "policy_refused") return "policy_refused";
  return "no_body";
}

export function contentNeedsRecovery(item: PendingDiscovery): boolean {
  const readiness = contentReadiness(item);
  // An unauthenticated retry of a session-walled row that already has retained
  // content reads the same wall, so there is nothing to recover.
  if (readiness === "session_required" && item.current_content_id) return false;
  return ![
    "ready",
    "screening_refused",
    "withdrawn",
    "policy_refused",
    "identity_not_verified",
    "identity_ambiguous",
    "identity_contradicted",
  ].includes(readiness);
}

export const IDENTITY_REASONS = new Set<ContentReadiness>([
  "identity_not_verified",
  "identity_ambiguous",
  "identity_contradicted",
]);

// The batch payloads and the paid-action guard both accept 250 rows in one action.
export const MAX_SELECTION_SIZE = 250;

// A selection survives paging, so select-all is bounded by what is already held
// plus what the click would add, and is refused whole rather than filled part way.
export function selectAllRefusal(alreadySelected: number, wouldAdd: number): string {
  if (alreadySelected + wouldAdd <= MAX_SELECTION_SIZE) return "";
  // A smaller page is the way out only when nothing is held yet; once rows are
  // held, paging down cannot help and acting on them or clearing them can.
  return alreadySelected > 0
    ? `Select at most ${MAX_SELECTION_SIZE}; ${alreadySelected} already selected and ${wouldAdd} more of the rows loaded on this page match the filters. Act on those ${alreadySelected} or clear them first.`
    : `Select at most ${MAX_SELECTION_SIZE}; ${wouldAdd} of the rows loaded on this page match the filters. Set Rows per page to 50 and select a page at a time; your selection is kept across pages.`;
}

// policy_refused was attempted and refused, so only not_yet_read is never read.
export function neverReadDiscoveryIds(discoveries: PendingDiscovery[]): Set<string> {
  return new Set(
    discoveries.filter((item) => item.content_status === "not_yet_read").map((item) => item.id),
  );
}

// The fragment is the section id SourceScanOutcomes renders on the Screened out tab.
export function screenedCandidatesHref(taskId: string, sourceName: string): string {
  return `?tab=refusals&scan=${encodeURIComponent(taskId)}&source=${encodeURIComponent(sourceName)}#screened-candidates`;
}

const ARRANGEMENT_LABELS: Record<ArrangementCategory, string> = {
  remote: "Remote",
  hybrid: "Hybrid",
  on_site: "On site",
  period_limited: "Period limited",
  unknown: "Unknown or not stated",
};

/** Every work arrangement category in the generated vocabulary, in display order. */
export const ARRANGEMENT_CATEGORY_VALUES: readonly ArrangementCategory[] = Object.freeze(
  Object.keys(ARRANGEMENT_LABELS) as ArrangementCategory[],
);

export function arrangementLabel(category: ArrangementCategory): string {
  return ARRANGEMENT_LABELS[category];
}

export const CONSTRAINT_CONFIDENCE_LABELS = Object.fromEntries(
  Object.entries(CONSTRAINT_CONFIDENCE_ROUTES).map(([value, route]) => [value, route.label]),
) as Record<ConstraintConfidence, string>;
