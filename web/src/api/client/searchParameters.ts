import { requestJson as request } from "../http";
import type {
  SearchParameterOptions,
  JobSearchParameters,
  SearchParametersState,
  SearchParametersSaved,
  SearchParameterVersion,
  GuidedCampaignPreview,
  GuidedCampaignReport,
} from "./types";

export const searchParametersApi = {
  guidedCampaignPreview: (
    workspaceId: string,
    maxModelCalls: number | null,
    maxHostedSearches: number | null,
  ) => {
    const query = new URLSearchParams({ workspace_id: workspaceId });
    if (maxModelCalls !== null) query.set("max_model_calls", String(maxModelCalls));
    if (maxHostedSearches !== null) query.set("max_hosted_searches", String(maxHostedSearches));
    return request<GuidedCampaignPreview>(`/guided-campaign/preview?${query.toString()}`);
  },
  latestGuidedCampaignReport: (workspaceId: string) =>
    request<GuidedCampaignReport | null>(
      `/guided-campaigns/latest?workspace_id=${encodeURIComponent(workspaceId)}`,
    ),
  abandonGuidedCampaignAttempt: (workspaceId: string, campaignId: string, position: number) =>
    request<GuidedCampaignReport>(
      `/guided-campaigns/${encodeURIComponent(campaignId)}/attempts/${position}/abandon`,
      { method: "POST", body: JSON.stringify({ workspace_id: workspaceId }) },
    ),
  searchParameterOptions: () => request<SearchParameterOptions>("/job-search-parameters/options"),
  searchParameters: (workspaceId: string) =>
    request<SearchParametersState>(
      `/job-search-parameters?workspace_id=${encodeURIComponent(workspaceId)}`,
    ),
  saveSearchParameters: (body: {
    workspace_id: string;
    parameters: JobSearchParameters;
    expected_version: number | null;
  }) =>
    request<SearchParametersSaved>("/job-search-parameters", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  searchParameterHistory: (workspaceId: string) =>
    request<SearchParameterVersion[]>(
      `/job-search-parameters/history?workspace_id=${encodeURIComponent(workspaceId)}`,
    ),
};
