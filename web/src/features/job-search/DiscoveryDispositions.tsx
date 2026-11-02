import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";

import type { PendingDiscovery } from "../../api/client";
import type { ExplicitDismissalCategory } from "../../api/client/types";
import type { DismissedDiscovery } from "../../api/client/types";
import { SortableHeader } from "../../components/SortableHeader";
import { compareValues, nextSort, type SortDirection } from "../../components/sorting";
import {
  useAllJobs,
  useDismissedPendingDiscoveries,
  usePendingDuplicateCandidates,
  usePendingDispositionHistory,
  useSetPendingMarkers,
} from "../../api/hooks";
import { ErrorState, LoadingState } from "../../components/States";
import { safeHttpUrl } from "../../api/safeUrl";
import { AggregatorCreditNote } from "../../components/AggregatorCredit";
import { postingAddress } from "../../components/aggregatorHosts";
import { EmployerName } from "./EmployerName";
import { postingLabel } from "./fallbacks";
import { neverReadDiscoveryIds } from "./pendingDiscoveryFilterModel";
import { SkippedMarkedNote } from "./SkippedMarkedNote";
import { useDispositionAction } from "./useDispositionAction";
import { KEEP_NOTE_MAX } from "./usePendingRowMarkers";

const EXPLICIT_DISMISSAL_OPTIONS = [
  ["closed_at_source", "Closed at source"],
  ["out_of_scope_title", "Title outside scope"],
  ["out_of_scope_location", "Location outside scope"],
  ["out_of_scope_arrangement", "Work arrangement outside scope"],
  ["out_of_scope_travel", "Travel requirement outside scope"],
  ["out_of_scope_compensation", "Compensation outside scope"],
  ["invalid_page", "Not a job posting"],
  ["duplicate_pending", "Duplicate pending discovery"],
  ["duplicate_posting", "Duplicate posting"],
  ["employer_excluded", "Employer excluded"],
  ["current_evidence_unavailable", "Current evidence unavailable"],
  ["other", "Other reason"],
] as const satisfies ReadonlyArray<readonly [ExplicitDismissalCategory, string]>;

const EXPLICIT_DISMISSAL_REASONS: Record<Exclude<ExplicitDismissalCategory, "other">, string> = {
  closed_at_source: "The posting is closed at its source.",
  out_of_scope_title: "The title is outside the current search scope.",
  out_of_scope_location: "The location is outside the current search scope.",
  out_of_scope_arrangement: "The work arrangement is outside the current search scope.",
  out_of_scope_travel: "The travel requirement is outside the current search scope.",
  out_of_scope_compensation: "The compensation is outside the current search scope.",
  invalid_page: "The page is not a job posting.",
  duplicate_pending: "The posting duplicates another pending discovery.",
  duplicate_posting: "The posting duplicates another tracked opportunity.",
  employer_excluded: "The employer is excluded from the current search.",
  current_evidence_unavailable:
    "The posting was not evaluated: I chose to dismiss it because trustworthy current posting evidence remained unavailable.",
};

function dismissalCategoryLabel(code: string): string {
  return (
    EXPLICIT_DISMISSAL_OPTIONS.find(([value]) => value === code)?.[1] ?? code.replaceAll("_", " ")
  );
}

const DISPOSITION_ORIGIN_LABELS: Record<string, string> = {
  "evaluation.refusal": "Automatic hard-gate refusal",
  "relevance_review.refusal": "Automatic hard-gate refusal",
};

const DISMISSED_PAGE_SIZE = 100;

type DismissedSortKey = "employer" | "title" | "location" | "category" | "dismissed";
type DismissalOrigin = "all" | "automatic" | "explicit" | "rule";

function dismissalOrigin(
  event: Pick<DismissedDiscovery, "origin" | "selection_mode">,
): DismissalOrigin {
  return event.origin in DISPOSITION_ORIGIN_LABELS ? "automatic" : event.selection_mode;
}

