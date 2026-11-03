import type { ArtifactLogicalType } from "../documentNames";
import { requestJson as request, requestText } from "../http";
import type { Artifact, ArtifactDiff, CitationCheck } from "./types";

export const artifactsApi = {
  artifacts: (
    workspaceId: string,
    entityType?: string,
    entityId?: string,
    includeRelated = false,
    signal?: AbortSignal,
  ) => {
    const query = new URLSearchParams({ workspace_id: workspaceId });
    if (entityType && entityId) {
      query.set("entity_type", entityType);
      query.set("entity_id", entityId);
      if (includeRelated) query.set("include_related", "true");
    }
    return request<Artifact[]>(`/artifacts?${query}`, { signal });
  },
  artifactText: (artifactId: string, workspaceId: string, signal?: AbortSignal) =>
    requestText(
      `/artifacts/${encodeURIComponent(artifactId)}/content?workspace_id=${encodeURIComponent(workspaceId)}`,
      signal,
    ),
  artifactUrl: (
    artifactId: string,
    workspaceId: string,
    disposition: "inline" | "attachment" = "inline",
  ) =>
    `/api/v1/artifacts/${encodeURIComponent(artifactId)}/content?workspace_id=${encodeURIComponent(workspaceId)}&disposition=${disposition}`,
  artifactVersions: (artifactId: string, workspaceId: string, signal?: AbortSignal) =>
    request<Artifact[]>(
      `/artifacts/${encodeURIComponent(artifactId)}/versions?workspace_id=${encodeURIComponent(workspaceId)}`,
      { signal },
    ),
  artifactDiff: (artifactId: string, otherId: string, workspaceId: string, signal?: AbortSignal) =>
    request<ArtifactDiff>(
      `/artifacts/${encodeURIComponent(artifactId)}/diff/${encodeURIComponent(otherId)}?workspace_id=${encodeURIComponent(workspaceId)}`,
      { signal },
    ),
  citationCheck: (artifactId: string, body: { workspace_id: string; content: string }) =>
    request<CitationCheck>(`/artifacts/${encodeURIComponent(artifactId)}/citation-check`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  saveArtifact: (
    artifactId: string,
    body: {
      workspace_id: string;
      content: string;
      author_type: "human" | "model";
      override_reason: string;
    },
  ) =>
    request<Artifact>(`/artifacts/${encodeURIComponent(artifactId)}/versions`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  revertArtifact: (
    artifactId: string,
    body: { workspace_id: string; target_artifact_id: string },
  ) =>
    request<Artifact>(`/artifacts/${encodeURIComponent(artifactId)}/revert`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  relabelArtifact: (
    artifactId: string,
    body: { workspace_id: string; logical_type: ArtifactLogicalType },
  ) =>
    request<Artifact>(`/artifacts/${encodeURIComponent(artifactId)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  archiveArtifact: (artifactId: string, workspaceId: string) =>
    request<Artifact>(
      `/artifacts/${encodeURIComponent(artifactId)}?workspace_id=${encodeURIComponent(workspaceId)}`,
      { method: "DELETE" },
    ),
  uploadArtifact: (
    workspaceId: string,
    entityType: string,
    entityId: string,
    logicalType: ArtifactLogicalType,
    file: File,
    submissionRole?: "resume" | "cover_letter" | "other",
  ) => {
    const query = new URLSearchParams({
      workspace_id: workspaceId,
      entity_type: entityType,
      entity_id: entityId,
      logical_type: logicalType,
      filename: file.name,
    });
    if (submissionRole) query.set("submission_role", submissionRole);
    return request<Artifact>(`/artifacts/upload?${query}`, {
      method: "POST",
      headers: { "Content-Type": file.type || "application/octet-stream" },
      body: file,
    });
  },
};
