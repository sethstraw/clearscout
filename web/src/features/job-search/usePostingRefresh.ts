import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { useEnqueueTask, useTask } from "../../api/hooks";
import { queryKeys } from "../../api/hooks/keys";

/** Queue and observe the one free publisher refresh for exact pending discoveries. */
export function usePostingRefresh(workspaceId: string) {
  const queryClient = useQueryClient();
  const enqueue = useEnqueueTask(workspaceId);
  const task = useTask(workspaceId, enqueue.data?.id ?? "");
  const status = task.data?.status;
  const terminal = status === "completed" || status === "failed" || status === "cancelled";
  const progress: Record<string, unknown> = task.data?.progress ?? {};

  useEffect(() => {
    if (terminal) {
      void queryClient.invalidateQueries({ queryKey: queryKeys.pendingQueuePrefix(workspaceId) });
    }
  }, [queryClient, terminal, workspaceId]);

  return {
    count: (key: string) => (typeof progress[key] === "number" ? progress[key] : 0),
    error: enqueue.error ?? task.error,
    failureMessage: task.data?.error_message ?? "",
    refresh: (pendingDiscoveryIds: string[]) =>
      enqueue.mutateAsync({
        workspace_id: workspaceId,
        task_type: "discovery.refresh_batch",
        payload: { pending_discovery_ids: pendingDiscoveryIds },
      }),
    running: enqueue.isPending || status === "queued" || status === "running",
    status,
  };
}
