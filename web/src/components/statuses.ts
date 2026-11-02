/** Durable status vocabulary, labels, tones, and accessible text. */
import type { components } from "../api/schema";

export type Tone = "positive" | "accent" | "info" | "warning" | "danger" | "muted" | "neutral";

export type StatusEntry = { label: string; tone: Tone };

type ApplicationStatus = components["schemas"]["ApplicationStatus"];
export type EvaluationDecision = components["schemas"]["EvaluationDecision"];
type PostingStatus = components["schemas"]["PostingStatus"];
type TaskStatus = components["schemas"]["TaskStatus"];
type SourceScanStatus = components["schemas"]["SourceScanResponse"]["status"];
type FreshnessState = components["schemas"]["FreshnessState"];
type ArtifactAuthorType = components["schemas"]["ArtifactResponse"]["author_type"];
type CompletenessStatus = components["schemas"]["CompletenessResponse"]["status"];
type ReviewDecision = components["schemas"]["ReviewDecisionResponse"]["decision"];
type SpanStatus = components["schemas"]["SpanStatus"];

/** Every machine status rendered by the shared badge. */
export type StatusValue =
  | ApplicationStatus
  | ArtifactAuthorType
  | CompletenessStatus
  | EvaluationDecision
  | FreshnessState
  | PostingStatus
  | ReviewDecision
  | SourceScanStatus
  | SpanStatus
  | TaskStatus
  | "archived"
  | "undecided";

// Key the map by generated vocabulary so new server values fail type checking.
const APPLICATION_STATUSES: Record<ApplicationStatus, StatusEntry> = {
  not_applied: { label: "Not applied", tone: "muted" },
  package_ready: { label: "Package ready", tone: "accent" },
  applied: { label: "Applied", tone: "info" },
  interviewing: { label: "Interviewing", tone: "info" },
  offer: { label: "Offer", tone: "positive" },
  rejected: { label: "Employer rejected", tone: "danger" },
  withdrawn: { label: "Withdrawn", tone: "muted" },
  declined: { label: "Declined", tone: "muted" },
  closed_before_application: { label: "Closed before applying", tone: "muted" },
};

/** Every application status in the exhaustive display vocabulary. */
export const APPLICATION_STATUS_VALUES: readonly ApplicationStatus[] = Object.freeze(
  Object.keys(APPLICATION_STATUSES) as ApplicationStatus[],
);

/** Statuses a user may append after the initial not-applied state. */
export const APPLICATION_EVENT_STATUS_VALUES: readonly ApplicationStatus[] = Object.freeze(
  APPLICATION_STATUS_VALUES.filter((status) => status !== "not_applied"),
);

const PROGRESSED_APPLICATION_STATUSES: ReadonlySet<ApplicationStatus> = new Set([
  "applied",
  "interviewing",
  "offer",
  "rejected",
  "withdrawn",
  "declined",
  "closed_before_application",
]);

/** Plain status moves the server will accept from one projected current status. */
export function applicationEventStatusValues(
  currentStatus: ApplicationStatus,
  hasApplicationHistory = false,
): readonly ApplicationStatus[] {
  return APPLICATION_EVENT_STATUS_VALUES.filter(
    (status) =>
      status !== currentStatus &&
      !(
        status === "package_ready" &&
        (PROGRESSED_APPLICATION_STATUSES.has(currentStatus) ||
          (currentStatus === "not_applied" && hasApplicationHistory))
      ),
  );
}

const EVALUATION_DECISIONS: Record<EvaluationDecision, StatusEntry> = {
  ineligible: { label: "Ineligible", tone: "danger" },
  closed_evidence: { label: "Closed evidence", tone: "danger" },
  deprioritize: { label: "Deprioritize", tone: "muted" },
  apply_now: { label: "Apply now", tone: "positive" },
  premium_target: { label: "Premium target", tone: "positive" },
  investigate: { label: "Investigate", tone: "accent" },
  monitor: { label: "Monitor", tone: "info" },
};

