import { requestJson as request, requestVoid } from "../http";
import type {
  MasterCvImport,
  MasterCvProposal,
  MasterCvProposalAggregationReport,
  ProposalRejectionReason,
  RefusalPatternProposal,
  MasterCvVersion,
  MasterCvContent,
  MasterCvImpact,
  MasterCvEdit,
  PendingDiscovery,
  PendingDiscoveryChanges,
  PendingDiscoveryPage,
  PendingQueueQuery,
  RequirementGaps,
  RequirementImportance,
  RequirementRuling,
  RequirementRulingBody,
  RequirementRulings,
  SearchArchetypeProposal,
  SearchArchetypeProposalInbox,
  SearchClusterProposal,
  SearchClusterProposalInbox,
  SearchTitleProposal,
  SearchTitleProposalInbox,
  TitleExclusionCollateral,
} from "./types";

function appendQueueFilters(query: URLSearchParams, filters: PendingQueueQuery): void {
  for (const [key, value] of Object.entries(filters)) {
    for (const item of [value].flat()) {
      if (item !== undefined && item !== "" && item !== false) query.append(key, String(item));
    }
  }
}

function rejectRequestInit(
  ruling: "accept" | "reject",
  reason: ProposalRejectionReason | null | undefined,
): RequestInit {
  if (ruling !== "reject" || reason == null) return { method: "POST" };
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason }),
  };
}

