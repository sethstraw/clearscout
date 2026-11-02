import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, delay, http, type HttpHandler } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  GuidedCampaignConfiguration,
  SearchVocabularyConfiguration,
  Task,
  TaskBilling,
} from "../../api/client";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { TASK_TYPE_CATALOG } from "../../test/taskTypes";
import { TasksPage } from "./TaskList";

const API = "*/api/v1";
const WORKSPACE = `${API}/workspaces/:workspaceId`;

const running = {
  id: "running-1",
  workspace_id: "workspace-tasks",
  task_type: "posting.refresh",
  status: "running",
  attempt_count: 1,
  job_posting_ids: [],
  pending_discovery_ids: [],
  source_ids: [],
  progress: { current: 1, total: 2 },
  cancellation_requested_at: null,
  error_category: null,
  error_message: null,
  created_at: "2026-08-06T12:00:00Z",
  started_at: "2026-08-06T12:00:01Z",
  updated_at: "2026-08-06T12:00:02Z",
  completed_at: null,
} satisfies Task;
const failed = {
  ...running,
  id: "failed-1",
  task_type: "posting.refresh_batch",
  status: "failed",
  attempt_count: 3,
  error_category: "IngestError",
  error_message: "Source unavailable",
  progress: {
    failures: [
      {
        pending_discovery_id: "pending-closure-1",
        error_message: "The publisher returned HTTP 410.",
      },
    ],
  },
  completed_at: "2026-08-06T12:00:03Z",
} satisfies Task;
const paid = {
  ...running,
  id: "paid-1",
  task_type: "workflow.batch",
  status: "completed",
  completed_at: "2026-08-06T12:00:03Z",
} satisfies Task;
const failedPaid = {
  ...running,
  id: "paid-failed-1",
  task_type: "search.guided_campaign",
  status: "failed",
  error_category: "ProviderError",
  error_message: "The provider refused the request.",
  completed_at: "2026-08-06T12:00:03Z",
} satisfies Task;

const CAMPAIGN_CONFIGURATION = {
  provider: "openai",
  model_choice: { model: "gpt-5.6-terra", effort: "low", service_tier: "flex" },
} satisfies GuidedCampaignConfiguration;

const VOCABULARY_CONFIGURATION = {
  provider: "openai",
  model_choice: { model: "gpt-5.6-terra", effort: "low", service_tier: "flex" },
} satisfies SearchVocabularyConfiguration;

class FakeEventSource {
  static opened: string[] = [];
  url: string;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.opened.push(url);
  }

  addEventListener() {}
  close() {}
  onerror: (() => void) | null = null;
}

interface TaskPageReads {
  tasks: Task[] | ((limit: number) => Task[]);
  /** Undefined leaves a task unbilled, so a billing read for it is a failure. */
  billingFor?: (taskId: string) => TaskBilling | Promise<TaskBilling> | undefined;
  /** Hold the campaign-configuration response open, for the provider-loading state. */
  configurationPending?: boolean;
  onWrite?: (taskId: string, action: string) => Task;
}

/** Every read the Tasks page makes, plus the retry and cancel writes it sends. */
function taskPageHandlers(reads: TaskPageReads): { handlers: HttpHandler[]; billed: string[] } {
  const billed: string[] = [];
  const handlers = [
    http.get(`${API}/tasks/types`, () => HttpResponse.json(TASK_TYPE_CATALOG)),
    http.get(`${WORKSPACE}/spend-history`, () => HttpResponse.json([])),
    http.get(`${WORKSPACE}/guided-campaign-configuration`, async () => {
      if (reads.configurationPending) await delay("infinite");
      return HttpResponse.json(CAMPAIGN_CONFIGURATION);
    }),
    http.get(`${WORKSPACE}/search-vocabulary-configuration`, () =>
      HttpResponse.json(VOCABULARY_CONFIGURATION),
    ),
    http.post(`${API}/tasks/:taskId/retry`, ({ params }) =>
      HttpResponse.json(reads.onWrite?.(String(params.taskId), "retry") ?? running),
    ),
    http.post(`${API}/tasks/:taskId/cancel`, ({ params }) =>
      HttpResponse.json(reads.onWrite?.(String(params.taskId), "cancel") ?? running),
    ),
    http.get(`${API}/tasks/:taskId/billing`, async ({ params }) => {
      const taskId = String(params.taskId);
      billed.push(taskId);
      const answer = await reads.billingFor?.(taskId);
      return answer ? HttpResponse.json(answer) : HttpResponse.error();
    }),
    http.get(`${API}/tasks`, ({ request }) => {
      const limit = Number(new URL(request.url).searchParams.get("limit") ?? 50);
      const tasks = typeof reads.tasks === "function" ? reads.tasks(limit) : reads.tasks;
      return HttpResponse.json(tasks);
    }),
  ];
  return { handlers, billed };
}

function useTaskPage(reads: TaskPageReads): string[] {
  const { handlers, billed } = taskPageHandlers(reads);
  server.use(...handlers);
  return billed;
}

function renderTasks() {
  render(
    <AppProviders>
      <TasksPage />
    </AppProviders>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
  FakeEventSource.opened = [];
});

