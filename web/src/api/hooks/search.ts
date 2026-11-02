import { type QueryClient, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api } from "../client";
import { ApiError } from "../http";
import type {
  DispositionApply,
  DispositionSelection,
  FoundPostingCreate,
  FoundPostingCheck,
  PendingDiscoveryTitleUpdate,
  PendingMarkers,
  ClosingDateEditInput,
  PendingPostingIdentityConfirm,
  PendingPostingTextAttach,
  PendingClosureReconcileRequest,
  PendingQueueQuery,
  PostingResolutionPreflightRequest,
  ProposalRejectionReason,
  RelevanceDispute,
  RelevanceDisputeBody,
  RelevanceDisputeCleared,
  ScoreWeightSetInput,
  Task,
} from "../client";
import { queryKeys } from "./keys";
import { useWorkspaceMutation } from "./records";
import { useEnqueueTask, useTask, useTasks } from "./tasks";

export function useSearchParameterOptions() {
  return useQuery({
    queryKey: queryKeys.searchParameterOptions,
    queryFn: api.searchParameterOptions,
    staleTime: Infinity,
  });
}

export function useSearchParameters(workspaceId: string) {
  return useQuery({
    queryKey: queryKeys.searchParameters(workspaceId),
    queryFn: () => api.searchParameters(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

export function useGuidedCampaignPreview(
  workspaceId: string,
  maxModelCalls: number | null,
  maxHostedSearches: number | null,
) {
  return useQuery({
    queryKey: queryKeys.guidedCampaignPreview(workspaceId, maxModelCalls, maxHostedSearches),
    queryFn: () => api.guidedCampaignPreview(workspaceId, maxModelCalls, maxHostedSearches),
    enabled: Boolean(workspaceId),
    retry: false,
  });
}

export function useLatestGuidedCampaignReport(workspaceId: string) {
  return useQuery({
    queryKey: queryKeys.latestGuidedCampaignReport(workspaceId),
    queryFn: () => api.latestGuidedCampaignReport(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

export function useAbandonGuidedCampaignAttempt(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ campaignId, position }: { campaignId: string; position: number }) =>
      api.abandonGuidedCampaignAttempt(workspaceId, campaignId, position),
    onSuccess: (report) => {
      // The abandon response is the updated report itself, so cache it
      // directly instead of waiting on a refetch.
      client.setQueryData(queryKeys.latestGuidedCampaignReport(workspaceId), report);
      return client.invalidateQueries({ queryKey: queryKeys.pendingQueuePrefix(workspaceId) });
    },
  });
}

export function useSearchParameterHistory(workspaceId: string) {
  return useQuery({
    queryKey: queryKeys.searchParameterHistory(workspaceId),
    queryFn: () => api.searchParameterHistory(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

export function useSaveSearchParameters(workspaceId: string) {
  return useWorkspaceMutation(workspaceId, api.saveSearchParameters, {
    successMessage: (saved) =>
      saved.unchanged ? undefined : `Saved search parameters as version ${saved.version}.`,
  });
}

export function usePendingDiscoveries(
  workspaceId: string,
  limit = 250,
  closedOnly = false,
  enabled = true,
  separateReadinessCohorts = false,
) {
  return useQuery({
    queryKey: [
      "pending-discoveries",
      workspaceId,
      limit,
      closedOnly,
      separateReadinessCohorts,
    ] as const,
    queryFn: () => api.pendingDiscoveries(workspaceId, limit, closedOnly, separateReadinessCohorts),
    enabled: Boolean(workspaceId) && enabled,
  });
}

export function usePendingDiscoveryPage(
  workspaceId: string,
  limit: number,
  readyOffset = 0,
  attentionOffset = 0,
  expectedSnapshotSha256 = "",
  snapshotBefore = "",
  filters: PendingQueueQuery = {},
) {
  return useQuery({
    queryKey: queryKeys.pendingDiscoveryPage(
      workspaceId,
      limit,
      readyOffset,
      attentionOffset,
      expectedSnapshotSha256,
      snapshotBefore,
      filters,
    ),
    queryFn: () =>
      api.pendingDiscoveryPage(
        workspaceId,
        limit,
        readyOffset,
        attentionOffset,
        expectedSnapshotSha256,
        snapshotBefore,
        filters,
      ),
    enabled: Boolean(workspaceId),
    // A restart reads the first page unpinned and then pinned, so without the
    // held-over page the table unmounts twice between two identical renders.
    placeholderData: (previous) => previous,
    retry: (failureCount, error) =>
      error instanceof ApiError && error.status === 409 ? false : failureCount < 1,
  });
}

const PENDING_DISCOVERY_CHANGES_ACTIVE_INTERVAL = 15_000;
const PENDING_DISCOVERY_CHANGES_IDLE_INTERVAL = 60_000;

export function usePendingDiscoveryChanges(
  workspaceId: string,
  snapshotBefore: string,
  filters: PendingQueueQuery = {},
) {
  return useQuery({
    queryKey: queryKeys.pendingDiscoveryChanges(workspaceId, snapshotBefore, filters),
    queryFn: () => api.pendingDiscoveryChanges(workspaceId, snapshotBefore, filters),
    enabled: Boolean(workspaceId) && Boolean(snapshotBefore),
    refetchInterval: (query) => {
      const data = query.state.data;
      const active =
        data !== undefined &&
        (data.follow_up.active_sources > 0 ||
          data.newer_visible.ready > 0 ||
          data.newer_visible.attention > 0);
      return active
        ? PENDING_DISCOVERY_CHANGES_ACTIVE_INTERVAL
        : PENDING_DISCOVERY_CHANGES_IDLE_INTERVAL;
    },
  });
}

/** Count the queue rows each draft title-skip phrase would also hide. */
export function useTitleExclusionCollateral(workspaceId: string, phrases: readonly string[]) {
  return useQuery({
    queryKey: queryKeys.titleExclusionCollateral(workspaceId, phrases),
    queryFn: () => api.titleExclusionCollateral(workspaceId, phrases),
    enabled: Boolean(workspaceId) && phrases.length > 0,
  });
}

export function usePostingRecoveryPreflight(workspaceId: string) {
  return useMutation({
    mutationFn: (body: PostingResolutionPreflightRequest) =>
      api.postingRecoveryPreflight(workspaceId, body),
  });
}

export function useDismissedPendingDiscoveries(workspaceId: string) {
  return useQuery({
    queryKey: queryKeys.dismissedPendingDiscoveries(workspaceId),
    queryFn: () => api.dismissedPendingDiscoveries(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

export function usePendingDuplicateCandidates(workspaceId: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.pendingDuplicateCandidates(workspaceId),
    queryFn: () => api.pendingDuplicateCandidates(workspaceId),
    enabled: Boolean(workspaceId) && enabled,
  });
}

export function usePendingDispositionHistory(workspaceId: string, pendingId: string) {
  return useQuery({
    queryKey: queryKeys.pendingDispositionHistory(workspaceId, pendingId),
    queryFn: () => api.pendingDispositionHistory(workspaceId, pendingId),
    enabled: Boolean(workspaceId && pendingId),
  });
}

export function usePreviewPendingDisposition(workspaceId: string) {
  return useMutation({
    mutationFn: (body: DispositionSelection) => api.previewPendingDisposition(workspaceId, body),
  });
}

export function useApplyPendingDisposition(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: DispositionApply) => api.applyPendingDisposition(workspaceId, body),
    onSuccess: () =>
      Promise.all([
        client.invalidateQueries({ queryKey: queryKeys.pendingQueuePrefix(workspaceId) }),
        client.invalidateQueries({ queryKey: queryKeys.sourceScansPrefix(workspaceId) }),
        client.invalidateQueries({
          queryKey: queryKeys.dismissedPendingDiscoveries(workspaceId),
        }),
        client.invalidateQueries({
          queryKey: queryKeys.pendingDuplicateCandidates(workspaceId),
        }),
      ]),
  });
}

export function useAddFoundPosting(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: FoundPostingCreate) => api.addFoundPosting(workspaceId, body),
    // Scan history counts screened-out listings not yet in Pending.
    onSuccess: () =>
      Promise.all([
        client.invalidateQueries({ queryKey: queryKeys.pendingQueuePrefix(workspaceId) }),
        client.invalidateQueries({ queryKey: queryKeys.sourceScansPrefix(workspaceId) }),
      ]),
  });
}

export function useCheckFoundPosting(workspaceId: string) {
  return useMutation({
    mutationFn: (body: FoundPostingCheck) => api.checkFoundPosting(workspaceId, body),
  });
}

/** Every view a whole-queue change moves rows between. */
function invalidatePendingQueueViews(client: QueryClient, workspaceId: string) {
  return [
    queryKeys.pendingQueuePrefix(workspaceId),
    queryKeys.dismissedPendingDiscoveries(workspaceId),
    queryKeys.pendingDuplicateCandidates(workspaceId),
    queryKeys.pendingDispositionHistoryPrefix(workspaceId),
  ].map((queryKey) => client.invalidateQueries({ queryKey }));
}

export function useResetPendingQueue(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.resetPendingQueue(workspaceId),
    onSuccess: () =>
      Promise.all([
        ...invalidatePendingQueueViews(client, workspaceId),
        client.invalidateQueries({ queryKey: queryKeys.sourceScansPrefix(workspaceId) }),
      ]),
  });
}

const RESCREEN_TASK = "discovery.rescreen_queue";

/**
 * Queue the free whole-queue re-screen and follow the latest one, whoever started it,
 * so a second run cannot be queued while one is still in flight.
 */
export function usePendingQueueRescreen(workspaceId: string) {
  const client = useQueryClient();
  const enqueue = useEnqueueTask(workspaceId);
  const tasks = useTasks(workspaceId);
  const latest = (tasks.data ?? [])
    .filter((item) => item.task_type === RESCREEN_TASK)
    .reduce<Task | undefined>(
      (newest, item) => (newest && newest.created_at >= item.created_at ? newest : item),
      undefined,
    );
  const task = useTask(workspaceId, enqueue.data?.id ?? latest?.id ?? "").data;
  const active = task?.status === "queued" || task?.status === "running";
  const [followed, setFollowed] = useState<ReadonlySet<string>>(() => new Set());
  if (task && (active || task.id === enqueue.data?.id) && !followed.has(task.id)) {
    setFollowed(new Set(followed).add(task.id));
  }
  // Only a run this view followed to its end reports an outcome.
  const finished = task && !active && followed.has(task.id) ? task : undefined;
  const finishedId = finished?.id;

  useEffect(() => {
    if (finishedId) void Promise.all(invalidatePendingQueueViews(client, workspaceId));
  }, [client, finishedId, workspaceId]);

  return {
    error: enqueue.error,
    running: enqueue.isPending || active,
    start: () =>
      enqueue.mutateAsync({ workspace_id: workspaceId, task_type: RESCREEN_TASK, payload: {} }),
    task,
    finished,
  };
}

export function useCorrectPendingDiscoveryTitle(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ pendingId, body }: { pendingId: string; body: PendingDiscoveryTitleUpdate }) =>
      api.correctPendingDiscoveryTitle(workspaceId, pendingId, body),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: queryKeys.pendingQueuePrefix(workspaceId) }),
  });
}

