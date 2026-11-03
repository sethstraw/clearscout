import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { useForm } from "react-hook-form";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { api, PAGE_SIZE, type TaskBilling } from "../api/client";
import { ApiError } from "../api/http";
import {
  useAppendApplicationEvent,
  useArchiveJob,
  useUnarchiveJob,
  useEnqueueTask,
  useEvidenceSource,
  useFitReport,
  useJob,
  useReviewEvaluationCurrentness,
  useNotes,
  taskBillingSummary,
  useTask,
  useTaskBilling,
  useTaskCosts,
} from "../api/hooks";
import { text } from "../api/json";
import { ConfirmedActionButton } from "../components/ConfirmedActionButton";
import { ErrorNotice } from "../components/ErrorNotice";
import { errorNotice } from "../components/freeMode";
import { PaidAction } from "../components/PaidAction";
import { Page } from "../components/Page";
import { nextSort, type SortDirection } from "../components/sorting";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { TabPanel, Tabs } from "../components/Tabs";
import { applicationEventStatusValues } from "../components/statuses";
import { evaluationReview, evaluationReviewMessage } from "../components/evaluationReview";
import {
  ApplicationHistory,
  type ApplicationEventForm,
  FitReport,
  JobEvaluation,
  type EvaluationSort,
  JobHint,
  JobNotes,
  JobOverview,
  type ScoreSort,
  JobSummary,
  PostingReader,
} from "../features/job-detail";
import { EvidenceWorkspace } from "../features/job-evidence/EvidenceWorkspace";
import { TailoringStudio } from "../features/tailoring/TailoringStudio";
import { useWorkspace } from "../providers";
import { useToast } from "../providers/useToast";

const tabs = [
  "Overview",
  "Studio",
  "Files",
  "Posting",
  "Evaluation",
  "Evidence & gaps",
  "Fit report",
  "Notes",
  "Tailor hint",
  "Application history",
] as const;
type Tab = (typeof tabs)[number];
const workspaceTabs = ["Overview", "Fit", "Tailor", "Posting", "Application"] as const;
type WorkspaceTab = (typeof workspaceTabs)[number];
const tabSlugs: Record<Tab, string> = {
  Overview: "overview",
  Studio: "studio",
  Files: "documents",
  Posting: "posting",
  Evaluation: "evaluation",
  "Evidence & gaps": "evidence",
  "Fit report": "fit-report",
  Notes: "notes",
  "Tailor hint": "hint",
  "Application history": "application",
};
const workspaceTabSlugs: Record<WorkspaceTab, string> = {
  Overview: "overview",
  Fit: "fit",
  Tailor: "tailor",
  Posting: "posting",
  Application: "application",
};
const workspaceForTab: Record<Tab, WorkspaceTab> = {
  Overview: "Overview",
  Evaluation: "Fit",
  "Evidence & gaps": "Fit",
  "Fit report": "Fit",
  Studio: "Tailor",
  Files: "Tailor",
  "Tailor hint": "Tailor",
  Posting: "Posting",
  Notes: "Application",
  "Application history": "Application",
};
const defaultTabForWorkspace: Record<WorkspaceTab, Tab> = {
  Overview: "Overview",
  Fit: "Evaluation",
  Tailor: "Studio",
  Posting: "Posting",
  Application: "Notes",
};
const fitTabs = ["Evaluation", "Evidence & gaps", "Fit report"] as const satisfies readonly Tab[];
const tailorTabs = ["Studio", "Files", "Tailor hint"] as const satisfies readonly Tab[];
const applicationTabs = ["Notes", "Application history"] as const satisfies readonly Tab[];
const workspaceTabItems = workspaceTabs.map((label) => ({
  id: workspaceTabSlugs[label],
  label,
}));
const fitTabItems = fitTabs.map((label) => ({ id: tabSlugs[label], label }));
const tailorTabItems = tailorTabs.map((label) => ({ id: tabSlugs[label], label }));
const applicationTabItems = applicationTabs.map((label) => ({ id: tabSlugs[label], label }));
const HISTORY_PAGE_SIZE = PAGE_SIZE;
// The Studio links to the header evaluation control rather than repeating it.
const EVALUATE_ACTION_ID = "job-evaluate-action";
// Matches _REEVAL_FETCH_STALE_HOURS in the tailoring re-evaluation workflow.
const REEVAL_STALE_HOURS = 24;

