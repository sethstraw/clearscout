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
    queryFn: () => api.artifacts(workspaceId, entityType, entityId, includeRelated),
    enabled: Boolean(workspaceId),
  });
}

export function useArtifactText(workspaceId: string, artifactId: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.artifactText(workspaceId, artifactId),
    queryFn: () => api.artifactText(artifactId, workspaceId),
    enabled: Boolean(enabled && workspaceId && artifactId),
  });
}

export function useArtifactVersions(workspaceId: string, artifactId: string) {
  return useQuery({
    queryKey: queryKeys.artifactVersions(workspaceId, artifactId),
    queryFn: () => api.artifactVersions(artifactId, workspaceId),
    enabled: Boolean(workspaceId && artifactId),
  });
}

export function useTailoringReview(workspaceId: string, runId: string) {
  return useQuery({
    queryKey: queryKeys.tailoringReview(workspaceId, runId),
    queryFn: () => api.tailoringReview(runId, workspaceId),
    enabled: Boolean(workspaceId && runId),
  });
}