function dispositionOriginLabel(
  origin: string,
  selectionMode: DismissedDiscovery["selection_mode"],
): string {
  if (origin in DISPOSITION_ORIGIN_LABELS) return DISPOSITION_ORIGIN_LABELS[origin] ?? "";
  return selectionMode === "explicit" ? "Human choice" : "Screening rule";
}

function dismissedOn(value: string | undefined): string {
  const parsed = value ? new Date(value) : null;
  return parsed && !Number.isNaN(parsed.valueOf()) ? parsed.toISOString().slice(0, 10) : "";
}

function dismissalSortValue(event: DismissedDiscovery, key: DismissedSortKey): string {
  switch (key) {
    case "dismissed":
      return event.created_at;
    default:
      return event[key];
  }
}

export function DismissalEditor({
  id,
  initialCategory,
  initialDuplicateOfJobId,
  initialDuplicateOfPendingId,
  marked = false,
  neverReadIds,
  onApplied,
  onClose,
  onPendingChange,
  pendingDiscoveryIds,
  workspaceId,
}: {
  id: string;
  initialCategory?: ExplicitDismissalCategory;
  initialDuplicateOfJobId?: string;
  initialDuplicateOfPendingId?: string;
  // A favorite or kept row: the confirmation says so and the dismissal clears its markers.
  marked?: boolean;
  neverReadIds?: ReadonlySet<string>;
  onApplied?: (skippedMarked: number) => void;
  onClose: () => void;
  onPendingChange?: (pending: boolean) => void;
  pendingDiscoveryIds: string[];
  workspaceId: string;
}) {
  const [category, setCategory] = useState<ExplicitDismissalCategory | "">(initialCategory ?? "");
  const [note, setNote] = useState("");
  const [duplicateJobSearch, setDuplicateJobSearch] = useState("");
  const [duplicateOfJobId, setDuplicateOfJobId] = useState(initialDuplicateOfJobId ?? "");
  const [duplicatePendingSearch, setDuplicatePendingSearch] = useState("");
  const [duplicateOfPendingId, setDuplicateOfPendingId] = useState(
    initialDuplicateOfPendingId ?? "",
  );
  // A duplicate can correctly point to an archived evaluation. The disposition
  // contract accepts it, so the picker must render that exact selected target.
  const jobs = useAllJobs(workspaceId, { includeArchived: true });
  const pendingCandidates = usePendingDuplicateCandidates(
    workspaceId,
    category === "duplicate_pending",
  );
  const categoryRef = useRef<HTMLSelectElement | null>(null);
  const action = useDispositionAction(workspaceId, neverReadIds);
  const trimmedNote = note.trim();
  const reason =
    category === "other" ? trimmedNote : category ? EXPLICIT_DISMISSAL_REASONS[category] : "";
  const duplicateSearchNeedle = duplicateJobSearch.trim().toLowerCase();
  const matchingDuplicateJobs = (jobs.data ?? []).filter(
    (job) =>
      !duplicateSearchNeedle ||
      `${job.title} ${job.employer}`.toLowerCase().includes(duplicateSearchNeedle),
  );
  const selectedDuplicateJob = (jobs.data ?? []).find(
    (job) => job.job_posting_id === duplicateOfJobId,
  );
  const duplicateJobs =
    selectedDuplicateJob && !matchingDuplicateJobs.includes(selectedDuplicateJob)
      ? [selectedDuplicateJob, ...matchingDuplicateJobs]
      : matchingDuplicateJobs;
  const duplicatePendingNeedle = duplicatePendingSearch.trim().toLowerCase();
  const matchingPendingCandidates = (pendingCandidates.data ?? []).filter(
    (candidate) =>
      !pendingDiscoveryIds.includes(candidate.id) &&
      (!duplicatePendingNeedle ||
        `${candidate.title} ${candidate.employer} ${candidate.location}`
          .toLowerCase()
          .includes(duplicatePendingNeedle)),
  );
  // A target chosen elsewhere can leave the queue before this list loads.
  const pendingTargetMissing =
    category === "duplicate_pending" &&
    duplicateOfPendingId !== "" &&
    pendingCandidates.data !== undefined &&
    !pendingCandidates.data.some((candidate) => candidate.id === duplicateOfPendingId);
  const pendingTargetRef = useRef<HTMLSelectElement | null>(null);
  useEffect(
    () =>
      (initialCategory === "duplicate_pending" ? pendingTargetRef : categoryRef).current?.focus(),
    [initialCategory],
  );
  const closeOnEscape = (event: KeyboardEvent) => {
    if (event.key !== "Escape" || action.pending) return;
    event.preventDefault();
    onClose();
  };
  const previewDismissal = () => {
    if (!category || !reason) return;
    onPendingChange?.(true);
    void action
      .act({
        action: "dismiss",
        category,
        dismiss_marked: marked,
        duplicate_of_job_id: category === "duplicate_posting" ? duplicateOfJobId : undefined,
        duplicate_of_pending_id:
          category === "duplicate_pending" ? duplicateOfPendingId : undefined,
        pending_discovery_ids: pendingDiscoveryIds,
        reason,
        selection_mode: "explicit",
      })
      .then((applied) => {
        if (!applied) return;
        onApplied?.(applied.skippedMarked);
        onClose();
      })
      .finally(() => onPendingChange?.(false));
  };
  return (
    <form
      className="discovery-disposition__form"
      id={id}
      onSubmit={(event) => event.preventDefault()}
    >
      <div className="discovery-disposition__category">
        <label htmlFor={`${id}-category`}>Dismissal reason</label>
        <select
          id={`${id}-category`}
          onChange={(event) => {
            const nextCategory = event.target.value as ExplicitDismissalCategory | "";
            setCategory(nextCategory);
            setNote("");
            setDuplicateJobSearch("");
            setDuplicatePendingSearch("");
            setDuplicateOfPendingId(
              nextCategory === "duplicate_pending" ? (initialDuplicateOfPendingId ?? "") : "",
            );
            setDuplicateOfJobId(
              nextCategory === "duplicate_posting" ? (initialDuplicateOfJobId ?? "") : "",
            );
          }}
          onKeyDown={closeOnEscape}
          ref={categoryRef}
          value={category}
        >
          <option value="">Choose a reason</option>
          {EXPLICIT_DISMISSAL_OPTIONS.map(([value, optionLabel]) => (
            <option key={value} value={value}>
              {optionLabel}
            </option>
          ))}
        </select>
      </div>
      {category === "other" && (
        <div className="discovery-disposition__other">
          <label htmlFor={`${id}-other-reason`}>Other reason</label>
          <textarea
            id={`${id}-other-reason`}
            onChange={(event) => setNote(event.target.value)}
            onKeyDown={closeOnEscape}
            placeholder="Why this should leave the pending list"
            rows={2}
            value={note}
          />
        </div>
      )}
      {category === "duplicate_posting" && (
        <div className="discovery-disposition__other">
          <label htmlFor={`${id}-duplicate-job-search`}>Find tracked job</label>
          <input
            id={`${id}-duplicate-job-search`}
            onChange={(event) => setDuplicateJobSearch(event.target.value)}
            onKeyDown={closeOnEscape}
            placeholder="Employer or role"
            type="search"
            value={duplicateJobSearch}
          />
          <label htmlFor={`${id}-duplicate-job`}>Duplicate of tracked job</label>
          <select
            id={`${id}-duplicate-job`}
            onChange={(event) => setDuplicateOfJobId(event.target.value)}
            onKeyDown={closeOnEscape}
            value={duplicateOfJobId}
          >
            <option value="">Choose a tracked job</option>
            {duplicateJobs.map((job) => (
              <option key={job.job_posting_id} value={job.job_posting_id}>
                {job.title} at {job.employer}
                {job.archived_at ? " (archived)" : ""}
                {job.job_posting_id === duplicateOfJobId ? " (selected)" : ""}
              </option>
            ))}
          </select>
          {duplicateSearchNeedle && !jobs.isLoading && matchingDuplicateJobs.length === 0 ? (
            <p className="field-note">No other tracked job matches that search.</p>
          ) : null}
          {jobs.isError ? <ErrorState error={jobs.error} /> : null}
        </div>
      )}
      {category === "duplicate_pending" && (
        <div className="discovery-disposition__other">
          <label htmlFor={`${id}-duplicate-pending-search`}>Find pending discovery</label>
          <input
            id={`${id}-duplicate-pending-search`}
            onChange={(event) => setDuplicatePendingSearch(event.target.value)}
            onKeyDown={closeOnEscape}
            placeholder="Employer, role, or location"
            type="search"
            value={duplicatePendingSearch}
          />
          <label htmlFor={`${id}-duplicate-pending`}>Duplicate of pending discovery</label>
          <select
            id={`${id}-duplicate-pending`}
            onChange={(event) => setDuplicateOfPendingId(event.target.value)}
            onKeyDown={closeOnEscape}
            ref={pendingTargetRef}
            value={pendingTargetMissing ? "" : duplicateOfPendingId}
          >
            <option value="">Choose a pending discovery</option>
            {matchingPendingCandidates.map((candidate) => (
              <option key={candidate.id} value={candidate.id}>
                {candidate.title} at {candidate.employer}
                {candidate.location ? ` (${candidate.location})` : ""}
                {candidate.dismissed ? " (dismissed)" : ""}
              </option>
            ))}
          </select>
          {pendingCandidates.isLoading ? (
            <p className="field-note">Loading pending discoveries...</p>
          ) : null}
          {pendingTargetMissing ? (
            <p className="field-error" role="alert">
              The suggested job is no longer in the pending list. Choose another, or pick a
              different reason.
            </p>
          ) : null}
          {duplicatePendingNeedle &&
          !pendingCandidates.isLoading &&
          matchingPendingCandidates.length === 0 ? (
            <p className="field-note">No other pending discovery matches that search.</p>
          ) : null}
          {pendingCandidates.isError ? <ErrorState error={pendingCandidates.error} /> : null}
        </div>
      )}
      <div className="button-row">
        <button
          className="button button--danger"
          disabled={
            !reason.trim() ||
            action.pending ||
            (category === "duplicate_posting" && (!duplicateOfJobId || jobs.isLoading)) ||
            (category === "duplicate_pending" &&
              (!duplicateOfPendingId || pendingCandidates.isLoading || pendingTargetMissing))
          }
          onClick={previewDismissal}
          onKeyDown={closeOnEscape}
          type="button"
        >
          Preview dismissal
        </button>
        <button
          className="button button--quiet"
          disabled={action.pending}
          onClick={onClose}
          onKeyDown={closeOnEscape}
          type="button"
        >
          Cancel
        </button>
      </div>
      {action.error && <ErrorState error={action.error} />}
    </form>
  );
}

