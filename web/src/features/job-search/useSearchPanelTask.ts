import { useEffect, useState } from "react";

import { taskBillingSummary, useTask, useTaskBilling } from "../../api/hooks";
import type { Task } from "../../api/client";

/** How long a run may go without an update before the panel says it is slow. */
const SLOW_AFTER_MS = 35_000;

/** Both panels say this, so the wording cannot drift from the threshold above. */
export const SLOW_RUN_NOTICE =
  `No new progress has been recorded for ${SLOW_AFTER_MS / 1000} seconds. ` +
  "The worker may still be waiting on a bounded site response.";

export type SearchPanelTask = {
  task: ReturnType<typeof useTask>;
  queued: boolean;
  running: boolean;
  failedTask: Task | null;
  terminal: boolean;
  billing: ReturnType<typeof useTaskBilling>;
  taskCost: string;
};

/** Watch the one search task a panel owns and derive what that panel renders. */
export function useSearchPanelTask(workspaceId: string, taskId: string): SearchPanelTask {
  const task = useTask(workspaceId, taskId);
  const status = task.data?.status;
  const terminal = status === "completed" || status === "failed" || status === "cancelled";
  const billing = useTaskBilling(workspaceId, taskId, terminal);
  return {
    task,
    queued: status === "queued",
    running: status === "running",
    failedTask: status === "failed" && task.data ? task.data : null,
    terminal,
    billing,
    taskCost: taskBillingSummary(billing.data, billing.isError),
  };
}

/** Report a run as slow once it has gone quiet, measured from its last update. */
export function useSlowNotice(updatedAt: string): boolean {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    const updated = Date.parse(updatedAt);
    const elapsed = Number.isNaN(updated) ? 0 : Math.max(0, Date.now() - updated);
    const timeout = window.setTimeout(() => setSlow(true), Math.max(0, SLOW_AFTER_MS - elapsed));
    return () => window.clearTimeout(timeout);
  }, [updatedAt]);
  return slow;
}
