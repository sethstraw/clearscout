import { requestIntentJson, requestJson as request } from "../http";
import type {
  Job,
  JobDetail,
  ManualJobReview,
  ClosingDateEditInput,
  JobArchive,
  JobArchiveBatch,
  JobArchiveBatchBody,
  PostingHint,
  PostingHintBody,
  JobEmployerFacet,
  JobPage,
  WorkspaceSummary,
  SpendSummary,
  ScoreWeightSet,
  ScoreWeightSetInput,
  ApplicationEvent,
  PostingObservationBody,
  PostingDiff,
  EvidenceSource,
  FitReport,
  ApplicationOption,
} from "./types";
import { MAX_PAGE_SIZE, PAGE_SIZE } from "./constants";

export type JobHistoryQuery = {
  observationOffset?: number;
  evaluationOffset?: number;
  applicationEventOffset?: number;
  historyLimit?: number;
};

export function manualReviewFromJob(data: JobDetail): ManualJobReview {
  return {
    priority: data.manual_priority,
    fit: data.manual_fit,
    note: data.manual_note,
    recorded_fit_score: data.recorded_fit_score,
    recorded_compensation_score: data.recorded_compensation_score,
    recorded_overall_score: data.recorded_overall_score,
    recorded_compensation: data.recorded_compensation,
    recorded_work_model: data.recorded_work_model,
    recorded_eligibility: data.recorded_eligibility,
    next_action: data.next_action,
    recorded_deadline: data.recorded_deadline,
    recorded_source: data.recorded_source,
  };
}

export type JobListQuery = {
  search?: string;
  decision?: string;
  status?: string | string[];
  completeness?: string;
  place?: string;
  employer?: readonly string[];
  postingStatus?: string;
  includeArchived?: boolean;
  offset?: number;
  limit?: number;
  sort?: string;
  descending?: boolean;
};

/** Every row the filters match, with the employer counts the server computed for them. */
type JobLedger = { items: Job[]; employers: JobEmployerFacet[] };

