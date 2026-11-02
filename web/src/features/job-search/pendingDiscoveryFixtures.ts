import type {
  DismissedDiscovery,
  DispositionPreview,
  DispositionSelection,
  FoundPostingCheckResponse,
  PendingDisposition,
  PendingDiscovery,
  PendingDiscoveryChanges,
  PendingDiscoveryPage,
  PendingDuplicateCandidate,
  RelevanceReview,
  Task,
} from "../../api/client/types";
import { CONSTRAINT_CONFIDENCE_ROUTES, type ConstraintConfidence } from "./constraintConfidence";
import { CONTENT_READINESS_ORDER, contentReadiness } from "./pendingDiscoveryFilterModel";
import { discoveryRelevanceKey } from "./relevanceReviewModel";

type DispositionTarget = DispositionPreview["targets"][number];

type StagedFacts = PendingDiscovery["staged_facts"];
type PublishedFact = NonNullable<StagedFacts["locations"]>;

/** A fact the publisher did not state, in the shape every staged dimension uses. */
function unstatedFact(): PublishedFact {
  return {
    values: [],
    unresolved_values: [],
    provenance: "",
    complete: false,
  } satisfies PublishedFact;
}

export function publishedFact(values: string[], provenance: string): PublishedFact {
  return { values, unresolved_values: [], provenance, complete: true } satisfies PublishedFact;
}

export function stagedFacts(overrides: Partial<StagedFacts> = {}): StagedFacts {
  const base = {
    schema_version: 5,
    identity_trust: "publisher",
    freshness: "unknown",
    locations: unstatedFact(),
    arrangements: unstatedFact(),
    employment_types: unstatedFact(),
    compensation: unstatedFact(),
  } satisfies StagedFacts;
  return { ...base, ...overrides };
}

function baseRow(): PendingDiscovery {
  return {
    id: "pending-0",
    employer: "Globex",
    title: "Role 0",
    location: "Remote",
    discovery_url: "https://jobs.example.test/0",
    canonical_url: "https://jobs.example.test/0",
    content_note: "",
    content_status: "readable",
    retained_content_id: "content-0",
    current_content_id: "content-0",
    reason: "Synthetic board read.",
    first_seen_on: "2026-08-21",
    rank: 0,
    ranked_because: [],
    matched_target_title: null,
    recommendation: "investigate",
    constraint_confidence: "investigate",
    readiness: "ready",
    screening_standing: "unknown",
    screening_findings: [],
    recommendation_because: "Typed screening result.",
    arrangement_categories: ["remote"],
    arrangement_categories_source: "published",
    staged_facts: stagedFacts(),
    also_in: [],
    withdrawn_at: null,
    favorite: false,
    kept: false,
    keep_note: null,
    observed_publisher_title: "",
    observed_publisher_employer: "",
    relevance_review: null,
    duplicate_suggestions: [],
    staging_provenance: "board_scan",
  } satisfies PendingDiscovery;
}

export function pendingDiscoveryRow(overrides: Partial<PendingDiscovery> = {}): PendingDiscovery {
  return { ...baseRow(), ...overrides };
}

/** `count` ready rows named Role 0..n-1, ranked in that order. */
export function pendingDiscoveryRows(count: number): PendingDiscovery[] {
  return Array.from({ length: count }, (_, index) =>
    pendingDiscoveryRow({
      id: `pending-${index}`,
      title: `Role ${index}`,
      discovery_url: `https://jobs.example.test/${index}`,
      canonical_url: `https://jobs.example.test/${index}`,
      retained_content_id: `content-${index}`,
      current_content_id: `content-${index}`,
      rank: index,
    }),
  );
}

export const QUEUE_SNAPSHOT_SHA256 = "a".repeat(64);
export const QUEUE_SNAPSHOT_BEFORE = "2026-09-12T00:00:00Z";

export interface LaneOptions {
  items?: PendingDiscovery[];
  total?: number;
  offset?: number;
  limit?: number;
  has_more?: boolean;
}

function lane(options: LaneOptions, limit: number): PendingDiscoveryPage["ready"] {
  const items = options.items ?? [];
  return {
    items,
    total: options.total ?? items.length,
    offset: options.offset ?? 0,
    limit: options.limit ?? limit,
    has_more: options.has_more ?? false,
  } satisfies PendingDiscoveryPage["ready"];
}

type Facets = PendingDiscoveryPage["facets"];

export type QueueDimension =
  | "readiness"
  | "relevance"
  | "review_status"
  | "constraint"
  | "category"
  | "arrangement"
  | "provenance"
  | "markers"
  | "search"
  | "employer";