function localDateTimeNow(): string {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

function reevaluationFreshness(
  latestObserved: string | null | undefined,
  now: Date = new Date(),
): { stale: boolean; timestamp: string } | null {
  if (!latestObserved) return null;
  const observedAt = new Date(latestObserved);
  if (Number.isNaN(observedAt.getTime())) return null;
  const stale = now.getTime() - observedAt.getTime() >= REEVAL_STALE_HOURS * 3600_000;
  return { stale, timestamp: observedAt.toISOString() };
}

// A worker records the provider's own wording, which often has no full stop,
// and the billing sentence is appended straight after it.
function terminated(text: string | null | undefined): string {
  const trimmed = text?.trim() ?? "";
  return !trimmed || /[.!?]$/.test(trimmed) ? trimmed : `${trimmed}.`;
}

function reevaluationFailure(
  message: string | null,
  billing?: TaskBilling,
  billingError = false,
  taskStarted = true,
): string {
  const detail = terminated(message);
  const failure = detail
    ? `Re-evaluation failed: ${detail}`
    : "Re-evaluation failed. The task recorded no reason.";
  const retry =
    taskStarted && billing?.status !== "no_model_calls"
      ? "A retry can add another charge."
      : "A retry starts a new run.";
  return `${failure} ${taskBillingSummary(billing, billingError, taskStarted)} ${retry}`;
}

const JobDocuments = lazy(() =>
  import("../features/tailoring/JobDocuments").then((module) => ({
    default: module.JobDocuments,
  })),
);

type DisputeKind = "extraction" | "fit";

const DISPUTE_ACTION: Record<
  DisputeKind,
  {
    taskType: "posting.refresh_with_reextraction" | "posting.refresh_with_rematch";
    label: string;
    title: string;
    description: string;
    confirmLabel: string;
  }
> = {
  extraction: {
    taskType: "posting.refresh_with_reextraction",
    label: "Refresh with dispute re-extraction",
    title: "Refresh with dispute re-extraction?",
    description:
      "Re-reads the posting and asks the model to re-check disputed extraction requirements. A ruling reverses only when the model cites new posting text.",
    confirmLabel: "Refresh with re-extraction",
  },
  fit: {
    taskType: "posting.refresh_with_rematch",
    label: "Refresh with fit re-match",
    title: "Refresh with fit re-match?",
    description:
      "Re-matches disputed fit requirements against the current master CV. A ruling reverses only when the model cites a CV entry the previous match did not cite.",
    confirmLabel: "Confirm fit re-match",
  },
};

const DISPUTE_TASK_TYPES: string[] = Object.values(DISPUTE_ACTION).map((item) => item.taskType);

function DisputeRefreshAction({
  billingPending,
  kind,
  workspaceId,
  jobId,
  refresh,
  refreshBusy,
  costs,
}: {
  billingPending: boolean;
  kind: DisputeKind;
  workspaceId: string;
  jobId: string;
  refresh: ReturnType<typeof useEnqueueTask>;
  refreshBusy: boolean;
  costs: ReturnType<typeof useTaskCosts>;
}) {
  const meta = DISPUTE_ACTION[kind];
  return (
    <PaidAction
      confirmation={{
        title: meta.title,
        description: `${meta.description} ${costs.confirmation(meta.taskType)}`,
        confirmLabel: meta.confirmLabel,
      }}
      costs={costs}
      disabled={refreshBusy || billingPending || !costs.loaded}
      label={meta.label}
      onAct={() =>
        refresh.mutate({
          workspace_id: workspaceId,
          task_type: meta.taskType,
          payload: { job_posting_id: jobId },
        })
      }
      taskType={meta.taskType}
    />
  );
}

export function JobPage() {
  const { jobId = "" } = useParams();
  const { workspaceId } = useWorkspace();
  const [searchParams, setSearchParams] = useSearchParams();
  const [unsupportedOnly, setUnsupportedOnly] = useState(false);
  const [scoreSort, setScoreSort] = useState<ScoreSort>("component");
  const [scoreDirection, setScoreDirection] = useState<SortDirection>("asc");
  const [evaluationSort, setEvaluationSort] = useState<EvaluationSort>("version_number");
  const [evaluationDirection, setEvaluationDirection] = useState<SortDirection>("desc");
  const [observationOffset, setObservationOffset] = useState(0);
  const [evaluationOffset, setEvaluationOffset] = useState(0);
  const [applicationEventOffset, setApplicationEventOffset] = useState(0);
  const job = useJob(workspaceId, jobId, {
    observationOffset,
    evaluationOffset,
    applicationEventOffset,
    historyLimit: HISTORY_PAGE_SIZE,
  });
  const navigate = useNavigate();
  const archive = useArchiveJob(workspaceId);
  const restore = useUnarchiveJob(workspaceId);
  const refresh = useEnqueueTask(workspaceId);
  const refreshTask = useTask(workspaceId, refresh.data?.id ?? "");
  // The same enqueue drives the free posting.refresh, so only the paid dispute
  // task types are billed and asked about.
  const disputeRefresh = DISPUTE_TASK_TYPES.includes(refreshTask.data?.task_type ?? "");
  const refreshTerminal = ["completed", "failed", "cancelled"].includes(
    refreshTask.data?.status ?? "",
  );
  const awaitingRefreshStatus = Boolean(
    refresh.data?.id && !refreshTask.data && !refreshTask.isError,
  );
  const refreshActive = Boolean(refreshTask.data && !refreshTerminal);
  const refreshBusy =
    refresh.isPending || Boolean(refresh.data?.id && (!refreshTask.data || !refreshTerminal));
  const refreshActionLabel = refresh.isPending
    ? "Starting refresh"
    : refreshTask.isError
      ? "Refresh unavailable"
      : awaitingRefreshStatus
        ? "Checking refresh status"
        : refreshActive
          ? "Refresh pending"
          : "Refresh posting";
  const refreshBilling = useTaskBilling(
    workspaceId,
    refreshTask.data?.id ?? "",
    disputeRefresh && refreshTerminal,
  );
  const awaitingRefreshBilling =
    disputeRefresh &&
    refreshTerminal &&
    !refreshBilling.isError &&
    refreshBilling.data === undefined;
  const reevaluate = useEnqueueTask(workspaceId);
  const reevaluateNotice = reevaluate.isError ? errorNotice(reevaluate.error) : null;
  const reevaluateTask = useTask(workspaceId, reevaluate.data?.id ?? "");
  const reevaluationTerminal = ["completed", "failed"].includes(reevaluateTask.data?.status ?? "");
  const reevaluationBilling = useTaskBilling(
    workspaceId,
    reevaluateTask.data?.id ?? "",
    reevaluationTerminal,
  );
  const completedReevaluation = useRef("");
  const failedReevaluation = useRef("");
  const { showToast } = useToast();
  const costs = useTaskCosts(workspaceId);
  const requestedTab = tabs.find((item) => tabSlugs[item] === searchParams.get("tab"));
  const tab = requestedTab ?? "Overview";
  const workspaceTab = workspaceForTab[tab];
  const selectedEvidenceId = searchParams.get("evidence") ?? "";
  const evidenceSource = useEvidenceSource(workspaceId, selectedEvidenceId);
  const fitReport = useFitReport(
    workspaceId,
    jobId,
    (tab === "Fit report" || tab === "Evidence & gaps") && Boolean(job.data?.evaluation_id),
  );
  const jobNotes = useNotes(workspaceId, "job_posting", jobId);
  const setTab = (value: Tab) => {
    const next = new URLSearchParams(searchParams);
    next.set("tab", tabSlugs[value]);
    setSearchParams(next);
  };
  useEffect(() => {
    const task = reevaluateTask.data;
    if (!task || task.status !== "completed" || completedReevaluation.current === task.id) return;
    if (!reevaluationBilling.isSuccess && !reevaluationBilling.isError) return;
    completedReevaluation.current = task.id;
    const next = new URLSearchParams(searchParams);
    next.set("tab", "fit-report");
    setSearchParams(next);
    showToast(
      `Re-evaluation completed. ${taskBillingSummary(reevaluationBilling.data, reevaluationBilling.isError)} The current Fit report is ready to review.`,
      "success",
    );
  }, [
    reevaluateTask.data,
    reevaluationBilling.data,
    reevaluationBilling.isError,
    reevaluationBilling.isSuccess,
    searchParams,
    setSearchParams,
    showToast,
  ]);
  useEffect(() => {
    const task = reevaluateTask.data;
    if (!task || task.status !== "failed" || failedReevaluation.current === task.id) return;
    if (!reevaluationBilling.isSuccess && !reevaluationBilling.isError) return;
    failedReevaluation.current = task.id;
    showToast(
      reevaluationFailure(
        task.error_message,
        reevaluationBilling.data,
        reevaluationBilling.isError,
      ),
      "error",
    );
  }, [
    reevaluateTask.data,
    reevaluationBilling.data,
    reevaluationBilling.isError,
    reevaluationBilling.isSuccess,
    showToast,
  ]);
  const append = useAppendApplicationEvent(workspaceId, jobId);
  const retainRecorded = useReviewEvaluationCurrentness(workspaceId);
  const { register, handleSubmit, reset } = useForm<ApplicationEventForm>({
    defaultValues: { status: "package_ready", notes: "", occurred_at: localDateTimeNow() },
  });
  const currentApplicationStatus = job.data?.application_status ?? "not_applied";
  const defaultApplicationStatus =
    applicationEventStatusValues(
      currentApplicationStatus,
      (job.data?.application_event_count ?? 0) > 0,
    )[0] ?? "";
  useEffect(() => {
    reset({ status: defaultApplicationStatus, notes: "", occurred_at: localDateTimeNow() });
  }, [defaultApplicationStatus, reset]);
  if (job.isLoading) return <LoadingState label="Opening job workspace" />;
  if (job.error instanceof ApiError && job.error.status === 404) {
    return (
      <EmptyState>
        <strong>No job with this id in this workspace.</strong>
        <p>
          Job id <code>{jobId}</code> is not stored under the current workspace. It may have been
          archived, moved to another workspace, or the link is stale.
        </p>
        <Link className="button button--primary" to="/jobs">
          Back to jobs
        </Link>
      </EmptyState>
    );
  }
  if (job.isError || !job.data) return <ErrorState error={job.error} />;
  const data = job.data;
  const evaluationReviewReason = evaluationReviewMessage(
    evaluationReview(data.evaluation_inputs, data.evaluation_review),
  );
  const disputeRefreshBilling = disputeRefresh
    ? taskBillingSummary(refreshBilling.data, refreshBilling.isError)
    : "";
  const extractionDisputable =
    !data.archived_at &&
    data.enable_dispute_reextraction &&
    data.disputed_extraction_ruling_count > 0;
  const fitDisputable =
    !data.archived_at && data.enable_dispute_rematch && data.disputed_fit_ruling_count > 0;
  const correction = data.application_events
    .slice()
    .reverse()
    .find((event) => event.event_type === "correction");
  const sortScores = (column: ScoreSort) => {
    const next = nextSort(scoreSort, scoreDirection, column);
    setScoreSort(next.key);
    setScoreDirection(next.direction);
  };
  const sortEvaluations = (column: EvaluationSort) => {
    const next = nextSort(evaluationSort, evaluationDirection, column);
    setEvaluationSort(next.key);
    setEvaluationDirection(next.direction);
  };
  const showEvidence = (evidenceId: string) => {
    const next = new URLSearchParams(searchParams);
    next.set("tab", "evidence");
    next.set("evidence", evidenceId);
    setSearchParams(next);
  };
  return (
    <Page
      eyebrow={data.employer}
      help="job"
      title={data.title}
      description={
        data.evaluation_id ? "Evaluation and job history." : "Tracked job and application history."
      }
      action={
        <div className="page-actions">
          {!data.archived_at && (
            <button
              className="button button--primary"
              disabled={awaitingRefreshBilling || refreshBusy}
              onClick={() =>
                refresh.mutate({
                  workspace_id: workspaceId,
                  task_type: "posting.refresh",
                  payload: { job_posting_id: jobId },
                })
              }
              type="button"
            >
              {refreshActionLabel}
            </button>
          )}
          {!data.archived_at && (
            <div aria-label="Evaluation" id={EVALUATE_ACTION_ID} role="group" tabIndex={-1}>
              <PaidAction
                evaluation={{ workspaceId, selection: { job_posting_ids: [jobId] } }}
                confirmation={{
                  title: `${data.evaluation_id ? "Re-run" : "Run"} the full evaluation for ${data.title}?`,
                  description: costs.confirmation("discovery.evaluate"),
                  details: (() => {
                    const freshness = reevaluationFreshness(data.latest_observation?.observed_at);
                    if (!freshness) return null;
                    return freshness.stale ? (
                      <p className="field-note">
                        Last verified {freshness.timestamp} (over {REEVAL_STALE_HOURS}h ago); a
                        fresh fetch will run before evaluation.
                      </p>
                    ) : (
                      <>
                        <p className="field-note">
                          Last verified {freshness.timestamp} (within {REEVAL_STALE_HOURS}h).
                        </p>
                        <p className="field-note">
                          Uses stored body; run Refresh posting first to re-fetch.
                        </p>
                      </>
                    );
                  })(),
                  confirmLabel: data.evaluation_id ? "Confirm re-evaluation" : "Confirm evaluation",
                }}
                costs={costs}
                disabled={
                  reevaluate.isPending ||
                  !costs.loaded ||
                  ["queued", "running"].includes(reevaluateTask.data?.status ?? "")
                }
                label={
                  reevaluate.isPending ||
                  ["queued", "running"].includes(reevaluateTask.data?.status ?? "")
                    ? "Evaluating"
                    : data.evaluation_id
                      ? "Re-run evaluation"
                      : "Full evaluation"
                }
                onAct={() =>
                  reevaluate.mutate({
                    workspace_id: workspaceId,
                    task_type: "discovery.evaluate",
                    payload: { job_posting_id: jobId, fresh: true },
                  })
                }
                taskType="discovery.evaluate"
              />
            </div>
          )}
          {refreshActive && <small className="field-note">Refresh in progress</small>}
          {awaitingRefreshStatus && (
            <small className="field-note" role="status">
              Checking refresh status
            </small>
          )}
          {refresh.data?.id && refreshTask.isError && (
            <small className="field-error" role="alert">
              Refresh status is unavailable. Reload or open Activity before starting another
              refresh.
            </small>
          )}
          <ConfirmedActionButton
            className="button button--quiet"
            disabled={archive.isPending}
            onConfirm={() =>
              archive.mutate(
                { jobId, workspaceId },
                {
                  onSuccess: () => {
                    void navigate("/jobs");
                  },
                },
              )
            }
            options={{
              title: "Archive this job?",
              description:
                "It leaves the active tracked-job list but every record stays in durable local history, and it can be restored from this page.",
              confirmLabel: "Archive job",
              danger: true,
            }}
          >
            Archive
          </ConfirmedActionButton>
          {data.archived_at && (
            <button
              className="button button--quiet"
              disabled={restore.isPending}
              onClick={() => restore.mutate({ jobId, workspaceId })}
              type="button"
            >
              {restore.isPending ? "Restoring" : "Restore to tracked jobs"}
            </button>
          )}
          <Link className="button button--quiet" to="/jobs">
            Back to jobs
          </Link>
        </div>
      }
    >
      <JobSummary
        data={data}
        onRetainRecorded={
          data.evaluation_id
            ? () => retainRecorded.mutate({ jobId, evaluationId: data.evaluation_id! })
            : undefined
        }
        retainingRecorded={retainRecorded.isPending}
      />
      {data.orphaned_ruling_count > 0 && (
        <aside aria-label="Rulings needing re-ruling" className="orphaned-rulings" role="status">
          <p>
            {data.orphaned_ruling_count === 1
              ? "One of your rulings does not match a requirement in the current evaluation. Re-rule to keep it active."
              : `${data.orphaned_ruling_count} of your rulings do not match a requirement in the current evaluation. Re-rule to keep them active.`}
          </p>
          <ul>
            {data.orphaned_rulings.map((row) => (
              <li key={row.requirement_key}>
                <span className="orphaned-rulings__kind">
                  {row.ruling === "disputed_fit" ? "Disputed (fit)" : "Disputed (extraction)"}
                </span>
                <span className="orphaned-rulings__text">{row.requirement_text}</span>
                {row.note && <em className="orphaned-rulings__note">{row.note}</em>}
              </li>
            ))}
          </ul>
        </aside>
      )}
      {refresh.isError && (
        <p className="field-error refresh-result">
          <ErrorNotice error={refresh.error} />
        </p>
      )}
      {refreshTask.data?.status === "completed" && (
        <aside className="refresh-result" role="status">
          {refreshTask.data.task_type === "posting.refresh_with_rematch" ? (
            <>Fit re-match completed. Reload this job to read the current ruling state.</>
          ) : refreshTask.data.progress.observation_unchanged ? (
            <>
              Unchanged since{" "}
              {text(refreshTask.data.progress.unchanged_since, "the previous observation")}.
            </>
          ) : (
            <>
              Changed:{" "}
              {Array.isArray(refreshTask.data.progress.changed_fields)
                ? refreshTask.data.progress.changed_fields.join(", ")
                : "see posting diff"}
              .{" "}
              <button className="text-button" onClick={() => setTab("Posting")} type="button">
                View posting
              </button>
            </>
          )}
        </aside>
      )}
      {refreshTask.data?.status === "failed" && (
        <p className="field-error refresh-result">
          Refresh failed: {terminated(refreshTask.data.error_message)}
        </p>
      )}
      {(reevaluate.isError || reevaluateTask.data?.status === "failed") && (
        <p className="field-error refresh-result" role="alert">
          {reevaluationFailure(
            reevaluateNotice
              ? reevaluateNotice.message
              : (reevaluateTask.data?.error_message ?? null),
            reevaluationBilling.data,
            reevaluationBilling.isError,
            !reevaluate.isError,
          )}
          {reevaluateNotice?.action && (
            <>
              {" "}
              <Link to={reevaluateNotice.action.to}>{reevaluateNotice.action.label}</Link>
            </>
          )}
        </p>
      )}
      <Tabs
        id="job-tabs"
        items={workspaceTabItems}
        label="Job workspaces"
        onSelect={(id) => {
          const next = workspaceTabs.find((item) => workspaceTabSlugs[item] === id);
          if (next) setTab(defaultTabForWorkspace[next]);
        }}
        selected={workspaceTabSlugs[workspaceTab]}
      />
      <TabPanel id="job-tabs" selected={workspaceTabSlugs[workspaceTab]}>
        {workspaceTab === "Overview" && (
          <JobOverview
            data={data}
            hasCorrection={Boolean(correction)}
            jobId={jobId}
            key={`${jobId}:${data.manual_reviewed_at ?? ""}`}
            onOpenApplicationHistory={() => setTab("Application history")}
            onOpenPosting={() => setTab("Posting")}
            workspaceId={workspaceId}
          />
        )}
        {workspaceTab === "Fit" && !data.evaluation_id && (
          <EmptyState>
            No model evaluation is recorded for this job. Your review, application history, and
            submitted files remain available.
          </EmptyState>
        )}
        {workspaceTab === "Fit" && data.evaluation_id && (
          <>
            <Tabs
              id="job-fit-tabs"
              items={fitTabItems}
              label="Fit workspace sections"
              onSelect={(id) => {
                const next = fitTabs.find((item) => tabSlugs[item] === id);
                if (next) setTab(next);
              }}
              selected={tabSlugs[tab]}
            />
            <TabPanel id="job-fit-tabs" selected={tabSlugs[tab]}>
              {tab === "Evaluation" && (
                <JobEvaluation
                  data={data}
                  evaluationDirection={evaluationDirection}
                  evaluationSort={evaluationSort}
                  onOpenStudio={() => setTab("Studio")}
                  onShowEvidence={showEvidence}
                  onEvaluationPageChange={setEvaluationOffset}
                  onSortEvaluations={sortEvaluations}
                  onSortScores={sortScores}
                  evaluationOffset={evaluationOffset}
                  pageSize={HISTORY_PAGE_SIZE}
                  scoreDirection={scoreDirection}
                  scoreSort={scoreSort}
                  workspaceId={workspaceId}
                />
              )}
              {tab === "Evidence & gaps" && (
                <EvidenceWorkspace
                  data={data}
                  evidenceSource={evidenceSource}
                  fitReportPending={fitReport.isLoading}
                  fitRequirements={fitReport.data?.requirements}
                  jobId={jobId}
                  onSelectEvidence={showEvidence}
                  onToggleUnsupported={() => setUnsupportedOnly((value) => !value)}
                  selectedEvidenceId={selectedEvidenceId}
                  unsupportedOnly={unsupportedOnly}
                />
              )}
              {tab === "Fit report" && (
                <FitReport
                  error={fitReport.error}
                  exportUrl={
                    data.fit_report_available && !data.archived_at
                      ? api.fitReportExportUrl(jobId, workspaceId)
                      : undefined
                  }
                  isError={fitReport.isError}
                  isLoading={fitReport.isLoading}
                  onShowEvidence={showEvidence}
                  report={fitReport.data}
                  markdown={fitReport.data?.markdown}
                  staleReason={
                    reevaluateTask.data?.status === "failed"
                      ? `The latest re-evaluation failed. This remains evaluation v${data.evaluation_version} (${data.evaluation_id}) from before that attempt and was not replaced.`
                      : (evaluationReviewReason ?? undefined)
                  }
                />
              )}
            </TabPanel>
            {disputeRefreshBilling && (
              <p aria-label="Dispute refresh billing" className="field-note" role="status">
                {disputeRefreshBilling}
              </p>
            )}
            {(extractionDisputable || fitDisputable) && (
              <details className="advanced-view">
                <summary>Advanced</summary>
                <div className="page-actions">
                  {extractionDisputable && (
                    <DisputeRefreshAction
                      billingPending={awaitingRefreshBilling}
                      kind="extraction"
                      workspaceId={workspaceId}
                      jobId={jobId}
                      refresh={refresh}
                      refreshBusy={refreshBusy}
                      costs={costs}
                    />
                  )}
                  {fitDisputable && (
                    <DisputeRefreshAction
                      billingPending={awaitingRefreshBilling}
                      kind="fit"
                      workspaceId={workspaceId}
                      jobId={jobId}
                      refresh={refresh}
                      refreshBusy={refreshBusy}
                      costs={costs}
                    />
                  )}
                </div>
              </details>
            )}
          </>
        )}
        {workspaceTab === "Tailor" && (
          <>
            <Tabs
              id="job-tailor-tabs"
              items={tailorTabItems}
              label="Tailoring workspace sections"
              onSelect={(id) => {
                const next = tailorTabs.find((item) => tabSlugs[item] === id);
                if (next) setTab(next);
              }}
              selected={tabSlugs[tab]}
            />
            <TabPanel id="job-tailor-tabs" selected={tabSlugs[tab]}>
              {tab === "Studio" && (
                <TailoringStudio
                  evaluateActionId={data.archived_at ? undefined : EVALUATE_ACTION_ID}
                  jobId={jobId}
                  workspaceId={workspaceId}
                />
              )}
              {tab === "Files" && (
                <Suspense fallback={<LoadingState label="Opening files" />}>
                  <JobDocuments jobId={jobId} workspaceId={workspaceId} />
                </Suspense>
              )}
              {tab === "Tailor hint" && (
                <JobHint
                  enabled
                  hint={data.posting_hint ?? null}
                  jobId={jobId}
                  workspaceId={workspaceId}
                />
              )}
            </TabPanel>
          </>
        )}
        {workspaceTab === "Posting" && (
          <PostingReader
            jobId={jobId}
            latestObservation={data.latest_observation}
            observationCount={data.observation_count}
            observationOffset={observationOffset}
            observations={data.observations}
            onObservationPageChange={setObservationOffset}
            pageSize={HISTORY_PAGE_SIZE}
            workspaceId={workspaceId}
          />
        )}
        {workspaceTab === "Application" && (
          <>
            <Tabs
              id="job-application-tabs"
              items={applicationTabItems}
              label="Application workspace sections"
              onSelect={(id) => {
                const next = applicationTabs.find((item) => tabSlugs[item] === id);
                if (next) setTab(next);
              }}
              selected={tabSlugs[tab]}
            />
            <TabPanel id="job-application-tabs" selected={tabSlugs[tab]}>
              {tab === "Notes" && <JobNotes jobId={jobId} workspaceId={workspaceId} />}
              {tab === "Application history" && (
                <ApplicationHistory
                  appendError={append.isError ? append.error.message : null}
                  appendPending={append.isPending}
                  currentStatus={data.application_status}
                  eventCount={data.application_event_count}
                  eventOffset={applicationEventOffset}
                  events={data.application_events}
                  jobId={jobId}
                  notes={jobNotes.data ?? []}
                  onEventPageChange={setApplicationEventOffset}
                  onSubmit={(event) =>
                    void handleSubmit((values) =>
                      append.mutate(
                        { ...values, occurred_at: new Date(values.occurred_at).toISOString() },
                        {
                          onSuccess: (saved) =>
                            reset({
                              status: applicationEventStatusValues(saved.status, true)[0] ?? "",
                              notes: "",
                              occurred_at: localDateTimeNow(),
                            }),
                        },
                      ),
                    )(event)
                  }
                  register={register}
                  pageSize={HISTORY_PAGE_SIZE}
                />
              )}
            </TabPanel>
          </>
        )}
      </TabPanel>
    </Page>
  );
}
