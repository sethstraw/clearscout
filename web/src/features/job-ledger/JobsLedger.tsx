import { Link } from "react-router-dom";

import { api, manualReviewFromJob, type Job } from "../../api/client";
import {
  useArchiveJobs,
  useEnqueueTask,
  useTask,
  useTaskCosts,
  useWorkspaceMutation,
} from "../../api/hooks";
import { text } from "../../api/json";
import { VIEW_KEYS } from "../../api/savedViews";
import { ConfirmedActionButton } from "../../components/ConfirmedActionButton";
import { CountryToken } from "../../components/CountryToken";
import { ClosingDates } from "../../components/ClosingDates";
import { evaluationReview } from "../../components/evaluationReview";
import { MultiSelectFilter } from "../../components/MultiSelectFilter";
import { PaidAction } from "../../components/PaidAction";
import { Page } from "../../components/Page";
import { Toggletip } from "../../components/Toggletip";
import { SortableHeader } from "../../components/SortableHeader";
import { StatusBadge } from "../../components/StatusBadge";
import { reviewScores, reviewScoreSource } from "../../components/reviewScores";
import {
  APPLICATION_STATUS_VALUES,
  applicationEventStatusValues,
  statusEntry,
} from "../../components/statuses";
import type { StatusEntry } from "../../components/statuses";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { useWorkspace } from "../../providers";
import { useJobLedgerQueryState } from "./useJobLedgerQueryState";

const statusOptions = ["", ...APPLICATION_STATUS_VALUES];
const priorityEntries: Record<NonNullable<Job["manual_priority"]>, StatusEntry> = {
  high: { label: "High", tone: "danger" },
  medium: { label: "Medium", tone: "warning" },
  low: { label: "Low", tone: "positive" },
};

/** Name the weights behind an overall, or say plainly that they are unknown. */
function policyLabel(job: Job): string {
  if (!job.score_policy_name || job.score_policy_version == null) {
    return "Recorded before scoring policies were tracked, so the weights behind this total are unknown.";
  }
  return `Scored under ${job.score_policy_name} v${job.score_policy_version}.`;
}

function policyTag(job: Job): string {
  return job.score_policy_version == null
    ? "Policy unknown"
    : `Policy v${job.score_policy_version}`;
}

function lowerBoundScoreLabel(job: Job): string {
  const count = job.unscored_weighted_component_count;
  const category = count === 1 ? "category is" : "categories are";
  return `Conservative minimum recorded score ${job.overall_score_lower_bound}. ${count} weighted ${category} unscored, so this is not a final total. ${policyLabel(job)}`;
}

function dateOnly(value: string): string {
  return value.slice(0, 10);
}

function resultRecords(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (item): item is Record<string, unknown> => typeof item === "object" && item !== null,
  );
}

function count(value: unknown): number {
  if (typeof value === "number") return value;
  return Array.isArray(value) ? value.length : 0;
}

function countedParts(parts: [number, string][]): string {
  return parts
    .filter(([value]) => value > 0)
    .map(([value, label]) => `${value} ${label}`)
    .join(", ");
}

/** State a completed refresh batch as counts grouped by what the operator does next. */
function refreshSentence(progress: Record<string, unknown>, failed: number): string {
  const refreshed = count(progress.refreshed);
  const outcomes = countedParts([
    [count(progress.unchanged), "unchanged"],
    [count(progress.changed), "changed"],
    [count(progress.withdrawn_as_closed), "closed"],
  ]);
  const trouble = countedParts([
    [failed, "failed"],
    [count(progress.skipped), "skipped"],
  ]);
  const unverified = count(progress.unverified);
  return [
    `Re-read ${refreshed} posting${refreshed === 1 ? "" : "s"}${outcomes ? `: ${outcomes}` : ""}.`,
    trouble ? `${trouble}.` : "",
    unverified > 0 ? `The publisher answered but would not confirm ${unverified}.` : "",
  ]
    .filter(Boolean)
    .join(" ");
}

