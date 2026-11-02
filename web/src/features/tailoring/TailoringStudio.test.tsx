import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RunCostHistory, TailoringPreflight, Task, TaskBilling } from "../../api/client";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { evaluationPreflightHandler, TASK_TYPE_CATALOG } from "../../test/taskTypes";
import { TailoringStudio } from "./TailoringStudio";

const API = "*/api/v1";
const WORKSPACE = `${API}/workspaces/:workspaceId`;

const CONFIRMATIONS = [
  "the exact factual wording",
  "the metrics, including that there are none",
  "the ownership level",
  "the scope",
  "the evidence category",
  "the job gaps it relates to",
  "that its source is user-confirmed discovery",
  "what remains ambiguous",
] as const;

const evaluationCostHistory = [
  {
    task_type: "discovery.evaluate",
    currency: "USD",
    unit: "posting",
    measured_runs: 6,
    unpriced_runs: 0,
    replayed_runs: 0,
    per_unit_low: "0.10",
    per_unit_median: "0.25",
    per_unit_high: "0.40",
  },
] satisfies RunCostHistory[];

const NO_REUSE = {
  reuse_available: false,
  tailoring_run_id: null,
  artifact_ids: [],
  cost_usd: null,
} satisfies TailoringPreflight;

/** What the studio observed of the requests it sent. */
interface StudioTraffic {
  posts: Record<string, unknown>[];
  preflights: URL[];
  billed: string[];
}

interface StudioReads {
  /** A function answers the task list late, for the loading and failure states. */
  tasks: Task[] | (() => Promise<Task[]>);
  preflight?: TailoringPreflight;
  costHistory?: RunCostHistory[];
  billing?: () => TaskBilling | Promise<TaskBilling>;
  enqueued?: Task;
  refuseEnqueue?: boolean;
}

/** Every read the studio makes for one job, plus the enqueues its buttons send. */
function useStudio(reads: StudioReads): StudioTraffic {
  const traffic: StudioTraffic = { posts: [], preflights: [], billed: [] };
  server.use(
    evaluationPreflightHandler(),
    http.get(`${API}/tasks/types`, () => HttpResponse.json(TASK_TYPE_CATALOG)),
    http.get(`${WORKSPACE}/spend-history`, () => HttpResponse.json(reads.costHistory ?? [])),
    http.get(`${WORKSPACE}/jobs/:jobId/tailoring-preflight`, ({ request }) => {
      traffic.preflights.push(new URL(request.url));
      return HttpResponse.json(reads.preflight ?? NO_REUSE);
    }),
    http.post(`${API}/tasks`, async ({ request }) => {
      traffic.posts.push((await request.json()) as Record<string, unknown>);
      if (reads.refuseEnqueue) {
        return HttpResponse.json(
          { detail: "The worker is not running." },
          { status: 503, headers: { "Content-Type": "application/problem+json" } },
        );
      }
      return HttpResponse.json(reads.enqueued ?? DISCOVERY_TASK);
    }),
    http.get(`${API}/tasks/:taskId/billing`, async ({ params }) => {
      traffic.billed.push(String(params.taskId));
      if (!reads.billing) return HttpResponse.error();
      try {
        return HttpResponse.json(await reads.billing());
      } catch {
        return HttpResponse.error();
      }
    }),
    http.get(`${API}/tasks`, async () => {
      try {
        const tasks = typeof reads.tasks === "function" ? await reads.tasks() : reads.tasks;
        return HttpResponse.json(tasks);
      } catch {
        return HttpResponse.error();
      }
    }),
  );
  return traffic;
}

function discoveryTask(progress: Record<string, unknown>): Task {
  return {
    id: "task-1",
    workspace_id: "workspace-1",
    task_type: "workflow.discovery",
    status: "completed",
    job_posting_ids: ["job-1"],
    pending_discovery_ids: [],
    source_ids: [],
    progress,
    attempt_count: 1,
    cancellation_requested_at: null,
    error_category: null,
    error_message: null,
    created_at: "2026-08-20T12:00:00Z",
    started_at: "2026-08-20T12:00:05Z",
    updated_at: "2026-08-20T12:01:00Z",
    completed_at: "2026-08-20T12:01:00Z",
  };
}

