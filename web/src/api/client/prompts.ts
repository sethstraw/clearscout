import { requestJson as request } from "../http";
import type { PromptState, PromptDetail, PromptVersion, PromptDiff } from "./types";

export const promptsApi = {
  prompts: (workspaceId: string, signal?: AbortSignal) =>
    request<PromptState[]>(`/prompts?workspace_id=${encodeURIComponent(workspaceId)}`, { signal }),
  prompt: (key: string, workspaceId: string, signal?: AbortSignal) =>
    request<PromptDetail>(
      `/prompts/${encodeURIComponent(key)}?workspace_id=${encodeURIComponent(workspaceId)}`,
      { signal },
    ),
  savePrompt: (
    key: string,
    body: { workspace_id: string; content: string; expected_version?: number },
  ) =>
    request<PromptDetail>(`/prompts/${encodeURIComponent(key)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  promptVersions: (key: string, workspaceId: string, signal?: AbortSignal) =>
    request<PromptVersion[]>(
      `/prompts/${encodeURIComponent(key)}/versions?workspace_id=${encodeURIComponent(workspaceId)}`,
      { signal },
    ),
  promptDiff: (
    key: string,
    workspaceId: string,
    fromVersion: number,
    toVersion: number,
    signal?: AbortSignal,
  ) =>
    request<PromptDiff>(
      `/prompts/${encodeURIComponent(key)}/diff?workspace_id=${encodeURIComponent(workspaceId)}&from_version=${fromVersion}&to_version=${toVersion}`,
      { signal },
    ),
  revertPrompt: (key: string, body: { workspace_id: string; version: number }) =>
    request<PromptDetail>(`/prompts/${encodeURIComponent(key)}/revert`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  resetPrompt: (key: string, workspaceId: string) =>
    request<PromptDetail>(`/prompts/${encodeURIComponent(key)}/reset`, {
      method: "POST",
      body: JSON.stringify({ workspace_id: workspaceId }),
    }),
};