export function useTrackPendingDiscovery(workspaceId: string) {
  return useWorkspaceMutation(
    workspaceId,
    (pendingId: string) => api.trackPendingDiscovery(workspaceId, pendingId),
    { successMessage: "Job added to tracked postings." },
  );
}

export function useSetPendingClosingDate(workspaceId: string) {
  return useWorkspaceMutation(
    workspaceId,
    ({ pendingId, body }: { pendingId: string; body: ClosingDateEditInput }) =>
      api.setPendingClosingDate(workspaceId, pendingId, body),
    { successMessage: "Closing date saved." },
  );
}

/** Set a discovery family's favorite and keep markers, which move it in the queue order. */
export function useSetPendingMarkers(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    // One scope runs marker saves in order, so a note save lands before the next toggle.
    scope: { id: `pending-markers-${workspaceId}` },
    mutationFn: ({ pendingId, body }: { pendingId: string; body: PendingMarkers }) =>
      api.setPendingMarkers(workspaceId, pendingId, body),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: queryKeys.pendingQueuePrefix(workspaceId) }),
  });
}

/** Set aside or restore the model's relevance verdict on one discovery. */
export function useRelevanceDispute(workspaceId: string) {
  const client = useQueryClient();
  return useMutation<
    RelevanceDispute | RelevanceDisputeCleared,
    Error,
    { pendingId: string; body: RelevanceDisputeBody | null }
  >({
    mutationFn: ({ pendingId, body }) =>
      body
        ? api.disputeRelevanceVerdict(workspaceId, pendingId, body)
        : api.clearRelevanceDispute(workspaceId, pendingId),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: queryKeys.pendingQueuePrefix(workspaceId) }),
  });
}