export function JobsPage() {
  const { workspaceId } = useWorkspace();
  const costs = useTaskCosts(workspaceId);
  const ledger = useJobLedgerQueryState(workspaceId);
  const {
    applyView,
    clearSelection,
    completeness,
    decision,
    employers,
    includeArchived,
    jobs,
    place,
    places,
    postingStatus,
    savedViews,
    search,
    selected,
    setRowSelected,
    setVisibleSelected,
    sortBy,
    sortDirection,
    sortKey,
    status,
    updateCompleteness,
    updateDecision,
    updateEmployers,
    updateIncludeArchived,
    updatePlace,
    updatePostingStatus,
    updateSearch,
    updateStatus,
  } = ledger;

  const refresh = useEnqueueTask(workspaceId);
  const compare = useEnqueueTask(workspaceId);
  const reevaluate = useEnqueueTask(workspaceId);
  const archive = useArchiveJobs(workspaceId);
  const savePriority = useWorkspaceMutation(
    workspaceId,
    async ({ jobId, priority }: { jobId: string; priority: Job["manual_priority"] }) => {
      const current = await api.job(jobId, workspaceId);
      return api.saveManualJobReview(jobId, workspaceId, {
        ...manualReviewFromJob(current),
        priority,
      });
    },
    { successMessage: "Your priority was saved." },
  );
  const appendStatus = useWorkspaceMutation(
    workspaceId,
    ({ jobId, status }: { jobId: string; status: string }) =>
      api.appendApplicationEvent(jobId, workspaceId, {
        status,
        occurred_at: new Date().toISOString(),
        notes: "",
      }),
    { successMessage: "Application status recorded." },
  );
  const rowEditPending = savePriority.isPending || appendStatus.isPending;
  const refreshTask = useTask(workspaceId, refresh.data?.id ?? "");
  const compareTask = useTask(workspaceId, compare.data?.id ?? "");
  const reevaluateTask = useTask(workspaceId, reevaluate.data?.id ?? "");
  const items = jobs.data?.items ?? [];
  const refreshFailures = resultRecords(refreshTask.data?.progress.failures);
  const unverifiedRefreshes = resultRecords(refreshTask.data?.progress.unverified_postings);
  const reevaluateFailures = resultRecords(reevaluateTask.data?.progress.failures);
  // Retained policies make mixed-policy totals incomparable.
  const policies = new Set(
    items.filter((item) => item.evaluation_id).map((item) => policyTag(item)),
  );
  const refreshBusy =
    refresh.isPending || ["queued", "running"].includes(refreshTask.data?.status ?? "");
  const compareBusy =
    compare.isPending || ["queued", "running"].includes(compareTask.data?.status ?? "");
  const reevaluateBusy =
    reevaluate.isPending || ["queued", "running"].includes(reevaluateTask.data?.status ?? "");
  const selectionLimitReason =
    selected.size > 250 ? "Batch actions accept at most 250 tracked postings." : undefined;
  const reevaluateReason =
    selectionLimitReason ??
    (selected.size ? undefined : "Select one or more postings to re-evaluate");
  const refreshReason =
    selectionLimitReason ?? (selected.size ? undefined : "Select one or more jobs to refresh");
  const compareReason =
    selected.size < 2
      ? "Select 2 to 5 jobs to compare"
      : selected.size > 5
        ? "Compare takes at most 5 jobs"
        : undefined;
  const narrowed = Boolean(
    search ||
    status ||
    decision ||
    completeness ||
    place ||
    employers.length ||
    postingStatus ||
    includeArchived,
  );
  // The Advanced fold starts closed, so its filters are named where they stay visible.
  const advancedFilters = [
    includeArchived ? "archived jobs included" : "",
    completeness === "incomplete_only" ? "evidence gaps only" : "",
    completeness === "complete" ? "evidence complete only" : "",
  ].filter(Boolean);

  return (
    <Page
      action={
        <Link className="button button--quiet" to="/find/scoring">
          How jobs are scored
        </Link>
      }
      description="Every posting you track, with its scores and application status."
      density="dense"
      eyebrow="Decide"
      help="jobs"
      title="Jobs"
    >
      <section className="toolbar" aria-label="Job filters">
        <label className="search-field">
          <span>Search jobs</span>
          <input
            onChange={(event) => updateSearch(event.target.value)}
            placeholder="Employer, role, location, evidence"
            type="search"
            value={search}
          />
        </label>
        <label>
          <span>Decision</span>
          <select onChange={(event) => updateDecision(event.target.value)} value={decision}>
            <option value="">Any decision</option>
            {[
              "apply_now",
              "premium_target",
              "investigate",
              "monitor",
              "deprioritize",
              "ineligible",
              "closed_evidence",
            ].map((value) => (
              <option key={value} value={value}>
                {value.replaceAll("_", " ")}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Application status</span>
          <select onChange={(event) => updateStatus(event.target.value)} value={status}>
            {statusOptions.map((value) => (
              <option key={value || "all"} value={value}>
                {value ? value.replaceAll("_", " ") : "All statuses"}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Place</span>
          <select onChange={(event) => updatePlace(event.target.value)} value={place}>
            <option value="">Anywhere recorded</option>
            {places.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
        <MultiSelectFilter
          allLabel="All employers"
          label="Employer"
          onChange={updateEmployers}
          options={(jobs.data?.employers ?? []).map((employer) => ({
            value: employer.name,
            label: employer.name,
            count: employer.count,
          }))}
          selected={employers}
        />
        <label>
          <span>Posting status</span>
          <select
            onChange={(event) => updatePostingStatus(event.target.value)}
            value={postingStatus}
          >
            <option value="">All statuses</option>
            <option value="active">Active</option>
            <option value="closed">Closed</option>
          </select>
        </label>
        <details className="advanced-view jobs-ledger__advanced">
          <summary>Advanced</summary>
          <div className="jobs-ledger__advanced-fields">
            {savedViews.data && savedViews.data.length > 0 && (
              <div className="jobs-ledger__saved-view">
                <label>
                  <span>Saved view</span>
                  <select onChange={(event) => applyView(event.target.value)} value="">
                    <option value="">Apply a saved view</option>
                    {savedViews.data.map((view) => (
                      <option key={view.id} value={view.id}>
                        {view.name}
                      </option>
                    ))}
                  </select>
                </label>
                <Toggletip label="About saved views">
                  Applies the filters and sort a view stores. Recognised keys: {VIEW_KEYS}. A stored
                  column list is ignored, because this table&apos;s columns are fixed.
                </Toggletip>
              </div>
            )}
            <label>
              <span>Archived jobs</span>
              <select
                onChange={(event) => updateIncludeArchived(event.target.value === "include")}
                value={includeArchived ? "include" : "exclude"}
              >
                <option value="exclude">Active only</option>
                <option value="include">Include archived</option>
              </select>
            </label>
            <label>
              <span>Evidence integrity</span>
              <select
                onChange={(event) => updateCompleteness(event.target.value)}
                value={completeness}
              >
                <option value="">All jobs</option>
                <option value="incomplete_only">Evidence gaps only</option>
                <option value="complete">Evidence complete only</option>
              </select>
            </label>
          </div>
        </details>
        {advancedFilters.length > 0 && (
          <p className="field-note jobs-ledger__advanced-active" role="status">
            Advanced filter on: {advancedFilters.join(", ")}.{" "}
            <button
              className="text-button"
              onClick={() => {
                updateCompleteness("");
                updateIncludeArchived(false);
              }}
              type="button"
            >
              Clear advanced filters
            </button>
          </p>
        )}
        <div className="toolbar-actions">
          <ConfirmedActionButton
            className="button button--quiet"
            disabled={!selected.size || archive.isPending || Boolean(selectionLimitReason)}
            onConfirm={() =>
              archive.mutate({ jobIds: [...selected], workspaceId }, { onSuccess: clearSelection })
            }
            options={{
              title: `Archive ${selected.size} selected jobs?`,
              description:
                "They leave the active ledger, but their evaluations, documents, and application history are preserved.",
              confirmLabel: "Archive selected",
              danger: true,
            }}
          >
            {archive.isPending ? "Archiving selected" : `Archive selected (${selected.size})`}
          </ConfirmedActionButton>
          <div className="action-with-reason">
            <ConfirmedActionButton
              className="button button--quiet"
              disabled={Boolean(refreshReason) || refreshBusy}
              onConfirm={() =>
                refresh.mutate(
                  {
                    workspace_id: workspaceId,
                    task_type: "posting.refresh_batch",
                    payload: { job_posting_ids: [...selected], force: true },
                  },
                  { onSuccess: clearSelection },
                )
              }
              options={
                selected.size === 1
                  ? {
                      title: "Refresh this tracked posting?",
                      description:
                        "Re-reads this posting from its publisher. If the publisher no longer lists it, it is marked closed, and a later read that finds it active reopens it.",
                      confirmLabel: "Refresh",
                    }
                  : {
                      title: `Refresh ${selected.size} tracked postings?`,
                      description: `Re-reads all ${selected.size} from their publishers, which takes a few minutes and can be cancelled from Activity. A posting its publisher no longer lists is marked closed, and a later read that finds it active reopens it.`,
                      confirmLabel: `Refresh ${selected.size}`,
                    }
              }
            >
              {refreshBusy ? "Refresh pending" : `Refresh selected (${selected.size})`}
            </ConfirmedActionButton>
            {refreshReason && <small className="field-note">{refreshReason}</small>}
          </div>
          <div className="action-with-reason">
            <PaidAction
              costs={costs}
              taskType="workflow.batch"
              label={compareBusy ? "Comparison pending" : `Compare selected (${selected.size})`}
              disabled={Boolean(compareReason) || compareBusy || !costs.loaded}
              onAct={() =>
                compare.mutate(
                  {
                    workspace_id: workspaceId,
                    task_type: "workflow.batch",
                    payload: { job_posting_ids: [...selected] },
                  },
                  { onSuccess: clearSelection },
                )
              }
              confirmation={{
                title: `Compare ${selected.size} jobs?`,
                description: `One task compares all ${selected.size} selected jobs with advisory cost estimates. ${costs.confirmation("workflow.batch")}`,
                confirmLabel: "Compare jobs",
              }}
            />
            {compareReason && <small className="field-note">{compareReason}</small>}
          </div>
          <div className="action-with-reason">
            <PaidAction
              costs={costs}
              taskType="discovery.evaluate_batch"
              evaluation={{ workspaceId, selection: { job_posting_ids: [...selected] } }}
              count={selected.size}
              label={
                reevaluateBusy ? "Re-evaluation pending" : `Re-evaluate selected (${selected.size})`
              }
              disabled={Boolean(reevaluateReason) || reevaluateBusy || !costs.loaded}
              onAct={() =>
                reevaluate.mutate(
                  {
                    workspace_id: workspaceId,
                    task_type: "discovery.evaluate_batch",
                    payload: { job_posting_ids: [...selected] },
                  },
                  { onSuccess: clearSelection },
                )
              }
              confirmation={{
                title: `Re-evaluate ${selected.size} tracked ${selected.size === 1 ? "posting" : "postings"}?`,
                description: `One task re-evaluates all ${selected.size} selected postings with advisory cost estimates.`,
                confirmLabel: `Re-evaluate ${selected.size}`,
              }}
            />
            {reevaluateReason && <small className="field-note">{reevaluateReason}</small>}
          </div>
          <span className="result-count" aria-live="polite">
            {items.length} matching tracked postings
          </span>
        </div>
      </section>
      {refresh.isError && <p className="field-error">{refresh.error.message}</p>}
      {compare.isError && <p className="field-error">{compare.error.message}</p>}
      {reevaluate.isError && <p className="field-error">{reevaluate.error.message}</p>}
      {refreshTask.data?.status === "completed" && (
        <p className="refresh-result" role="status">
          {refreshSentence(refreshTask.data.progress, refreshFailures.length)}
        </p>
      )}
      {refreshTask.data?.status === "completed" && unverifiedRefreshes.length > 0 && (
        <ul aria-label="Could not verify postings" className="field-note">
          {unverifiedRefreshes.map((posting, index) => (
            <li key={`${text(posting.job_posting_id, "unknown-posting")}-${index}`}>
              {text(posting.job_posting_id, "Unknown posting")}: {text(posting.reason)} Previous
              posting record left unchanged.
            </li>
          ))}
        </ul>
      )}
      {refreshTask.data?.status === "completed" && refreshFailures.length > 0 && (
        <ul aria-label="Refresh failures" className="field-error">
          {refreshFailures.map((failure, index) => (
            <li key={`${text(failure.job_posting_id, "unknown-posting")}-${index}`}>
              {text(failure.job_posting_id, "Unknown posting")}: {text(failure.error_message)}
            </li>
          ))}
        </ul>
      )}
      {compareTask.data?.status === "completed" && (
        <p className="refresh-result" role="status">
          Comparison stored for{" "}
          {text(compareTask.data.progress.member_count, String(selected.size))} jobs. The report is
          filed against every job in the comparison; open any of their Files tabs to read it.
        </p>
      )}
      {compareTask.data?.status === "failed" && (
        <p className="field-error">Comparison failed: {compareTask.data.error_message}</p>
      )}
      {reevaluateTask.data?.status === "completed" && (
        <p className="refresh-result" role="status">
          Re-evaluated {text(reevaluateTask.data.progress.evaluated, "0")} of{" "}
          {text(reevaluateTask.data.progress.selected, "0")} selected postings; refused by rule{" "}
          {text(reevaluateTask.data.progress.refused, "0")}; failed {reevaluateFailures.length}.
        </p>
      )}
      {reevaluateTask.data?.status === "completed" && reevaluateFailures.length > 0 && (
        <ul aria-label="Re-evaluation failures" className="field-error">
          {reevaluateFailures.map((failure, index) => (
            <li key={`${text(failure.job_posting_id, "unknown-posting")}-${index}`}>
              {text(failure.job_posting_id, "Unknown posting")}: {text(failure.error_message)}
            </li>
          ))}
        </ul>
      )}
      {reevaluateTask.data?.status === "failed" && (
        <p className="field-error">Re-evaluation failed: {reevaluateTask.data.error_message}</p>
      )}
      {jobs.isLoading && <LoadingState label="Reading evaluations" />}
      {jobs.isError && <ErrorState error={jobs.error} />}
      {jobs.data && (
        <>
          <p className="jobs-ledger__score-guide" id="recorded-score-guide">
            Your review takes precedence when you enter a score; otherwise the TailorCV evaluation
            appears. Open a job to edit your review or inspect its evaluation. <b>Min.</b> is a
            conservative evaluation score when a weighted category is unscored.
          </p>
          {policies.size > 1 && (
            <p className="policy-note" role="status">
              Evaluation scores from different policies are not directly comparable. Open a job to
              review its policy before comparing them.
            </p>
          )}
          <div className="table-wrap jobs-ledger__table-wrap">
            <table
              aria-describedby="recorded-score-guide"
              aria-label="Tracked postings"
              className="jobs-table"
            >
              <colgroup>
                <col className="jobs-table__selection-column" />
                <col className="jobs-table__employer-column" />
                <col className="jobs-table__role-column" />
                <col className="jobs-table__decision-column" />
                <col className="jobs-table__priority-column" />
                <col className="jobs-table__fit-column" />
                <col className="jobs-table__compensation-column" />
                <col className="jobs-table__overall-column" />
                <col className="jobs-table__found-column" />
                <col className="jobs-table__evaluated-column" />
                <col className="jobs-table__application-column" />
              </colgroup>
              <thead>
                <tr>
                  <th scope="col">
                    <input
                      aria-label="Select all matching jobs"
                      checked={
                        items.length > 0 && items.every((item) => selected.has(item.job_posting_id))
                      }
                      onChange={(event) =>
                        setVisibleSelected(
                          items.map((item) => item.job_posting_id),
                          event.target.checked,
                        )
                      }
                      type="checkbox"
                    />
                  </th>
                  <SortableHeader
                    activeKey={sortKey}
                    column="employer"
                    direction={sortDirection}
                    label="Employer"
                    onSort={sortBy}
                  />
                  <SortableHeader
                    activeKey={sortKey}
                    column="title"
                    direction={sortDirection}
                    label="Role"
                    onSort={sortBy}
                  />
                  <SortableHeader
                    activeKey={sortKey}
                    column="decision"
                    direction={sortDirection}
                    label="Decision / review"
                    onSort={sortBy}
                  />
                  <SortableHeader
                    activeKey={sortKey}
                    column="manual_priority"
                    direction={sortDirection}
                    label="Your priority"
                    onSort={sortBy}
                  />
                  <SortableHeader
                    activeKey={sortKey}
                    column="fit_score"
                    direction={sortDirection}
                    description="Your fit score when entered, otherwise the TailorCV evaluation score."
                    label="Fit"
                    onSort={sortBy}
                  />
                  <SortableHeader
                    activeKey={sortKey}
                    column="compensation_score"
                    direction={sortDirection}
                    description="Your compensation score when entered, otherwise the TailorCV evaluation score."
                    label="Comp"
                    onSort={sortBy}
                  />
                  <SortableHeader
                    activeKey={sortKey}
                    column="recorded_score"
                    description="Your overall score when entered, otherwise the TailorCV evaluation score. Min. is a conservative evaluation score, not a final total."
                    direction={sortDirection}
                    label="Recorded score"
                    onSort={sortBy}
                  />
                  <SortableHeader
                    activeKey={sortKey}
                    column="first_seen_at"
                    direction={sortDirection}
                    label="Found"
                    onSort={sortBy}
                  />
                  <SortableHeader
                    activeKey={sortKey}
                    column="evaluated_at"
                    direction={sortDirection}
                    label="Evaluated"
                    onSort={sortBy}
                  />
                  <SortableHeader
                    activeKey={sortKey}
                    column="application_status"
                    direction={sortDirection}
                    label="Application"
                    onSort={sortBy}
                  />
                </tr>
              </thead>
              <tbody>
                {items.map((job) => {
                  const scores = reviewScores(job);
                  const allowedStatuses = applicationEventStatusValues(
                    job.application_status,
                    job.application_event_count > 0,
                  );
                  return (
                    <tr key={job.job_posting_id}>
                      <td>
                        <input
                          aria-label={`Select ${job.employer} ${job.title}`}
                          checked={selected.has(job.job_posting_id)}
                          onChange={(event) =>
                            setRowSelected(job.job_posting_id, event.target.checked)
                          }
                          type="checkbox"
                        />
                      </td>
                      <td>
                        <strong>{job.employer}</strong>
                        {job.archived_at && <StatusBadge value="archived" />}
                      </td>
                      <td>
                        <Link
                          className="job-link"
                          title={job.title}
                          to={`/jobs/${job.job_posting_id}`}
                        >
                          <span>{job.title}</span>
                        </Link>
                        {job.posting_status === "closed" && <StatusBadge value="closed" />}
                        {job.place ? (
                          <p className="field-note jobs-table__place">
                            <span>Matched saved location: {job.place}</span>
                            <CountryToken countries={job.countries} />
                          </p>
                        ) : null}
                        <ClosingDates
                          compact
                          label={`Closing dates for ${job.employer} ${job.title}`}
                          publisher={job.publisher_closing_date}
                          override={job.closing_date_override}
                        />
                      </td>
                      <td>
                        <span title={job.rationale || undefined}>
                          {job.decision ? <StatusBadge value={job.decision} /> : "Not evaluated"}
                        </span>
                        {job.evaluation_id &&
                          (() => {
                            const review = evaluationReview(
                              job.evaluation_inputs,
                              job.evaluation_review,
                            );
                            return (
                              <div className="jobs-ledger__review">
                                {review.status === "needs_review" ? (
                                  <Link
                                    className="status status--tone-warning"
                                    aria-label={`Review needed: ${review.reason === "posting_changed" ? "posting changed since evaluation" : "decision criteria changed since evaluation"}`}
                                    title={
                                      review.reason === "posting_changed"
                                        ? "Posting changed since evaluation"
                                        : "Decision criteria changed since evaluation"
                                    }
                                    to={`/jobs/${job.job_posting_id}${review.reason === "posting_changed" ? "?tab=posting" : ""}`}
                                  >
                                    {review.reason === "posting_changed"
                                      ? "Review: posting"
                                      : "Review: criteria"}
                                  </Link>
                                ) : review.status === "reviewed" ? (
                                  <span
                                    className="status status--tone-neutral"
                                    title="Recorded evaluation retained after review"
                                  >
                                    Reviewed
                                  </span>
                                ) : review.status === "unknown" ? (
                                  <span
                                    className="status status--tone-neutral"
                                    title="TailorCV cannot determine whether this evaluation remains current"
                                  >
                                    Unknown
                                  </span>
                                ) : (
                                  <span className="status status--tone-positive">Current</span>
                                )}
                              </div>
                            );
                          })()}
                      </td>
                      <td title={job.manual_note || undefined}>
                        <select
                          aria-label={`Set priority for ${job.employer} ${job.title}`}
                          className={`jobs-table__inline-select jobs-table__priority-select status--tone-${job.manual_priority ? priorityEntries[job.manual_priority].tone : "neutral"}`}
                          disabled={rowEditPending}
                          onChange={(event) => {
                            const priority = (event.target.value || null) as Job["manual_priority"];
                            if (priority === job.manual_priority) return;
                            savePriority.mutate({ jobId: job.job_posting_id, priority });
                          }}
                          value={job.manual_priority ?? ""}
                        >
                          <option value="">Not set</option>
                          {Object.entries(priorityEntries).map(([value, entry]) => (
                            <option key={value} value={value}>
                              {entry.label}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td className="score-cell">
                        <span
                          aria-label={`Fit ${scores.fit.value ?? "not scored"}. ${reviewScoreSource(scores.fit)}`}
                          title={reviewScoreSource(scores.fit)}
                        >
                          {text(scores.fit.value, "-")}
                        </span>
                      </td>
                      <td className="score-cell">
                        <span
                          aria-label={`Compensation ${scores.compensation.value ?? "not scored"}. ${reviewScoreSource(scores.compensation)}`}
                          title={reviewScoreSource(scores.compensation)}
                        >
                          {text(scores.compensation.value, "-")}
                        </span>
                      </td>
                      <td className="score-cell">
                        {scores.overall.source === "review" ? (
                          <span
                            aria-label={`Recorded score ${scores.overall.value} from your review`}
                            title="From your review"
                          >
                            {String(scores.overall.value)}
                          </span>
                        ) : scores.overall.source === "evaluation" ? (
                          <span
                            aria-label={`Recorded score ${scores.overall.value}. ${policyLabel(job)}`}
                            title={`Recorded score ${scores.overall.value}. ${policyLabel(job)}`}
                          >
                            {String(scores.overall.value)}
                          </span>
                        ) : scores.overall.source === "minimum" ? (
                          <span
                            aria-label={lowerBoundScoreLabel(job)}
                            className="score-cell__lower-bound"
                            title={lowerBoundScoreLabel(job)}
                          >
                            Min. {scores.overall.value}
                          </span>
                        ) : (
                          <span
                            aria-label="No recorded score"
                            title={
                              job.evaluation_id
                                ? "This evaluation has no recorded weighted score."
                                : "No model evaluation has been run."
                            }
                          >
                            --
                          </span>
                        )}
                      </td>
                      <td>
                        <time dateTime={job.first_seen_at}>{dateOnly(job.first_seen_at)}</time>
                      </td>
                      <td>
                        {job.evaluated_at ? (
                          <time dateTime={job.evaluated_at}>{dateOnly(job.evaluated_at)}</time>
                        ) : (
                          "Not evaluated"
                        )}
                      </td>
                      <td>
                        {job.archived_at ? (
                          <span className="jobs-table__archived-status">
                            <StatusBadge value={job.application_status} />
                          </span>
                        ) : (
                          <select
                            aria-label={`Set application status for ${job.employer} ${job.title}`}
                            className="jobs-table__inline-select"
                            disabled={rowEditPending}
                            onChange={(event) => {
                              const status = allowedStatuses.find(
                                (value) => value === event.target.value,
                              );
                              if (!status) return;
                              appendStatus.mutate({ jobId: job.job_posting_id, status });
                            }}
                            title={statusEntry(job.application_status).label}
                            value={job.application_status}
                          >
                            <option value={job.application_status}>
                              {statusEntry(job.application_status).label}
                            </option>
                            {allowedStatuses.map((status) => (
                              <option key={status} value={status}>
                                {statusEntry(status).label}
                              </option>
                            ))}
                          </select>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {!items.length && (
              <EmptyState>
                {narrowed ? (
                  "No jobs match these filters."
                ) : (
                  <>
                    No tracked postings yet. <Link to="/find">Find postings</Link> and select Track
                    job to add one here.
                  </>
                )}
              </EmptyState>
            )}
          </div>
        </>
      )}
    </Page>
  );
}
