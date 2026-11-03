import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import type { PostingRecoveryApprovalManifest } from "../../api/client";
import {
  useEnqueueTask,
  usePostingRecoveryPreflight,
  useTask,
  useTaskCosts,
} from "../../api/hooks";
import { ErrorNotice } from "../../components/ErrorNotice";
import { PaidAction } from "../../components/PaidAction";
import { queryKeys } from "../../api/hooks/keys";

const MAX_ASSISTED_POSTING_RECOVERY_ITEMS = 20;

/** Search for a posting the free refresh cannot read, under an approved estimate. */
export function AssistedRecoveryAction({
  itemLabel,
  pendingDiscoveryIds,
  workspaceId,
}: {
  itemLabel?: string;
  pendingDiscoveryIds: string[];
  workspaceId: string;
}) {
  const queryClient = useQueryClient();
  const assistedPreflight = usePostingRecoveryPreflight(workspaceId);
  const assistedEnqueue = useEnqueueTask(workspaceId);
  const costs = useTaskCosts(workspaceId);
  const [approvalState, setApprovalState] = useState<{
    selectionKey: string;
    manifest: PostingRecoveryApprovalManifest;
  } | null>(null);
  const assistedTask = useTask(workspaceId, assistedEnqueue.data?.id ?? "");
  const assistedStatus = assistedTask.data?.status;
  const assistedTerminal =
    assistedStatus === "completed" || assistedStatus === "failed" || assistedStatus === "cancelled";
  const assistedTaskPending =
    Boolean(assistedEnqueue.data?.id) && assistedTask.data === undefined && !assistedTask.isError;
  useEffect(() => {
    if (assistedTerminal) {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.pendingQueuePrefix(workspaceId),
      });
    }
  }, [assistedTerminal, queryClient, workspaceId]);
  const running =
    assistedEnqueue.isPending ||
    assistedTaskPending ||
    assistedStatus === "queued" ||
    assistedStatus === "running";
  const assistedAllowed =
    pendingDiscoveryIds.length > 0 &&
    pendingDiscoveryIds.length <= MAX_ASSISTED_POSTING_RECOVERY_ITEMS;
  const selectionKey = pendingDiscoveryIds.join("\u0000");
  const approval = approvalState?.selectionKey === selectionKey ? approvalState.manifest : null;

  const estimateAssisted = async () => {
    try {
      setApprovalState({
        selectionKey,
        manifest: await assistedPreflight.mutateAsync({
          pending_discovery_ids: pendingDiscoveryIds,
        }),
      });
    } catch {
      // The mutation exposes the preflight error beside the controls.
    }
  };

  const recoverAssisted = () => {
    if (!approval) return;
    assistedEnqueue.mutate({
      workspace_id: workspaceId,
      task_type: "posting.recover_batch",
      payload: { approval },
    });
  };

  return (
    <div className="posting-resolution-action">
      {!approval ? (
        // The estimate reads the preflight and enqueues nothing, so it carries
        // neither the paid mark nor a price.
        <span className="posting-resolution-action__estimate">
          <button
            aria-label={itemLabel ? `Estimate assisted recovery for ${itemLabel}` : undefined}
            className="button button--quiet"
            disabled={!assistedAllowed || running || assistedPreflight.isPending}
            onClick={() => void estimateAssisted()}
            type="button"
          >
            {assistedPreflight.isPending
              ? "Estimating assisted recovery..."
              : `Estimate assisted recovery (${pendingDiscoveryIds.length})`}
          </button>
        </span>
      ) : (
        <PaidAction
          confirmation={{
            title: `Use assisted recovery for ${approval.pending_discovery_ids.length} ${approval.pending_discovery_ids.length === 1 ? "posting" : "postings"}?`,
            description: `About $${(approval.estimate_cents / 100).toFixed(2)} for ${approval.pending_discovery_ids.length} ${approval.pending_discovery_ids.length === 1 ? "posting" : "postings"}. This approval expires in 15 minutes; changing the selection or your settings voids it.`,
            confirmLabel: "Run assisted recovery",
          }}
          costs={costs}
          disabled={running || !costs.loaded}
          label="Run assisted recovery"
          onAct={recoverAssisted}
          taskType="posting.recover_batch"
        />
      )}
      {assistedStatus === "completed" && (
        <span className="field-note" role="status">
          Assisted recovery finished. Each row shows its outcome.
        </span>
      )}
      {assistedStatus === "failed" && (
        <span className="field-error" role="alert">
          Assisted recovery failed:{" "}
          {assistedTask.data?.error_message || "No failure detail was returned."}
        </span>
      )}
      {assistedStatus === "cancelled" && (
        <span className="field-note" role="status">
          Assisted recovery was cancelled. No unstarted posting was searched.
        </span>
      )}
      {(assistedPreflight.isError || assistedEnqueue.isError || assistedTask.isError) && (
        <span className="field-error" role="alert">
          Could not run assisted recovery:{" "}
          {assistedPreflight.error?.message ??
            (assistedEnqueue.error ? (
              <ErrorNotice error={assistedEnqueue.error} />
            ) : (
              assistedTask.error?.message
            ))}
        </span>
      )}
    </div>
  );
}