export function useReconcileSavedClosures(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: PendingClosureReconcileRequest) =>
      api.reconcileSavedClosures(workspaceId, body),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: queryKeys.pendingQueuePrefix(workspaceId) }),
  });
}

export function useAttachPendingPostingText(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ pendingId, body }: { pendingId: string; body: PendingPostingTextAttach }) =>
      api.attachPendingPostingText(workspaceId, pendingId, body),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: queryKeys.pendingQueuePrefix(workspaceId) }),
  });
}

export function useConfirmPendingPostingIdentity(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ pendingId, body }: { pendingId: string; body: PendingPostingIdentityConfirm }) =>
      api.confirmPendingPostingIdentity(workspaceId, pendingId, body),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: queryKeys.pendingQueuePrefix(workspaceId) }),
  });
}

export function useSearchTitleProposals(workspaceId: string) {
  return useQuery({
    queryKey: ["search-title-proposals", workspaceId] as const,
    queryFn: () => api.searchTitleProposals(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

export function useRuleOnSearchTitle(workspaceId: string) {
  return useWorkspaceMutation(
    workspaceId,
    ({
      proposalId,
      ruling,
      reason,
    }: {
      proposalId: string;
      ruling: "accept" | "reject";
      reason?: ProposalRejectionReason | null;
    }) => api.ruleOnSearchTitle(workspaceId, proposalId, ruling, reason),
    {
      successMessage: (ruled) =>
        ruled.state === "accepted"
          ? `Searches may now look under ${ruled.title}.`
          : `Searches will not look under ${ruled.title}.`,
    },
  );
}

export function useSearchArchetypeProposals(workspaceId: string) {
  return useQuery({
    queryKey: ["search-archetype-proposals", workspaceId] as const,
    queryFn: () => api.searchArchetypeProposals(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

export function useRuleOnSearchArchetype(workspaceId: string) {
  return useWorkspaceMutation(
    workspaceId,
    ({
      proposalId,
      ruling,
      reason,
    }: {
      proposalId: string;
      ruling: "accept" | "reject";
      reason?: ProposalRejectionReason | null;
    }) => api.ruleOnSearchArchetype(workspaceId, proposalId, ruling, reason),
    {
      successMessage: (ruled) =>
        ruled.state === "accepted"
          ? `${ruled.name} is approved as context for future searches.`
          : `Searches will not look at ${ruled.name}.`,
    },
  );
}

export function useSearchClusterProposals(workspaceId: string) {
  return useQuery({
    queryKey: ["search-cluster-proposals", workspaceId] as const,
    queryFn: () => api.searchClusterProposals(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

export function useRuleOnSearchCluster(workspaceId: string) {
  return useWorkspaceMutation(
    workspaceId,
    ({
      proposalId,
      ruling,
      reason,
    }: {
      proposalId: string;
      ruling: "accept" | "reject";
      reason?: ProposalRejectionReason | null;
    }) => api.ruleOnSearchCluster(workspaceId, proposalId, ruling, reason),
    {
      successMessage: (ruled) =>
        ruled.state === "accepted"
          ? `${ruled.name} is added to your ${
              ruled.kind === "capability" ? "capabilities" : "domains"
            } list.`
          : `Searches will not use ${ruled.name}.`,
    },
  );
}

export function useScoreWeightSets(workspaceId: string) {
  return useQuery({
    queryKey: ["score-weight-sets", workspaceId] as const,
    queryFn: () => api.scoreWeightSets(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

/** Append a scoring policy; `searchPolicy` is the name the saved search parameters choose. */
export function useCreateScoreWeightSet(workspaceId: string, searchPolicy = "") {
  return useWorkspaceMutation(
    workspaceId,
    (body: ScoreWeightSetInput) => api.createScoreWeightSet(workspaceId, body),
    {
      successMessage: (created) =>
        searchPolicy && searchPolicy !== created.name
          ? `Saved ${created.name} v${created.version}. It is not in force: this search names ${searchPolicy}. Choose it under Policy for your search to use it.`
          : `Saved ${created.name} v${created.version}. It applies to new evaluations only.`,
    },
  );
}
