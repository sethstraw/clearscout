import { QueryClient } from "@tanstack/react-query";
import type { InvalidateQueryFilters } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { createElement } from "react";
import type { PropsWithChildren } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { DispositionApply } from "./client";
import type { PendingMarkers, RunCostHistory, TaskTypeInfo } from "./client/types";
import {
  useAppendApplicationEvent,
  useApplyPendingDisposition,
  useArchiveJob,
  useContinueSourceScan,
  useEnqueueTask,
  useImportMasterCv,
  useReviewEvaluationCurrentness,
  useResetPendingQueue,
  useSetPendingMarkers,
  useTaskCosts,
  useUnarchiveJob,
} from "./hooks";
import { queryKeys } from "./hooks/keys";
import { AppProviders } from "../providers";
import { server } from "../test/msw/server";

const workspaceId = "workspace-hooks";

/** Record the query filters a mutation invalidates. */
function recordInvalidations(): InvalidateQueryFilters[] {
  const filters: InvalidateQueryFilters[] = [];
  vi.spyOn(QueryClient.prototype, "invalidateQueries").mockImplementation(
    (next?: InvalidateQueryFilters) => {
      filters.push(next ?? {});
      return Promise.resolve();
    },
  );
  return filters;
}

function expectWorkspaceInvalidation(filters: InvalidateQueryFilters[], queryKey: unknown[]) {
  expect(filters).toHaveLength(1);
  const predicate = filters[0]?.predicate;
  expect(predicate).toBeTypeOf("function");
  const query = (key: unknown[]) =>
    ({ queryKey: key }) as unknown as Parameters<NonNullable<typeof predicate>>[0];
  expect(predicate?.(query(queryKey))).toBe(true);
  expect(predicate?.(query([queryKey[0], "another-workspace"]))).toBe(false);
  expect(predicate?.(query(["task-billing", workspaceId, "task-1"]))).toBe(false);
}

/** Accept every mutation: no hook here reads the body, only its invalidations. */
function acceptWrites() {
  server.use(http.post("*/api/v1/*", () => HttpResponse.json({})));
}

function wrapper({ children }: PropsWithChildren) {
  return createElement(AppProviders, null, children);
}

