import type { PendingDiscovery, Task } from "../../api/client";
import { isEvaluationDecision } from "../../components/statuses";
import type { EvaluationDecision } from "../../components/statuses";

export type AlreadyEvaluatedOutcome = {
  decision: "already_evaluated";
  pendingDiscoveryId: string;
  jobPostingId: string;
  evaluationId: string;
  existingDecision: EvaluationDecision;
};

export type EvaluationState = {
  completedDuplicate: AlreadyEvaluatedOutcome | null;
  jobPostingId: string | null;
};

function stringField(progress: Task["progress"], key: string): string | null {
  const value = progress[key];
  return typeof value === "string" && value ? value : null;
}

export function evaluationPendingDiscoveryIds(task: Task): readonly string[] {
  if (task.task_type !== "discovery.evaluate" && task.task_type !== "discovery.evaluate_batch") {
    return [];
  }
  return task.pending_discovery_ids;
}

export function alreadyEvaluatedOutcome(task: Task | undefined): AlreadyEvaluatedOutcome | null {
  if (!task || task.task_type !== "discovery.evaluate" || task.status !== "completed") return null;
  const progress = task.progress;
  if (progress.decision !== "already_evaluated") return null;
  const pendingDiscoveryId = stringField(progress, "pending_discovery_id");
  const jobPostingId = stringField(progress, "job_posting_id");
  const evaluationId = stringField(progress, "evaluation_id");
  const existingDecision = progress.existing_decision;
  if (!pendingDiscoveryId || !jobPostingId || !evaluationId || !existingDecision) return null;
  if (!isEvaluationDecision(existingDecision)) return null;
  return {
    decision: "already_evaluated",
    pendingDiscoveryId,
    jobPostingId,
    evaluationId,
    existingDecision,
  };
}

export function evaluationState(
  evaluatedJobId: string | null | undefined,
  task: Task | undefined,
): EvaluationState {
  const completedDuplicate = alreadyEvaluatedOutcome(task);
  return {
    completedDuplicate,
    jobPostingId: evaluatedJobId ?? completedDuplicate?.jobPostingId ?? null,
  };
}

function progressEntries(progress: Task["progress"], key: string): readonly Task["progress"][] {
  const value = progress[key];
  return Array.isArray(value)
    ? value.filter(
        (entry): entry is Task["progress"] => typeof entry === "object" && entry !== null,
      )
    : [];
}

// A batch's per-row results omit the pending discovery id, so a targeted row not
// listed in skipped_rows or failures was the one that got evaluated or reused.
function taskEvaluatedPendingDiscovery(
  pendingDiscoveryId: string,
  task: Task | undefined,
): boolean {
  if (!task || task.status !== "completed") return false;
  if (task.task_type === "discovery.evaluate") {
    return stringField(task.progress, "job_posting_id") !== null;
  }
  if (task.task_type !== "discovery.evaluate_batch") return false;
  if (!evaluationPendingDiscoveryIds(task).includes(pendingDiscoveryId)) return false;
  const unresolvedRows = [
    ...progressEntries(task.progress, "skipped_rows"),
    ...progressEntries(task.progress, "failures"),
  ];
  return !unresolvedRows.some(
    (entry) => stringField(entry, "pending_discovery_id") === pendingDiscoveryId,
  );
}

// The queue and the row read this one answer, so a row finished inside a batch
// is never selectable in one place and counted as evaluated in the other.
export function pendingDiscoveryIsEvaluated(
  discovery: PendingDiscovery,
  task: Task | undefined,
): boolean {
  return (
    evaluationState(discovery.evaluated_job_id, task).jobPostingId !== null ||
    taskEvaluatedPendingDiscovery(discovery.id, task)
  );
}
