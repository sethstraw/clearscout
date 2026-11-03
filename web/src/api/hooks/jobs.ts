import { useQuery } from "@tanstack/react-query";

import {
  MAX_PAGE_SIZE,
  PAGE_SIZE,
  api,
  type JobHistoryQuery,
  type JobListQuery,
  type ManualJobReview,
  type ClosingDateEditInput,
} from "../client";
import { ApiError } from "../http";
import { queryKeys } from "./keys";
import { useWorkspaceMutation } from "./records";

/** Everything the ledger can narrow by. */
type JobQuery = JobListQuery;

export function useAllJobs(workspaceId: string, query: Omit<JobQuery, "offset" | "limit"> = {}) {
  const options = {
    sort: "recorded_score",
    descending: true,
    ...query,
  };
  return useQuery({
    queryKey: [...queryKeys.allJobs(workspaceId), options] as const,
    queryFn: ({ signal }) => api.allJobs(workspaceId, options, signal),
    enabled: Boolean(workspaceId),
  });
}

/** The ledger's rows plus its employer facet, cached apart from bare row lists. */
export function useJobLedger(workspaceId: string, query: Omit<JobQuery, "offset" | "limit"> = {}) {
  const options = {
    sort: "recorded_score",
    descending: true,
    ...query,
  };
  return useQuery({
    queryKey: [...queryKeys.allJobs(workspaceId), "ledger", options] as const,
    queryFn: ({ signal }) => api.jobLedger(workspaceId, options, signal),
    enabled: Boolean(workspaceId),
  });
}

export function useApplicationPipeline(workspaceId: string, statuses: readonly string[]) {
  return useQuery({
    queryKey: queryKeys.applicationPipeline(workspaceId, statuses),
    // Filtered by the server, so four columns do not cost the whole ledger.
    queryFn: ({ signal }) =>
      api.jobs(workspaceId, { status: [...statuses], limit: MAX_PAGE_SIZE }, signal),
    enabled: Boolean(workspaceId),
  });
}

export function usePostingObservation(workspaceId: string, observationId: string) {
  return useQuery({
    queryKey: queryKeys.posting(workspaceId, observationId),
    queryFn: ({ signal }) => api.postingObservation(observationId, workspaceId, signal),
    enabled: Boolean(workspaceId && observationId),
  });
}

export function usePostingDiff(
  jobId: string,
  workspaceId: string,
  fromObservationId: string,
  toObservationId: string,
) {
  return useQuery({
    queryKey: queryKeys.postingDiff(workspaceId, jobId, fromObservationId, toObservationId),
    queryFn: ({ signal }) =>
      api.postingDiff(jobId, workspaceId, fromObservationId, toObservationId, signal),
    enabled: Boolean(
      jobId &&
      workspaceId &&
      fromObservationId &&
      toObservationId &&
      fromObservationId !== toObservationId,
    ),
  });
}

export function useJob(workspaceId: string, jobId: string, history: JobHistoryQuery = {}) {
  const offsets = {
    observationOffset: history.observationOffset ?? 0,
    evaluationOffset: history.evaluationOffset ?? 0,
    applicationEventOffset: history.applicationEventOffset ?? 0,
    historyLimit: history.historyLimit ?? PAGE_SIZE,
  };
  // `offsets` carries every field of JobHistoryQuery with defaults applied, so
  // the key is complete; the rule cannot see through the derivation.
  // eslint-disable-next-line @tanstack/query/exhaustive-deps
  return useQuery({
    queryKey: queryKeys.job(workspaceId, jobId, offsets),
    queryFn: ({ signal }) => api.job(jobId, workspaceId, history, signal),
    enabled: Boolean(workspaceId && jobId),
  });
}

export function useEvidenceSource(workspaceId: string, evidenceId: string) {
  return useQuery({
    queryKey: queryKeys.evidenceSource(workspaceId, evidenceId),
    queryFn: ({ signal }) => api.evidenceSource(evidenceId, workspaceId, signal),
    enabled: Boolean(workspaceId && evidenceId),
  });
}

export function useFitReport(workspaceId: string, jobId: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.fitReport(workspaceId, jobId),
    queryFn: ({ signal }) => api.fitReport(jobId, workspaceId, signal),
    enabled: Boolean(enabled && workspaceId && jobId),
    // A 404 means the evaluation has no fit report, which retrying cannot change.
    retry: (failureCount, error) =>
      error instanceof ApiError && error.status === 404 ? false : failureCount < 1,
  });
}

export function useAppendApplicationEvent(workspaceId: string, jobId: string) {
  return useWorkspaceMutation(
    workspaceId,
    api.appendApplicationEvent.bind(api, jobId, workspaceId),
    { successMessage: "Application status recorded." },
  );
}

export function useUnarchiveJob(workspaceId: string) {
  return useWorkspaceMutation(workspaceId, api.unarchiveJob, {
    successMessage: "Job restored to the active ledger.",
  });
}

export function useArchiveJob(workspaceId: string) {
  return useWorkspaceMutation(workspaceId, api.archiveJob, {
    successMessage: "Job archived.",
  });
}

export function useArchiveJobs(workspaceId: string) {
  return useWorkspaceMutation(workspaceId, api.archiveJobs, {
    successMessage: (result) => {
      if (!result.archived_count) {
        return `All ${result.already_archived_count} selected jobs were already archived.`;
      }
      return result.already_archived_count
        ? `Archived ${result.archived_count} jobs; ${result.already_archived_count} already archived.`
        : `Archived ${result.archived_count} selected jobs.`;
    },
  });
}

export function useReviewEvaluationCurrentness(workspaceId: string) {
  return useWorkspaceMutation(
    workspaceId,
    ({ jobId, evaluationId }: { jobId: string; evaluationId: string }) =>
      api.reviewEvaluationCurrentness(jobId, evaluationId, workspaceId),
    { successMessage: "Recorded evaluation retained after review." },
  );
}

export function useSaveManualJobReview(workspaceId: string, jobId: string) {
  return useWorkspaceMutation(
    workspaceId,
    (body: ManualJobReview) => api.saveManualJobReview(jobId, workspaceId, body),
    { successMessage: "Your review was saved." },
  );
}

export function useSaveClosingDate(workspaceId: string) {
  return useWorkspaceMutation(
    workspaceId,
    ({ jobId, body }: { jobId: string; body: ClosingDateEditInput }) =>
      api.saveClosingDate(jobId, workspaceId, body),
  );
}
