import { describe, expect, it } from "vitest";

import type { Task } from "../../api/client";
import {
  alreadyEvaluatedOutcome,
  evaluationPendingDiscoveryIds,
  evaluationState,
} from "./evaluationTaskOutcome";

function task(progress: Record<string, unknown>, overrides: Partial<Task> = {}): Task {
  return {
    attempt_count: 1,
    cancellation_requested_at: null,
    completed_at: "2026-08-26T00:02:00Z",
    created_at: "2026-08-26T00:00:00Z",
    error_category: null,
    error_message: null,
    id: "task-1",
    job_posting_ids: [],
    pending_discovery_ids: ["pending-1"],
    source_ids: [],
    progress,
    started_at: "2026-08-26T00:01:00Z",
    status: "completed",
    task_type: "discovery.evaluate",
    updated_at: "2026-08-26T00:02:00Z",
    workspace_id: "workspace-1",
    ...overrides,
  };
}

const COMPLETE_OUTCOME = {
  decision: "already_evaluated",
  pending_discovery_id: "pending-1",
  job_posting_id: "job-1",
  evaluation_id: "evaluation-1",
  existing_decision: "apply_now",
};

describe("evaluation task outcomes", () => {
  it("combines a durable evaluation with a completed duplicate outcome", () => {
    expect(evaluationState("durable-job", undefined)).toEqual({
      completedDuplicate: null,
      jobPostingId: "durable-job",
    });
    expect(evaluationState(null, task(COMPLETE_OUTCOME))).toEqual({
      completedDuplicate: {
        decision: "already_evaluated",
        pendingDiscoveryId: "pending-1",
        jobPostingId: "job-1",
        evaluationId: "evaluation-1",
        existingDecision: "apply_now",
      },
      jobPostingId: "job-1",
    });
  });

  it.each([
    ["wrong task type", task(COMPLETE_OUTCOME, { task_type: "search.guided_campaign" })],
    ["wrong status", task(COMPLETE_OUTCOME, { status: "running" })],
    ["wrong decision", task({ ...COMPLETE_OUTCOME, decision: "apply_now" })],
    ["missing identity", task({ ...COMPLETE_OUTCOME, evaluation_id: undefined })],
    ["non-string identity", task({ ...COMPLETE_OUTCOME, job_posting_id: 42 })],
    ["unknown existing decision", task({ ...COMPLETE_OUTCOME, existing_decision: "later" })],
  ])("rejects malformed completed progress: %s", (_label, malformed) => {
    expect(alreadyEvaluatedOutcome(malformed)).toBeNull();
    expect(evaluationState(null, malformed).jobPostingId).toBeNull();
  });

  it("reads pending identity only from evaluation tasks", () => {
    expect(evaluationPendingDiscoveryIds(task(COMPLETE_OUTCOME))).toEqual(["pending-1"]);
    expect(
      evaluationPendingDiscoveryIds(
        task(COMPLETE_OUTCOME, {
          task_type: "search.guided_campaign",
          pending_discovery_ids: ["pending-1"],
        }),
      ),
    ).toEqual([]);
  });

  it("names every row of a running evaluate batch, including rows it has not reached", () => {
    const running = task(
      { current: 1, total: 3, pending_discovery_id: "pending-2" },
      {
        task_type: "discovery.evaluate_batch",
        status: "running",
        pending_discovery_ids: ["pending-2", "pending-3", "pending-4"],
      },
    );
    expect(evaluationPendingDiscoveryIds(running)).toEqual(["pending-2", "pending-3", "pending-4"]);
  });
});
