import type { components, operations } from "../schema";

export type Health = components["schemas"]["HealthResponse"];
export type Provider = components["schemas"]["ProviderResponse"];
export type Workspace = components["schemas"]["WorkspaceResponse"];
export type ScanScheduleConfiguration = components["schemas"]["ScanScheduleConfiguration"];
export type ScanScheduleInput = components["schemas"]["ScanScheduleInput"];
export type ScanSchedule = components["schemas"]["ScanScheduleResponse"];
export type GuidedCampaignConfiguration =
  components["schemas"]["GuidedCampaignConfigurationResponse"];
export type SearchVocabularyConfiguration =
  components["schemas"]["SearchVocabularyConfigurationResponse"];
export type RelevanceReviewConfiguration =
  components["schemas"]["RelevanceReviewConfigurationResponse"];
export type WorkspaceCreate = components["schemas"]["WorkspaceCreate"];
export type MasterCvImport = components["schemas"]["MasterCvImportResponse"];
export type Job = components["schemas"]["JobSummaryResponse"];
export type JobDetail = components["schemas"]["JobDetailResponse"];
export type ManualJobReview = components["schemas"]["ManualJobReviewInput"];
export type JobArchive = components["schemas"]["JobArchiveResponse"];
export type JobArchiveBatch = components["schemas"]["JobArchiveBatchResponse"];
export type JobArchiveBatchBody = components["schemas"]["JobArchiveBatchBody"];
export type PostingHint = components["schemas"]["PostingHintResponse"];
export type PostingHintBody = components["schemas"]["PostingHintBody"];
export type JobPage = components["schemas"]["JobPageResponse"];
export type JobEmployerFacet = components["schemas"]["JobEmployerFacetResponse"];
export type LearningLoopMetrics = components["schemas"]["LearningLoopMetricsResponse"];
export type LearningLoopSignal = components["schemas"]["LearningLoopSignal"];
export type LearningLoopPolicy = components["schemas"]["LearningLoopPolicy"];
export type LearningLoopPolicyState = components["schemas"]["LearningLoopPolicyStateResponse"];
export type LearningLoopPolicySaved = components["schemas"]["LearningLoopPolicySavedResponse"];
export type WorkspaceSummary = components["schemas"]["WorkspaceSummaryResponse"];
export type SpendSummary = components["schemas"]["SpendSummaryResponse"];
export type OpenAiActualSpend = components["schemas"]["OpenAiActualSpendResponse"];
export type ReconciliationWindow = components["schemas"]["ReconciliationWindowBody"];

export type PriceMove = components["schemas"]["ModelPriceChange"];
export type PriceRefresh = components["schemas"]["PriceRefreshResponse"];
export type ScoreWeightSet = components["schemas"]["ScoreWeightSetResponse"];
export type ScoreWeightSetInput = components["schemas"]["ScoreWeightSetBody"];
export type ScoreWeightsInput = components["schemas"]["ScoreWeightsInput"];
export type MasterCvVersion = components["schemas"]["MasterCvVersionResponse"];
export type MasterCvContent = components["schemas"]["MasterCvContentResponse"];
export type MasterCvImpact = components["schemas"]["MasterCvImpactResponse"];
export type MasterCvCitationImpact = components["schemas"]["CitationImpactResponse"];
export type MasterCvEdit = components["schemas"]["MasterCvEditResponse"];
export type RequirementGaps = components["schemas"]["RequirementGapSummaryResponse"];
export type RequirementImportance =
  components["schemas"]["RequirementGapSourceResponse"]["importance"];
export type RulingValue = components["schemas"]["RequirementRulingBody"]["ruling"];
export type RequirementRulingBody = components["schemas"]["RequirementRulingBody"];
export type RequirementRuling = components["schemas"]["RequirementRulingResponse"];
export type RequirementRulings = components["schemas"]["RequirementRulingsResponse"];
export type PendingDiscovery = components["schemas"]["PendingDiscoveryResponse"];
export type PendingDiscoveryReadiness = components["schemas"]["PendingDiscoveryReadiness"];
export type PendingDiscoveryPage = components["schemas"]["PendingDiscoveryPageResponse"];
export type PendingDiscoveryFacets = components["schemas"]["PendingDiscoveryFacetsResponse"];
type PendingDiscoveryPageQuery = NonNullable<
  operations["pending_discovery_page_api_v1_workspaces__workspace_id__pending_discoveries_page_get"]["parameters"]["query"]