/** Run one mutation under the application's providers and await success. */
async function fire<T>(useMutationHook: () => T, mutate: (result: T) => void) {
  const { result } = renderHook(useMutationHook, { wrapper });
  mutate(result.current);
  await waitFor(() => expect((result.current as { isSuccess: boolean }).isSuccess).toBe(true));
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("mutation invalidations", () => {
  it("appending an application event refreshes the application pipeline", async () => {
    acceptWrites();
    const filters = recordInvalidations();

    await fire(
      () => useAppendApplicationEvent(workspaceId, "job-1"),
      (mutation) =>
        mutation.mutate({
          status: "applied",
          occurred_at: "2026-08-08T12:00:00Z",
          notes: "",
        }),
    );

    expectWorkspaceInvalidation(filters, ["application-pipeline", workspaceId]);
  });

  it("importing a master CV refreshes the CV and its version list", async () => {
    acceptWrites();
    const filters = recordInvalidations();

    await fire(
      () => useImportMasterCv(workspaceId),
      (mutation) =>
        mutation.mutate({
          file: new File(["# CV\n"], "cv.md", { type: "text/markdown" }),
          confirmReread: false,
        }),
    );

    expectWorkspaceInvalidation(filters, ["master-cv-versions", workspaceId]);
  });

  it("archiving a job refreshes the workspace summary", async () => {
    acceptWrites();
    const filters = recordInvalidations();

    await fire(
      () => useArchiveJob(workspaceId),
      (mutation) => mutation.mutate({ jobId: "job-1", workspaceId }),
    );

    expectWorkspaceInvalidation(filters, ["workspace-summary", workspaceId]);
  });

  it("unarchiving a job refreshes the workspace summary too", async () => {
    acceptWrites();
    const filters = recordInvalidations();

    await fire(
      () => useUnarchiveJob(workspaceId),
      (mutation) => mutation.mutate({ jobId: "job-1", workspaceId }),
    );

    expectWorkspaceInvalidation(filters, ["workspace-summary", workspaceId]);
  });

  it("retaining a reviewed evaluation refreshes both its detail and the full ledger", async () => {
    acceptWrites();
    const filters = recordInvalidations();

    await fire(
      () => useReviewEvaluationCurrentness(workspaceId),
      (mutation) => mutation.mutate({ jobId: "job-1", evaluationId: "evaluation-1" }),
    );

    expectWorkspaceInvalidation(filters, ["all-jobs", workspaceId]);
    expectWorkspaceInvalidation(filters, ["job", workspaceId, "job-1"]);
  });

  it("enqueueing a task refreshes spend", async () => {
    acceptWrites();
    const filters = recordInvalidations();

    await fire(
      () => useEnqueueTask(workspaceId),
      (mutation) =>
        mutation.mutate({
          workspace_id: workspaceId,
          task_type: "discovery.evaluate",
          payload: {},
        }),
    );

    expectWorkspaceInvalidation(filters, ["workspace-spend", workspaceId]);
  });

  it("continuing a source scan invalidates every page of the source-scan history", async () => {
    acceptWrites();
    const filters = recordInvalidations();

    await fire(
      () => useContinueSourceScan(workspaceId),
      (mutation) => mutation.mutate("scan-1"),
    );

    expect(filters).toHaveLength(1);
    // The invalidation key is the factory prefix, so renaming the key cannot
    // silently break it, and the prefix must lead the concrete page key.
    expect(filters[0]?.queryKey).toEqual(queryKeys.sourceScansPrefix(workspaceId));
    expect(queryKeys.sourceScans(workspaceId, { offset: 0, limit: 50 }).slice(0, 2)).toEqual(
      queryKeys.sourceScansPrefix(workspaceId),
    );
  });

  it("resetting pending discoveries invalidates every affected queue and disposition view", async () => {
    acceptWrites();
    const filters = recordInvalidations();

    await fire(
      () => useResetPendingQueue(workspaceId),
      (mutation) => mutation.mutate(),
    );

    expect(filters.map((item) => item.queryKey)).toEqual([
      queryKeys.pendingQueuePrefix(workspaceId),
      queryKeys.dismissedPendingDiscoveries(workspaceId),
      queryKeys.pendingDuplicateCandidates(workspaceId),
      queryKeys.pendingDispositionHistoryPrefix(workspaceId),
      queryKeys.sourceScansPrefix(workspaceId),
    ]);
  });

  it.each([
    {
      action: "dismiss",
      category: "out_of_scope_title",
      reason: "",
      dismiss_marked: false,
      pending_discovery_ids: ["pending-1"],
      selection_mode: "explicit",
      snapshot_sha256: "a".repeat(64),
    },
    {
      action: "restore",
      category: "out_of_scope_title",
      reason: "Restored after review.",
      pending_discovery_ids: ["pending-1"],
      selection_mode: "explicit",
      snapshot_sha256: "a".repeat(64),
    },
  ] satisfies DispositionApply[])(
    "applying a $action refreshes the source-scan history's screened-out counts",
    async (body) => {
      acceptWrites();
      const filters = recordInvalidations();

      await fire(
        () => useApplyPendingDisposition(workspaceId),
        (mutation) => mutation.mutate(body),
      );

      expect(filters.map((filter) => filter.queryKey)).toContainEqual(
        queryKeys.sourceScansPrefix(workspaceId),
      );
    },
  );
});

describe("marker saves", () => {
  it("send the next save only after the previous one answers", async () => {
    let firstReleased = false;
    let releaseFirst = () => {};
    const firstHeld = new Promise<void>((resolve) => {
      releaseFirst = () => {
        firstReleased = true;
        resolve();
      };
    });
    const arrivals: { pendingId: string; afterFirst: boolean }[] = [];
    server.use(
      http.put(
        "*/api/v1/workspaces/:workspaceId/pending-discoveries/:pendingId/markers",
        async ({ params }) => {
          arrivals.push({ pendingId: String(params.pendingId), afterFirst: firstReleased });
          if (params.pendingId === "pending-1") await firstHeld;
          return HttpResponse.json({});
        },
      ),
    );
    recordInvalidations();
    const body = {
      favorite: false,
      kept: true,
      keep_note: "Advisory work, not sales.",
    } satisfies PendingMarkers;

    const { result } = renderHook(() => useSetPendingMarkers(workspaceId), { wrapper });
    result.current.mutate({ pendingId: "pending-1", body });
    result.current.mutate({ pendingId: "pending-2", body });
    await waitFor(() => expect(arrivals.map((item) => item.pendingId)).toContain("pending-1"));
    releaseFirst();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(arrivals).toEqual([
      { pendingId: "pending-1", afterFirst: false },
      { pendingId: "pending-2", afterFirst: true },
    ]);
  });
});

const CAMPAIGN_SPEND = [
  {
    task_type: "search.guided_campaign",
    currency: "USD",
    unit: "attempt",
    measured_runs: 3,
    unpriced_runs: 0,
    replayed_runs: 0,
    per_unit_low: "0.05",
    per_unit_median: "0.12",
    per_unit_high: "0.20",
  },
] satisfies RunCostHistory[];

const PRICED_TASK_TYPES = [
  {
    task_type: "search.guided_campaign",
    paid: true,
    cost: "Hosted-search prices vary.",
  },
  { task_type: "posting.refresh", paid: false, cost: "No provider charge." },
  {
    task_type: "posting.refresh_with_reextraction",
    paid: true,
    cost: "One extraction-tier call per invocation.",
  },
  {
    task_type: "posting.refresh_with_rematch",
    paid: true,
    cost: "One match-stage call per invocation.",
  },
] satisfies TaskTypeInfo[];

describe("task costs", () => {
  it("shows measured history and a scaled batch total", async () => {
    server.use(
      http.get("*/api/v1/workspaces/:workspaceId/spend-history", () =>
        HttpResponse.json(CAMPAIGN_SPEND),
      ),
      http.get("*/api/v1/tasks/types", () => HttpResponse.json(PRICED_TASK_TYPES)),
    );

    const { result } = renderHook(() => useTaskCosts(workspaceId), { wrapper });

    expect(result.current.loaded).toBe(false);
    expect(result.current.isPaid("search.guided_campaign")).toBe(false);

    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.isPaid("search.guided_campaign")).toBe(true);
    expect(result.current.cost("search.guided_campaign")).toBe("Hosted-search prices vary.");
    expect(result.current.summary("search.guided_campaign")).toBe("about $0.12");
    expect(result.current.summary("search.guided_campaign", 3)).toBe("about $0.36");
    expect(result.current.summary("search.vocabulary")).toBe("");
    expect(result.current.summary("posting.refresh")).toBe("No provider charge.");
    expect(result.current.inline("search.guided_campaign")).toBe("about $0.12 per attempt");
    expect(result.current.inline("search.guided_campaign", 3)).toBe("about $0.36 total");
    expect(result.current.confirmation("search.guided_campaign", 3)).toBe(
      "About $0.36 for 3 attempts (range $0.15 to $0.60, from 3 runs). Charges may be higher and nothing stops the task once you confirm.",
    );
    // The estimate has to price the work selected, so one unit and fifty differ.
    expect(result.current.confirmation("search.guided_campaign", 50)).toContain(
      "About $6.00 for 50 attempts",
    );
    expect(result.current.confirmation("search.vocabulary")).toContain("No completed runs yet.");
    expect(result.current.confirmation("search.vocabulary")).toContain(
      "The cost is unknown until the first run",
    );
    expect(result.current.confirmation("search.vocabulary")).not.toContain("may be higher");
    expect(result.current.isPaid("posting.refresh")).toBe(false);
    expect(result.current.isPaid("posting.refresh_with_reextraction")).toBe(true);
    expect(result.current.isPaid("posting.refresh_with_rematch")).toBe(true);
    expect(result.current.cost("future.work")).toBe("");
  });
});