describe("TasksPage", () => {
  it("resumes retained scan work with workspace scope and idempotency, separately from a fresh scan", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const scan = {
      ...failed,
      id: "scan-interrupted",
      task_type: "source.scan",
    } satisfies Task;
    let history: Task[] = [scan];
    const requests: { workspace: string | null; key: string | null }[] = [];
    const billed = useTaskPage({ tasks: () => history });
    server.use(
      http.post(`${API}/tasks/:taskId/resume`, ({ request }) => {
        requests.push({
          workspace: new URL(request.url).searchParams.get("workspace_id"),
          key: request.headers.get("Idempotency-Key"),
        });
        history = [
          {
            ...scan,
            id: "scan-resumed",
            status: "queued",
            source_resume_from_task_id: scan.id,
          },
        ];
        return HttpResponse.json(history[0], { status: 202 });
      }),
    );
    renderTasks();
    const article = await screen.findByRole("article", { name: /^Scan for openings created/ });
    expect(within(article).getByRole("button", { name: /^Scan again / })).toBeTruthy();
    const resume = within(article).getByRole("button", { name: /^Resume / });
    resume.focus();
    fireEvent.click(resume);
    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).toContain("retained");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(document.activeElement).toBe(resume);
    fireEvent.click(resume);
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Resume retained work",
      }),
    );
    expect(await screen.findByText("Queued")).toBeTruthy();
    expect(requests).toHaveLength(1);
    expect(requests[0]?.workspace).toBe("workspace-tasks");
    expect(requests[0]?.key).toBeTruthy();
    expect(billed).toEqual([]);
  });

  it("keeps free scan recovery available when cost metadata fails and shows resume errors at its task", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const scan = {
      ...failed,
      id: "scan-failed",
      task_type: "source.scan",
      source_resume_from_task_id: "original-scan",
    } satisfies Task;
    const continuation = {
      ...failed,
      id: "continuation-failed",
      task_type: "source.continue",
    } satisfies Task;
    useTaskPage({ tasks: [scan, continuation] });
    server.use(
      http.get(`${API}/tasks/types`, () => HttpResponse.error()),
      http.post(`${API}/tasks/:taskId/resume`, () =>
        HttpResponse.json({ detail: "Retained work is unavailable." }, { status: 409 }),
      ),
    );
    renderTasks();
    const article = await screen.findByRole("article", { name: /^Scan for openings created/ });
    const resume = within(article).getByRole("button", { name: /^Resume / });
    expect(resume).toHaveProperty("disabled", false);
    expect(within(article).queryByRole("button", { name: /^Scan again / })).toBeNull();
    fireEvent.click(resume);
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Resume retained work",
      }),
    );
    expect((await within(article).findByRole("alert")).textContent).toContain(
      "Resume failed: Retained work is unavailable.",
    );
    const continued = screen.getByRole("article", { name: /^Continue reading a source created/ });
    expect(within(continued).queryByRole("button", { name: /^Retry / })).toBeNull();
    expect(
      within(continued)
        .getByRole("link", { name: "Open board scan history to read its deferred links" })
        .getAttribute("href"),
    ).toBe("/find?tab=scans#source-scan-history-heading");
    expect(continued.textContent).toContain(
      "This read stays incomplete at least until its deferred links are read, and board scan history keeps it only while no newer read of the same kind exists for this source.",
    );
    expect(within(continued).queryByRole("alert")).toBeNull();
  });

  it("shows a drained continuation as completed with its batch and link counts", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const continuation = {
      ...running,
      id: "continuation-1",
      task_type: "source.continue",
      status: "completed",
      progress: {
        pending: 2,
        completed: 19,
        failed: 0,
        policy_deferred: 2,
        read: 60,
        matched: 1,
        no_title_match: 25,
        staged: 0,
        refused: 34,
        content_resolved: 1,
      },
      completed_at: "2026-08-06T12:00:03Z",
    } satisfies Task;
    useTaskPage({ tasks: [continuation] });
    renderTasks();

    const article = await screen.findByRole("article", {
      name: /Continue reading a source created/,
    });
    expect(within(article).getByText("Completed")).toBeTruthy();
    expect(within(article).queryByText("Incomplete")).toBeNull();
    expect(
      within(article).getByText(
        "Openings this batch: 60 read, 1 title match, 25 no title match, 34 refused on a hard constraint, 0 newly queued, 1 body on file",
      ),
    ).toBeTruthy();
    expect(
      within(article).getByText(
        "Publisher links for this source: 19 read, 0 queued, 0 failed, 2 need your read",
      ),
    ).toBeTruthy();
    expect(within(article).queryByText(/Still pending/)).toBeNull();
    expect(within(article).queryByText(/remaining total is unknown/)).toBeNull();
    expect(within(article).queryByText("Full payload")).toBeNull();
  });

  it.each([
    { status: "running", completed: 589, pending: 1 },
    { status: "queued", completed: 589, pending: 1 },
    { status: "running", completed: 0, pending: 0 },
    { status: "queued", completed: 0, pending: 0 },
  ] as const)(
    "shows known links and an unknown remaining total for $status continuation with $pending queued",
    async ({ status, completed, pending }) => {
      window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
      const continuation = {
        ...running,
        id: "continuation-running",
        task_type: "source.continue",
        status,
        progress: {
          pending,
          completed,
          failed: 0,
          policy_deferred: 0,
          continuation_id: "continuation-aggregate-1",
          count: 1,
        },
      } satisfies Task;
      useTaskPage({ tasks: [continuation] });
      renderTasks();

      const article = await screen.findByRole("article", {
        name: /Continue reading a source created/,
      });
      expect(
        within(article).getByText(
          `Publisher links for this source: ${completed} read, ${pending} queued, 0 failed, 0 need your read`,
        ),
      ).toBeTruthy();
      expect(
        within(article).getByText(
          "Queued links are those found so far. The final remaining total is unknown because pages can reveal more links.",
        ),
      ).toBeTruthy();
      expect(within(article).getByText(/Links in this batch 1/)).toBeTruthy();
      expect(within(article).getByRole("button", { name: /^Cancel / })).toBeTruthy();
    },
  );

  it.each(["completed", "cancelled", "failed"] as const)(
    "keeps $status continuation counts without claiming ongoing reading",
    async (status) => {
      window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
      useTaskPage({
        tasks: [
          {
            ...running,
            id: "continuation-terminal",
            task_type: "source.continue",
            status,
            progress: { pending: 1, completed: 589, failed: 0, policy_deferred: 0 },
            completed_at: "2026-08-06T12:00:03Z",
          } satisfies Task,
        ],
      });
      renderTasks();

      const article = await screen.findByRole("article", {
        name: /Continue reading a source created/,
      });
      expect(
        within(article).getByText(
          "Publisher links for this source: 589 read, 1 queued, 0 failed, 0 need your read",
        ),
      ).toBeTruthy();
      expect(within(article).queryByText(/remaining total is unknown/)).toBeNull();
      expect(within(article).queryByText(/Queued links are those found so far/)).toBeNull();
      expect(within(article).queryByRole("button", { name: /^Cancel / })).toBeNull();
    },
  );

  it("keeps other tasks in view when one scan's continuation chain passes fifty tasks", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const chain = Array.from(
      { length: 60 },
      (_, index) =>
        ({
          ...running,
          id: `continuation-${index}`,
          task_type: "source.continue",
          status: "completed",
          progress: {},
          completed_at: "2026-08-06T12:00:03Z",
        }) satisfies Task,
    );
    const history = [...chain, paid];
    // The route answers newest first and stops at the requested limit, 50 by default.
    useTaskPage({ tasks: (limit) => history.slice(0, limit) });
    renderTasks();

    expect(await screen.findByRole("article", { name: /^Compare postings created / })).toBeTruthy();
  });

  it("names a failed posting-text read without calling the row itself failed", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const refreshed = {
      ...running,
      id: "refresh-1",
      task_type: "discovery.refresh_batch",
      status: "completed",
      progress: {
        refreshed: 2,
        failed: 0,
        content_failures: [
          { pending_discovery_id: "pending-7", error_message: "The publisher timed out." },
        ],
      },
      completed_at: "2026-08-06T12:00:03Z",
    } satisfies Task;
    useTaskPage({ tasks: [refreshed] });
    renderTasks();

    const article = await screen.findByRole("article", {
      name: /^Refresh queued postings created /,
    });
    expect(
      within(article).getByText(
        "Posting text could not be read. The publisher outcome for these rows stands.",
      ),
    ).toBeTruthy();
    expect(
      within(article).getByRole("list", { name: "Posting text read failures" }).textContent,
    ).toContain("pending-7: The publisher timed out.");
    expect(within(article).queryByRole("list", { name: "Task item failures" })).toBeNull();
    expect(within(article).queryByText("Full payload")).toBeNull();
  });

  it("keeps a by-reason breakdown out of the comma-joined counts line", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const refreshed = {
      ...running,
      id: "refresh-2",
      task_type: "discovery.refresh_batch",
      status: "completed",
      progress: {
        selected: 2,
        refreshed: 2,
        unverified: 1,
        unverified_postings: [
          { job_posting_id: "job-4", reason: "The publisher could not verify this posting." },
        ],
        content_readable: 1,
        content_unreadable: 1,
        content_kept: 1,
        content_outcomes: { readable: 1, fetch_failed: 1 },
      },
      completed_at: "2026-08-06T12:00:03Z",
    } satisfies Task;
    useTaskPage({ tasks: [refreshed] });
    renderTasks();

    const article = await screen.findByRole("article", {
      name: /^Refresh queued postings created /,
    });
    expect(
      within(article).getByText(
        "Selected 2, Refreshed 2, Could not verify 1, " +
          "Posting text readable 1, Posting text unreadable 1, " +
          "Readable text kept after a failed re-read 1",
      ),
    ).toBeTruthy();
    // The same rows carry both keys, so the sentence names that bucket once.
    expect(within(article).queryByText(/Unverified postings/)).toBeNull();
    expect(within(article).getByText("Full payload")).toBeTruthy();
    expect(within(article).getByText("unverified_postings")).toBeTruthy();
    expect(
      within(article).getByText("job-4: The publisher could not verify this posting."),
    ).toBeTruthy();
    const outcomes = within(article).getByRole("table", { name: "Content outcomes" });
    expect(within(outcomes).getByRole("rowheader", { name: "Readable" })).toBeTruthy();
    expect(outcomes.textContent).toContain("Fetch failed");
    expect(within(article).queryByText("content_outcomes")).toBeNull();
  });

  it("leaves the identifier lists a discovery refresh carries out of the counts line", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const refreshed = {
      ...running,
      id: "refresh-3",
      task_type: "discovery.refresh_batch",
      status: "completed",
      progress: {
        selected: 12,
        refreshed: 8,
        withdrawn_as_closed: 2,
        not_checkable: 1,
        unverified: 1,
        failed: 0,
        refreshed_ids: Array.from({ length: 8 }, (_, index) => `pending-${index}`),
        withdrawn_as_closed_ids: ["pending-20", "pending-21"],
        not_checkable_ids: ["pending-30"],
        unverified_ids: ["pending-40"],
      },
      completed_at: "2026-08-06T12:00:03Z",
    } satisfies Task;
    useTaskPage({ tasks: [refreshed] });
    renderTasks();

    const article = await screen.findByRole("article", {
      name: /^Refresh queued postings created /,
    });
    expect(
      within(article).getByText(
        "Selected 12, Refreshed 8, Withdrawn as closed 2, Not checkable 1, " +
          "Could not verify 1, Failed 0",
      ),
    ).toBeTruthy();
    expect(within(article).queryByText(/Refreshed ids/)).toBeNull();
    expect(within(article).queryByText(/Unverified ids/)).toBeNull();
    expect(within(article).getByText("refreshed_ids")).toBeTruthy();
    expect(within(article).getByText(/pending-20, pending-21/)).toBeTruthy();
  });

  it("shows the reasons a campaign stopped rather than how many there were", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const campaign = {
      ...running,
      id: "campaign-2",
      task_type: "search.guided_campaign",
      status: "completed",
      progress: {
        reasons: ["query_budget_exhausted", "no_new_sources"],
        cancelled: false,
        report_available: true,
      },
      completed_at: "2026-08-06T12:00:03Z",
    } satisfies Task;
    useTaskPage({ tasks: [campaign] });
    renderTasks();

    const article = await screen.findByRole("article", { name: /^Search for new jobs created / });
    expect(
      within(article).getByText("Reasons: query budget exhausted, no new sources"),
    ).toBeTruthy();
    expect(within(article).getByText("Cancelled: no, Report available: yes")).toBeTruthy();
    expect(within(article).queryByText(/Reasons 2/)).toBeNull();
    expect(within(article).queryByText("Full payload")).toBeNull();
  });

  it("names a source the same way whether or not its name has a space", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const oneWord = {
      ...running,
      id: "scan-5",
      task_type: "source.scan",
      status: "completed",
      progress: { source: "Greenhouse" },
      completed_at: "2026-08-06T12:00:03Z",
    } satisfies Task;
    const twoWords = {
      ...oneWord,
      id: "scan-6",
      created_at: "2026-08-06T11:00:00Z",
      progress: { source: "Acme Health careers" },
    } satisfies Task;
    useTaskPage({ tasks: [oneWord, twoWords] });
    renderTasks();

    expect(await screen.findByText("Source: Greenhouse")).toBeTruthy();
    expect(screen.getByText("Source: Acme Health careers")).toBeTruthy();
    expect(screen.queryByText("Full payload")).toBeNull();
  });

  it("names a count and a state the label map never listed", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const scan = {
      ...running,
      id: "scan-1",
      task_type: "source.scan",
      status: "completed",
      progress: {
        frontier_capped: 7,
        continuation_budget_reached: 331,
        attempt_id: 12,
        cancelled: true,
        paused: false,
        cost_usd: 1.2345,
        refused_by: "policy",
      },
      completed_at: "2026-08-06T12:00:03Z",
    } satisfies Task;
    useTaskPage({ tasks: [scan] });
    renderTasks();

    const article = await screen.findByRole("article", { name: /^Scan for openings created / });
    expect(
      within(article).getByText(
        "Frontier capped 7, Continuation budget reached 331, Cancelled: yes, " +
          "Paused: no, Cost $1.2345, Refused by: policy",
      ),
    ).toBeTruthy();
    expect(within(article).getByText("attempt_id")).toBeTruthy();
  });

  it("names the counts a sweep reports as lists", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const sweep = {
      ...running,
      id: "scan-3",
      task_type: "source.scan",
      status: "completed",
      progress: {
        sources_considered: 3,
        scanned: [{ source: "Acme careers" }, { source: "Globex jobs" }],
        failed: [{ source: "Initech jobs", reason: "network_unreachable" }],
      },
      completed_at: "2026-08-06T12:00:03Z",
    } satisfies Task;
    useTaskPage({ tasks: [sweep] });
    renderTasks();

    const article = await screen.findByRole("article", { name: /^Scan for openings created / });
    expect(
      within(article).getByText("Sources considered 3, Sources scanned 2, Failed 1"),
    ).toBeTruthy();
    // The bare count says everything the scanned rows carry, so only the failures,
    // which carry a reason breakdown, stay in the payload.
    expect(within(article).queryByText("scanned")).toBeNull();
    expect(within(article).getByText("failed")).toBeTruthy();
    expect(within(article).getByText("1 (network unreachable (1))")).toBeTruthy();
  });

  it("names the verdict a tailoring run reports as a string", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const tailored = {
      ...running,
      id: "tailor-1",
      task_type: "workflow.tailor",
      status: "completed",
      progress: { tailoring_run_id: "run-1", outcome: "needs_rewrite" },
      completed_at: "2026-08-06T12:00:03Z",
    } satisfies Task;
    useTaskPage({ tasks: [tailored] });
    renderTasks();

    const article = await screen.findByRole("article", { name: /created /, hidden: false });
    expect(within(article).getByText("Outcome: needs rewrite")).toBeTruthy();
    expect(within(article).getByText("tailoring_run_id")).toBeTruthy();
  });

  it("names a false state a person is looking for instead of hiding it", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const campaign = {
      ...running,
      id: "campaign-1",
      task_type: "search.guided_campaign",
      status: "completed",
      progress: { report_available: false, reuse_available: false },
      completed_at: "2026-08-06T12:00:03Z",
    } satisfies Task;
    useTaskPage({ tasks: [campaign] });
    renderTasks();

    const article = await screen.findByRole("article", { name: /^Search for new jobs created / });
    expect(within(article).getByText("Report available: no, Reuse available: no")).toBeTruthy();
    expect(within(article).queryByText("Full payload")).toBeNull();
  });

  it("spells out the three payload keys whose own names mislead", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const retry = {
      ...running,
      id: "campaign-retry-1",
      task_type: "search.guided_campaign_retry",
      status: "completed",
      progress: { processed_attempts: 1, withdrawn_on_read: 2, count: 4 },
      completed_at: "2026-08-06T12:00:03Z",
    } satisfies Task;
    useTaskPage({ tasks: [retry] });
    renderTasks();

    const article = await screen.findByRole("article", { name: /created / });
    expect(
      within(article).getByText(
        "Search attempts reprocessed 1, Queued rows withdrawn once their posting was fetched 2, " +
          "Links in this batch 4",
      ),
    ).toBeTruthy();
  });

  it("renders a nested funnel group as a table of groups and counts", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const scan = {
      ...running,
      id: "scan-2",
      task_type: "source.scan",
      status: "completed",
      progress: {
        staged: 2,
        funnel: {
          counts: { proposed: 12, read: 9, fetched: 8, screened: 7, staged: 2, rejected: 5 },
          outcomes: [{ url: "https://example.test/a", reason: "screened_out" }],
          total_outcomes: 1,
        },
      },
      completed_at: "2026-08-06T12:00:03Z",
    } satisfies Task;
    useTaskPage({ tasks: [scan] });
    renderTasks();

    const article = await screen.findByRole("article", { name: /^Scan for openings created / });
    const counts = within(article).getByRole("table", { name: "Funnel counts" });
    expect(within(counts).getByRole("rowheader", { name: "Proposed" })).toBeTruthy();
    expect(counts.textContent).toContain("12");
    expect(counts.textContent).toContain("Rejected");
    expect(within(counts).getByRole("columnheader", { name: "Name" })).toBeTruthy();
    expect(within(counts).queryByRole("columnheader", { name: "Funnel counts" })).toBeNull();
    expect(within(article).getByText("Staged 2")).toBeTruthy();
    // The tabled counts leave the payload; the funnel members with no table stay.
    expect(within(article).getByText("funnel")).toBeTruthy();
    expect(within(article).getByText(/total_outcomes/)).toBeTruthy();
    expect(within(article).queryByText(/proposed/)).toBeNull();
  });

  it("keeps two count groups that humanize to one label apart", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const duplicated = vi.spyOn(console, "error").mockImplementation(() => {});
    const scan = {
      ...running,
      id: "scan-4",
      task_type: "source.scan",
      status: "completed",
      progress: {
        funnel_counts: { proposed: 3 },
        funnel: { counts: { proposed: 4 } },
      },
      completed_at: "2026-08-06T12:00:03Z",
    } satisfies Task;
    useTaskPage({ tasks: [scan] });
    renderTasks();

    const article = await screen.findByRole("article", { name: /^Scan for openings created / });
    const top = within(article).getByRole("table", { name: "Funnel counts (funnel_counts)" });
    const nested = within(article).getByRole("table", { name: "Funnel counts (funnel.counts)" });
    expect(top.textContent).toContain("3");
    expect(nested.textContent).toContain("4");
    expect(duplicated.mock.calls.map(String).join(" ")).not.toContain("same key");
    duplicated.mockRestore();
  });

  it("names the rows a relevance review paid for and did not review", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const reviewed = {
      ...running,
      id: "relevance-1",
      task_type: "discovery.relevance_review",
      status: "completed",
      progress: {
        candidates: 3,
        reviewed: 1,
        model_calls_made: 1,
        validation_failures: [
          {
            pending_discovery_id: "pending-11",
            failure_code: "invalid_cv_evidence",
            detail:
              "A positive direct or transferable recommendation must cite master-CV evidence.",
          },
          {
            pending_discovery_id: "pending-12",
            failure_code: "leading_excerpt_only",
            detail: "The result cited only the leading excerpt of a longer posting.",
          },
          { pending_discovery_id: "pending-13", failure_code: "malformed_answer" },
        ],
      },
      completed_at: "2026-08-06T12:00:03Z",
    } satisfies Task;
    useTaskPage({ tasks: [reviewed] });
    renderTasks();

    const article = await screen.findByRole("article", {
      name: /^Review discovery relevance created /,
    });
    expect(
      within(article).getByText(
        "These rows were not reviewed. The model call was paid for and returned nothing " +
          "usable for them, so no verdict was recorded.",
      ),
    ).toBeTruthy();
    const table = within(article).getByRole("table", { name: "Rows left unreviewed" });
    expect(within(table).getByRole("rowheader", { name: "pending-11" })).toBeTruthy();
    expect(table.textContent).toContain("invalid cv evidence");
    expect(table.textContent).toContain(
      "A positive direct or transferable recommendation must cite master-CV evidence.",
    );
    expect(within(table).getByRole("rowheader", { name: "pending-12" })).toBeTruthy();
    expect(table.textContent).toContain("leading excerpt only");
    // A validation failure with no detail is a gap in the record, so it says so.
    const bare = within(table).getByRole("rowheader", { name: "pending-13" }).closest("tr");
    expect(bare?.textContent).toBe("pending-13malformed answerNo detail returned.");
    expect(within(article).queryByText("validation_failures")).toBeNull();
  });

  it("shows a running batch's step, its tallies so far, and the current item's outcome", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const batch = {
      ...running,
      id: "evaluate-batch-1",
      task_type: "discovery.evaluate_batch",
      pending_discovery_ids: ["pending-1", "pending-2"],
      progress: {
        job_posting_id: "job-7",
        decision: "ineligible",
        refused_by: "employer",
        reason: "Globex is on the excluded employer list.",
        evaluation_id: "evaluation-7",
        evaluation_version: 2,
        current: 7,
        total: 60,
        so_far: { evaluated: 4, refused: 1, skipped: 1, failed: 0 },
      },
    } satisfies Task;
    useTaskPage({ tasks: [batch] });
    renderTasks();

    const article = await screen.findByRole("article", { name: /^Evaluate discoveries created / });
    const step = within(article).getByText("Step 7 of 60");
    // The tallies read as one line under the step, in the completed sentence's shape.
    expect(step.nextElementSibling?.textContent).toBe(
      "So far: Evaluated 4, Refused 1, Skipped 1, Failed 0",
    );
    expect(within(article).queryByRole("table")).toBeNull();
    expect(
      within(article).getByText(
        "Current item - Decision: ineligible, Refused by: employer, " +
          "Reason: Globex is on the excluded employer list.",
      ),
    ).toBeTruthy();
    expect(within(article).queryByText(/Evaluation version/)).toBeNull();
    expect(within(article).getByText("evaluation_version")).toBeTruthy();
    expect(within(article).queryByText("so_far")).toBeNull();
  });

  it("labels a stopped batch's item line as its last item, not its result", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const stepped = {
      job_posting_id: "job-7",
      decision: "ineligible",
      refused_by: "credential",
      current: 7,
      total: 60,
      so_far: { evaluated: 4, refused: 1, skipped: 1, failed: 0 },
    };
    const cancelled = {
      ...running,
      id: "cancelled-batch",
      task_type: "discovery.evaluate_batch",
      status: "cancelled",
      pending_discovery_ids: ["pending-1"],
      progress: stepped,
      completed_at: "2026-08-06T12:00:03Z",
    } satisfies Task;
    const requeued = {
      ...cancelled,
      id: "requeued-batch",
      status: "queued",
      completed_at: null,
    } satisfies Task;
    useTaskPage({ tasks: [cancelled, requeued] });
    renderTasks();

    for (const reference of ["cancelle", "requeued"]) {
      const article = await screen.findByRole("article", {
        name: new RegExp(`^Evaluate discoveries created .*task ${reference}$`),
      });
      expect(within(article).getByText("Step 7 of 60")).toBeTruthy();
      expect(
        within(article).getByText("Last item - Decision: ineligible, Refused by: credential"),
      ).toBeTruthy();
      expect(within(article).queryByText(/^Current item/)).toBeNull();
    }
  });

  it("reads a failed re-read as a fallback to the stored posting text", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const batch = {
      ...running,
      id: "evaluate-batch-5",
      task_type: "discovery.evaluate_batch",
      job_posting_ids: ["job-1", "job-2"],
      progress: {
        refresh_fallback: "stored_posting_text",
        host: "jobs.example.test",
        error_type: "ConnectTimeout",
        current: 1,
        total: 2,
        so_far: { evaluated: 0, refused: 0, skipped: 0, failed: 0 },
      },
    } satisfies Task;
    useTaskPage({ tasks: [batch] });
    renderTasks();

    expect(
      await screen.findByText(
        "Current item - Re-read failed, used the stored posting text, " +
          "Host: jobs.example.test, Error type: ConnectTimeout",
      ),
    ).toBeTruthy();
  });

  it("keeps the stored-text fallback on a completed re-evaluation", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const rerun = {
      ...running,
      id: "reevaluate-1",
      task_type: "discovery.evaluate",
      status: "completed",
      job_posting_ids: ["job-1"],
      progress: {
        job_posting_id: "job-1",
        evaluation_id: "evaluation-2",
        evaluation_version: 2,
        decision: "apply_now",
        refresh_fallback: "stored_posting_text",
      },
      completed_at: "2026-08-06T12:00:03Z",
    } satisfies Task;
    useTaskPage({ tasks: [rerun] });
    renderTasks();

    const article = await screen.findByRole("article", {
      name: /^Re-evaluate a tracked posting created /,
    });
    expect(
      within(article).getByText(
        "Decision: apply now, Re-read failed, used the stored posting text",
      ),
    ).toBeTruthy();
  });

  it("names each skip reason of a posting refresh in words", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const refreshed = {
      ...running,
      id: "refresh-4",
      task_type: "posting.refresh_batch",
      status: "completed",
      progress: {
        selected: 2,
        skipped: [
          { job_posting_id: "job-3", reason: "read_today" },
          { job_posting_id: "job-5", reason: "read_today" },
        ],
      },
      completed_at: "2026-08-06T12:00:03Z",
    } satisfies Task;
    useTaskPage({ tasks: [refreshed] });
    renderTasks();

    const article = await screen.findByRole("article", {
      name: /^Refresh tracked postings created /,
    });
    expect(within(article).getByText("job-3: read today; job-5: read today")).toBeTruthy();
  });

  it("shows a URL, host, or free-text reason exactly as the task reported it", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const refresh = {
      ...running,
      id: "refresh-batch-1",
      task_type: "posting.refresh_batch",
      progress: {
        job_posting_id: "job-2",
        url: "https://jobs.example.test/careers/job_detail?req_id=R_1042",
        current: 2,
        total: 5,
        so_far: { refreshed: 1, failed: 0 },
      },
    } satisfies Task;
    const failedRead = {
      ...running,
      id: "refresh-failed-1",
      task_type: "discovery.evaluate",
      status: "completed",
      progress: {
        host: "careers.example_co.test",
        error_type: "IngestError",
        skipped: "unreadable",
      },
      completed_at: "2026-08-06T12:00:03Z",
    } satisfies Task;
    useTaskPage({ tasks: [refresh, failedRead] });
    renderTasks();

    expect(
      await screen.findByText(
        "Current item - Url: https://jobs.example.test/careers/job_detail?req_id=R_1042",
      ),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Host: careers.example_co.test, Error type: IngestError, Skipped: unreadable",
      ),
    ).toBeTruthy();
  });

  it("lists an evaluation batch's skipped rows one per row and leaves results uncounted", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const batch = {
      ...running,
      id: "evaluate-batch-2",
      task_type: "discovery.evaluate_batch",
      status: "completed",
      pending_discovery_ids: ["pending-8", "pending-9", "pending-10", "pending-11"],
      progress: {
        selected: 4,
        evaluated: 1,
        refused: 1,
        skipped: 2,
        skipped_rows: [
          {
            pending_discovery_id: "pending-9",
            skipped: "unreadable",
            detail: "The posting returned no readable text.",
          },
          { pending_discovery_id: "pending-10", skipped: "dismissed" },
        ],
        failed: 0,
        failures: [],
        results: [
          { job_posting_id: "job-8", evaluation_id: "evaluation-8", decision: "apply_now" },
          { job_posting_id: null, decision: "ineligible", refused_by: "employer" },
        ],
      },
      completed_at: "2026-08-06T12:00:03Z",
    } satisfies Task;
    useTaskPage({ tasks: [batch] });
    renderTasks();

    const article = await screen.findByRole("article", { name: /^Evaluate discoveries created / });
    expect(
      within(article).getByText("Selected 4, Evaluated 1, Refused 1, Skipped 2, Failed 0"),
    ).toBeTruthy();
    expect(within(article).queryByText(/Results/)).toBeNull();
    const skipped = within(article).getByRole("table", { name: "Skipped rows" });
    const row = within(skipped).getByRole("rowheader", { name: "pending-9" }).closest("tr");
    expect(row?.textContent).toBe("pending-9unreadableThe posting returned no readable text.");
    // A dismissal is its whole reason, so nothing reads as missing.
    const dismissed = within(skipped).getByRole("rowheader", { name: "pending-10" }).closest("tr");
    expect(dismissed?.textContent).toBe("pending-10dismissed");
    expect(within(article).queryByText("skipped_rows")).toBeNull();
    expect(within(article).queryByText("Full payload")).toBeNull();
  });

  it("names an evaluation batch over tracked postings for the postings it re-evaluates", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    const rerun = {
      ...running,
      id: "evaluate-batch-3",
      task_type: "discovery.evaluate_batch",
      job_posting_ids: ["job-1", "job-2"],
      progress: {},
    } satisfies Task;
    const discoveries = {
      ...rerun,
      id: "evaluate-batch-4",
      job_posting_ids: [],
      pending_discovery_ids: ["pending-1"],
    } satisfies Task;
    useTaskPage({ tasks: [rerun, discoveries] });
    renderTasks();

    const tracked = await screen.findByRole("article", {
      name: /^Re-evaluate tracked postings created .*task evaluate/,
    });
    expect(
      within(tracked).getByText(
        "Evaluate each selected tracked posting again against your current master CV and rules. " +
          "Costs are estimates and do not stop the task.",
      ),
    ).toBeTruthy();
    expect(screen.getByRole("article", { name: /^Evaluate discoveries created / })).toBeTruthy();
  });

  it("streams active progress and offers an idempotent free retry", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    vi.stubGlobal("EventSource", FakeEventSource);
    const retried: string[] = [];
    const billed = useTaskPage({
      tasks: [running, failed, paid],
      onWrite: (taskId) => {
        retried.push(taskId);
        return { ...failed, id: "retry-1", status: "queued" };
      },
    });
    renderTasks();

    expect(await screen.findByText("Activity")).toBeTruthy();
    expect(await screen.findByText("Source unavailable")).toBeTruthy();
    expect(screen.getByRole("list", { name: "Task item failures" }).textContent).toContain(
      "pending-closure-1: The publisher returned HTTP 410.",
    );
    // The Command Center names the same count the same way.
    expect(screen.getByText(/^Created .*, 3 attempts, task failed-1$/)).toBeTruthy();
    expect(screen.getByText("May spend").getAttribute("title")).toMatch(/scale with the number/);
    await waitFor(() =>
      expect(FakeEventSource.opened).toEqual([
        "/api/v1/tasks/running-1/events?workspace_id=workspace-tasks",
      ]),
    );
    fireEvent.click(
      screen.getByRole("button", { name: /^Retry Refresh tracked postings created / }),
    );
    await waitFor(() => expect(retried).toEqual(["failed-1"]));
    expect(billed).toEqual([]);
  });

  it("shows recorded billing and confirms before retrying a paid task", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    vi.stubGlobal("EventSource", FakeEventSource);
    let resolveBilling!: (value: TaskBilling) => void;
    const billingResponse = new Promise<TaskBilling>((resolve) => {
      resolveBilling = resolve;
    });
    const retried: string[] = [];
    const billed = useTaskPage({
      tasks: [failedPaid],
      billingFor: (taskId) => (taskId === "paid-failed-1" ? billingResponse : undefined),
      onWrite: (taskId) => {
        retried.push(taskId);
        return { ...failedPaid, id: "paid-retry-1", status: "queued" };
      },
    });
    renderTasks();

    const review = await screen.findByRole("button", {
      name: /^Review paid Search for new jobs created /,
    });
    expect(billed).toEqual([]);
    fireEvent.click(review);

    const billing = await screen.findByRole("status", {
      name: /^Billing for Search for new jobs created /,
    });
    const retry = screen.getByRole("button", {
      name: /^Retry paid Search for new jobs created /,
    });
    expect(billing.textContent).toBe("Checking recorded cost for this task.");
    expect((retry as HTMLButtonElement).disabled).toBe(true);

    resolveBilling({
      task_id: "paid-failed-1",
      status: "priced",
      call_count: 6,
      unpriced_call_count: 0,
      resent_on_standard_count: 0,
      actual_cost_usd: "1.036659",
    });
    await waitFor(() =>
      expect(billing.textContent).toBe(
        "Recorded cost for this task: $1.0367 across 6 model calls.",
      ),
    );
    expect((retry as HTMLButtonElement).disabled).toBe(false);
    expect(billing.compareDocumentPosition(retry) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    fireEvent.click(retry);
    const dialog = await screen.findByRole("dialog", {
      name: /^Retry Search for new jobs created .+\?$/,
    });
    expect(dialog.textContent).toContain(
      "Recorded cost for this task: $1.0367 across 6 model calls.",
    );
    expect(dialog.textContent).toContain(
      "Provider charges may include model tokens, search fees, and a web-content-retrieval charge.",
    );
    expect(dialog.textContent).toContain(
      "Configured provider: OpenAI. Search campaign: gpt-5.6-terra. Effort: low. Flex tier.",
    );
    expect(retried).toEqual([]);

    fireEvent.click(within(dialog).getByRole("button", { name: "Confirm paid retry" }));
    await waitFor(() => expect(retried).toEqual(["paid-failed-1"]));
    await screen.findByText("Task queued again.");
    expect(billed).toEqual(["paid-failed-1"]);
  });

  it("explains why a reviewed paid retry is disabled while provider choices load", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    vi.stubGlobal("EventSource", FakeEventSource);
    useTaskPage({
      tasks: [failedPaid],
      configurationPending: true,
      billingFor: () => ({
        task_id: "paid-failed-1",
        status: "priced",
        call_count: 6,
        unpriced_call_count: 0,
        resent_on_standard_count: 0,
        actual_cost_usd: "1.036659",
      }),
    });
    renderTasks();

    const review = await screen.findByRole("button", {
      name: /^Review paid Search for new jobs created /,
    });
    fireEvent.click(review);
    const taskLabel = `Search for new jobs created ${new Date(
      failedPaid.created_at,
    ).toLocaleString()}, task ${failedPaid.id.slice(0, 8)}`;
    const article = screen.getByRole("article", { name: taskLabel });
    expect(
      await within(article).findByRole("status", { name: `Provider status for ${taskLabel}` }),
    ).toHaveProperty("textContent", "Reading the provider model and effort before payment.");
    expect(within(article).getByRole("button", { name: `Retry paid ${taskLabel}` })).toHaveProperty(
      "disabled",
      true,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("says how many calls the Flex tier refused and standard answered", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    vi.stubGlobal("EventSource", FakeEventSource);
    useTaskPage({
      tasks: [failedPaid],
      billingFor: () => ({
        task_id: "paid-failed-1",
        status: "priced",
        call_count: 6,
        unpriced_call_count: 0,
        resent_on_standard_count: 1,
        actual_cost_usd: "1.036659",
      }),
    });
    renderTasks();

    fireEvent.click(
      await screen.findByRole("button", { name: /^Review paid Search for new jobs created / }),
    );

    const billing = await screen.findByRole("status", {
      name: /^Billing for Search for new jobs created /,
    });
    await waitFor(() =>
      expect(billing.textContent).toBe(
        "Recorded cost for this task: $1.0367 across 6 model calls. " +
          "1 call was resent on the standard tier after the Flex tier refused it.",
      ),
    );
  });

  it("reports unavailable retry-cost metadata instead of waiting forever", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    vi.stubGlobal("EventSource", FakeEventSource);
    const billed = useTaskPage({ tasks: [failed] });
    renderTasks();

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /^Retry Refresh tracked postings/ }),
      ).toHaveProperty("disabled", false),
    );
    expect(screen.queryByRole("button", { name: /^Checking retry cost/ })).toBeNull();
    expect(billed).toEqual([]);
  });

  it("distinguishes repeated paid attempts and recovers from a billing-read failure", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    vi.stubGlobal("EventSource", FakeEventSource);
    const earlier = {
      ...failedPaid,
      id: "earlier-paid-failed",
      created_at: "2026-08-06T11:00:00Z",
    } satisfies Task;
    const retried: string[] = [];
    const billed = useTaskPage({
      tasks: [failedPaid, earlier],
      // earlier-paid-failed has no answer, so its billing read fails.
      billingFor: (taskId) =>
        taskId === "paid-failed-1"
          ? {
              task_id: "paid-failed-1",
              status: "no_model_calls",
              call_count: 0,
              unpriced_call_count: 0,
              resent_on_standard_count: 0,
              actual_cost_usd: null,
            }
          : undefined,
      onWrite: (taskId) => {
        retried.push(taskId);
        return { ...earlier, id: "paid-retry-2", status: "queued" };
      },
    });
    renderTasks();

    const currentLabel = `Search for new jobs created ${new Date(
      failedPaid.created_at,
    ).toLocaleString()}, task ${failedPaid.id.slice(0, 8)}`;
    const earlierLabel = `Search for new jobs created ${new Date(
      earlier.created_at,
    ).toLocaleString()}, task ${earlier.id.slice(0, 8)}`;
    const currentArticle = await screen.findByRole("article", { name: currentLabel });
    const earlierArticle = screen.getByRole("article", { name: earlierLabel });
    const currentReview = within(currentArticle).getByRole("button", {
      name: `Review paid ${currentLabel}`,
    });
    const earlierReview = within(earlierArticle).getByRole("button", {
      name: `Review paid ${earlierLabel}`,
    });
    expect(billed).toEqual([]);

    fireEvent.click(currentReview);
    fireEvent.click(earlierReview);
    // The earlier billing query fails and retries once after react-query's
    // default 1000ms backoff before its warning shows; drive that wait with
    // fake timers before either status settles.
    vi.useFakeTimers();
    try {
      await vi.advanceTimersByTimeAsync(1_100);
    } finally {
      vi.useRealTimers();
    }
    const currentBilling = await within(currentArticle).findByRole("status", {
      name: `Billing for ${currentLabel}`,
    });
    expect(currentBilling).toHaveProperty(
      "textContent",
      "No model charge was recorded for this task.",
    );
    const failedBilling = await within(earlierArticle).findByRole("status", {
      name: `Billing for ${earlierLabel}`,
    });
    expect(failedBilling).toHaveProperty(
      "textContent",
      "Recorded cost for this task is unavailable; review Spend before retrying.",
    );
    const earlierRetry = within(earlierArticle).getByRole("button", {
      name: `Retry paid ${earlierLabel}`,
    });
    expect(earlierRetry).toHaveProperty("disabled", false);

    fireEvent.click(earlierRetry);
    const dialog = await screen.findByRole("dialog", { name: `Retry ${earlierLabel}?` });
    expect(dialog.textContent).toContain(
      "Recorded cost for this task is unavailable; review Spend before retrying.",
    );
    expect(retried).toEqual([]);
  });

  it("requires confirmation before requesting cancellation", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-tasks");
    vi.stubGlobal("EventSource", FakeEventSource);
    const otherRunning = { ...running, id: "other-running" } satisfies Task;
    const cancelled: string[] = [];
    useTaskPage({
      tasks: () => [
        running,
        {
          ...otherRunning,
          cancellation_requested_at: cancelled.length ? "2026-08-31T20:30:00Z" : null,
        },
      ],
      onWrite: (taskId) => {
        cancelled.push(taskId);
        return {
          ...(taskId === "other-running" ? otherRunning : running),
          cancellation_requested_at: "2026-08-31T20:30:00Z",
        };
      },
    });
    renderTasks();

    const targetLabel = `Refresh a posting created ${new Date(
      otherRunning.created_at,
    ).toLocaleString()}, task ${otherRunning.id.slice(0, 8)}`;
    fireEvent.click(await screen.findByRole("button", { name: `Cancel ${targetLabel}` }));
    const dialog = screen.getByRole("dialog", { name: `Cancel ${targetLabel}?` });
    expect(dialog.textContent).toContain(`This cancels ${targetLabel}.`);
    expect(cancelled).toEqual([]);
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel task" }));
    await waitFor(() => expect(cancelled).toEqual(["other-running"]));
    const targetArticle = screen.getByRole("article", { name: targetLabel });
    expect(within(targetArticle).getByRole("status").textContent).toContain(
      "Cancellation requested",
    );
    expect(
      within(targetArticle).queryByRole("button", { name: `Cancel ${targetLabel}` }),
    ).toBeNull();
  });
});