/** Narrow untyped task progress before presenting an evaluation decision. */
export function isEvaluationDecision(value: unknown): value is EvaluationDecision {
  return typeof value === "string" && Object.hasOwn(EVALUATION_DECISIONS, value);
}

const TASK_STATUSES: Record<TaskStatus, StatusEntry> = {
  queued: { label: "Queued", tone: "info" },
  running: { label: "Running", tone: "accent" },
  needs_review: { label: "Needs review", tone: "accent" },
  committing: { label: "Committing", tone: "accent" },
  completed: { label: "Completed", tone: "positive" },
  failed: { label: "Failed", tone: "danger" },
  cancelled: { label: "Cancelled", tone: "muted" },
};

const COMPLETENESS_STATUSES: Record<CompletenessStatus, StatusEntry> = {
  complete: { label: "Complete", tone: "positive" },
  incomplete: { label: "Incomplete", tone: "warning" },
};

const POSTING_STATUSES: Record<PostingStatus, StatusEntry> = {
  active: { label: "Active", tone: "positive" },
  closed: { label: "Posting closed", tone: "muted" },
};

const SCAN_STATUSES: Record<SourceScanStatus, StatusEntry> = {
  completed: { label: "Completed", tone: "positive" },
  completed_empty: { label: "No listings", tone: "muted" },
  incomplete: { label: "Incomplete", tone: "warning" },
  failed: { label: "Failed", tone: "danger" },
  omitted: { label: "Omitted", tone: "warning" },
};

/** Every source scan status in display order. */
export const SOURCE_SCAN_STATUS_VALUES: readonly SourceScanStatus[] = Object.freeze(
  Object.keys(SCAN_STATUSES) as SourceScanStatus[],
);

const AUTHORSHIP_STATUSES: Record<ArtifactAuthorType, StatusEntry> = {
  human: { label: "Human", tone: "accent" },
  model: { label: "Model", tone: "info" },
  system: { label: "System", tone: "neutral" },
};

const REVIEW_STATUSES: Record<ReviewDecision | "undecided", StatusEntry> = {
  accept: { label: "Accepted", tone: "positive" },
  reject: { label: "Edit rejected", tone: "danger" },
  replace: { label: "Replaced", tone: "warning" },
  undecided: { label: "Undecided", tone: "neutral" },
};

const RECORD_STATUSES: Record<"archived", StatusEntry> = {
  archived: { label: "Archived", tone: "muted" },
};

const FRESHNESS_STATUSES: Record<FreshnessState, StatusEntry> = {
  verified_current: { label: "Verified current", tone: "positive" },
  likely_stale: { label: "Likely stale", tone: "warning" },
  unknown: { label: "Unknown", tone: "neutral" },
};

const SPAN_STATUSES: Record<SpanStatus, StatusEntry> = {
  verified: { label: "Verified", tone: "positive" },
  needs_review: { label: "Needs review", tone: "accent" },
};

/** Statuses that keep a job open in pipeline and document views. */
export const PURSUED_STATUSES: readonly ApplicationStatus[] = [
  "package_ready",
  "applied",
  "interviewing",
  "offer",
];

const STATUS_ENTRIES = {
  ...APPLICATION_STATUSES,
  ...EVALUATION_DECISIONS,
  ...TASK_STATUSES,
  ...COMPLETENESS_STATUSES,
  ...POSTING_STATUSES,
  ...SCAN_STATUSES,
  ...AUTHORSHIP_STATUSES,
  ...REVIEW_STATUSES,
  ...RECORD_STATUSES,
  ...FRESHNESS_STATUSES,
  ...SPAN_STATUSES,
} satisfies Record<StatusValue, StatusEntry>;

export function statusEntry(value: StatusValue): StatusEntry {
  return STATUS_ENTRIES[value];
}
