import { Fragment, memo, useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import type {
  RelevanceReview,
  RelevanceReviewRejection,
  PendingDiscovery,
  Task,
} from "../../api/client";
import {
  type TaskCosts,
  useArtifactText,
  useSetPendingClosingDate,
  useTrackPendingDiscovery,
} from "../../api/hooks";
import { safeHttpUrl } from "../../api/safeUrl";
import { AggregatorCreditLink } from "../../components/AggregatorCredit";
import { PaidAction } from "../../components/PaidAction";
import { aggregatorCredit, postingAddress } from "../../components/aggregatorHosts";
import { CountryToken } from "../../components/CountryToken";
import { ClosingDates } from "../../components/ClosingDates";
import { ErrorState, LoadingState } from "../../components/States";
import { StatusBadge } from "../../components/StatusBadge";
import { type Tone, statusEntry } from "../../components/statuses";
import { employmentTypeLabel } from "../../components/text";
import { DismissalEditor } from "./DiscoveryDispositions";
import { RelevanceReview as RelevanceReviewCard } from "./RelevanceReview";
import {
  discoveryRecommendationLabel,
  discoveryRelevanceKey,
  modelRelevanceKey,
  rejectionFollowsReview,
  rejectionReasonLabel,
  relevanceLabel,
  relevanceTone,
  relevanceReviewIsOutOfDate,
} from "./relevanceReviewModel";
import { evaluationState, pendingDiscoveryIsEvaluated } from "./evaluationTaskOutcome";
import { EmployerName, HostName } from "./EmployerName";
import { postingHost, postingLabel, titleFallback } from "./fallbacks";
import {
  CONTENT_READINESS_LABELS,
  CONSTRAINT_CONFIDENCE_LABELS,
  STAGING_PROVENANCE_LABELS,
  arrangementLabel as categoryLabel,
  type ArrangementCategory,
  type ContentReadiness,
  contentReadiness,
} from "./pendingDiscoveryFilterModel";
import { PendingDiscoveryProvenance } from "./PendingDiscoveryProvenance";
import { PendingDuplicateSuggestions } from "./PendingDuplicateSuggestions";
import { sharesPostingText } from "./pendingDuplicateSuggestionModel";
import { PendingPostingRemediation } from "./PendingPostingRemediation";
import { PendingRowKeepNote, PendingRowMarkerToggles } from "./PendingRowMarkers";
import { PendingRowTitle } from "./PendingRowTitle";
import { usePendingRowMarkers } from "./usePendingRowMarkers";

const CONTENT_STATUS_LABELS: Record<PendingDiscovery["content_status"], string> = {
  not_yet_read: "Not read yet",
  readable: "Publisher content is readable",
  access_blocked: "Publisher blocked access",
  session_required: "Publisher session required",
  render_required: "Rendered read required",
  policy_refused: "Publisher policy refused the read",
  fetch_failed: "Read failed; retry is available",
};

const READINESS_DETAIL_TEXT: Partial<Record<ContentReadiness, string>> = {
  screening_refused:
    "The posting text is readable, but it conflicts with your search rules. Review the findings and your rules, or dismiss this posting.",
  access_blocked:
    "The publisher blocked the read. Refresh postings retries; if it fails again, paste the posting text below.",
  render_required: "This page needs a rendered read. Refresh postings tries one.",
  no_body: "No posting text saved. Refresh postings reads it.",
  fetch_failed: "Read failed; retry is available",
};

// Plain text for each backend page classification a "classified as <token>"
// note can carry; unmapped tokens get a generic sentence.
const CLASSIFICATION_PLAIN_TEXT: Partial<Record<string, string>> = {
  error_or_challenge: "The publisher showed a sign-in or verification page instead of the posting.",
  collection: "The address shows a list of postings rather than one posting.",
  navigation: "The address shows a navigation page, not the posting itself.",
  application_channel: "The address leads to an application form, not the posting text.",
  js_shell: "This page needs a script to load its content before the posting text appears.",
  ambiguous: "The publisher page could not be classified with confidence.",
};

function plainContentNote(contentNote: string): string | null {
  const classification = /^.*?classified as ([a-z_]+)[.)]*(?:\s|$)/.exec(contentNote);
  if (!classification) return contentNote || null;
  const explanation =
    CLASSIFICATION_PLAIN_TEXT[classification[1] ?? ""] ??
    "The publisher did not show the posting to an automated read.";
  const recovery = contentNote.slice(classification[0].length).trim();
  return recovery ? `${explanation} ${recovery}` : explanation;
}

const PLAIN_ERROR_CATEGORY: Record<string, string> = {
  ProviderError: "provider throttle",
  ProviderRateLimit: "provider throttle",
  ValidationError: "workspace state changed",
  ProviderTimeout: "provider timeout",
  ProviderRefusal: "provider refusal",
  NetworkError: "network error",
};

function plainErrorCategory(category: string | null | undefined): {
  plain: string;
  raw: string;
} {
  const raw = category ?? "Unknown";
  return { plain: PLAIN_ERROR_CATEGORY[raw] ?? raw.toLowerCase(), raw };
}

