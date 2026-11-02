import type {
  PendingQueueQuery,
  ScreenedOutTitleOptions,
  SourceScanHistoryOptions,
  SourceScanOutcomeOptions,
} from "../client";

export const queryKeys = {
  health: ["health"] as const,
  providers: ["providers"] as const,
  workspaces: ["workspaces"] as const,
  guidedCampaignConfiguration: (workspaceId: string) =>
    ["guided-campaign-configuration", workspaceId] as const,
  searchVocabularyConfiguration: (workspaceId: string) =>
    ["search-vocabulary-configuration", workspaceId] as const,
  relevanceReviewConfiguration: (workspaceId: string) =>
    ["relevance-review-configuration", workspaceId] as const,
  allJobs: (workspaceId: string) => ["all-jobs", workspaceId] as const,
  workspaceSummary: (workspaceId: string) => ["workspace-summary", workspaceId] as const,
  workspaceSpend: (workspaceId: string) => ["workspace-spend", workspaceId] as const,
  scanSchedule: (workspaceId: string) => ["scan-schedule", workspaceId] as const,
  guardrails: (workspaceId: string) => ["guardrails", workspaceId] as const,
  guardrailHistory: (workspaceId: string) => ["guardrail-history", workspaceId] as const,
  learningLoopMetrics: (workspaceId: string) => ["learning-loop-metrics", workspaceId] as const,
  learningLoopPolicy: (workspaceId: string) => ["learning-loop-policy", workspaceId] as const,
  runCostHistory: (workspaceId: string) => ["spend-history", workspaceId] as const,
  applicationPipeline: (workspaceId: string, statuses: readonly string[]) =>
    ["application-pipeline", workspaceId, statuses] as const,
  job: (
    workspaceId: string,
    jobId: string,
    history: {
      observationOffset: number;
      evaluationOffset: number;
      applicationEventOffset: number;
      historyLimit: number;
    },
  ) => ["job", workspaceId, jobId, history] as const,
  evidenceSource: (workspaceId: string, evidenceId: string) =>
    ["evidence-source", workspaceId, evidenceId] as const,
  fitReport: (workspaceId: string, jobId: string) => ["fit-report", workspaceId, jobId] as const,
  posting: (workspaceId: string, observationId: string) =>
    ["posting", workspaceId, observationId] as const,
  postingDiff: (workspaceId: string, jobId: string, from: string, to: string) =>
    ["posting-diff", workspaceId, jobId, from, to] as const,
  tasks: (workspaceId: string) => ["tasks", workspaceId] as const,
  taskTypes: ["task-types"] as const,
  task: (workspaceId: string, taskId: string) => ["task", workspaceId, taskId] as const,
  taskBilling: (workspaceId: string, taskId: string) =>
    ["task-billing", workspaceId, taskId] as const,
  prompts: (workspaceId: string) => ["prompts", workspaceId] as const,
  prompt: (workspaceId: string, key: string) => ["prompt", workspaceId, key] as const,
  promptVersions: (workspaceId: string, key: string) =>
    ["prompt-versions", workspaceId, key] as const,
  artifacts: (workspaceId: string, entityType = "", entityId = "", includeRelated = false) =>
    ["artifacts", workspaceId, entityType, entityId, includeRelated] as const,
  artifactText: (workspaceId: string, artifactId: string) =>
    ["artifact-text", workspaceId, artifactId] as const,
  artifactVersions: (workspaceId: string, artifactId: string) =>
    ["artifact-versions", workspaceId, artifactId] as const,
  tailoringReview: (workspaceId: string, runId: string) =>
    ["tailoring-review", workspaceId, runId] as const,
  notes: (workspaceId: string, entityType = "", entityId = "", search = "") =>
    ["notes", workspaceId, entityType, entityId, search] as const,
  tags: (workspaceId: string) => ["tags", workspaceId] as const,
  savedViews: (workspaceId: string, resource = "") =>
    ["saved-views", workspaceId, resource] as const,
  searchParameterOptions: ["search-parameter-options"] as const,
  searchParameters: (workspaceId: string) => ["search-parameters", workspaceId] as const,
  guidedCampaignPreview: (
    workspaceId: string,
    maxModelCalls: number | null,
    maxHostedSearches: number | null,
  ) => ["guided-campaign-preview", workspaceId, maxModelCalls, maxHostedSearches] as const,
  latestGuidedCampaignReport: (workspaceId: string) =>
    ["guided-campaign-report", workspaceId] as const,
  searchParameterHistory: (workspaceId: string) =>
    ["search-parameter-history", workspaceId] as const,
  postingRefusals: (workspaceId: string) => ["posting-refusals", workspaceId] as const,
  sourceScansPrefix: (workspaceId: string) => ["source-scans", workspaceId] as const,
  sourceScans: (workspaceId: string, options: SourceScanHistoryOptions) =>
    [...queryKeys.sourceScansPrefix(workspaceId), options] as const,
  sourceScanOutcomes: (workspaceId: string, taskId: string, options: SourceScanOutcomeOptions) =>
    ["source-scan-outcomes", workspaceId, taskId, options] as const,
  screenedOutTitlesPrefix: (workspaceId: string) => ["screened-out-titles", workspaceId] as const,
  screenedOutTitles: (workspaceId: string, options: ScreenedOutTitleOptions) =>
    [...queryKeys.screenedOutTitlesPrefix(workspaceId), options] as const,
  sources: (workspaceId: string) => ["sources", workspaceId] as const,
  pendingQueuePrefix: (workspaceId: string) => ["pending-discoveries", workspaceId] as const,
  pendingDiscoveryPage: (
    workspaceId: string,
    limit: number,
    readyOffset: number,
    attentionOffset: number,
    expectedSnapshotSha256: string,
    snapshotBefore: string,
    filters: PendingQueueQuery,
  ) =>
    [
      ...queryKeys.pendingQueuePrefix(workspaceId),
      "page",
      limit,
      readyOffset,
      attentionOffset,
      expectedSnapshotSha256,
      snapshotBefore,
      filters,
    ] as const,
  pendingDiscoveryChanges: (
    workspaceId: string,
    snapshotBefore: string,
    filters: PendingQueueQuery,
  ) => [...queryKeys.pendingQueuePrefix(workspaceId), "changes", snapshotBefore, filters] as const,
  titleExclusionCollateral: (workspaceId: string, phrases: readonly string[]) =>
    [...queryKeys.pendingQueuePrefix(workspaceId), "title-exclusion-collateral", phrases] as const,
  dismissedPendingDiscoveries: (workspaceId: string) =>
    ["dismissed-pending-discoveries", workspaceId] as const,
  pendingDuplicateCandidates: (workspaceId: string) =>
    ["pending-duplicate-candidates", workspaceId] as const,
  pendingDispositionHistoryPrefix: (workspaceId: string) =>
    ["pending-disposition-history", workspaceId] as const,
  pendingDispositionHistory: (workspaceId: string, pendingId: string) =>
    [...queryKeys.pendingDispositionHistoryPrefix(workspaceId), pendingId] as const,
};