const DISCOVERY_TASK = discoveryTask({});

function tailorTask(status: Task["status"]): Task {
  return {
    id: "tailor-1",
    workspace_id: "workspace-1",
    task_type: "workflow.tailor",
    status,
    job_posting_ids: ["job-1"],
    pending_discovery_ids: [],
    source_ids: [],
    progress: status === "failed" ? {} : { outcome: "Package v3 stored." },
    error_message: status === "failed" ? "The provider refused the request" : null,
    error_category: status === "failed" ? "provider" : null,
    attempt_count: 1,
    cancellation_requested_at: status === "cancelled" ? "2026-08-20T12:00:30Z" : null,
    created_at: "2026-08-20T12:00:00Z",
    started_at: "2026-08-20T12:00:05Z",
    updated_at: "2026-08-20T12:01:00Z",
    completed_at: ["completed", "failed", "cancelled"].includes(status)
      ? "2026-08-20T12:01:00Z"
      : null,
  };
}

/** One discovery task carrying `progress`, the shape the gap flow reads. */
function useDiscovery(
  progress: Record<string, unknown>,
  preflight?: TailoringPreflight,
  costHistory: RunCostHistory[] = [],
): StudioTraffic {
  stubEventSource();
  return useStudio({ tasks: [discoveryTask(progress)], preflight, costHistory });
}

/** One tailoring run in `status`, billed by `billing`. */
function useTailorRun(
  status: Task["status"],
  billing: () => TaskBilling | Promise<TaskBilling>,
  history: Task[] = [],
  tasks?: () => Promise<Task[]>,
): StudioTraffic {
  stubEventSource();
  return useStudio({ tasks: tasks ?? [tailorTask(status), ...history], billing });
}

function stubEventSource() {
  vi.stubGlobal(
    "EventSource",
    class {
      close() {}
      addEventListener() {}
    },
  );
}

const GENERATE = "Generate or reuse the CV and letter";

/** What the control itself says it may spend, read through its own description. */
function spendOn(name: string): string {
  const control = screen.getByRole("button", { name });
  const described = control.getAttribute("aria-describedby") ?? "";
  return document.getElementById(described)?.textContent ?? "";
}