// Short readiness word and a one-line reason for the main row; the fuller
// per-status explanation (content notes, policy text) stays in Details.
const READINESS_SHORT_LABEL: Record<ContentReadiness, string> = {
  ready: "Ready",
  screening_refused: "Screened out",
  no_body: "Needs recovery",
  access_blocked: "Needs recovery",
  fetch_failed: "Needs recovery",
  render_required: "Needs recovery",
  session_required: "Needs recovery",
  policy_refused: "Needs recovery",
  identity_not_verified: "Needs review",
  identity_ambiguous: "Needs review",
  identity_contradicted: "Needs review",
  url_mismatch: "Needs review",
  evidence_incomplete: "Needs review",
  partial_text: "Needs recovery",
  withdrawn: "Closed",
};

const READINESS_SHORT_REASON: Record<ContentReadiness, string> = {
  ready: "full description retained",
  screening_refused: "conflicts with your search rules",
  no_body: "no body retained",
  access_blocked: "publisher blocked access",
  fetch_failed: "read failed",
  render_required: "rendered read required",
  session_required: "session required",
  policy_refused: "policy refused automation",
  identity_not_verified: "identity not verified",
  identity_ambiguous: "identity ambiguous",
  identity_contradicted: "identity contradicted",
  url_mismatch: "evidence belongs to another URL",
  evidence_incomplete: "evidence incomplete",
  partial_text: "text appears cut off",
  withdrawn: "no longer listed",
};

function pendingPlace(discovery: PendingDiscovery): { label: string; unresolved: boolean } {
  const stated = discovery.location?.trim() ?? "";
  const locationCount = /^\d+\s+locations?$/i.test(stated);
  if (stated && !locationCount) return { label: stated, unresolved: false };
  const unresolved = discovery.staged_facts?.locations?.unresolved_values ?? [];
  const evidence = unresolved.length > 0 ? unresolved : locationCount ? [stated] : [];
  return {
    label: evidence.length > 0 ? `${evidence.join(", ")} unresolved` : "Not stated",
    unresolved: true,
  };
}

let countryNameCache: Set<string> | null = null;

// English region names from the runtime's own data; unknown codes echo back and are skipped.
function countryNames(): Set<string> {
  if (countryNameCache) return countryNameCache;
  const names = new Set<string>();
  const display = new Intl.DisplayNames(["en"], { type: "region" });
  for (let first = 65; first <= 90; first += 1) {
    for (let second = 65; second <= 90; second += 1) {
      const code = String.fromCharCode(first, second);
      const name = display.of(code);
      if (name && name !== code) names.add(name.toLowerCase());
    }
  }
  countryNameCache = names;
  return names;
}

function additionalLocations(discovery: PendingDiscovery): {
  extras: string[];
  incomplete: boolean;
} {
  const facts = discovery.staged_facts?.locations;
  const values = facts?.values ?? [];
  const stated = discovery.location?.trim() ?? "";
  // Boards stage the stated place's country beside it; a bare country adds no place to the row.
  const lowered = stated.toLowerCase();
  const extras = values.filter((value) => {
    const place = value.trim().toLowerCase();
    return place && !lowered.includes(place) && !(stated && countryNames().has(place));
  });
  return { extras, incomplete: facts?.complete === false };
}

// Short forms a place writes for a country the countries list spells in full.
const COUNTRY_SHORT_FORMS: Partial<Record<string, readonly string[]>> = {
  "united states": ["us", "usa", "u.s.", "u.s.a.", "united states of america"],
  "united kingdom": ["uk", "u.k.", "great britain"],
};

// A short form counts only as a whole word, so "us" in "Columbus" names no country.
function placeNamesCountry(lowercasePlaces: string, country: string): boolean {
  const name = country.toLowerCase();
  if (lowercasePlaces.includes(name)) return true;
  return (COUNTRY_SHORT_FORMS[name] ?? []).some((form) =>
    new RegExp(`(^|[^a-z.])${form.replaceAll(".", "\\.")}($|[^a-z])`).test(lowercasePlaces),
  );
}

// Keep the first items for one line of the row; the rest stay countable.
function clamped<T>(items: readonly T[], max: number): { shown: T[]; rest: T[] } {
  return { shown: items.slice(0, max), rest: items.slice(max) };
}

// Truncate the extra-locations list for the row; the full list stays in Details.
function truncatedExtras(extras: string[], max = 2): string {
  const { shown, rest } = clamped(extras, max);
  return rest.length > 0 ? `${shown.join(", ")} +${rest.length}` : shown.join(", ");
}

function arrangementLabel(category: ArrangementCategory, discovery: PendingDiscovery): string {
  if (category !== "unknown") return categoryLabel(category);
  const values = discovery.staged_facts.arrangements?.values ?? [];
  return values.length ? `Unclassified: ${values.join(", ")}` : "Not stated";
}

function arrangementSourceLabel(source: PendingDiscovery["arrangement_categories_source"]): string {
  if (source === "published") return "publisher";
  if (source === "inferred_from_url") return "from URL";
  if (source === "inferred_from_location") return "from location";
  return "";
}