export const masterCvApi = {
  importMasterCv: (workspaceId: string, file: File, replace = false) => {
    const query = new URLSearchParams({ filename: file.name, replace: String(replace) });
    return request<MasterCvImport>(
      `/workspaces/${encodeURIComponent(workspaceId)}/master-cv/import?${query}`,
      {
        method: "POST",
        headers: { "Content-Type": file.type || "application/octet-stream" },
        body: file,
      },
    );
  },
  masterCv: (workspaceId: string, versionId = "") => {
    const query = versionId ? `?version_id=${encodeURIComponent(versionId)}` : "";
    return request<MasterCvContent>(
      `/workspaces/${encodeURIComponent(workspaceId)}/master-cv${query}`,
    );
  },
  masterCvVersions: (workspaceId: string) =>
    request<MasterCvVersion[]>(`/workspaces/${encodeURIComponent(workspaceId)}/master-cv/versions`),
  masterCvRequirementGaps: (
    workspaceId: string,
    query: { minRaised?: number; importance?: readonly RequirementImportance[] } = {},
  ) => {
    const params = new URLSearchParams();
    if (query.minRaised !== undefined) params.set("min_raised", String(query.minRaised));
    for (const value of query.importance ?? []) params.append("importance", value);
    const suffix = params.size ? `?${params}` : "";
    return request<RequirementGaps>(
      `/workspaces/${encodeURIComponent(workspaceId)}/master-cv/requirement-gaps${suffix}`,
    );
  },
  listRequirementRulings: (workspaceId: string) =>
    request<RequirementRulings>(
      `/workspaces/${encodeURIComponent(workspaceId)}/requirement-rulings`,
    ),
  setRequirementRuling: (
    workspaceId: string,
    requirementKey: string,
    body: RequirementRulingBody,
  ) =>
    request<RequirementRuling>(
      `/workspaces/${encodeURIComponent(workspaceId)}/requirement-rulings/` +
        `${encodeURIComponent(requirementKey)}`,
      { method: "PUT", body: JSON.stringify(body) },
    ),
  clearRequirementRuling: (workspaceId: string, requirementKey: string) =>
    requestVoid(
      `/workspaces/${encodeURIComponent(workspaceId)}/requirement-rulings/` +
        `${encodeURIComponent(requirementKey)}`,
      { method: "DELETE" },
    ),
  masterCvDiff: (workspaceId: string, fromVersion: string, toVersion: string) => {
    const query = new URLSearchParams({ from_version: fromVersion, to_version: toVersion });
    return request<{ from_version: string; to_version: string; diff: string }>(
      `/workspaces/${encodeURIComponent(workspaceId)}/master-cv/diff?${query}`,
    );
  },

  // Reads only. The impact check writes nothing, so it is safe to call on any
  // draft the user has not decided to keep.
  masterCvImpact: (workspaceId: string, content: string) =>
    request<MasterCvImpact>(`/workspaces/${encodeURIComponent(workspaceId)}/master-cv/impact`, {
      method: "POST",
      body: JSON.stringify({ content }),
    }),
  saveMasterCv: (workspaceId: string, content: string) =>
    request<MasterCvEdit>(`/workspaces/${encodeURIComponent(workspaceId)}/master-cv`, {
      method: "POST",
      body: JSON.stringify({ content }),
    }),
  pendingDiscoveries: (
    workspaceId: string,
    limit = 250,
    closedOnly = false,
    separateReadinessCohorts = false,
  ) =>
    request<PendingDiscovery[]>(
      `/workspaces/${encodeURIComponent(workspaceId)}/pending-discoveries?limit=${limit}` +
        `&closed_only=${closedOnly}` +
        (separateReadinessCohorts ? `&ready_limit=${limit}&attention_limit=${limit}` : ""),
    ),
  pendingDiscoveryPage: (
    workspaceId: string,
    limit: number,
    readyOffset = 0,
    attentionOffset = 0,
    expectedSnapshotSha256 = "",
    snapshotBefore = "",
    filters: PendingQueueQuery = {},
  ) => {
    const query = new URLSearchParams({
      limit: String(limit),
      ready_offset: String(readyOffset),
      attention_offset: String(attentionOffset),
    });
    if (expectedSnapshotSha256) {
      query.set("expected_snapshot_sha256", expectedSnapshotSha256);
    }
    if (snapshotBefore) query.set("snapshot_before", snapshotBefore);
    appendQueueFilters(query, filters);
    return request<PendingDiscoveryPage>(
      `/workspaces/${encodeURIComponent(workspaceId)}/pending-discoveries/page?${query}`,
    );
  },
  pendingDiscoveryChanges: (
    workspaceId: string,
    snapshotBefore: string,
    filters: PendingQueueQuery = {},
  ) => {
    const query = new URLSearchParams({ snapshot_before: snapshotBefore });
    appendQueueFilters(query, filters);
    return request<PendingDiscoveryChanges>(
      `/workspaces/${encodeURIComponent(workspaceId)}/pending-discoveries/changes?${query}`,
    );
  },
  titleExclusionCollateral: (workspaceId: string, phrases: readonly string[]) => {
    const query = new URLSearchParams(phrases.map((phrase) => ["phrase", phrase]));
    return request<TitleExclusionCollateral>(
      `/workspaces/${encodeURIComponent(workspaceId)}/pending-discoveries/` +
        `title-exclusion-collateral?${query.toString()}`,
    );
  },
  searchTitleProposals: (workspaceId: string) =>
    request<SearchTitleProposalInbox>(
      `/workspaces/${encodeURIComponent(workspaceId)}/search-title-proposals`,
    ),
  ruleOnSearchTitle: (
    workspaceId: string,
    proposalId: string,
    ruling: "accept" | "reject",
    reason?: ProposalRejectionReason | null,
  ) =>
    request<SearchTitleProposal>(
      `/workspaces/${encodeURIComponent(workspaceId)}/search-title-proposals/` +
        `${encodeURIComponent(proposalId)}/${ruling}`,
      rejectRequestInit(ruling, reason),
    ),
  searchArchetypeProposals: (workspaceId: string) =>
    request<SearchArchetypeProposalInbox>(
      `/workspaces/${encodeURIComponent(workspaceId)}/search-archetype-proposals`,
    ),
  ruleOnSearchArchetype: (
    workspaceId: string,
    proposalId: string,
    ruling: "accept" | "reject",
    reason?: ProposalRejectionReason | null,
  ) =>
    request<SearchArchetypeProposal>(
      `/workspaces/${encodeURIComponent(workspaceId)}/search-archetype-proposals/` +
        `${encodeURIComponent(proposalId)}/${ruling}`,
      rejectRequestInit(ruling, reason),
    ),
  searchClusterProposals: (workspaceId: string) =>
    request<SearchClusterProposalInbox>(
      `/workspaces/${encodeURIComponent(workspaceId)}/search-cluster-proposals`,
    ),
  ruleOnSearchCluster: (
    workspaceId: string,
    proposalId: string,
    ruling: "accept" | "reject",
    reason?: ProposalRejectionReason | null,
  ) =>
    request<SearchClusterProposal>(
      `/workspaces/${encodeURIComponent(workspaceId)}/search-cluster-proposals/` +
        `${encodeURIComponent(proposalId)}/${ruling}`,
      rejectRequestInit(ruling, reason),
    ),
  masterCvProposals: (workspaceId: string) =>
    request<MasterCvProposal[]>(
      `/workspaces/${encodeURIComponent(workspaceId)}/master-cv-proposals`,
    ),
  refreshMasterCvProposals: (workspaceId: string) =>
    request<MasterCvProposalAggregationReport>(
      `/workspaces/${encodeURIComponent(workspaceId)}/master-cv-proposals/refresh`,
      { method: "POST" },
    ),
  ruleOnMasterCvProposal: (workspaceId: string, proposalId: string, ruling: "accept" | "reject") =>
    request<MasterCvProposal>(
      `/workspaces/${encodeURIComponent(workspaceId)}/master-cv-proposals/` +
        `${encodeURIComponent(proposalId)}/${ruling}`,
      { method: "POST" },
    ),
  refusalPatternProposals: (workspaceId: string) =>
    request<RefusalPatternProposal[]>(
      `/workspaces/${encodeURIComponent(workspaceId)}/refusal-pattern-proposals`,
    ),
  ruleOnRefusalPattern: (workspaceId: string, proposalId: string, ruling: "accept" | "reject") =>
    request<RefusalPatternProposal>(
      `/workspaces/${encodeURIComponent(workspaceId)}/refusal-pattern-proposals/` +
        `${encodeURIComponent(proposalId)}/${ruling}`,
      { method: "POST" },
    ),
};