function studio() {
  render(
    <AppProviders>
      <TailoringStudio jobId="job-1" workspaceId="workspace-1" />
    </AppProviders>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("TailoringStudio", () => {
  it("shows recorded evaluation cost before re-evaluating", async () => {
    const traffic = useDiscovery({}, undefined, evaluationCostHistory);
    studio();

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Re-evaluate job" })).toHaveProperty(
        "disabled",
        false,
      ),
    );
    // The dialog keeps the cost line it was opened with, so the read must have landed.
    await waitFor(() => expect(spendOn("Re-evaluate job")).toMatch(/about \$0.25 per posting/));
    fireEvent.click(screen.getByRole("button", { name: "Re-evaluate job" }));

    expect(await screen.findByRole("dialog")).toBeTruthy();
    expect(screen.getByRole("dialog").textContent).toContain(
      "Estimated total at configured rates: $0.2000 USD.",
    );
    expect(traffic.posts).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Confirm re-evaluation" }));

    await waitFor(() => expect(traffic.posts).toHaveLength(1));
    expect(traffic.posts[0]).toMatchObject({
      task_type: "discovery.evaluate",
      payload: { job_posting_id: "job-1", fresh: true },
    });
  });

  it("labels an exact package reuse as free while forced regeneration stays paid", async () => {
    const traffic = useDiscovery(
      {},
      {
        reuse_available: true,
        tailoring_run_id: "run-reusable",
        artifact_ids: ["resume-1", "letter-1"],
        cost_usd: 0,
      },
    );
    studio();

    await waitFor(() =>
      expect(screen.getByRole("button", { name: GENERATE })).toHaveProperty("disabled", false),
    );
    fireEvent.click(screen.getByRole("button", { name: GENERATE }));
    expect(await screen.findByText(/Exact completed package found/)).toBeTruthy();
    expect(screen.getByText(/does not call the provider and adds no provider charge/)).toBeTruthy();
    expect(traffic.preflights.map((url) => url.searchParams.get("cover_letter"))).toEqual(["true"]);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    fireEvent.click(screen.getByRole("button", { name: "Regenerate new version" }));
    expect(screen.getByRole("dialog").textContent).toMatch(/skips completed-run reuse/);
    expect(traffic.preflights).toHaveLength(1);
    expect(traffic.posts).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Regenerate package" }));

    await waitFor(() => {
      expect(traffic.posts).toHaveLength(1);
      expect(traffic.posts[0]).toMatchObject({
        task_type: "workflow.tailor",
        payload: { regenerate: true },
      });
    });
  });

  it("shows a preflight miss and recorded tailoring cost when no exact reuse exists", async () => {
    const traffic = useDiscovery({});
    studio();

    await waitFor(() =>
      expect(screen.getByRole("button", { name: GENERATE })).toHaveProperty("disabled", false),
    );
    fireEvent.click(screen.getByRole("button", { name: GENERATE }));

    expect(await screen.findByText(/No exact completed package is available/)).toBeTruthy();
    expect(screen.getByRole("dialog").textContent).toMatch(
      /Preflight did not find a reusable package cost/,
    );
    expect(traffic.posts).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Generate package" }));

    await waitFor(() => expect(traffic.posts).toHaveLength(1));
  });

  it("marks every paid control and states its cost, leaving the free one unmarked", async () => {
    useDiscovery({});
    studio();

    await waitFor(() =>
      expect(spendOn("Re-evaluate job")).toMatch(
        /cost shown before you run|about \$[\d.]+ per run/,
      ),
    );
    for (const name of ["Re-evaluate job", GENERATE, "Regenerate new version"]) {
      expect(spendOn(name)).toMatch(/cost shown before you run|about \$[\d.]+ per run/);
      expect(screen.getByRole("button", { name }).className).toContain("button--paid");
    }
    const free = screen.getByRole("button", { name: "Explore a gap" });
    expect(free.className).toContain("button--quiet");
    expect(free.closest(".paid-action")).toBeNull();
    expect(screen.getAllByText("Spends money")).toHaveLength(3);
    expect(await screen.findByText("No provider charge.")).toBeTruthy();
  });

  it("includes the cover letter in the approved action", () => {
    useDiscovery({});
    studio();

    expect(screen.getByRole("button", { name: GENERATE })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Generate or reuse CV and letter" })).toBeNull();
  });

  it("will not write to the master CV until all eight points are confirmed", async () => {
    useDiscovery({ proposal: ["- Ran the migration end to end"], answered: true });
    studio();
    fireEvent.click(screen.getByRole("button", { name: "Explore a gap" }));

    const write = await screen.findByRole("button", {
      name: "Confirm all eight and write to master CV",
    });
    expect(write.hasAttribute("disabled")).toBe(true);

    for (const point of CONFIRMATIONS.slice(0, 7)) {
      fireEvent.click(screen.getByLabelText(point));
    }
    expect(write.hasAttribute("disabled")).toBe(true);

    fireEvent.click(screen.getByLabelText(CONFIRMATIONS[7]));
    expect(write.hasAttribute("disabled")).toBe(false);

    fireEvent.click(screen.getByLabelText(CONFIRMATIONS[0]));
    expect(write.hasAttribute("disabled")).toBe(true);
  });

  it("names every confirmed point in the write it sends", async () => {
    const traffic = useDiscovery({ proposal: ["- Ran the migration end to end"], answered: true });
    studio();
    fireEvent.click(screen.getByRole("button", { name: "Explore a gap" }));
    await screen.findByRole("button", { name: "Confirm all eight and write to master CV" });

    for (const point of CONFIRMATIONS) fireEvent.click(screen.getByLabelText(point));
    fireEvent.click(
      screen.getByRole("button", { name: "Confirm all eight and write to master CV" }),
    );

    await waitFor(() => {
      const writes = traffic.posts
        .map((body) => body.payload)
        .filter(
          (payload): payload is Record<string, unknown> =>
            typeof payload === "object" && payload !== null && "write_confirmed" in payload,
        );
      expect(writes).toHaveLength(1);
      expect(writes[0]?.confirmed_points).toEqual([...CONFIRMATIONS]);
    });
  });

  it("shows a refused enqueue beside the buttons that asked for it", async () => {
    stubEventSource();
    useStudio({ tasks: [DISCOVERY_TASK], refuseEnqueue: true });
    studio();
    fireEvent.click(screen.getByRole("button", { name: "Explore a gap" }));

    await waitFor(() => expect(document.querySelector(".field-error")).not.toBeNull());
    expect(document.querySelector(".field-error")?.closest(".studio-actions")).not.toBeNull();
  });

  it("shows a finished run this visit did not start", async () => {
    useDiscovery({});
    studio();

    expect(await screen.findByText("Progress appears on the Tasks page.")).toBeTruthy();
    expect(screen.queryByText("No work has run for this job yet.")).toBeNull();
  });

  it("names recorded work the way the rest of the application names it", async () => {
    useDiscovery({});
    studio();

    expect(await screen.findByText("Explore a gap", { selector: "span" })).toBeTruthy();
    expect(screen.queryByText("discovery")).toBeNull();
  });

  it("lists a render filed under this job with each warning on its own line", async () => {
    stubEventSource();
    const message =
      "Rendered Resume.pdf, Resume.txt\npressume warned:\n    readability: the final page is lightly filled";
    useStudio({
      tasks: [
        {
          ...discoveryTask({ message }),
          id: "render-1",
          task_type: "document.render",
        },
      ],
    });
    studio();

    const line = await screen.findByText(/the final page is lightly filled/, {
      selector: "small > span",
    });
    expect(Array.from(line.parentElement?.children ?? [], (each) => each.textContent)).toEqual([
      "Rendered Resume.pdf, Resume.txt",
      "pressume warned:",
      "    readability: the final page is lightly filled",
    ]);
  });

  it("keeps a failed task's error as one block with its paragraph breaks", async () => {
    stubEventSource();
    const error = "Nothing was billed.\n\nAdd credit at the provider.";
    useStudio({
      tasks: [{ ...discoveryTask({}), status: "failed", error_message: error }],
    });
    studio();

    const shown = await screen.findByText(/Nothing was billed/, { selector: "small" });
    expect(shown.textContent).toBe(error);
    expect(shown.children).toHaveLength(0);
  });

  it("names an evaluation batch over this tracked posting as a re-evaluation", async () => {
    stubEventSource();
    useStudio({
      tasks: [{ ...discoveryTask({}), id: "rerun-1", task_type: "discovery.evaluate_batch" }],
    });
    studio();

    expect(
      await screen.findByText("Re-evaluate tracked postings", { selector: "span" }),
    ).toBeTruthy();
    expect(screen.queryByText("Evaluate discoveries")).toBeNull();
  });

  it("states what a completed tailoring run cost", async () => {
    const traffic = useTailorRun(
      "completed",
      () => ({
        task_id: "tailor-1",
        status: "priced",
        call_count: 4,
        unpriced_call_count: 0,
        resent_on_standard_count: 0,
        actual_cost_usd: "0.3172049",
      }),
      [
        {
          ...tailorTask("completed"),
          id: "tailor-old",
          progress: { outcome: "Package v1 stored." },
          created_at: "2026-08-19T12:00:00Z",
          started_at: "2026-08-19T12:00:05Z",
          updated_at: "2026-08-19T12:01:00Z",
          completed_at: "2026-08-19T12:01:00Z",
        },
      ],
    );
    studio();

    expect(await screen.findByText("Package v3 stored.")).toBeTruthy();
    const billingStatus = await screen.findByRole("status", {
      name: "Latest tailoring task billing",
    });
    expect(billingStatus.textContent).toBe(
      "Recorded cost for this task: $0.3172 across 4 model calls.",
    );
    const regenerate = screen.getByRole("button", { name: "Regenerate new version" });
    expect(
      billingStatus.compareDocumentPosition(regenerate) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(traffic.billed).toEqual(["tailor-1"]);
  });

  it("waits for terminal billing before offering another tailoring run", async () => {
    let finishBilling: (value: TaskBilling) => void = () => {};
    const billing = new Promise<TaskBilling>((resolve) => {
      finishBilling = resolve;
    });
    useTailorRun("completed", () => billing);
    studio();

    const billingStatus = await screen.findByRole("status", {
      name: "Latest tailoring task billing",
    });
    expect(billingStatus.textContent).toBe("Checking recorded cost for this task.");
    const generate = screen.getByRole("button", { name: GENERATE });
    const regenerate = screen.getByRole("button", { name: "Regenerate new version" });
    expect(generate).toHaveProperty("disabled", false);
    expect(regenerate).toHaveProperty("disabled", false);
    expect(
      billingStatus.compareDocumentPosition(generate) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    finishBilling({
      task_id: "tailor-1",
      status: "no_model_calls",
      call_count: 0,
      unpriced_call_count: 0,
      resent_on_standard_count: 0,
      actual_cost_usd: null,
    });
    await waitFor(() => {
      expect(generate).toHaveProperty("disabled", false);
      expect(regenerate).toHaveProperty("disabled", false);
    });
  });

  it("shows a billing-read warning before restoring tailoring actions", async () => {
    let failBilling: (reason: Error) => void = () => {};
    const billing = new Promise<TaskBilling>((_, reject) => {
      failBilling = reject;
    });
    useTailorRun("completed", () => billing);
    studio();

    expect(
      await screen.findByRole("status", { name: "Latest tailoring task billing" }),
    ).toHaveProperty("textContent", "Checking recorded cost for this task.");
    failBilling(new Error("Billing unavailable"));

    // The billing query retries once after react-query's default 1000ms backoff
    // before the warning shows, so drive that wait with fake timers.
    vi.useFakeTimers();
    try {
      await vi.advanceTimersByTimeAsync(1_100);
    } finally {
      vi.useRealTimers();
    }
    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Latest tailoring task billing" })).toHaveProperty(
        "textContent",
        "Recorded cost for this task is unavailable; review Spend before retrying.",
      ),
    );
    expect(screen.getByRole("button", { name: GENERATE })).toHaveProperty("disabled", false);
    expect(screen.getByRole("button", { name: "Regenerate new version" })).toHaveProperty(
      "disabled",
      false,
    );
  });

  it("reports billing for a cancelled tailoring run", async () => {
    useTailorRun("cancelled", () => ({
      task_id: "tailor-1",
      status: "priced",
      call_count: 1,
      unpriced_call_count: 0,
      resent_on_standard_count: 0,
      actual_cost_usd: "0.0123",
    }));
    studio();

    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Latest tailoring task billing" })).toHaveProperty(
        "textContent",
        "Recorded cost for this task: $0.0123 across 1 model call.",
      ),
    );
  });

  it("uses the newest recorded discovery task for its busy state", async () => {
    useTailorRun(
      "completed",
      () => ({
        task_id: "tailor-1",
        status: "no_model_calls",
        call_count: 0,
        unpriced_call_count: 0,
        resent_on_standard_count: 0,
        actual_cost_usd: null,
      }),
      [
        {
          ...discoveryTask({}),
          id: "discovery-new",
          status: "running",
          created_at: "2026-08-21T12:00:00Z",
          started_at: "2026-08-21T12:00:05Z",
          updated_at: "2026-08-21T12:01:00Z",
          completed_at: null,
        },
        {
          ...discoveryTask({ next_question: null, closed: "The older run ended." }),
          id: "discovery-old",
          created_at: "2026-08-19T12:00:00Z",
          started_at: "2026-08-19T12:00:05Z",
          updated_at: "2026-08-19T12:01:00Z",
          completed_at: "2026-08-19T12:01:00Z",
        },
      ],
    );
    studio();

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Explore a gap" })).toHaveProperty(
        "disabled",
        true,
      ),
    );
  });

  it("says a failed tailoring run recorded no model charge", async () => {
    useTailorRun("failed", () => ({
      task_id: "tailor-1",
      status: "no_model_calls",
      call_count: 0,
      unpriced_call_count: 0,
      resent_on_standard_count: 0,
      actual_cost_usd: null,
    }));
    studio();

    expect(await screen.findByText("The provider refused the request")).toBeTruthy();
    expect(
      await screen.findByRole("status", { name: "Latest tailoring task billing" }),
    ).toHaveProperty("textContent", "No model charge was recorded for this task.");
    expect(document.body.textContent).not.toContain(
      "The provider refused the request No model charge was recorded",
    );
    expect(document.body.textContent).not.toContain("$0.0000");
  });

  it("withholds a tailoring cost while the run is still going", async () => {
    const traffic = useTailorRun("running", () => ({
      task_id: "tailor-1",
      status: "priced",
      call_count: 4,
      unpriced_call_count: 0,
      resent_on_standard_count: 0,
      actual_cost_usd: "0.3172049",
    }));
    studio();

    expect(await screen.findByText("Package v3 stored.")).toBeTruthy();
    expect(screen.getByRole("button", { name: GENERATE })).toHaveProperty("disabled", true);
    expect(screen.getByRole("button", { name: "Regenerate new version" })).toHaveProperty(
      "disabled",
      true,
    );
    expect(document.body.textContent).not.toContain("Recorded cost for this task");
    expect(traffic.billed).toEqual([]);
  });

  it("waits for task history before enabling paid tailoring", async () => {
    let finishTasks: (tasks: Task[]) => void = () => {};
    const taskList = new Promise<Task[]>((resolve) => {
      finishTasks = resolve;
    });
    useTailorRun(
      "completed",
      () => ({
        task_id: "tailor-1",
        status: "no_model_calls",
        call_count: 0,
        unpriced_call_count: 0,
        resent_on_standard_count: 0,
        actual_cost_usd: null,
      }),
      [],
      () => taskList,
    );
    studio();

    expect(
      await screen.findByText("Checking current tailoring work before enabling paid actions."),
    ).toBeTruthy();
    const generate = screen.getByRole("button", { name: GENERATE });
    expect(generate).toHaveProperty("disabled", true);
    expect(screen.queryByText("No work has run for this job yet.")).toBeNull();

    finishTasks([]);
    await waitFor(() => expect(generate).toHaveProperty("disabled", false));
    expect(screen.getByText("No work has run for this job yet.")).toBeTruthy();
  });

  it("keeps paid tailoring unavailable when task history cannot be read", async () => {
    let failTasks: (reason: Error) => void = () => {};
    const taskList = new Promise<Task[]>((_, reject) => {
      failTasks = reject;
    });
    let asked = false;
    useTailorRun(
      "completed",
      () => ({
        task_id: "tailor-1",
        status: "no_model_calls",
        call_count: 0,
        unpriced_call_count: 0,
        resent_on_standard_count: 0,
        actual_cost_usd: null,
      }),
      [],
      () => {
        asked = true;
        return taskList;
      },
    );
    studio();

    await waitFor(() => expect(asked).toBe(true));
    failTasks(new Error("Task history unavailable"));

    // The task list query retries once after react-query's default 1000ms backoff
    // before the error shows, so drive that wait with fake timers.
    vi.useFakeTimers();
    try {
      await vi.advanceTimersByTimeAsync(1_100);
    } finally {
      vi.useRealTimers();
    }
    expect(await screen.findByRole("alert")).toHaveProperty(
      "textContent",
      "Current tailoring work is unavailable. Reload or review Tasks before starting paid work.",
    );
    expect(screen.getByRole("button", { name: GENERATE })).toHaveProperty("disabled", true);
    expect(screen.getByRole("button", { name: "Regenerate new version" })).toHaveProperty(
      "disabled",
      true,
    );
    expect(screen.queryByText("No work has run for this job yet.")).toBeNull();
  });

  it("reports a discovery that stopped without adding anything", async () => {
    useDiscovery({ next_question: null, closed: "no answer supported the claim" });
    studio();
    fireEvent.click(screen.getByRole("button", { name: "Explore a gap" }));

    expect(await screen.findByText(/Nothing was added to the master CV/)).toBeTruthy();
    expect(
      screen.queryByRole("button", {
        name: "Confirm all eight and write to master CV",
      }),
    ).toBeNull();
  });
});