function pendingEmployment(discovery: PendingDiscovery): string {
  const fact = discovery.staged_facts?.employment_types;
  if (!fact?.values?.length) return "Employment not stated";
  const qualifier = fact.complete ? "" : " (incomplete)";
  return `${fact.values.map(employmentTypeLabel).join(", ")}${qualifier}`;
}

const PAY_TARGET_STANDING: Partial<
  Record<PendingDiscovery["screening_findings"][number]["code"], string>
> = {
  compensation_target_met: ", top reaches target",
  compensation_below_target: ", top under target",
  compensation_stated: "",
  compensation_match: ", top reaches minimum",
  compensation_below_minimum: ", top under minimum",
  compensation_unknown: "",
};

function pendingPay(discovery: PendingDiscovery): string | null {
  const finding = (discovery.screening_findings ?? []).find(
    (item) => item.dimension === "compensation" && item.code in PAY_TARGET_STANDING,
  );
  if (!finding?.subject) return null;
  return `Pay: ${finding.subject}${PAY_TARGET_STANDING[finding.code]}`;
}

// A short chip label for one non-match screening finding; the full sentence
// explaining how to resolve it stays in Details.
const TITLE_CHIP_LABELS: Partial<Record<string, string>> = {
  title_near_match: "Title near match",
  title_role_family_review: "Title family review",
  title_specialist_ic_review: "Specialist IC review",
  title_domain_review: "Domain evidence review",
  title_extra_role_words: "Title extra words",
  title_unverified: "Title unverified",
  title_mismatch: "Title mismatch",
  title_role_family_conflict: "Title family conflict",
  title_excluded: "Title excluded",
  title_test_requisition: "Test requisition",
};

function screeningChipLabel(discovery: PendingDiscovery["screening_findings"][number]): string {
  const { dimension, standing, code, subject } = discovery;
  switch (dimension) {
    case "title":
      return TITLE_CHIP_LABELS[code] ?? "Title unclear";
    case "employer":
      return standing === "unknown" ? "Employer unclear" : "Employer mismatch";
    case "seniority":
      return standing === "unknown" ? "Seniority unclear" : "Seniority mismatch";
    case "location":
      return standing === "unknown" ? "Location unresolved" : "Location mismatch";
    case "employment_type":
      return standing === "unknown" ? "Employment unclear" : "Employment mismatch";
    case "arrangement":
      return subject || (standing === "unknown" ? "Arrangement unclear" : "Arrangement mismatch");
    case "compensation":
      return standing === "unknown" ? "Comp unknown" : "Comp mismatch";
    default:
      return dimension;
  }
}

type Chip = { label: string; tone?: Tone; title?: string };
type ScreeningStanding = PendingDiscovery["screening_findings"][number]["standing"];

const SCREENING_STANDING_TONES: Record<ScreeningStanding, Tone> = {
  match: "positive",
  refuse: "danger",
  unknown: "muted",
};

// A refusal reads apart from an open question, and a missing finding reads as open.
function screeningStandingTone(standing: ScreeningStanding | undefined): Tone {
  return standing ? SCREENING_STANDING_TONES[standing] : "muted";
}

const MAX_ROW_CHIPS = 2;