>;
/** The queue filters the page route applies before paging; an absent key narrows nothing. */
export type PendingQueueQuery = {
  [
    Key in Exclude<
      keyof PendingDiscoveryPageQuery,
      "limit" | "ready_offset" | "attention_offset" | "expected_snapshot_sha256" | "snapshot_before"
    >
  ]?: NonNullable<PendingDiscoveryPageQuery[Key]>;
};
export type PendingDiscoveryChanges = components["schemas"]["PendingDiscoveryChangesResponse"];
export type PendingDiscoveryTitleUpdate = components["schemas"]["PendingDiscoveryTitleUpdate"];
export type PendingMarkers = components["schemas"]["PendingMarkersRequest"];
export type ClosingDateEditInput = components["schemas"]["ClosingDateEditInput"];
export type PendingPostingTextAttach = components["schemas"]["PendingPostingTextAttach"];
export type PendingPostingIdentityConfirm = components["schemas"]["PendingPostingIdentityConfirm"];
export type PendingPostingContentAction =
  components["schemas"]["PendingPostingContentActionResponse"];
export type PendingClosureReconcileRequest =
  components["schemas"]["PendingClosureReconcileRequest"];
export type PendingClosureReconcileResponse =
  components["schemas"]["PendingClosureReconcileResponse"];
export type PendingQueueReset = components["schemas"]["PendingQueueResetResponse"];
export type TitleExclusionCollateral = components["schemas"]["TitleExclusionCollateralResponse"];
export type RelevanceReview = components["schemas"]["RelevanceReviewResponse"];
export type RelevanceReviewRejection = components["schemas"]["RelevanceReviewRejectionResponse"];
export type RelevanceDispute = components["schemas"]["RelevanceDisputeResponse"];
export type RelevanceDisputeBody = components["schemas"]["RelevanceDisputeBody"];
export type RelevanceDisputeCleared = components["schemas"]["RelevanceDisputeClearedResponse"];
export type PostingResolutionPreflightRequest =
  components["schemas"]["PostingResolutionPreflightRequest"];
export type PostingRecoveryApprovalManifest =
  components["schemas"]["PostingRecoveryApprovalManifest"];
export type DispositionSelection = components["schemas"]["DispositionSelection"];
export type ExplicitDismissalCategory = components["schemas"]["ExplicitDismissalCategory"];
export type DispositionPreview = components["schemas"]["DispositionPreview"];
export type DispositionApply = components["schemas"]["DispositionApply"];
export type PendingDisposition = components["schemas"]["PendingDispositionResponse"];
export type DismissedDiscovery = components["schemas"]["DismissedDiscoveryResponse"];
export type PendingDuplicateCandidate = components["schemas"]["PendingDuplicateCandidateResponse"];
export type PendingDuplicateSuggestion =
  components["schemas"]["PendingDuplicateSuggestionResponse"];
export type SearchTitleProposal = components["schemas"]["SearchTitleProposalResponse"];
export type SearchArchetypeProposal = components["schemas"]["SearchArchetypeProposalResponse"];
export type SearchClusterProposal = components["schemas"]["SearchClusterProposalResponse"];
export type SearchTitleProposalInbox = components["schemas"]["SearchTitleProposalInboxResponse"];
export type SearchArchetypeProposalInbox =
  components["schemas"]["SearchArchetypeProposalInboxResponse"];
export type SearchClusterProposalInbox =
  components["schemas"]["SearchClusterProposalInboxResponse"];
export type ProposalRejectionReason = components["schemas"]["ProposalRejectionReason"];
export type MasterCvProposal = components["schemas"]["MasterCvProposalResponse"];
export type MasterCvProposalAggregationReport = components["schemas"]["AggregationReport"];
export type RefusalPatternProposal = components["schemas"]["RefusalPatternProposalResponse"];
export type RunCostHistory = components["schemas"]["RunCostHistoryResponse"];
export type ApplicationEvent = components["schemas"]["ApplicationEventResponse"];
export type Task = components["schemas"]["TaskResponse"];
export type TaskBilling = components["schemas"]["TaskBillingResponse"];
export type TaskTypeInfo = components["schemas"]["TaskTypeResponse"];
export type PostingObservationBody = components["schemas"]["PostingObservationBodyResponse"];
export type PostingDiff = components["schemas"]["PostingDiffResponse"];
export type EvidenceSource = components["schemas"]["EvidenceSourceResponse"];
export type FitReport = components["schemas"]["FitReportResponse"];
export type PromptState = components["schemas"]["PromptStateResponse"];
export type PromptDetail = components["schemas"]["PromptDetailResponse"];
export type PromptVersion = components["schemas"]["PromptVersionResponse"];
export type PromptDiff = components["schemas"]["PromptDiffResponse"];
export type Artifact = components["schemas"]["ArtifactResponse"];
export type ArtifactDiff = components["schemas"]["ArtifactDiffResponse"];
export type CitationCheck = components["schemas"]["CitationCheckResponse"];
export type SearchParameterOptions = components["schemas"]["SearchParameterOptions"];
export type JobSearchParameters = components["schemas"]["JobSearchParameters-Output"];
export type SearchLocation = components["schemas"]["Location"];
export type CompensationBand = components["schemas"]["CompensationBand-Output"];
export type SearchParametersState = components["schemas"]["SearchParametersStateResponse"];
export type SearchParametersSaved = components["schemas"]["SearchParametersSavedResponse"];
export type SearchParameterVersion = components["schemas"]["SearchParameterVersionResponse"];
export type GuidedCampaignStagedBasis = components["schemas"]["StagedBasis"];
export type GuidedCampaignReason = components["schemas"]["CampaignReason"];
export type GuidedCampaignPreview = components["schemas"]["GuidedCampaignPreviewResponse"];
export type GuidedCampaignReport = components["schemas"]["GuidedCampaignReportResponse"];
export type Note = components["schemas"]["NoteResponse"];
export type ApplicationOption = components["schemas"]["ApplicationOptionResponse"];
export type Tag = components["schemas"]["TagResponse"];
export type TagAssignment = components["schemas"]["TagAssignmentResponse"];
export type SavedView = components["schemas"]["SavedViewResponse"];
export type SavedViewInput = components["schemas"]["SavedViewBody"];
export type ReviewDecision = components["schemas"]["ReviewDecisionResponse"];
export type TailoringReview = components["schemas"]["TailoringReviewResponse"];