export const jobsApi = {
  jobs: (workspaceId: string, options: JobListQuery = {}) => {
    const query = new URLSearchParams({
      workspace_id: workspaceId,
      offset: String(options.offset ?? 0),
      limit: String(options.limit ?? PAGE_SIZE),
      sort: options.sort ?? "recorded_score",
      descending: String(options.descending ?? true),
    });
    if (options.search) query.set("search", options.search);
    if (options.decision) query.append("decision", options.decision);
    // Send repeated status parameters so filtering remains server-side.
    for (const status of [options.status ?? []].flat()) {
      if (status) query.append("application_status", status);
    }
    if (options.completeness) query.set("completeness", options.completeness);
    if (options.place) query.append("place", options.place);
    for (const employer of options.employer ?? []) query.append("employer", employer);
    if (options.postingStatus) query.append("posting_status", options.postingStatus);
    if (options.includeArchived) query.set("include_archived", "true");
    return request<JobPage>(`/jobs?${query.toString()}`);
  },
  jobLedger: async (
    workspaceId: string,
    options: Omit<JobListQuery, "offset" | "limit"> = {},
  ): Promise<JobLedger> => {
    const items: Job[] = [];
    for (;;) {
      const page = await jobsApi.jobs(workspaceId, {
        ...options,
        offset: items.length,
        limit: MAX_PAGE_SIZE,
      });
      items.push(...page.items);
      if (!page.items.length || items.length >= page.total) {
        return { items, employers: page.employers };
      }
    }
  },
  allJobs: async (workspaceId: string, options: Omit<JobListQuery, "offset" | "limit"> = {}) =>
    (await jobsApi.jobLedger(workspaceId, options)).items,
  workspaceSummary: (workspaceId: string) =>
    request<WorkspaceSummary>(`/workspaces/${encodeURIComponent(workspaceId)}/summary`),
  workspaceSpend: (workspaceId: string) =>
    request<SpendSummary>(`/workspaces/${encodeURIComponent(workspaceId)}/spend`),
  scoreWeightSets: (workspaceId: string) =>
    request<ScoreWeightSet[]>(`/workspaces/${encodeURIComponent(workspaceId)}/score-weight-sets`),
  createScoreWeightSet: (workspaceId: string, body: ScoreWeightSetInput) =>
    request<ScoreWeightSet>(`/workspaces/${encodeURIComponent(workspaceId)}/score-weight-sets`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  job: (jobId: string, workspaceId: string, options: JobHistoryQuery = {}) => {
    const query = new URLSearchParams({
      workspace_id: workspaceId,
      observation_offset: String(options.observationOffset ?? 0),
      evaluation_offset: String(options.evaluationOffset ?? 0),
      application_event_offset: String(options.applicationEventOffset ?? 0),
      history_limit: String(options.historyLimit ?? PAGE_SIZE),
    });
    return request<JobDetail>(`/jobs/${encodeURIComponent(jobId)}?${query.toString()}`);
  },
  reviewEvaluationCurrentness: (jobId: string, evaluationId: string, workspaceId: string) =>
    request<JobDetail>(
      `/jobs/${encodeURIComponent(jobId)}/evaluations/${encodeURIComponent(evaluationId)}/review-currentness?workspace_id=${encodeURIComponent(workspaceId)}`,
      { method: "POST" },
    ),
  saveManualJobReview: (jobId: string, workspaceId: string, body: ManualJobReview) =>
    request<JobDetail>(
      `/jobs/${encodeURIComponent(jobId)}/manual-review?workspace_id=${encodeURIComponent(workspaceId)}`,
      { method: "PUT", body: JSON.stringify(body) },
    ),
  saveClosingDate: (jobId: string, workspaceId: string, body: ClosingDateEditInput) =>
    request<JobDetail>(
      `/jobs/${encodeURIComponent(jobId)}/closing-date?workspace_id=${encodeURIComponent(workspaceId)}`,
      { method: "PUT", body: JSON.stringify(body) },
    ),
  evidenceSource: (evidenceId: string, workspaceId: string) =>
    request<EvidenceSource>(
      `/evidence/${encodeURIComponent(evidenceId)}/source?workspace_id=${encodeURIComponent(workspaceId)}`,
    ),
  fitReport: (jobId: string, workspaceId: string) =>
    request<FitReport>(
      `/jobs/${encodeURIComponent(jobId)}/fit-report?workspace_id=${encodeURIComponent(workspaceId)}`,
    ),
  fitReportExportUrl: (jobId: string, workspaceId: string) =>
    `/api/v1/jobs/${encodeURIComponent(jobId)}/fit-report/export?workspace_id=${encodeURIComponent(workspaceId)}`,
  archiveJob: ({ jobId, workspaceId }: { jobId: string; workspaceId: string }) =>
    request<JobArchive>(
      `/jobs/${encodeURIComponent(jobId)}/archive?workspace_id=${encodeURIComponent(workspaceId)}`,
      { method: "POST" },
    ),
  archiveJobs: ({ jobIds, workspaceId }: { jobIds: string[]; workspaceId: string }) =>
    request<JobArchiveBatch>(`/jobs/archive?workspace_id=${encodeURIComponent(workspaceId)}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ job_posting_ids: jobIds } satisfies JobArchiveBatchBody),
    }),
  unarchiveJob: ({ jobId, workspaceId }: { jobId: string; workspaceId: string }) =>
    request<JobArchive>(
      `/jobs/${encodeURIComponent(jobId)}/unarchive?workspace_id=${encodeURIComponent(workspaceId)}`,
      { method: "POST" },
    ),
  setPostingHint: ({
    jobId,
    workspaceId,
    hint,
  }: {
    jobId: string;
    workspaceId: string;
    hint: string | null;
  }) =>
    request<PostingHint>(
      `/jobs/${encodeURIComponent(jobId)}/hint?workspace_id=${encodeURIComponent(workspaceId)}`,
      {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ hint } satisfies PostingHintBody),
      },
    ),
  postingObservation: (observationId: string, workspaceId: string) =>
    request<PostingObservationBody>(
      `/posting-observations/${encodeURIComponent(observationId)}?workspace_id=${encodeURIComponent(workspaceId)}`,
    ),
  postingDiff: (
    jobId: string,
    workspaceId: string,
    fromObservationId: string,
    toObservationId: string,
  ) => {
    const query = new URLSearchParams({
      workspace_id: workspaceId,
      from: fromObservationId,
      to: toObservationId,
    });
    return request<PostingDiff>(`/jobs/${encodeURIComponent(jobId)}/posting-diff?${query}`);
  },
  appendApplicationEvent: (
    jobId: string,
    workspaceId: string,
    body: { status: string; occurred_at: string; notes: string },
  ) => {
    const path = `/jobs/${encodeURIComponent(jobId)}/application-events?workspace_id=${encodeURIComponent(workspaceId)}`;
    // The client timestamp stays out of the intent, so a retry is the same write.
    const intent = JSON.stringify({ status: body.status, notes: body.notes });
    return requestIntentJson<ApplicationEvent>(path, intent, JSON.stringify(body));
  },
  applicationOptions: (workspaceId: string) =>
    request<ApplicationOption[]>(
      `/applications/options?workspace_id=${encodeURIComponent(workspaceId)}`,
    ),
};