function screeningRemediations(discovery: PendingDiscovery): string[] {
  const actions = new Set<string>();
  for (const finding of discovery.screening_findings ?? []) {
    if (finding.standing === "match") continue;
    if (finding.dimension === "title") {
      actions.add(
        finding.code === "title_unverified"
          ? "Open the posting, then correct the row title or refresh the posting."
          : finding.code === "title_domain_review"
            ? "Open the posting to check its responsibilities against your target roles."
            : finding.code === "title_specialist_ic_review"
              ? "Open the posting to confirm its stated focus describes this role, or run a relevance review."
              : "Correct the row title if the publisher identity is wrong, or edit target titles if this role should be in scope.",
      );
    } else if (finding.code === "seniority_outside" && finding.standing === "unknown") {
      actions.add(
        "Open the posting to review its responsibilities and level against your preferred band.",
      );
    } else if (finding.code === "compensation_unknown") {
      actions.add(
        "Compare any stated pay with your preference yourself. You can track this job while pay remains unconfirmed.",
      );
    } else if (finding.standing === "unknown") {
      actions.add("Refresh postings, or paste the current posting text below.");
    } else {
      actions.add(
        "Review the conflicting search preference, or dismiss this posting if the rule is correct.",
      );
    }
  }
  return [...actions];
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

// Relative inside a day, then the UTC year-month-day that first_seen_on beside
// it already carries; an exact minute stops mattering once it is yesterday's.
function shortWhen(value: string, now: number): string {
  const at = new Date(value);
  const elapsed = now - at.getTime();
  if (Number.isNaN(elapsed)) return "";
  if (elapsed < MINUTE) return "just now";
  if (elapsed < HOUR) return `${Math.floor(elapsed / MINUTE)}m ago`;
  if (elapsed < DAY) return `${Math.floor(elapsed / HOUR)}h ago`;
  return at.toISOString().slice(0, 10);
}

function activityLine(item: PendingDiscovery, review: RelevanceReview | undefined): string {
  const now = Date.now();
  const stated: string[] = [];
  const add = (label: string, value: string | null | undefined) => {
    const when = value ? shortWhen(value, now) : "";
    if (when) stated.push(`${label} ${when}`);
  };
  add("read", item.current_content_observed_at);
  add("reviewed", review?.reviewed_at);
  add("evaluated", item.evaluated_at);
  if (stated.length === 0) return "";
  const line = stated.join(", ");
  return line.charAt(0).toUpperCase() + line.slice(1);
}

// A mark newer than the stored verdict means the row was reviewed again and the
// second answer recorded nothing; an older one belongs to the verdict's own run.
function rejectionNote(
  review: RelevanceReview | undefined,
  rejection: RelevanceReviewRejection,
): string | null {
  if (!rejectionFollowsReview(review, rejection)) return null;
  const because = rejectionReasonLabel(rejection);
  return review
    ? `The newest paid review recorded nothing, because ${because}. The verdict shown is from an earlier run. Reviewing it again spends again.`
    : `The last paid review recorded no verdict here, because ${because}. Reviewing it again spends again.`;
}

/** Which stored text a row's disclosure shows: the board's listing or the publisher's posting. */
export type ListingKind = "board" | "stored";

const LISTING_TEXT: Record<ListingKind, { label: string; caption: string }> = {
  board: { label: "board listing", caption: "Board listing as captured at discovery." },
  stored: { label: "stored posting", caption: "Stored posting as last read from the publisher." },
};

// Mounted only while the disclosure is open, so the full text is fetched on
// demand and never rides along in the pending-list payload.
function CapturedListing({
  artifactId,
  caption,
  id,
  label,
  workspaceId,
}: {
  artifactId: string;
  caption: string;
  id: string;
  label: string;
  workspaceId: string;
}) {
  const text = useArtifactText(workspaceId, artifactId);
  const panel = useRef<HTMLDivElement | null>(null);
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    // The panel opens as a full-width row below a tall row, often off-screen.
    panel.current?.scrollIntoView?.({ block: "nearest" });
  }, []);
  useEffect(() => {
    if (!text.isLoading) return undefined;
    const timer = window.setTimeout(() => setSlow(true), 12000);
    return () => window.clearTimeout(timer);
  }, [text.isLoading]);
  return (
    <div className="pending-listing" id={id} ref={panel}>
      <p className="field-note">
        <strong>{caption}</strong>
      </p>
      {text.isError ? (
        <ErrorState error={text.error} />
      ) : text.data === undefined ? (
        <>
          <LoadingState label={`Loading the ${label}`} />
          {slow ? (
            <p className="field-note" role="status">
              A running board scan can delay this; it appears once the read completes.
            </p>
          ) : null}
        </>
      ) : (
        <pre className="pending-listing__text">{text.data}</pre>
      )}
    </div>
  );
}