type ConstraintRoute = Facets["constraint"][number]["value"];

const CONSTRAINT_ROUTES: readonly ConstraintRoute[] = [
  "not_excluded",
  "pursue_only",
  "needs_clarification",
  "excluded_only",
  ...(Object.keys(CONSTRAINT_CONFIDENCE_ROUTES) as ConstraintConfidence[]),
];

function reviewStatuses(row: PendingDiscovery): string[] {
  const review = row.relevance_review;
  if (!review) return [];
  if (!review.out_of_date_because.length) return ["current"];
  return ["out_of_date", ...review.out_of_date_because.map((input) => `out_of_date_${input}`)];
}

function relevanceKeyOf(row: PendingDiscovery) {
  return discoveryRelevanceKey(row.relevance_review ?? undefined, row.relevance_review_rejection);
}

function routeAdmits(route: string, row: PendingDiscovery): boolean {
  const own = row.constraint_confidence;
  if (route === "pursue_only") return own === "pursue";
  if (route === "excluded_only") return own === "excluded";
  if (route === "needs_clarification") {
    return own === "investigate" || own === "needs_verification" || own === "uncertain_comp";
  }
  return route === "not_excluded" ? own !== "excluded" : own === route;
}

function exactGroup(route: string | null): boolean {
  return route === "pursue_only" || route === "needs_clarification" || route === "excluded_only";
}

function normalizedEmployer(name: string): string {
  return name.trim().replace(/\s+/g, " ").toLowerCase();
}

/** The page-route filters this row fails, with the server's marker bypass for readiness and route. */
export function queueFilterFailures(
  row: PendingDiscovery,
  params: URLSearchParams,
): Set<QueueDimension> {
  const marked = Boolean(row.favorite || row.kept) && !exactGroup(params.get("constraint"));
  const wanted = (name: string) => params.get(name);
  const failed = new Set<QueueDimension>();
  const readiness = wanted("readiness");
  if (readiness && !marked && contentReadiness(row) !== readiness) failed.add("readiness");
  const relevance = wanted("relevance");
  if (relevance && relevanceKeyOf(row) !== relevance) failed.add("relevance");
  const reviewStatus = wanted("review_status");
  if (reviewStatus && !reviewStatuses(row).includes(reviewStatus)) failed.add("review_status");
  const constraint = wanted("constraint");
  if (constraint && !marked && !routeAdmits(constraint, row)) failed.add("constraint");
  const category = wanted("category");
  if (category && row.recommendation !== category) failed.add("category");
  const arrangement = wanted("arrangement");
  if (arrangement && !(row.arrangement_categories as string[]).includes(arrangement)) {
    failed.add("arrangement");
  }
  const provenance = wanted("provenance");
  if (provenance && row.staging_provenance !== provenance) failed.add("provenance");
  const favorite = wanted("favorite") === "true";
  const kept = wanted("kept") === "true";
  if ((favorite || kept) && !((favorite && row.favorite) || (kept && row.kept))) {
    failed.add("markers");
  }
  const search = (wanted("search") ?? "").trim().toLowerCase();
  const haystack = [row.employer, row.title, row.location].join("\n").toLowerCase();
  if (search && !haystack.includes(search)) failed.add("search");
  const employers = params.getAll("employer").map(normalizedEmployer);
  if (employers.length && !employers.includes(normalizedEmployer(row.employer))) {
    failed.add("employer");
  }
  return failed;
}

export function tally<Value extends string>(
  values: readonly Value[],
): { value: Value; count: number }[] {
  const counts = new Map<Value, number>();
  for (const value of values) counts.set(value, (counts.get(value) ?? 0) + 1);
  return [...counts].map(([value, count]) => ({ value, count }));
}

/**
 * Facet counts the server reports for these rows under the request's filters: each
 * dimension over the rows every other filter keeps, with exact presentation groups.
 */
