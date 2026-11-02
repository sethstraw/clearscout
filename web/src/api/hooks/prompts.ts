import { useQuery } from "@tanstack/react-query";

import { api } from "../client";
import { queryKeys } from "./keys";

export function usePrompts(workspaceId: string) {
  return useQuery({
    queryKey: queryKeys.prompts(workspaceId),
    queryFn: () => api.prompts(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

export function usePrompt(workspaceId: string, key: string) {
  return useQuery({
    queryKey: queryKeys.prompt(workspaceId, key),
    queryFn: () => api.prompt(key, workspaceId),
    enabled: Boolean(workspaceId && key),
  });
}

export function usePromptVersions(workspaceId: string, key: string) {
  return useQuery({
    queryKey: queryKeys.promptVersions(workspaceId, key),
    queryFn: () => api.promptVersions(key, workspaceId),
    enabled: Boolean(workspaceId && key),
  });
}
