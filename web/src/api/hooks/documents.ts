import { useQuery } from "@tanstack/react-query";

import { api } from "../client";
import { queryKeys } from "./keys";

export function useArtifacts(
  workspaceId: string,
  entityType = "",
  entityId = "",
  includeRelated = false,
) {
  return useQuery({
    queryKey: queryKeys.artifacts(workspaceId, entityType, entityId, includeRelated),
    queryFn: ({ signal }) =>
      api.artifacts(workspaceId, entityType, entityId, includeRelated, signal),
    enabled: Boolean(workspaceId),
  });
}

export function useArtifactText(workspaceId: string, artifactId: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.artifactText(workspaceId, artifactId),
    queryFn: ({ signal }) => api.artifactText(artifactId, workspaceId, signal),
    enabled: Boolean(enabled && workspaceId && artifactId),
  });
}

export function useArtifactVersions(workspaceId: string, artifactId: string) {
  return useQuery({
    queryKey: queryKeys.artifactVersions(workspaceId, artifactId),
    queryFn: ({ signal }) => api.artifactVersions(artifactId, workspaceId, signal),
    enabled: Boolean(workspaceId && artifactId),
  });
}

export function useTailoringReview(workspaceId: string, runId: string) {
  return useQuery({
    queryKey: queryKeys.tailoringReview(workspaceId, runId),
    queryFn: ({ signal }) => api.tailoringReview(runId, workspaceId, signal),
    enabled: Boolean(workspaceId && runId),
  });
}
