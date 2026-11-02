import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../client";
import { queryKeys } from "./keys";
import type {
  Guardrails,
  LearningLoopPolicy,
  SourceCreate,
  SourceEdit,
  SourcePreviewRequest,
  SourceScanHistoryOptions,
  SourceScanOutcomeOptions,
  ScreenedOutTitleOptions,
  ScanScheduleInput,
} from "../client";

export function useWorkspaces() {
  return useQuery({ queryKey: queryKeys.workspaces, queryFn: api.workspaces });
}

/** Saved views for one table, so a page can offer the views that fit it. */
export function useSavedViews(workspaceId: string, resource = "") {
  return useQuery({
    queryKey: queryKeys.savedViews(workspaceId, resource),
    queryFn: () => api.savedViews(workspaceId, resource),
    enabled: Boolean(workspaceId),
  });
}

export function useWorkspaceSummary(workspaceId: string) {
  return useQuery({
    queryKey: queryKeys.workspaceSummary(workspaceId),
    queryFn: () => api.workspaceSummary(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

export function useWorkspaceSpend(workspaceId: string) {
  return useQuery({
    queryKey: queryKeys.workspaceSpend(workspaceId),
    queryFn: () => api.workspaceSpend(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

export function useScanSchedule(workspaceId: string) {
  return useQuery({
    queryKey: queryKeys.scanSchedule(workspaceId),
    queryFn: () => api.scanSchedule(workspaceId),
    enabled: Boolean(workspaceId),
    refetchInterval: 30_000,
  });
}

export function useSaveScanSchedule(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ScanScheduleInput) => api.saveScanSchedule(workspaceId, body),
    onSuccess: (schedule) => client.setQueryData(queryKeys.scanSchedule(workspaceId), schedule),
    onError: () => client.invalidateQueries({ queryKey: queryKeys.scanSchedule(workspaceId) }),
  });
}

export function useGuardrails(workspaceId: string) {
  return useQuery({
    queryKey: queryKeys.guardrails(workspaceId),
    queryFn: () => api.guardrails(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

export function useGuardrailHistory(workspaceId: string) {
  return useQuery({
    queryKey: queryKeys.guardrailHistory(workspaceId),
    queryFn: () => api.guardrailHistory(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

export function usePostingRefusals(workspaceId: string) {
  return useQuery({
    queryKey: queryKeys.postingRefusals(workspaceId),
    queryFn: () => api.postingRefusals(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

export function useSourceScans(workspaceId: string, options: SourceScanHistoryOptions) {
  return useQuery({
    queryKey: queryKeys.sourceScans(workspaceId, options),
    queryFn: () => api.sourceScans(workspaceId, options),
    enabled: Boolean(workspaceId),
  });
}

export function useContinueSourceScan(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (sourceScanId: string) => api.continueSourceScan(workspaceId, sourceScanId),
    // Prefix key match invalidates every offset/limit page of this workspace's history.
    onSuccess: () =>
      client.invalidateQueries({ queryKey: queryKeys.sourceScansPrefix(workspaceId) }),
  });
}

export function useSourceScanOutcomes(workspaceId: string, options: SourceScanOutcomeOptions) {
  return useQuery({
    queryKey: queryKeys.sourceScanOutcomes(workspaceId, options.taskId, options),
    queryFn: () => api.sourceScanOutcomes(workspaceId, options),
    enabled: Boolean(workspaceId && options.taskId),
    // Keep the prior page mounted while a new search/filter/sort loads, so the
    // toolbar and its focused search input are not torn down mid-keystroke.
    placeholderData: (previous) => previous,
  });
}

export function useScreenedOutTitles(workspaceId: string, options: ScreenedOutTitleOptions) {
  return useQuery({
    queryKey: queryKeys.screenedOutTitles(workspaceId, options),
    queryFn: () => api.screenedOutTitles(workspaceId, options),
    enabled: Boolean(workspaceId),
    placeholderData: (previous) => previous,
  });
}

export function useReviewScreenedOutTitles(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (ids: string[]) => api.reviewScreenedOutTitles(workspaceId, ids),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: queryKeys.screenedOutTitlesPrefix(workspaceId) }),
  });
}

export function useSources(workspaceId: string) {
  return useQuery({
    queryKey: queryKeys.sources(workspaceId),
    queryFn: () => api.sources(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

export function useAddSource(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: SourceCreate) => api.addSource(workspaceId, body),
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.sources(workspaceId) }),
  });
}

export function useEditSource(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ sourceId, ...body }: SourceEdit & { sourceId: string }) =>
      api.editSource(workspaceId, sourceId, body),
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.sources(workspaceId) }),
  });
}

export function useArchiveSource(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (sourceId: string) => api.archiveSource(workspaceId, sourceId),
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.sources(workspaceId) }),
  });
}

export function usePreviewSource(workspaceId: string) {
  return useMutation({
    mutationFn: (body: SourcePreviewRequest) => api.previewSource(workspaceId, body),
  });
}

export function useRevalidateSource(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (sourceId: string) => api.revalidateSource(workspaceId, sourceId),
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.sources(workspaceId) }),
  });
}

export function useSaveGuardrails(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { expected_version: number; rules: Guardrails }) =>
      api.saveGuardrails(workspaceId, body),
    onSuccess: (saved) => {
      client.setQueryData(queryKeys.guardrails(workspaceId), saved);
      // A save writes a new guardrail version.
      void client.invalidateQueries({ queryKey: queryKeys.guardrailHistory(workspaceId) });
    },
  });
}

export function usePreviewGuardrails(workspaceId: string) {
  return useMutation({
    mutationFn: (rules: Guardrails) => api.previewGuardrails(workspaceId, rules),
  });
}

export function useLearningLoopMetrics(workspaceId: string) {
  return useQuery({
    queryKey: queryKeys.learningLoopMetrics(workspaceId),
    queryFn: () => api.learningLoopMetrics(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

export function useLearningLoopPolicy(workspaceId: string) {
  return useQuery({
    queryKey: queryKeys.learningLoopPolicy(workspaceId),
    queryFn: () => api.learningLoopPolicy(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

export function useSaveLearningLoopPolicy(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { expected_version: number; policy: LearningLoopPolicy }) =>
      api.saveLearningLoopPolicy(workspaceId, body),
    onSuccess: (saved) => client.setQueryData(queryKeys.learningLoopPolicy(workspaceId), saved),
  });
}

export function useRunCostHistory(workspaceId: string) {
  return useQuery({
    queryKey: queryKeys.runCostHistory(workspaceId),
    queryFn: () => api.runCostHistory(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

export function useInitializeWorkspace() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: {
      name: string;
      slug: string;
      root_path?: string;
      provider: "anthropic" | "openai";
    }) => {
      return api.createWorkspace(input);
    },
    onSuccess: async () => client.invalidateQueries({ queryKey: queryKeys.workspaces }),
  });
}