export function queueFacets(
  rows: readonly PendingDiscovery[],
  params: URLSearchParams = new URLSearchParams(),
): Facets {
  const failures = rows.map((row) => ({ row, failed: queueFilterFailures(row, params) }));
  const passing = (dimension: QueueDimension) =>
    failures
      .filter(({ failed }) => [...failed].every((item) => item === dimension))
      .map(({ row }) => row);
  const marked = (row: PendingDiscovery) => Boolean(row.favorite || row.kept);
  const readinessRows = passing("readiness");
  const constraintRows = failures
    .filter(({ failed }) =>
      [...failed].every((item) => item === "constraint" || item === "readiness"),
    )
    .map(({ row }) => row);
  const relevanceRows = passing("relevance");
  const reviewRows = passing("review_status");
  const markerRows = passing("markers");
  return {
    readiness: CONTENT_READINESS_ORDER.map((value) => ({
      value,
      count: readinessRows.filter(
        (row) =>
          (marked(row) && !exactGroup(params.get("constraint"))) || contentReadiness(row) === value,
      ).length,
      holding: readinessRows.filter((row) => contentReadiness(row) === value).length,
    })),
    relevance: tally(relevanceRows.map(relevanceKeyOf)).map((entry) => ({
      ...entry,
      out_of_date: relevanceRows.filter(
        (row) => relevanceKeyOf(row) === entry.value && reviewStatuses(row).includes("out_of_date"),
      ).length,
    })),
    review_status: tally(reviewRows.flatMap(reviewStatuses)) as Facets["review_status"],
    review_status_total: reviewRows.length,
    constraint: CONSTRAINT_ROUTES.map((value) => ({
      value,
      count: constraintRows.filter((row) =>
        exactGroup(value)
          ? routeAdmits(value, row) &&
            (!params.get("readiness") || params.get("readiness") === contentReadiness(row))
          : marked(row) ||
            (routeAdmits(value, row) &&
              (!params.get("readiness") || params.get("readiness") === contentReadiness(row))),
      ).length,
    })),
    constraint_total: constraintRows.filter(
      (row) =>
        marked(row) ||
        !params.get("readiness") ||
        params.get("readiness") === contentReadiness(row),
    ).length,
    category: tally(passing("category").map((row) => row.recommendation)),
    arrangement: tally(passing("arrangement").flatMap((row) => row.arrangement_categories)),
    arrangement_total: passing("arrangement").length,
    provenance: tally(passing("provenance").map((row) => row.staging_provenance)),
    markers: {
      favorite: markerRows.filter((row) => row.favorite).length,
      kept: markerRows.filter((row) => row.kept).length,
    },
  } satisfies Facets;
}

export function pendingDiscoveryPage(
  options: {
    ready?: LaneOptions;
    attention?: LaneOptions;
    limit?: number;
    snapshot_sha256?: string;
    snapshot_before?: string;
    queue_total?: number;
    facets?: Facets;
    employers?: PendingDiscoveryPage["employers"];
  } = {},
): PendingDiscoveryPage {
  const limit = options.limit ?? 50;
  const ready = lane(options.ready ?? {}, limit);
  const attention = lane(options.attention ?? {}, limit);
  return {
    snapshot_sha256: options.snapshot_sha256 ?? QUEUE_SNAPSHOT_SHA256,
    snapshot_before: options.snapshot_before ?? QUEUE_SNAPSHOT_BEFORE,
    queue_total: options.queue_total ?? ready.total + attention.total,
    ready,
    attention,
    facets: options.facets ?? queueFacets([...ready.items, ...attention.items]),
    employers: options.employers ?? [],
  } satisfies PendingDiscoveryPage;
}

export function pendingDiscoveryChanges(
  overrides: {
    active_sources?: number;
    automatic_pending_count?: number;
    frontier_completed_count?: number;
    ready?: number;
    attention?: number;
  } = {},
): PendingDiscoveryChanges {
  return {
    follow_up: {
      active_sources: overrides.active_sources ?? 0,
      automatic_pending_count: overrides.automatic_pending_count ?? 0,
      frontier_completed_count: overrides.frontier_completed_count ?? 0,
    },
    newer_visible: { ready: overrides.ready ?? 0, attention: overrides.attention ?? 0 },
  } satisfies PendingDiscoveryChanges;
}

export function relevanceReview(
  pendingDiscoveryId: string,
  overrides: Partial<RelevanceReview> = {},
): RelevanceReview {
  const base = {
    id: `review-${pendingDiscoveryId}`,
    pending_discovery_id: pendingDiscoveryId,
    employer: "Globex",
    title: "Reviewed role",
    outcome: "judged",
    out_of_date_because: [],
    recommendation: "review",
    summary: "Synthetic relevance summary.",
    core_work_alignment: "direct",
    core_qualification_alignment: "established",
    hard_condition: "passes",
    decisive_basis: "core_match",
    posting_citations: [],
    master_cv_evidence_ids: [],
    unresolved_questions: [],
    fetched_at: "2026-08-22T00:00:00Z",
    reviewed_at: "2026-08-22T00:00:00Z",
    task_id: "task-1",
    master_cv_version_id: "cv-current",
    search_parameter_set_id: "parameters-current",
    matched_exclusion: null,
  } satisfies RelevanceReview;
  return { ...base, ...overrides };
}