export function DismissSelection({
  label,
  neverReadIds,
  onSkippedMarkedChange,
  pendingDiscoveryIds,
  workspaceId,
}: {
  label: string;
  neverReadIds?: ReadonlySet<string>;
  // A caller that outlives this selection shows the skipped-row note itself.
  onSkippedMarkedChange?: (skippedMarked: number) => void;
  pendingDiscoveryIds: string[];
  workspaceId: string;
}) {
  const formId = useId();
  const [open, setOpen] = useState(false);
  const [editorPending, setEditorPending] = useState(false);
  const [ownSkippedMarked, setOwnSkippedMarked] = useState(0);
  const setSkippedMarked = onSkippedMarkedChange ?? setOwnSkippedMarked;
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const close = () => {
    setOpen(false);
    setEditorPending(false);
    triggerRef.current?.focus();
  };
  return (
    <div className="discovery-disposition">
      <button
        className="button button--danger"
        aria-controls={formId}
        aria-expanded={open}
        disabled={pendingDiscoveryIds.length === 0 || editorPending}
        onClick={() => {
          setSkippedMarked(0);
          setOpen((current) => !current);
        }}
        ref={triggerRef}
        type="button"
      >
        {label}
      </button>
      {open && (
        <DismissalEditor
          id={formId}
          neverReadIds={neverReadIds}
          onApplied={setSkippedMarked}
          onClose={close}
          onPendingChange={setEditorPending}
          pendingDiscoveryIds={pendingDiscoveryIds}
          workspaceId={workspaceId}
        />
      )}
      <SkippedMarkedNote count={ownSkippedMarked} />
    </div>
  );
}