export type Guardrails = Required<components["schemas"]["Guardrails"]>;
export type GuardrailsSaved = components["schemas"]["GuardrailSavedResponse"];
export type GuardrailsState = components["schemas"]["GuardrailStateResponse"];
export type GuardrailVersion = components["schemas"]["GuardrailVersionResponse"];
export type GuardrailPreviewReport = components["schemas"]["GuardrailPreviewReport"];
export type GuardrailPhraseMatch = components["schemas"]["PhraseMatch"];
export type PostingRefusal = components["schemas"]["PostingRefusalResponse"];
export type SourceScanHistory = components["schemas"]["SourceScanHistoryPageResponse"];
export type SourceScan = SourceScanHistory["items"][number];
type SourceScanHistoryQuery = NonNullable<
  operations["source_scan_history_api_v1_workspaces__workspace_id__source_scans_get"]["parameters"]["query"]
>;
export type SourceScanHistoryOptions = {
  offset: number;
  limit: number;
  sourceId?: string;
  status?: "" | NonNullable<SourceScanHistoryQuery["status"]>;
  latestOnly?: boolean;
};
export type SourceScanOutcomePage = components["schemas"]["SourceScanOutcomePageResponse"];
type SourceScanOutcomeQuery =
  operations["source_scan_outcomes_api_v1_workspaces__workspace_id__source_scan_outcomes_get"]["parameters"]["query"];
export type SourceScanOutcomeOptions = {
  taskId: SourceScanOutcomeQuery["task_id"];
  search: string;
  source: string;
  terminalStage: "" | NonNullable<SourceScanOutcomeQuery["terminal_stage"]>;
  reason: "" | NonNullable<SourceScanOutcomeQuery["reason"]>;
  candidateKind: "" | NonNullable<SourceScanOutcomeQuery["candidate_kind"]>;
  sort: NonNullable<SourceScanOutcomeQuery["sort"]>;
  direction: NonNullable<SourceScanOutcomeQuery["direction"]>;
  offset: number;
  limit: number;
};
export type ScreenedOutTitlePage = components["schemas"]["ScreenedOutTitlePageResponse"];
export type ScreenedOutTitle = components["schemas"]["ScreenedOutTitleResponse"];
export type ScreenedOutTitleReview = components["schemas"]["ScreenedOutTitleReviewResponse"];
export type ScreenedOutTitleOptions = {
  needsReview: boolean | null;
  search: string;
  source: string;
  offset: number;
  limit: number;
};
export type Source = components["schemas"]["SourceResponse"];
export type SourceList = components["schemas"]["SourceListResponse"];
export type FoundPostingCreate = components["schemas"]["FoundPostingCreate"];
export type FoundPostingCheck = components["schemas"]["FoundPostingCheck"];
export type FoundPostingCheckResponse = components["schemas"]["FoundPostingCheckResponse"];
export type FoundPostingAddResponse =
  | components["schemas"]["FoundPostingCreatedResponse"]
  | components["schemas"]["FoundPostingRequeuedResponse"];
export type SourceCreate = components["schemas"]["SourceCreate"];
export type SourceEdit = components["schemas"]["SourceEdit"];
export type SourcePreviewRequest = components["schemas"]["SourcePreviewRequest"];
export type SourcePreview = components["schemas"]["SourcePreviewResponse"];
export type TailoringPreflight = components["schemas"]["TailoringPreflightResponse"];
export type EvaluationPreflight = components["schemas"]["EvaluationPreflightResponse"];
export type EvaluationSelection = components["schemas"]["DiscoveryEvaluateBatchPayload"];