export function taskRecord(overrides: Partial<Task> = {}): Task {
  const base = {
    id: "task-1",
    workspace_id: "workspace-1",
    task_type: "discovery.evaluate",
    status: "running",
    progress: {},
    attempt_count: 1,
    cancellation_requested_at: null,
    completed_at: null,
    created_at: "2026-08-24T00:00:00Z",
    error_category: null,
    error_message: null,
    job_posting_ids: [],
    pending_discovery_ids: [],
    source_ids: [],
    started_at: "2026-08-24T00:01:00Z",
    updated_at: "2026-08-24T00:02:00Z",
  } satisfies Task;
  return { ...base, ...overrides };
}

export function dispositionTarget(
  id: string,
  overrides: Partial<DispositionTarget> = {},
): DispositionTarget {
  const base = {
    id,
    primary_id: id,
    kind: "primary",
    employer: "Northwind Research",
    title: "Warehouse Supervisor",
    location: "Northtown",
    discovery_url: `https://jobs.example.test/${id}`,
    canonical_url: `https://jobs.example.test/${id}`,
  } satisfies DispositionTarget;
  return { ...base, ...overrides };
}

/** Echo one selection back as the server does, with its exact affected rows. */
export function dispositionPreview(
  selection: DispositionSelection,
  overrides: {
    snapshot_sha256?: string;
    targets?: DispositionTarget[];
    skipped_marked_ids?: string[];
    skipped_withdrawn_ids?: string[];
  } = {},
): DispositionPreview {
  return {
    action: selection.action,
    category: selection.category,
    reason: selection.reason,
    search_parameter_set_id: null,
    skipped_marked_ids: overrides.skipped_marked_ids ?? [],
    skipped_withdrawn_ids: overrides.skipped_withdrawn_ids ?? [],
    snapshot_sha256: overrides.snapshot_sha256 ?? "d".repeat(64),
    targets:
      overrides.targets ?? selection.pending_discovery_ids.map((id) => dispositionTarget(id)),
  } satisfies DispositionPreview;
}

export function dismissedDiscovery(
  overrides: Partial<DismissedDiscovery> = {},
): DismissedDiscovery {
  const base = {
    id: "event-1",
    pending_discovery_id: "pending-1",
    action: "dismiss",
    category: "out_of_scope_title",
    reason: "Recorded during triage.",
    origin: "api",
    selection_mode: "explicit",
    created_at: "2026-08-30T12:00:00Z",
    duplicate_of_job_id: null,
    duplicate_of_pending_id: null,
    supersedes_event_id: null,
    employer: "Northwind Research",
    title: "Warehouse Supervisor",
    location: "Remote",
    discovery_url: "https://jobs.example.test/pending-1",
    canonical_url: "https://jobs.example.test/pending-1",
  } satisfies DismissedDiscovery;
  return { ...base, ...overrides };
}

export function pendingDisposition(
  overrides: Partial<PendingDisposition> = {},
): PendingDisposition {
  const base = {
    id: "event-1",
    pending_discovery_id: "pending-1",
    action: "dismiss",
    category: "out_of_scope_title",
    reason: "Recorded during triage.",
    origin: "api",
    selection_mode: "explicit",
    created_at: "2026-08-30T12:00:00Z",
    duplicate_of_job_id: null,
    duplicate_of_pending_id: null,
    supersedes_event_id: null,
  } satisfies PendingDisposition;
  return { ...base, ...overrides };
}

export function duplicateCandidate(
  overrides: Partial<PendingDuplicateCandidate> = {},
): PendingDuplicateCandidate {
  const base = {
    id: "pending-2",
    employer: "Globex",
    title: "Data Steward",
    location: "Remote",
    discovery_url: "https://jobs.example.test/opening/2",
    dismissed: false,
  } satisfies PendingDuplicateCandidate;
  return { ...base, ...overrides };
}

export function foundPostingCheck(
  overrides: Partial<FoundPostingCheckResponse> = {},
): FoundPostingCheckResponse {
  const base = {
    status: "readable",
    note: "Read from the publisher API.",
    platform: "lever",
    title: "Product Manager",
    employer: "",
    location: "Remote, Moncton",
    staged_facts: stagedFacts({
      locations: publishedFact(["Remote", "Moncton"], "lever.location"),
      arrangements: publishedFact(["Remote"], "lever.arrangement"),
      employment_types: publishedFact(["Full-time"], "lever.employment_type"),
    }),
  } satisfies FoundPostingCheckResponse;
  return { ...base, ...overrides };
}