export function DiscoveryDispositionManager({
  discoveries,
  visibleDiscoveries,
  workspaceId,
}: {
  discoveries: PendingDiscovery[];
  visibleDiscoveries: PendingDiscovery[];
  workspaceId: string;
}) {
  const dismissed = useDismissedPendingDiscoveries(workspaceId);
  const [historyId, setHistoryId] = useState("");
  const history = usePendingDispositionHistory(workspaceId, historyId);
  const neverRead = neverReadDiscoveryIds(discoveries);
  const bulk = useDispositionAction(workspaceId, neverRead);
  const restore = useDispositionAction(workspaceId);
  const keepMarkers = useSetPendingMarkers(workspaceId);
  // The confirm dialog renders the note input outside this tree, so its value lands here.
  const keepNote = useRef("");
  const [unkeptLabel, setUnkeptLabel] = useState("");
  const restoreAndKeep = async (event: DismissedDiscovery) => {
    keepNote.current = "";
    setUnkeptLabel("");
    keepMarkers.reset();
    const restored = await restore.act(
      {
        action: "restore",
        category: event.category,
        pending_discovery_ids: [event.pending_discovery_id],
        reason: `Restore dismissal: ${event.reason}`,
        selection_mode: "explicit",
      },
      {
        confirmLabel: "Restore and keep",
        details: (
          <div>
            <p className="callout">
              The row returns to the pending queue marked Kept, so bulk and program dismissals skip
              it.
            </p>
            <label className="compact-field">
              Keep note (optional)
              <input
                maxLength={KEEP_NOTE_MAX}
                onChange={(change) => {
                  keepNote.current = change.target.value;
                }}
                placeholder="Why you are keeping it"
                type="text"
              />
            </label>
          </div>
        ),
      },
    );
    if (!restored) return;
    try {
      await keepMarkers.mutateAsync({
        pendingId: event.pending_discovery_id,
        body: {
          favorite: false,
          kept: true,
          keep_note: keepNote.current.replace(/[\r\n]+/g, " ").trim() || null,
        },
      });
    } catch {
      setUnkeptLabel(`${event.title} at ${event.employer}`);
    }
  };
  const dismissedId = useId();
  const [dismissedSearch, setDismissedSearch] = useState("");
  const [dismissedCategory, setDismissedCategory] = useState("all");
  const [dismissedOrigin, setDismissedOrigin] = useState<DismissalOrigin>("all");
  const [dismissedSortKey, setDismissedSortKey] = useState<DismissedSortKey>("dismissed");
  const [dismissedSortDirection, setDismissedSortDirection] = useState<SortDirection>("desc");
  const [dismissedOffset, setDismissedOffset] = useState(0);
  const dismissedRows = Array.isArray(dismissed.data) ? dismissed.data : [];
  const categoryCounts = new Map<string, number>();
  for (const event of dismissedRows) {
    categoryCounts.set(event.category, (categoryCounts.get(event.category) ?? 0) + 1);
  }
  const dismissedCategories = [...categoryCounts.entries()].sort(([left], [right]) =>
    left.localeCompare(right),
  );
  const needle = dismissedSearch.trim().toLowerCase();
  const visibleDismissed = dismissedRows
    .filter(
      (event) =>
        (dismissedCategory === "all" || event.category === dismissedCategory) &&
        (dismissedOrigin === "all" || dismissalOrigin(event) === dismissedOrigin) &&
        (needle === "" ||
          `${event.title} ${event.employer} ${event.location} ${event.category} ${event.reason} ${dispositionOriginLabel(event.origin, event.selection_mode)}`
            .toLowerCase()
            .includes(needle)),
    )
    .sort((left, right) =>
      compareValues(
        dismissalSortValue(left, dismissedSortKey),
        dismissalSortValue(right, dismissedSortKey),
        dismissedSortDirection,
      ),
    );
  // The dismissed endpoint is unpaginated, so the page is sliced here.
  const dismissedPageStart =
    dismissedOffset < visibleDismissed.length
      ? dismissedOffset
      : Math.max(
          0,
          (Math.ceil(visibleDismissed.length / DISMISSED_PAGE_SIZE) - 1) * DISMISSED_PAGE_SIZE,
        );
  const pagedDismissed = visibleDismissed.slice(
    dismissedPageStart,
    dismissedPageStart + DISMISSED_PAGE_SIZE,
  );
  const sortDismissed = (key: DismissedSortKey) => {
    const next = nextSort(dismissedSortKey, dismissedSortDirection, key);
    setDismissedSortKey(next.key);
    setDismissedSortDirection(next.direction);
    setDismissedOffset(0);
  };
  const deterministic = new Map<string, string[]>();
  for (const discovery of visibleDiscoveries) {
    for (const finding of discovery.screening_findings) {
      if (finding.standing !== "refuse") continue;
      deterministic.set(finding.code, [...(deterministic.get(finding.code) ?? []), discovery.id]);
    }
  }
  return (
    <details className="discovery-dispositions discovery-advanced" tabIndex={-1}>
      <summary>Pending-list dispositions</summary>
      <p>
        Dismissal removes a discovery from this pending view and is always reversible. It can come
        from your own choice, from a screening rule, or automatically when an evaluation refuses a
        hard gate. A refusal is also kept as its own separate record, and neither dismissal nor
        refusal archives the job.
      </p>
      {visibleDiscoveries.length > 0 && (
        <fieldset>
          <legend>Dismiss the current filtered cohort</legend>
          <p>
            This cohort is the {visibleDiscoveries.length}{" "}
            {visibleDiscoveries.length === 1 ? "discovery" : "discoveries"} the current view lists,
            after every search, filter, marker, and page choice above. Preview names every primary
            and regional variant before anything changes. Dismiss an individual discovery from its
            row instead.
          </p>
          <DismissSelection
            label={`Dismiss all shown (${visibleDiscoveries.length})`}
            neverReadIds={neverRead}
            pendingDiscoveryIds={visibleDiscoveries.map((item) => item.id)}
            workspaceId={workspaceId}
          />
        </fieldset>
      )}
      {deterministic.size > 0 && (
        <div>
          <h4>Deterministic category previews</h4>
          <p>Only categories the current screening rules prove are offered for bulk dismissal.</p>
          <div className="button-row">
            {[...deterministic].map(([code, ids]) => (
              <button
                className="button button--danger"
                disabled={bulk.pending}
                key={code}
                onClick={() =>
                  void bulk.act({
                    action: "dismiss",
                    category: code,
                    pending_discovery_ids: ids,
                    reason: `Deterministic screening: ${code.replaceAll("_", " ")}.`,
                    selection_mode: "rule",
                  })
                }
                type="button"
              >
                Preview {code.replaceAll("_", " ")} ({ids.length})
              </button>
            ))}
          </div>
          <SkippedMarkedNote count={bulk.skippedMarked} />
          {bulk.error && <ErrorState error={bulk.error} />}
        </div>
      )}
      <details>
        <summary>Dismissed discoveries ({dismissed.data?.length ?? 0})</summary>
        <p>
          Categories retain the code recorded by their originating rule or decision. Similar names
          remain separate in counts and filters; the reason and origin explain each dismissal.
        </p>
        <div className="field-grid">
          <label className="compact-field" htmlFor={`${dismissedId}-search`}>
            Find a dismissed discovery
            <input
              id={`${dismissedId}-search`}
              onChange={(event) => {
                setDismissedSearch(event.target.value);
                setDismissedOffset(0);
              }}
              placeholder="Employer, title, place, reason, or origin"
              type="search"
              value={dismissedSearch}
            />
          </label>
          <label className="compact-field" htmlFor={`${dismissedId}-category`}>
            Dismissal category
            <select
              id={`${dismissedId}-category`}
              onChange={(event) => {
                setDismissedCategory(event.target.value);
                setDismissedOffset(0);
              }}
              value={dismissedCategory}
            >
              <option value="all">All categories</option>
              {dismissedCategories.map(([value, count]) => (
                <option key={value} value={value}>
                  {dismissalCategoryLabel(value)} [{value}] ({count})
                </option>
              ))}
            </select>
          </label>
          <label className="compact-field" htmlFor={`${dismissedId}-origin`}>
            Dismissal origin
            <select
              id={`${dismissedId}-origin`}
              onChange={(event) => {
                setDismissedOrigin(event.target.value as DismissalOrigin);
                setDismissedOffset(0);
              }}
              value={dismissedOrigin}
            >
              <option value="all">All origins</option>
              <option value="explicit">Human choice</option>
              <option value="rule">Screening rule</option>
              <option value="automatic">Automatic evaluation refusal</option>
            </select>
          </label>
        </div>
        {!dismissed.isPending && !dismissed.isError && dismissedRows.length > 0 && (
          <p aria-live="polite" className="field-note">
            {visibleDismissed.length > DISMISSED_PAGE_SIZE
              ? `Showing ${dismissedPageStart + 1}-${dismissedPageStart + pagedDismissed.length} of ${visibleDismissed.length} matching, from ${dismissedRows.length} dismissed discoveries.`
              : `Showing ${visibleDismissed.length} of ${dismissedRows.length} dismissed discoveries.`}
          </p>
        )}
        {dismissed.isPending && <LoadingState label="Loading dismissed discoveries" />}
        {dismissed.isError && <ErrorState error={dismissed.error} />}
        {!dismissed.isPending && !dismissed.isError && visibleDismissed.length === 0 && (
          <p className="field-note">
            {dismissed.data?.length
              ? "No dismissed discovery matches this search."
              : "No discoveries are currently dismissed."}
          </p>
        )}
        {visibleDismissed.length > 0 && (
          <div className="table-wrap">
            <table aria-label="Dismissed job discoveries" className="dismissed-discoveries-table">
              <thead>
                <tr>
                  <SortableHeader
                    activeKey={dismissedSortKey}
                    column="employer"
                    direction={dismissedSortDirection}
                    label="Employer"
                    onSort={sortDismissed}
                  />
                  <SortableHeader
                    activeKey={dismissedSortKey}
                    column="title"
                    direction={dismissedSortDirection}
                    label="Role"
                    onSort={sortDismissed}
                  />
                  <SortableHeader
                    activeKey={dismissedSortKey}
                    column="location"
                    direction={dismissedSortDirection}
                    label="Place"
                    onSort={sortDismissed}
                  />
                  <SortableHeader
                    activeKey={dismissedSortKey}
                    column="category"
                    direction={dismissedSortDirection}
                    label="Why dismissed"
                    onSort={sortDismissed}
                  />
                  <SortableHeader
                    activeKey={dismissedSortKey}
                    column="dismissed"
                    direction={dismissedSortDirection}
                    label="Dismissed"
                    onSort={sortDismissed}
                  />
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {pagedDismissed.map((event) => {
                  const url = safeHttpUrl(postingAddress(event));
                  const origin = dispositionOriginLabel(event.origin, event.selection_mode);
                  return (
                    <tr key={event.id}>
                      <td>
                        {/* The full address is shown under the title, so no host note here. */}
                        <EmployerName employer={event.employer} url={null} />
                      </td>
                      <td>
                        {url ? (
                          <>
                            <a
                              aria-label={postingLabel(event.title, event.employer, url)}
                              href={url}
                              rel="noreferrer"
                              target="_blank"
                            >
                              {event.title}
                            </a>
                            {/* Rows with the same title and employer are only ever told apart by
                                their address, so show it instead of leaving it in the href alone. */}
                            <p className="field-note dismissed-discovery-url">{url}</p>
                            <AggregatorCreditNote url={event.discovery_url} />
                          </>
                        ) : (
                          event.title
                        )}
                      </td>
                      <td>{event.location || "location not stated"}</td>
                      <td>
                        <div className="dismissed-discovery-reason">
                          <strong>{dismissalCategoryLabel(event.category)}</strong>
                          <small>
                            Recorded code: <code>{event.category}</code>
                          </small>
                          <span>{event.reason}</span>
                          {origin && <small>{origin}</small>}
                          {event.origin === "evaluation.refusal" && (
                            <small>
                              Restoring lets Full evaluation run past this condition. Another
                              condition can still refuse it.
                            </small>
                          )}
                        </div>
                      </td>
                      <td>{dismissedOn(event.created_at) || "Not stated"}</td>
                      <td>
                        <div className="row-actions">
                          <button
                            aria-label={`Preview restore ${event.title} at ${event.employer}`}
                            className="button dismissed-discovery-restore"
                            disabled={restore.pending}
                            onClick={() =>
                              void restore.act({
                                action: "restore",
                                category: event.category,
                                pending_discovery_ids: [event.pending_discovery_id],
                                reason: `Restore dismissal: ${event.reason}`,
                                selection_mode: "explicit",
                              })
                            }
                            type="button"
                          >
                            Preview restore
                          </button>
                          <button
                            aria-label={`Restore and keep ${event.title} at ${event.employer}`}
                            className="button dismissed-discovery-restore"
                            disabled={restore.pending || keepMarkers.isPending}
                            onClick={() => void restoreAndKeep(event)}
                            type="button"
                          >
                            Restore and keep
                          </button>
                          <button
                            aria-label={`Show history for ${event.title} at ${event.employer}`}
                            className="button button--quiet"
                            onClick={() => setHistoryId(event.pending_discovery_id)}
                            type="button"
                          >
                            Show history
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {visibleDismissed.length > DISMISSED_PAGE_SIZE && (
          <nav aria-label="Dismissed discovery pages" className="pagination">
            <button
              className="button button--quiet"
              disabled={dismissedPageStart === 0}
              onClick={() =>
                setDismissedOffset(Math.max(0, dismissedPageStart - DISMISSED_PAGE_SIZE))
              }
              type="button"
            >
              Previous
            </button>
            <button
              className="button button--quiet"
              disabled={dismissedPageStart + DISMISSED_PAGE_SIZE >= visibleDismissed.length}
              onClick={() => setDismissedOffset(dismissedPageStart + DISMISSED_PAGE_SIZE)}
              type="button"
            >
              Next
            </button>
          </nav>
        )}
        {restore.error && <ErrorState error={restore.error} />}
        {unkeptLabel && keepMarkers.error && (
          <div>
            <p className="field-error">
              Restored {unkeptLabel} but did not mark it kept. Press Keep on its row in the queue.
            </p>
            <ErrorState error={keepMarkers.error} />
          </div>
        )}
        {historyId && (
          <div aria-live="polite">
            <h4>Disposition history</h4>
            {history.isError && <ErrorState error={history.error} />}
            {history.data?.length ? (
              <ol>
                {history.data.map((event) => (
                  <li key={event.id}>
                    {event.action}: {event.reason} ({event.category.replaceAll("_", " ")})
                    {`, ${dispositionOriginLabel(event.origin, event.selection_mode)}`}
                  </li>
                ))}
              </ol>
            ) : (
              <p className="field-note">No disposition history recorded.</p>
            )}
          </div>
        )}
      </details>
    </details>
  );
}
