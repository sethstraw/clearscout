import { requestJson as request } from "../http";
import type {
  Guardrails,
  GuardrailPreviewReport,
  GuardrailsSaved,
  GuardrailsState,
  GuardrailVersion,
  JobDetail,
  GuidedCampaignConfiguration,
  SearchVocabularyConfiguration,
  RelevanceReviewConfiguration,
  FoundPostingCreate,
  FoundPostingCheck,
  FoundPostingCheckResponse,
  FoundPostingAddResponse,
  LearningLoopMetrics,
  LearningLoopPolicy,
  LearningLoopPolicySaved,
  LearningLoopPolicyState,
  PendingDiscovery,
  PendingClosureReconcileRequest,
  PendingClosureReconcileResponse,
  PendingDiscoveryTitleUpdate,
  PendingMarkers,
  ClosingDateEditInput,
  PendingPostingContentAction,
  PendingPostingIdentityConfirm,
  PendingPostingTextAttach,
  PostingResolutionPreflightRequest,
  PostingRecoveryApprovalManifest,
  DispositionApply,
  DispositionPreview,
  DispositionSelection,
  DismissedDiscovery,
  PendingDisposition,
  PendingDuplicateCandidate,
  PendingQueueReset,
  PostingRefusal,
  RelevanceDispute,
  RelevanceDisputeBody,
  RelevanceDisputeCleared,
  RunCostHistory,
  Source,
  ScanSchedule,
  ScanScheduleInput,
  SourceCreate,
  SourceEdit,
  SourceList,
  SourcePreview,
  SourcePreviewRequest,
  SourceScanHistory,
  SourceScanHistoryOptions,
  SourceScanOutcomeOptions,
  SourceScanOutcomePage,
  ScreenedOutTitleOptions,
  ScreenedOutTitlePage,
  ScreenedOutTitleReview,
  Task,
  Workspace,
  WorkspaceCreate,
} from "./types";