// A queue page holds hundreds of these, so the row only re-renders when one of
// its own props changes; the queue keeps every callback prop stable for that.
export const PendingDiscoveryRow = memo(function PendingDiscoveryRow({
  costs,
  detailsOpen,
  dismissalOpen,
  dismissalPending,
  enqueuePending,
  evaluationTask,
  isInFlight,
  isSelected,
  item,
  keyboardFocused,
  openListing,
  onDetailsToggle,
  onDismissalClose,
  onDismissalPendingChange,
  onDismissalToggle,
  onEvaluate,
  onListingToggle,
  onSelect,
  registerCheckbox,
  registerDismissButton,
  relevanceReview,
  workspaceId,
}: {
  costs: TaskCosts;
  detailsOpen: boolean;
  dismissalOpen: boolean;
  dismissalPending: boolean;
  enqueuePending: boolean;
  evaluationTask: Task | undefined;
  isInFlight: boolean;
  isSelected: boolean;
  item: PendingDiscovery;
  keyboardFocused: boolean;
  openListing: ListingKind | null;
  onDetailsToggle: (id: string) => void;
  onDismissalClose: (id: string) => void;
  onDismissalPendingChange: (id: string, pending: boolean) => void;
  onDismissalToggle: (id: string) => void;
  /** Prices and confirms one evaluation, the same path the e key takes. */
  onEvaluate: (item: PendingDiscovery) => void;
  onListingToggle: (id: string, kind: ListingKind) => void;
  onSelect: (item: PendingDiscovery) => void;
  registerCheckbox: (id: string, node: HTMLInputElement | null) => void;
  registerDismissButton: (id: string, node: HTMLButtonElement | null) => void;
  relevanceReview: RelevanceReview | undefined;
  workspaceId: string;
}) {
  const navigate = useNavigate();
  const track = useTrackPendingDiscovery(workspaceId);
  const closingDate = useSetPendingClosingDate(workspaceId);
  const [duplicateTargetId, setDuplicateTargetId] = useState<string | null>(null);
  const [detailsRetained, setDetailsRetained] = useState(detailsOpen);
  if (detailsOpen && !detailsRetained) setDetailsRetained(true);
  const duplicateSuggestions = item.duplicate_suggestions ?? [];
  const rowRef = useRef<HTMLTableRowElement | null>(null);
  useEffect(() => {
    if (keyboardFocused) rowRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [keyboardFocused]);
  // A fresh ref callback detaches and reattaches the node on every render, so
  // these stay stable while the row keeps the same id.
  const checkboxRef = useCallback(
    (node: HTMLInputElement | null) => {
      registerCheckbox(item.id, node);
    },
    [item.id, registerCheckbox],
  );
  const dismissButtonRef = useCallback(
    (node: HTMLButtonElement | null) => {
      registerDismissButton(item.id, node);
    },
    [item.id, registerDismissButton],
  );
  const failed = evaluationTask?.status === "failed";
  const isRunning = evaluationTask?.status === "running";
  const failureReason = failed ? plainErrorCategory(evaluationTask.error_category) : null;
  const failureExcerpt = failed ? (evaluationTask.error_message?.slice(0, 160) ?? "") : "";
  const url = safeHttpUrl(postingAddress(item));
  const displayTitle = item.title || titleFallback(url);
  // No dedicated source field is carried on the discovery, so the URL host labels it.
  const sourceHost = postingHost(url);
  const place = pendingPlace(item);
  const { extras: extraLocations, incomplete: locationsIncomplete } = additionalLocations(item);
  const shownPlaces = `${place.label} ${truncatedExtras(extraLocations)}`.toLowerCase();
  const unnamedCountries = item.countries?.filter(
    (country) => !placeNamesCountry(shownPlaces, country),
  );
  const arrangements = item.arrangement_categories;
  const arrangementSource = arrangementSourceLabel(item.arrangement_categories_source);
  // Publisher-stated is the expected case, so only an inferred source joins the badge text.
  const arrangementSuffix =
    arrangementSource && item.arrangement_categories_source !== "published"
      ? `, ${arrangementSource}`
      : "";
  const markerLabel = postingLabel(item.title, item.employer, url);
  const markers = usePendingRowMarkers(item, markerLabel, workspaceId);
  const remediationActions = screeningRemediations(item);
  const readiness = contentReadiness(item);
  const plainNote = plainContentNote(item.content_note ?? "");
  const closed = item.current_posting_status === "closed";
  // A marked row stays listed after its board drops it; paid work waits like a closed one.
  const gone = closed || Boolean(item.withdrawn_at);
  const { completedDuplicate, jobPostingId: evaluatedJobId } = evaluationState(
    item.evaluated_job_id,
    evaluationTask,
  );
  const alreadyEvaluated = pendingDiscoveryIsEvaluated(item, evaluationTask);
  const evaluationAllowed = readiness === "ready" && !gone;
  const existingDecision = completedDuplicate
    ? statusEntry(completedDuplicate.existingDecision).label
    : null;
  const rejection = item.relevance_review_rejection ?? null;
  const rejectionMessage = rejection ? rejectionNote(relevanceReview, rejection) : null;
  const key = discoveryRelevanceKey(relevanceReview, rejection);
  const initialLabel = discoveryRecommendationLabel(item.recommendation);
  const judged = Boolean(relevanceReview) || Boolean(rejection);
  const badgeLabel = judged ? relevanceLabel(key) : initialLabel;
  const badgeTone = relevanceTone(key);
  const showInitialLine = judged && initialLabel !== badgeLabel;
  const reviewOutOfDate = relevanceReview ? relevanceReviewIsOutOfDate(relevanceReview) : false;
  // The compensation-uncertain route already names an unknown compensation finding.
  // The arrangement badge in Place carries its finding, so no chip repeats it.
  const screeningChips: Chip[] = (item.screening_findings ?? [])
    .filter((finding) => finding.standing !== "match" && finding.dimension !== "arrangement")
    .filter(
      (finding) =>
        !(item.constraint_confidence === "uncertain_comp" && finding.dimension === "compensation"),
    )
    .map((finding) => ({
      label: screeningChipLabel(finding),
      tone: screeningStandingTone(finding.standing),
    }));
  const arrangementFinding = (item.screening_findings ?? []).find(
    (finding) => finding.dimension === "arrangement",
  );
  const arrangementTone = screeningStandingTone(arrangementFinding?.standing);
  const arrangementTitle =
    [arrangementFinding?.subject, arrangementSource ? `Source: ${arrangementSource}` : ""]
      .filter(Boolean)
      .join(". ") || undefined;
  const activity = activityLine(item, relevanceReview);
  const pay = pendingPay(item);
  const constraintLabel = CONSTRAINT_CONFIDENCE_LABELS[item.constraint_confidence];
  const compensationFacts = item.staged_facts.compensation;
  const compensationExplanation =
    item.constraint_confidence === "uncertain_comp"
      ? !compensationFacts?.values?.length
        ? "No pay was confirmed in the saved evidence."
        : !compensationFacts.complete
          ? "Pay range found in posting text; comparison with your pay preference is unconfirmed."
          : undefined
      : undefined;
  const chips = clamped<Chip>(
    [
      ...(sharesPostingText(duplicateSuggestions) ? [{ label: "Possible duplicate" }] : []),
      ...(item.constraint_confidence !== "pursue"
        ? [{ label: constraintLabel, title: compensationExplanation }]
        : []),
      ...screeningChips,
    ],
    MAX_ROW_CHIPS,
  );
  const readinessChip: Chip | null = closed
    ? { label: "Closed at publisher", tone: "muted" }
    : gone
      ? { label: "No longer listed", tone: "muted" }
      : readiness !== "ready"
        ? {
            label: `${READINESS_SHORT_LABEL[readiness]}: ${READINESS_SHORT_REASON[readiness]}`,
            tone: "warning",
          }
        : null;
  const detailsId = `pending-details-${item.id}`;
  const listingArtifacts = (
    [
      ["board", item.listing_artifact_id],
      ["stored", item.retained_content_artifact_id],
    ] as const
  ).filter((entry): entry is readonly [ListingKind, string] => Boolean(entry[1]));
  const openArtifact = listingArtifacts.find(([kind]) => kind === openListing);

  return (
    <Fragment>
      <tr
        aria-current={keyboardFocused ? "true" : undefined}
        className={`pending-discovery-row${isSelected ? " pending-row--selected" : ""}${keyboardFocused ? " pending-row--kbd-focus" : ""}`}
        ref={rowRef}
      >
        <td>
          <input
            aria-label={`Select ${markerLabel}${alreadyEvaluated ? " (already evaluated)" : ""}`}
            checked={isSelected && !alreadyEvaluated}
            disabled={isInFlight || alreadyEvaluated}
            onChange={() => onSelect(item)}
            ref={checkboxRef}
            type="checkbox"
          />
        </td>
        <th scope="row">
          <EmployerName employer={item.employer} url={url} />
          {item.board_employer ? (
            <p className="field-note">On {item.board_employer}&apos;s board</p>
          ) : null}
        </th>
        <td className={`content-readiness content-readiness--${readiness}`}>
          <PendingRowTitle
            currentTitle={item.title ?? ""}
            displayTitle={displayTitle}
            pendingId={item.id}
            url={url}
            workspaceId={workspaceId}
          />{" "}
          <PendingRowMarkerToggles markers={markers} />
          {readinessChip ? (
            <span className={`chip status--tone-${readinessChip.tone} pending-readiness`}>
              {readinessChip.label}
            </span>
          ) : null}
          {item.matched_target_title ? (
            <p className="field-note">Matches: {item.matched_target_title}</p>
          ) : null}
          {item.current_robots_basis === "single_posting_read" ? (
            <p
              className="field-note"
              title="robots.txt disallows crawling this page; TailorCV opened this one posting once, as a person would."
            >
              Single page read
            </p>
          ) : null}
          <p className="field-note pending-origin">
            <span>{STAGING_PROVENANCE_LABELS[item.staging_provenance]}</span>
            <span>First seen {item.first_seen_on}</span>
            <span>Rank {item.rank ?? "not stated"}</span>
            {aggregatorCredit(item.discovery_url) ? (
              <AggregatorCreditLink url={item.discovery_url} />
            ) : sourceHost ? (
              <span className="pending-origin__host" title={`Listed on ${sourceHost}`}>
                <HostName host={sourceHost} />
              </span>
            ) : null}
            {activity ? <span>{activity}</span> : null}
          </p>
          <PendingRowKeepNote markers={markers} />
        </td>
        <td>
          <span className={place.unresolved ? "field-note" : undefined}>{place.label}</span>
          {extraLocations.length > 0 ? (
            <p className="field-note" title={extraLocations.join(", ")}>
              Also: {truncatedExtras(extraLocations)}
            </p>
          ) : null}
          <div className="pending-work-facts">
            {arrangements.map((value, index) => (
              <StatusBadge
                entry={{
                  label: `${arrangementLabel(value, item)}${index === arrangements.length - 1 ? arrangementSuffix : ""}`,
                  tone: arrangementTone,
                }}
                key={value}
                title={arrangementTitle}
              />
            ))}
            <span className="pending-work-fact">{pendingEmployment(item)}</span>
            {pay ? <span className="pending-work-fact">{pay}</span> : null}
            <CountryToken countries={unnamedCountries} />
          </div>
          <ClosingDates
            compact
            publisher={item.publisher_closing_date}
            override={item.closing_date_override}
            label={`Closing date summary for ${item.title}`}
          />
        </td>
        <td className="pending-recommendation">
          <div className="pending-verdict">
            <span className={`status status--tone-${badgeTone} pending-verdict__badge`}>
              {badgeLabel}
            </span>
            {reviewOutOfDate ? (
              <span className="pending-verdict__stale" role="status">
                Out of date
              </span>
            ) : null}
          </div>
          {showInitialLine ? <p className="field-note">Initial: {initialLabel}</p> : null}
          {rejectionMessage ? <p className="field-note">{rejectionMessage}</p> : null}
          {relevanceReview?.disputed_at ? (
            <p className="field-note">
              Model verdict: {relevanceLabel(modelRelevanceKey(relevanceReview))}
            </p>
          ) : null}
          <p
            className="field-note pending-recommendation__reason"
            title={item.recommendation_because}
          >
            {item.recommendation_because}
          </p>
          {relevanceReview?.summary ? (
            <p
              className="field-note pending-recommendation__summary"
              title={relevanceReview.summary}
            >
              {relevanceReview.summary}
            </p>
          ) : null}
          {chips.shown.length > 0 ? (
            <ul className="chip-row chip-row--fit">
              {chips.shown.map((chip, index) => (
                <li
                  className={chip.tone ? `chip status--tone-${chip.tone}` : "chip"}
                  key={`${chip.label}-${index}`}
                  title={chip.title ?? chip.label}
                >
                  {chip.label}
                </li>
              ))}
              {chips.rest.length > 0 ? (
                <li
                  className="chip chip--count"
                  title={chips.rest.map((chip) => chip.label).join(", ")}
                >
                  +{chips.rest.length}
                </li>
              ) : null}
            </ul>
          ) : null}
        </td>
        <td>
          <div className="pending-row-actions">
            {!alreadyEvaluated && (
              <button
                type="button"
                className="button button--quiet"
                disabled={track.isPending || !item.employer.trim() || !item.title.trim()}
                onClick={() =>
                  track.mutate(item.id, {
                    onSuccess: (job) => {
                      void navigate(`/jobs/${job.job_posting_id}`);
                    },
                  })
                }
              >
                {track.isPending ? "Tracking" : "Track job"}
              </button>
            )}
            {alreadyEvaluated ? (
              <>
                <span className="field-note">Already evaluated</span>
                {/* A batch task records no per-row job id, so the link waits for the refreshed row. */}
                {evaluatedJobId ? (
                  <Link
                    className="button button--quiet"
                    to={`/jobs/${evaluatedJobId}?tab=evaluation`}
                  >
                    {completedDuplicate ? "View existing evaluation" : "View evaluated job"}
                  </Link>
                ) : null}
              </>
            ) : gone ? (
              <span className="field-note">
                {closed ? "Closure recorded" : "No longer listed"}; evaluation disabled
              </span>
            ) : evaluationAllowed ? (
              <PaidAction
                costs={costs}
                disabled={(!failed && isInFlight) || enqueuePending}
                label={
                  failed
                    ? "Retry evaluation"
                    : isRunning
                      ? "Running"
                      : isInFlight
                        ? "Queued"
                        : "Full evaluation"
                }
                onPrepare={() => onEvaluate(item)}
                taskType="discovery.evaluate_batch"
              />
            ) : readiness === "screening_refused" ? (
              <span className="field-note">Review your search rules or dismiss this posting.</span>
            ) : (
              <PendingPostingRemediation item={item} workspaceId={workspaceId} />
            )}
            <button
              aria-controls={detailsId}
              aria-expanded={detailsOpen}
              className="button button--quiet"
              onClick={() => onDetailsToggle(item.id)}
              type="button"
            >
              {detailsOpen ? "Hide details" : "Details"}
            </button>
            <button
              aria-controls={`pending-dismissal-${item.id}`}
              aria-expanded={dismissalOpen}
              aria-label="Dismiss from pending"
              className="button button--danger"
              disabled={dismissalPending}
              onClick={() => onDismissalToggle(item.id)}
              ref={dismissButtonRef}
              type="button"
            >
              Dismiss
            </button>
          </div>
        </td>
      </tr>
      {detailsOpen || detailsRetained ? (
        <tr className="pending-row-editor" hidden={!detailsOpen}>
          <td colSpan={6}>
            <div className="pending-row-details" id={detailsId}>
              <div className="pending-row-details__group">
                {readiness === "session_required" ? (
                  <>
                    {plainNote ? <p className="field-note">{plainNote}</p> : null}
                    <p className="field-note">
                      This publisher needs you to be signed in. Open the posting in your own
                      browser, then attach its text below.
                    </p>
                  </>
                ) : READINESS_DETAIL_TEXT[readiness] ? (
                  <>
                    <p className="field-note">{READINESS_DETAIL_TEXT[readiness]}</p>
                    {plainNote && plainNote !== READINESS_DETAIL_TEXT[readiness] ? (
                      <p className="field-note">{plainNote}</p>
                    ) : null}
                  </>
                ) : (
                  <>
                    {CONTENT_STATUS_LABELS[item.content_status] !==
                    CONTENT_READINESS_LABELS[readiness] ? (
                      <p className="field-note">{CONTENT_STATUS_LABELS[item.content_status]}</p>
                    ) : null}
                    {plainNote ? <p className="field-note">{plainNote}</p> : null}
                  </>
                )}
                {closed ? (
                  <p className="field-note">
                    Saved publisher evidence reports this posting closed. Select it to record the
                    closure without another publisher read.
                  </p>
                ) : null}
                {item.content_status === "policy_refused" ? (
                  <p className="field-note">
                    The publisher refused the read under its policy. Refresh postings retries; if
                    refused again, paste the text below.
                  </p>
                ) : null}
              </div>
              <div className="pending-row-details__group">
                {extraLocations.length || locationsIncomplete ? (
                  <p className="field-note">
                    {extraLocations.length ? `Also listed: ${extraLocations.join(", ")}.` : null}
                    {extraLocations.length && locationsIncomplete ? " " : null}
                    {locationsIncomplete ? "Location list incomplete." : null}
                  </p>
                ) : null}
                {item.also_in?.length ? (
                  <p className="field-note">Also advertised in {item.also_in.join(", ")}.</p>
                ) : null}
                <ClosingDates
                  publisher={item.publisher_closing_date}
                  override={item.closing_date_override}
                  label={`Closing dates for ${item.title}`}
                  onSave={async (value) => {
                    await closingDate.mutateAsync({
                      pendingId: item.id,
                      body: { closing_date_override: value },
                    });
                  }}
                />
              </div>
              <div className="pending-row-details__group">
                <p className="field-note">Constraint route: {constraintLabel ?? "Not reported"}</p>
                {compensationExplanation ? (
                  <p className="field-note">{compensationExplanation}</p>
                ) : null}
                {remediationActions.length > 0 ? (
                  <div className="field-note">
                    <p>
                      This row remains saved. You can check these findings before deciding whether
                      to keep or dismiss it:
                    </p>
                    <ul>
                      {remediationActions.map((action) => (
                        <li key={action}>{action}</li>
                      ))}
                    </ul>
                    <Link to="/settings?tab=search">Review search preferences</Link>
                  </div>
                ) : null}
                {url ? (
                  <a href={url} rel="noreferrer" target="_blank">
                    Review job posting
                  </a>
                ) : null}
                {listingArtifacts.map(([kind]) => (
                  <button
                    aria-controls={`pending-listing-${item.id}`}
                    aria-expanded={openListing === kind}
                    className="button button--quiet"
                    key={kind}
                    onClick={() => onListingToggle(item.id, kind)}
                    type="button"
                  >
                    {openListing === kind ? "Hide" : "Show"} {LISTING_TEXT[kind].label}
                  </button>
                ))}
                {relevanceReview ? (
                  <RelevanceReviewCard review={relevanceReview} workspaceId={workspaceId} />
                ) : null}
              </div>
              <div className="pending-row-details__group">
                <PendingDiscoveryProvenance discovery={item} />
              </div>
              <PendingDuplicateSuggestions
                onDismissAsDuplicateOf={(keeperId) => {
                  setDuplicateTargetId(keeperId);
                  if (!dismissalOpen) onDismissalToggle(item.id);
                }}
                pendingDiscoveryId={item.id}
                suggestions={duplicateSuggestions}
              />
            </div>
          </td>
        </tr>
      ) : null}
      {dismissalOpen ? (
        <tr className="pending-row-editor">
          <td colSpan={6}>
            <DismissalEditor
              id={`pending-dismissal-${item.id}`}
              initialCategory={duplicateTargetId ? "duplicate_pending" : undefined}
              initialDuplicateOfJobId={evaluatedJobId ?? undefined}
              initialDuplicateOfPendingId={duplicateTargetId ?? undefined}
              key={duplicateTargetId ?? "unprompted"}
              marked={Boolean(item.favorite || item.kept)}
              onClose={() => {
                setDuplicateTargetId(null);
                onDismissalClose(item.id);
              }}
              onPendingChange={(pending) => onDismissalPendingChange(item.id, pending)}
              pendingDiscoveryIds={[item.id]}
              workspaceId={workspaceId}
            />
          </td>
        </tr>
      ) : null}
      {openArtifact ? (
        <tr className="pending-row-editor">
          <td colSpan={6}>
            <CapturedListing
              artifactId={openArtifact[1]}
              caption={LISTING_TEXT[openArtifact[0]].caption}
              id={`pending-listing-${item.id}`}
              key={openArtifact[0]}
              label={LISTING_TEXT[openArtifact[0]].label}
              workspaceId={workspaceId}
            />
          </td>
        </tr>
      ) : null}
      {failed && failureReason ? (
        <tr className="pending-row-alert">
          <td colSpan={6}>
            <div className="pending-row-alert__content" role="status">
              <strong>Evaluation failed:</strong> {failureReason.plain} ({failureReason.raw})
              {failureExcerpt ? `. ${failureExcerpt}` : ""}
            </div>
          </td>
        </tr>
      ) : null}
      {completedDuplicate ? (
        <tr className="pending-row-alert">
          <td colSpan={6}>
            <div className="pending-row-alert__content" role="status">
              <strong>No paid evaluation ran.</strong> This discovery already has an evaluation with
              the {existingDecision} decision. Open the existing evaluation instead.
            </div>
          </td>
        </tr>
      ) : null}
    </Fragment>
  );
});