export const workspacesApi = {
  workspaces: (signal?: AbortSignal) => request<Workspace[]>("/workspaces", { signal }),
  createWorkspace: (body: WorkspaceCreate) =>
    request<Workspace>("/workspaces", { method: "POST", body: JSON.stringify(body) }),
  scanSchedule: (workspaceId: string, signal?: AbortSignal) =>
    request<ScanSchedule>(`/workspaces/${encodeURIComponent(workspaceId)}/scan-schedule`, {
      signal,
    }),
  saveScanSchedule: (workspaceId: string, body: ScanScheduleInput) =>
    request<ScanSchedule>(`/workspaces/${encodeURIComponent(workspaceId)}/scan-schedule`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  guidedCampaignConfiguration: (workspaceId: string, signal?: AbortSignal) =>
    request<GuidedCampaignConfiguration>(
      `/workspaces/${encodeURIComponent(workspaceId)}/guided-campaign-configuration`,
      { signal },
    ),
  searchVocabularyConfiguration: (workspaceId: string, signal?: AbortSignal) =>
    request<SearchVocabularyConfiguration>(
      `/workspaces/${encodeURIComponent(workspaceId)}/search-vocabulary-configuration`,
      { signal },
    ),
  relevanceReviewConfiguration: (workspaceId: string, signal?: AbortSignal) =>
    request<RelevanceReviewConfiguration>(
      `/workspaces/${encodeURIComponent(workspaceId)}/relevance-review-configuration`,
      { signal },
    ),
  guardrails: (workspaceId: string, signal?: AbortSignal) =>
    request<GuardrailsState>(`/workspaces/${encodeURIComponent(workspaceId)}/guardrails`, {
      signal,
    }),
  guardrailHistory: (workspaceId: string, signal?: AbortSignal) =>
    request<GuardrailVersion[]>(
      `/workspaces/${encodeURIComponent(workspaceId)}/guardrails/history`,
      { signal },
    ),
  learningLoopMetrics: (workspaceId: string, signal?: AbortSignal) =>
    request<LearningLoopMetrics>(
      `/workspaces/${encodeURIComponent(workspaceId)}/learning-loop-metrics`,
      { signal },
    ),
  learningLoopPolicy: (workspaceId: string, signal?: AbortSignal) =>
    request<LearningLoopPolicyState>(
      `/workspaces/${encodeURIComponent(workspaceId)}/learning-loop-policy`,
      { signal },
    ),
  saveLearningLoopPolicy: (
    workspaceId: string,
    body: { expected_version: number; policy: LearningLoopPolicy },
  ) =>
    request<LearningLoopPolicySaved>(
      `/workspaces/${encodeURIComponent(workspaceId)}/learning-loop-policy`,
      { method: "PUT", body: JSON.stringify(body) },
    ),
  saveGuardrails: (workspaceId: string, body: { expected_version: number; rules: Guardrails }) =>
    request<GuardrailsSaved>(`/workspaces/${encodeURIComponent(workspaceId)}/guardrails`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  previewGuardrails: (workspaceId: string, rules: Guardrails) =>
    request<GuardrailPreviewReport>(
      `/workspaces/${encodeURIComponent(workspaceId)}/guardrails/preview`,
      { method: "POST", body: JSON.stringify({ rules }) },
    ),
  runCostHistory: (workspaceId: string, signal?: AbortSignal) =>
    request<RunCostHistory[]>(`/workspaces/${encodeURIComponent(workspaceId)}/spend-history`, {
      signal,
    }),
  postingRefusals: (workspaceId: string, signal?: AbortSignal) =>
    request<PostingRefusal[]>(`/workspaces/${encodeURIComponent(workspaceId)}/posting-refusals`, {
      signal,
    }),
  sourceScans: (workspaceId: string, options: SourceScanHistoryOptions, signal?: AbortSignal) => {
    const params = new URLSearchParams({
      offset: String(options.offset),
      limit: String(options.limit),
    });
    if (options.sourceId) params.set("source_id", options.sourceId);
    if (options.status) params.set("status", options.status);
    if (options.latestOnly) params.set("latest_only", "true");
    return request<SourceScanHistory>(
      `/workspaces/${encodeURIComponent(workspaceId)}/source-scans?${params.toString()}`,
      { signal },
    );
  },
  continueSourceScan: (workspaceId: string, sourceScanId: string) =>
    request<Task>(
      `/workspaces/${encodeURIComponent(workspaceId)}/source-scans/${encodeURIComponent(sourceScanId)}/continue`,
      { method: "POST" },
    ),
  sourceScanOutcomes: (
    workspaceId: string,
    options: SourceScanOutcomeOptions,
    signal?: AbortSignal,
  ) => {
    const params = new URLSearchParams({
      task_id: options.taskId,
      sort: options.sort,
      direction: options.direction,
      offset: String(options.offset),
      limit: String(options.limit),
    });
    if (options.search) params.set("search", options.search);
    if (options.source) params.set("source", options.source);
    if (options.terminalStage) params.set("terminal_stage", options.terminalStage);
    if (options.reason) params.set("reason", options.reason);
    if (options.candidateKind) params.set("candidate_kind", options.candidateKind);
    return request<SourceScanOutcomePage>(
      `/workspaces/${encodeURIComponent(workspaceId)}/source-scan-outcomes?${params.toString()}`,
      { signal },
    );
  },
  screenedOutTitles: (
    workspaceId: string,
    options: ScreenedOutTitleOptions,
    signal?: AbortSignal,
  ) => {
    const params = new URLSearchParams({
      offset: String(options.offset),
      limit: String(options.limit),
    });
    if (options.needsReview !== null) params.set("needs_review", String(options.needsReview));
    if (options.search) params.set("search", options.search);
    if (options.source) params.set("source", options.source);
    return request<ScreenedOutTitlePage>(
      `/workspaces/${encodeURIComponent(workspaceId)}/screened-out-titles?${params.toString()}`,
      { signal },
    );
  },
  reviewScreenedOutTitles: (workspaceId: string, ids: string[]) =>
    request<ScreenedOutTitleReview>(
      `/workspaces/${encodeURIComponent(workspaceId)}/screened-out-titles/review`,
      { method: "POST", body: JSON.stringify({ ids }) },
    ),
  sources: (workspaceId: string, signal?: AbortSignal) =>
    request<SourceList>(`/workspaces/${encodeURIComponent(workspaceId)}/sources`, { signal }),
  addFoundPosting: (workspaceId: string, body: FoundPostingCreate) =>
    request<FoundPostingAddResponse>(
      `/workspaces/${encodeURIComponent(workspaceId)}/pending-discoveries`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  checkFoundPosting: (workspaceId: string, body: FoundPostingCheck) =>
    request<FoundPostingCheckResponse>(
      `/workspaces/${encodeURIComponent(workspaceId)}/pending-discoveries/check`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  resetPendingQueue: (workspaceId: string) =>
    request<PendingQueueReset>(
      `/workspaces/${encodeURIComponent(workspaceId)}/pending-discoveries/reset`,
      { method: "POST" },
    ),
  trackPendingDiscovery: (workspaceId: string, pendingId: string) =>
    request<JobDetail>(
      `/workspaces/${encodeURIComponent(workspaceId)}/pending-discoveries/${encodeURIComponent(pendingId)}/track`,
      { method: "POST" },
    ),
  correctPendingDiscoveryTitle: (
    workspaceId: string,
    pendingId: string,
    body: PendingDiscoveryTitleUpdate,
  ) =>
    request<PendingDiscovery>(
      `/workspaces/${encodeURIComponent(workspaceId)}/pending-discoveries/${encodeURIComponent(pendingId)}`,
      { method: "PATCH", body: JSON.stringify(body) },
    ),
  setPendingMarkers: (workspaceId: string, pendingId: string, body: PendingMarkers) =>
    request<PendingDiscovery>(
      `/workspaces/${encodeURIComponent(workspaceId)}/pending-discoveries/${encodeURIComponent(pendingId)}/markers`,
      { method: "PUT", body: JSON.stringify(body) },
    ),
  setPendingClosingDate: (workspaceId: string, pendingId: string, body: ClosingDateEditInput) =>
    request<PendingDiscovery>(
      `/workspaces/${encodeURIComponent(workspaceId)}/pending-discoveries/${encodeURIComponent(pendingId)}/closing-date`,
      { method: "PUT", body: JSON.stringify(body) },
    ),
  attachPendingPostingText: (
    workspaceId: string,
    pendingId: string,
    body: PendingPostingTextAttach,
  ) =>
    request<PendingPostingContentAction>(
      `/workspaces/${encodeURIComponent(workspaceId)}/pending-discoveries/${encodeURIComponent(pendingId)}/content`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  confirmPendingPostingIdentity: (
    workspaceId: string,
    pendingId: string,
    body: PendingPostingIdentityConfirm,
  ) =>
    request<PendingPostingContentAction>(
      `/workspaces/${encodeURIComponent(workspaceId)}/pending-discoveries/${encodeURIComponent(pendingId)}/confirm-identity`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  reconcileSavedClosures: (workspaceId: string, body: PendingClosureReconcileRequest) =>
    request<PendingClosureReconcileResponse>(
      `/workspaces/${encodeURIComponent(workspaceId)}/pending-discoveries/reconcile-closures`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  postingRecoveryPreflight: (workspaceId: string, body: PostingResolutionPreflightRequest) =>
    request<PostingRecoveryApprovalManifest>(
      `/workspaces/${encodeURIComponent(workspaceId)}/posting-recovery-preflight`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  previewPendingDisposition: (workspaceId: string, body: DispositionSelection) =>
    request<DispositionPreview>(
      `/workspaces/${encodeURIComponent(workspaceId)}/pending-dispositions/preview`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  applyPendingDisposition: (workspaceId: string, body: DispositionApply) =>
    request<PendingDisposition[]>(
      `/workspaces/${encodeURIComponent(workspaceId)}/pending-dispositions/apply`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  dismissedPendingDiscoveries: (workspaceId: string, signal?: AbortSignal) =>
    request<DismissedDiscovery[]>(
      `/workspaces/${encodeURIComponent(workspaceId)}/pending-dispositions/dismissed`,
      { signal },
    ),
  pendingDuplicateCandidates: (workspaceId: string, signal?: AbortSignal) =>
    request<PendingDuplicateCandidate[]>(
      `/workspaces/${encodeURIComponent(workspaceId)}/pending-dispositions/duplicate-candidates`,
      { signal },
    ),
  disputeRelevanceVerdict: (workspaceId: string, pendingId: string, body: RelevanceDisputeBody) =>
    request<RelevanceDispute>(
      `/workspaces/${encodeURIComponent(workspaceId)}/relevance-disputes/${encodeURIComponent(pendingId)}`,
      { method: "PUT", body: JSON.stringify(body) },
    ),
  clearRelevanceDispute: (workspaceId: string, pendingId: string) =>
    request<RelevanceDisputeCleared>(
      `/workspaces/${encodeURIComponent(workspaceId)}/relevance-disputes/${encodeURIComponent(pendingId)}`,
      { method: "DELETE" },
    ),
  pendingDispositionHistory: (workspaceId: string, pendingId: string, signal?: AbortSignal) =>
    request<PendingDisposition[]>(
      `/workspaces/${encodeURIComponent(workspaceId)}/pending-discoveries/${encodeURIComponent(pendingId)}/dispositions`,
      { signal },
    ),
  addSource: (workspaceId: string, body: SourceCreate) =>
    request<Source>(`/workspaces/${encodeURIComponent(workspaceId)}/sources`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  editSource: (workspaceId: string, sourceId: string, body: SourceEdit) =>
    request<Source>(
      `/workspaces/${encodeURIComponent(workspaceId)}/sources/${encodeURIComponent(sourceId)}`,
      { method: "PATCH", body: JSON.stringify(body) },
    ),
  archiveSource: (workspaceId: string, sourceId: string) =>
    request<void>(
      `/workspaces/${encodeURIComponent(workspaceId)}/sources/${encodeURIComponent(sourceId)}`,
      { method: "DELETE" },
    ),
  previewSource: (workspaceId: string, body: SourcePreviewRequest) =>
    request<SourcePreview>(`/workspaces/${encodeURIComponent(workspaceId)}/sources/preview`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  revalidateSource: (workspaceId: string, sourceId: string) =>
    request<Source>(
      `/workspaces/${encodeURIComponent(workspaceId)}/sources/${encodeURIComponent(sourceId)}/revalidate`,
      { method: "POST" },
    ),
};
