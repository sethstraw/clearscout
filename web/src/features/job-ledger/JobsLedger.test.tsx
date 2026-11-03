import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw/http";
import { describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";

import type {
  Job,
  JobDetail,
  JobPage,
  RunCostHistory,
  SavedView,
  Task,
} from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { evaluationPreflightHandler, taskTypesHandler } from "../../test/taskTypes";
import { searchParametersState } from "../search-parameters/searchParameterFixtures";
import { jobDetail } from "../job-detail/jobDetailFixtures";
import { jobSummary } from "./jobFixtures";
import { JobsPage } from "./JobsLedger";

const API = "*/api/v1";

const jobs = [
  jobSummary({
    job_posting_id: "job-1",
    employer: "Beta",
    title: "Director",
    fit_score: 70,
    compensation_score: 60,
    eligibility_score: 50,
    evidence_score: 40,
    opportunity_score: 30,
    overall_score: 55,
  }),
  jobSummary({
    job_posting_id: "job-2",
    employer: "Alpha",
    title: "Manager",
    fit_score: 90,
    compensation_score: 80,
    eligibility_score: 70,
    evidence_score: 60,
    opportunity_score: 50,
    overall_score: 75,
  }),
];

/** Two rows scored under different policies, plus one from before tracking. */
const mixedPolicyJobs = [
  { ...jobs[0]!, score_policy_name: "ClearScout default", score_policy_version: 1 },
  { ...jobs[1]!, score_policy_name: "ClearScout default", score_policy_version: 2 },
  jobSummary({
    job_posting_id: "job-3",
    employer: "Gamma",
    title: "Lead",
    fit_score: 50,
    compensation_score: 50,
    eligibility_score: 50,
    evidence_score: 50,
    opportunity_score: 50,
    overall_score: 50,
    score_policy_name: null,
    score_policy_version: null,
  }),
];

const partiallyScoredJob = {
  ...jobs[0]!,
  overall_score: null,
  overall_score_lower_bound: 42,
  unscored_weighted_component_count: 1,
  score_policy_name: "ClearScout default",
  score_policy_version: 1,
} satisfies Job;

/** One saved view written the way the workspace form stores them: free-form
 *  JSON, including keys this ledger has no filter for. */
const savedView = {
  id: "view-1",
  workspace_id: "jobs-view-test",
  resource: "jobs",
  name: "Monitors in Canada",
  created_at: "2026-08-06T12:00:00Z",
  archived_at: null,
  configuration: {
    filters: {
      decision: "monitor",
      place: "canada_remote",
      employer: ["Alpha", "Beta"],
      include_archived: true,
      mood: "hopeful",
    },
    sort: { key: "fit_score", descending: false },
  },
} satisfies SavedView;

const comparisonCostHistory = [
  {
    task_type: "workflow.batch",
    currency: "USD",
    unit: "run",
    measured_runs: 4,
    unpriced_runs: 0,
    replayed_runs: 0,
    per_unit_low: "0.20",
    per_unit_median: "0.30",
    per_unit_high: "0.50",
  },
] satisfies RunCostHistory[];

/** One posting per unit for the batch, priced apart from a single evaluation. */
const evaluationCostHistory = [
  {
    task_type: "discovery.evaluate",
    currency: "USD",
    unit: "posting",
    measured_runs: 3,
    unpriced_runs: 0,
    replayed_runs: 0,
    per_unit_low: "0.05",
    per_unit_median: "0.10",
    per_unit_high: "0.20",
  },
  {
    task_type: "discovery.evaluate_batch",
    currency: "USD",
    unit: "posting",
    measured_runs: 3,
    unpriced_runs: 0,
    replayed_runs: 0,
    per_unit_low: "0.30",
    per_unit_median: "0.40",
    per_unit_high: "0.60",
  },
] satisfies RunCostHistory[];

const ARCHIVED = {
  job_posting_ids: ["job-1", "job-2"],
  requested_count: 2,
  archived_count: 2,
  already_archived_count: 0,
};

function ledgerTask(overrides: Partial<Task> = {}): Task {
  return {
    id: "task-1",
    workspace_id: "workspace-jobs",
    task_type: "workflow.batch",
    status: "queued",
    attempt_count: 0,
    job_posting_ids: [],
    pending_discovery_ids: [],
    source_ids: [],
    progress: {},
    cancellation_requested_at: null,
    error_category: null,
    error_message: null,
    created_at: "2026-08-06T12:00:00Z",
    started_at: null,
    updated_at: "2026-08-06T12:00:00Z",
    completed_at: null,
    ...overrides,
  };
}

interface Recorded {
  /** The query string of every ledger read, so a filter or sort change is visible. */
  queries: string[];
  posts: { path: string; body: unknown }[];
}

/** Routes by path so the ledger, its saved views, and the search parameters
 *  can answer differently in one test. */
function installApi(
  items: Job[],
  views: SavedView[] = [],
  total = items.length,
  costHistory: RunCostHistory[] = [],
  task: Task = ledgerTask(),
): Recorded {
  const recorded: Recorded = { queries: [], posts: [] };
  const write = (request: Request, body: unknown) => {
    const url = new URL(request.url);
    recorded.posts.push({ path: `${url.pathname}${url.search}`, body });
  };
  server.use(
    evaluationPreflightHandler(),
    taskTypesHandler(),
    http.post(`${API}/jobs/archive`, async ({ request }) => {
      write(request, await request.json());
      return HttpResponse.json(ARCHIVED);
    }),
    http.post(`${API}/tasks`, async ({ request }) => {
      write(request, await request.json());
      return HttpResponse.json(task);
    }),
    http.get(`${API}/tasks/:taskId`, () => HttpResponse.json(task)),
    http.get(`${API}/saved-views`, () => HttpResponse.json(views)),
    http.get(`${API}/tasks`, () => HttpResponse.json([])),
    http.get(`${API}/job-search-parameters`, () => HttpResponse.json(searchParametersState())),
    http.get(`${API}/workspaces/:workspaceId/spend-history`, () => HttpResponse.json(costHistory)),
    http.get(`${API}/jobs`, ({ request }) => {
      const url = new URL(request.url);
      recorded.queries.push(url.search);
      const offset = Number(url.searchParams.get("offset") ?? 0);
      const limit = Number(url.searchParams.get("limit") ?? 50);
      const counts = new Map<string, number>();
      for (const item of items) counts.set(item.employer, (counts.get(item.employer) ?? 0) + 1);
      return HttpResponse.json({
        items: items.slice(offset, offset + limit),
        total,
        offset,
        limit,
        employers: [...counts].map(([name, count]) => ({ name, count })),
      } satisfies JobPage);
    }),
  );
  return recorded;
}

describe("JobsPage", () => {
  it("omits absent closing dates and links the role to its full job page", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-closing-empty-test");
    installApi([
      jobSummary({
        job_posting_id: "job-date",
        employer: "Example Health",
        title: "Product Lead",
        publisher_closing_date: null,
        closing_date_override: null,
      }),
    ]);
    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    const role = await screen.findByRole("link", { name: "Product Lead" });
    expect(role.getAttribute("href")).toBe("/jobs/job-date");
    const roleCell = role.closest("td")!;
    expect(within(roleCell).queryByRole("group", { name: /Closing dates/ })).toBeNull();
    expect(within(roleCell).queryByText("Not stated")).toBeNull();
    expect(within(roleCell).queryByText("Edit closing date")).toBeNull();
  });

  it("shows a compact publisher closing date without evidence or an editor in the role cell", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-closing-test");
    const items = [
      jobSummary({
        job_posting_id: "job-date",
        employer: "Example Health",
        title: "Product Lead",
        posting_status: "active",
        publisher_closing_date: {
          needs_clarification: false,
          normalized_value: "2999-11-12",
          precision: "date",
          source_url: "https://example.org/job-date",
          statements: [
            { basis: "posting_text", wording: "Closes November 12, 2999", clarification: "" },
          ],
        },
      }),
    ];
    const recorded = installApi(items);
    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    const dates = await screen.findByRole("group", {
      name: "Closing dates for Example Health Product Lead",
    });
    const roleCell = screen.getByRole("link", { name: "Product Lead" }).closest("td")!;
    expect(roleCell.contains(dates)).toBe(true);
    expect(within(dates).getByText("Publisher closing date: 2999-11-12")).toBeTruthy();
    expect(within(dates).queryByText("Publisher date evidence")).toBeNull();
    expect(within(dates).queryByText("Edit closing date")).toBeNull();
    expect(within(dates).queryByLabelText("Your closing date")).toBeNull();
    expect(within(dates).queryByText("Closing date passed; verify availability.")).toBeNull();
    expect(items[0]!.posting_status).toBe("active");
    expect(recorded.posts).toEqual([]);
  });

  it("shows the compact personal closing date and retains both passed-date warnings", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-closing-override-test");
    installApi([
      jobSummary({
        job_posting_id: "job-date",
        employer: "Example Health",
        title: "Product Lead",
        closing_date_override: "2000-11-09",
        publisher_closing_date: {
          needs_clarification: false,
          normalized_value: "1999-11-12",
          precision: "date",
          source_url: "https://example.org/job-date",
          statements: [
            { basis: "posting_text", wording: "Closes November 12, 1999", clarification: "" },
          ],
        },
      }),
    ]);
    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    const dates = await screen.findByRole("group", {
      name: "Closing dates for Example Health Product Lead",
    });
    expect(within(dates).getByText("Your closing date: 2000-11-09")).toBeTruthy();
    expect(within(dates).queryByText(/Publisher closing date/)).toBeNull();
    expect(within(dates).getByText("Closing date passed; verify availability.")).toBeTruthy();
    expect(
      within(dates).getByText("Your closing date has passed. It is a tracking reminder."),
    ).toBeTruthy();
    expect(within(dates).queryByText("Edit closing date")).toBeNull();
  });

  it("shows comparison fields and sorts on the server", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-sort-test");
    const recorded = installApi(jobs);

    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    const table = await screen.findByRole("table");
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((header) => header.textContent),
    ).toEqual([
      "",
      "Employer",
      "Role",
      "Decision / review",
      "Your priority",
      "Fit",
      "Comp",
      "Recorded score",
      "Found",
      "Evaluated",
      "Application",
    ]);

    fireEvent.click(
      within(table).getByRole("button", { name: "Sort by Recorded score ascending" }),
    );
    await waitFor(() =>
      expect(
        recorded.queries.some(
          (query) => query.includes("sort=recorded_score") && query.includes("descending=false"),
        ),
      ).toBe(true),
    );
    await waitFor(() =>
      expect(
        screen
          .getByRole("button", { name: "Sort by Recorded score descending" })
          .closest("th")
          ?.getAttribute("aria-sort"),
      ).toBe("ascending"),
    );
    expect(screen.getByRole("option", { name: "Closed" })).toBeTruthy();
    expect(screen.queryByRole("option", { name: "Filled" })).toBeNull();
    expect(screen.queryByRole("option", { name: "Unverified" })).toBeNull();
  });

  it("keeps ledger columns aligned and marks a recorded priority", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-priority-test");
    const recorded = installApi([
      jobSummary({
        job_posting_id: "job-priority",
        employer: "Example Health",
        title: "Senior Product Manager, Clinical Data Platform",
        manual_priority: "high",
        recorded_fit_score: 77,
        recorded_compensation_score: 68,
        recorded_overall_score: 79,
        recorded_compensation: "Synthetic pay range",
        next_action: "Follow up",
        fit_score: 90,
        compensation_score: 80,
      }),
    ]);

    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );

    const table = await screen.findByRole("table");
    expect(table.querySelectorAll("colgroup col").length).toBe(
      within(table).getAllByRole("columnheader").length,
    );
    expect(
      within(table).getByRole("link", { name: "Senior Product Manager, Clinical Data Platform" }),
    ).toBeTruthy();
    const row = within(table).getAllByRole("row")[1];
    expect(row).toBeDefined();
    const priority = within(row!).getByRole("combobox", {
      name: "Set priority for Example Health Senior Product Manager, Clinical Data Platform",
    });
    expect((priority as HTMLSelectElement).value).toBe("high");
    expect(priority.className).toContain("status--tone-danger");
    expect(within(table).getByRole("columnheader", { name: "Fit" })).toBeTruthy();
    expect(within(table).getByRole("columnheader", { name: "Comp" })).toBeTruthy();
    expect(within(row!).queryByText("Posted pay: Synthetic pay range")).toBeNull();
    expect(within(row!).queryByText("Follow up")).toBeNull();
    expect(within(row!).getByText("77")).toBeTruthy();
    expect(within(row!).getByText("68")).toBeTruthy();
    expect(within(row!).getByText("79")).toBeTruthy();
    expect(within(row!).queryByText("90")).toBeNull();
    expect(within(row!).queryByText("80")).toBeNull();
    fireEvent.click(within(table).getByRole("button", { name: "Sort by Fit ascending" }));
    await waitFor(() =>
      expect(
        recorded.queries.some(
          (query) => query.includes("sort=fit_score") && query.includes("descending=false"),
        ),
      ).toBe(true),
    );
  });

  it("edits priority from the latest complete review and records application status", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-inline-edit-test");
    const items = [
      jobSummary({
        job_posting_id: "job-inline",
        employer: "Example Health",
        title: "Product Lead",
        manual_priority: "high",
        application_status: "not_applied",
      }),
    ];
    installApi(items);
    const latest = jobDetail({
      job_posting_id: "job-inline",
      manual_priority: "high",
      manual_fit: "strong",
      manual_note: "Retain this note",
      recorded_fit_score: 81,
      recorded_compensation_score: 60,
      recorded_overall_score: 75,
      recorded_compensation: "Source-backed pay fact",
      recorded_work_model: "Remote",
      recorded_eligibility: "Eligible",
      next_action: "Follow up",
      recorded_deadline: "After the panel, before 17:00 MDT",
      closing_date_override: "2026-11-09",
      recorded_source: "Source record",
    });
    let reviewBody: unknown;
    let eventBody: unknown;
    server.use(
      http.get(`${API}/jobs/job-inline`, () => HttpResponse.json(latest)),
      http.put(`${API}/jobs/job-inline/manual-review`, async ({ request }) => {
        reviewBody = await request.json();
        items[0] = { ...items[0]!, manual_priority: "medium" };
        return HttpResponse.json({ ...latest, manual_priority: "medium" });
      }),
      http.post(`${API}/jobs/job-inline/application-events`, async ({ request }) => {
        eventBody = await request.json();
        items[0] = { ...items[0]!, application_status: "applied", application_event_count: 1 };
        return HttpResponse.json({ status: "applied" });
      }),
    );

    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    const priority = await screen.findByRole("combobox", {
      name: "Set priority for Example Health Product Lead",
    });
    fireEvent.change(priority, { target: { value: "medium" } });
    await waitFor(() => expect(reviewBody).toBeDefined());
    expect(reviewBody).toEqual({
      priority: "medium",
      fit: "strong",
      note: "Retain this note",
      recorded_fit_score: 81,
      recorded_compensation_score: 60,
      recorded_overall_score: 75,
      recorded_compensation: "Source-backed pay fact",
      recorded_work_model: "Remote",
      recorded_eligibility: "Eligible",
      next_action: "Follow up",
      recorded_deadline: "After the panel, before 17:00 MDT",
      recorded_source: "Source record",
    });
    await waitFor(() => expect((priority as HTMLSelectElement).value).toBe("medium"));

    const application = screen.getByRole("combobox", {
      name: "Set application status for Example Health Product Lead",
    });
    fireEvent.change(application, { target: { value: "applied" } });
    await waitFor(() => expect(eventBody).toBeDefined());
    const event = eventBody as { status: string; occurred_at: string; notes: string };
    expect(event.status).toBe("applied");
    expect(event.notes).toBe("");
    expect(new Date(event.occurred_at).toString()).not.toBe("Invalid Date");
    await waitFor(() => expect((application as HTMLSelectElement).value).toBe("applied"));
    expect(screen.queryByRole("option", { name: "Package ready" })).toBeNull();
  });

  it("keeps the old priority through a pending and failed clear", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-inline-error-test");
    installApi([
      jobSummary({
        job_posting_id: "job-error",
        employer: "Example Health",
        title: "Product Lead",
        manual_priority: "high",
      }),
    ]);
    let resolveDetail!: (detail: JobDetail) => void;
    const detailRead = new Promise<JobDetail>((resolve) => {
      resolveDetail = resolve;
    });
    let reads = 0;
    let writes = 0;
    server.use(
      http.get(`${API}/jobs/job-error`, () => {
        reads += 1;
        return detailRead.then((detail) => HttpResponse.json(detail));
      }),
      http.put(`${API}/jobs/job-error/manual-review`, async ({ request }) => {
        writes += 1;
        expect(((await request.json()) as { priority: string | null }).priority).toBeNull();
        return HttpResponse.json({ detail: "Review could not be saved." }, { status: 500 });
      }),
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    const priority = await screen.findByRole("combobox", {
      name: "Set priority for Example Health Product Lead",
    });
    fireEvent.change(priority, { target: { value: "high" } });
    expect(reads).toBe(0);
    fireEvent.change(priority, { target: { value: "" } });
    await waitFor(() => expect(reads).toBe(1));
    expect(priority).toHaveProperty("disabled", true);
    expect(writes).toBe(0);
    resolveDetail(jobDetail({ job_posting_id: "job-error", manual_priority: "high" }));
    await waitFor(() => expect(writes).toBe(1));
    await waitFor(() => expect(priority).toHaveProperty("disabled", false));
    expect(priority).toHaveProperty("value", "high");
  });

  it("keeps archived application history read only in the ledger", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-archived-edit-test");
    installApi([
      jobSummary({
        job_posting_id: "job-archived",
        employer: "Example Research",
        title: "Data Lead",
        archived_at: "2026-10-01T12:00:00Z",
      }),
    ]);
    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    fireEvent.change(screen.getByLabelText("Archived jobs"), { target: { value: "include" } });
    expect(await screen.findByText("Not applied")).toBeTruthy();
    expect(
      screen.queryByRole("combobox", {
        name: "Set application status for Example Research Data Lead",
      }),
    ).toBeNull();
    expect(
      screen.getByRole("combobox", {
        name: "Set priority for Example Research Data Lead",
      }),
    ).toHaveProperty("disabled", false);
  });

  it("shows evaluation freshness beside the decision without repeating an unevaluated state", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-review-test");
    installApi([
      jobSummary({
        job_posting_id: "job-needs-review",
        employer: "Example Health",
        title: "Platform Lead",
        evaluation_inputs: [
          { name: "posting", status: "changed", recorded: "old", current: "new" },
        ],
      }),
      jobSummary({
        job_posting_id: "job-not-evaluated",
        employer: "Example Research",
        title: "Data Analyst",
        evaluation_id: null,
        decision: null,
        evaluated_at: null,
      }),
    ]);

    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );

    const table = await screen.findByRole("table");
    const reviewed = within(table).getByRole("link", { name: "Platform Lead" }).closest("tr");
    expect(reviewed).toBeTruthy();
    const decision = within(reviewed!).getAllByRole("cell")[3];
    expect(decision).toBeDefined();
    expect(within(decision!).getByText("Investigate")).toBeTruthy();
    expect(
      within(decision!).getByRole("link", { name: /Review needed: posting changed/ }),
    ).toBeTruthy();
    const unevaluated = within(table).getByRole("link", { name: "Data Analyst" }).closest("tr");
    expect(unevaluated).toBeTruthy();
    expect(within(unevaluated!).getAllByText("Not evaluated")).toHaveLength(2);
    expect(within(unevaluated!).queryByText("No evaluation")).toBeNull();
  });

  it("labels a conservative recorded score without making it look like upside", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-partial-score-test");
    installApi([partiallyScoredJob]);

    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByRole("table");

    const score = screen.getByText("Min. 42");
    expect(score.className).toContain("score-cell__lower-bound");
    expect(score.getAttribute("aria-label")).toContain("not a final total");
    expect(score.getAttribute("title")).toContain("1 weighted category is");
    expect(document.getElementById("recorded-score-guide")?.textContent).toContain(
      "Min. is a conservative evaluation score",
    );
    expect(screen.queryByText("1 unscored")).toBeNull();
  });

  it("confirms recorded provider charges before comparing selected jobs", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-compare-test");
    const recorded = installApi(jobs, [], jobs.length, comparisonCostHistory);

    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByRole("table");
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Beta Director" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha Manager" }));
    // The dialog keeps the cost line it was opened with, so the read must have landed.
    await screen.findByText(/about \$0.30 per run/);
    fireEvent.click(screen.getByRole("button", { name: "Compare selected (2)" }));

    expect(screen.getByRole("dialog", { name: "Compare 2 jobs?" })).toBeTruthy();
    expect(
      within(screen.getByRole("dialog")).getByText(
        /About \$0.30 for 1 run \(range \$0.20 to \$0.50, from 4 runs\)\./,
      ),
    ).toBeTruthy();
    expect(recorded.posts).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Compare jobs" }));

    await waitFor(() => expect(recorded.posts).toHaveLength(1));
  });

  it("re-evaluates the exact selected postings in one batched paid task", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-reevaluate-test");
    const finished = ledgerTask({
      task_type: "discovery.evaluate_batch",
      status: "completed",
      progress: { selected: 2, evaluated: 1, refused: 1, skipped: 0, failed: 0, failures: [] },
    });
    const recorded = installApi(jobs, [], jobs.length, [], finished);

    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByRole("table");
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Beta Director" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha Manager" }));
    fireEvent.click(screen.getByRole("button", { name: "Re-evaluate selected (2)" }));

    expect(
      await screen.findByRole("dialog", { name: "Re-evaluate 2 tracked postings?" }),
    ).toBeTruthy();
    expect(recorded.posts).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Re-evaluate 2" }));

    await waitFor(() => expect(recorded.posts).toHaveLength(1));
    expect(recorded.posts[0]?.body).toMatchObject({
      task_type: "discovery.evaluate_batch",
      payload: { job_posting_ids: ["job-1", "job-2"] },
    });
    expect(
      await screen.findByText(
        "Re-evaluated 1 of 2 selected postings; refused by rule 1; failed 0.",
      ),
    ).toBeTruthy();
  });

  it("prices a batch re-evaluation from the batch task's own history", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-reevaluate-price-test");
    installApi(jobs, [], jobs.length, evaluationCostHistory);

    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByRole("table");
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Beta Director" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha Manager" }));

    const button = await screen.findByRole("button", { name: "Re-evaluate selected (2)" });
    await waitFor(() => expect(button.textContent).toContain("Past runs: about $0.80 total"));
    expect(button.textContent).toContain("Paid");
  });

  it("folds the rare filters under Advanced and keeps the page to one line of context", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-advanced-test");
    installApi(jobs, [savedView]);

    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByRole("table");

    expect(screen.getByRole("heading", { level: 1, name: "Jobs" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Help: Jobs" })).toBeTruthy();
    const advanced = screen.getByText("Advanced").closest("details");
    expect(advanced?.open).toBe(false);
    for (const label of ["Saved view", "Archived jobs", "Evidence integrity"]) {
      expect(advanced?.contains(screen.getByLabelText(label))).toBe(true);
    }
    expect(advanced?.contains(screen.getByLabelText("Decision"))).toBe(false);
  });

  it("names an Advanced filter outside the closed fold and clears it", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-advanced-active-test");
    installApi(jobs, [savedView]);

    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByRole("table");
    expect(screen.queryByText(/Advanced filter on/)).toBeNull();

    fireEvent.change(screen.getByLabelText("Evidence integrity"), {
      target: { value: "incomplete_only" },
    });
    fireEvent.change(screen.getByLabelText("Archived jobs"), { target: { value: "include" } });

    const note = await screen.findByText(/Advanced filter on/);
    expect(note.textContent).toContain("archived jobs included, evidence gaps only");
    expect(screen.getByText("Advanced").closest("details")?.contains(note)).toBe(false);
    fireEvent.click(within(note).getByRole("button", { name: "Clear advanced filters" }));

    await waitFor(() => expect(screen.queryByText(/Advanced filter on/)).toBeNull());
    expect(screen.getByLabelText<HTMLSelectElement>("Evidence integrity").value).toBe("");
    expect(screen.getByLabelText<HTMLSelectElement>("Archived jobs").value).toBe("exclude");
  });

  it("forces an explicit selected-posting refresh", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-refresh-test");
    const recorded = installApi(jobs);

    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByRole("table");
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Beta Director" }));
    fireEvent.click(screen.getByRole("button", { name: "Refresh selected (1)" }));

    const dialog = await screen.findByRole("dialog", { name: "Refresh this tracked posting?" });
    expect(dialog.textContent).toContain("Re-reads this posting from its publisher.");
    expect(dialog.textContent).not.toContain("spent");
    expect(screen.queryByText("No provider charge.")).toBeNull();
    expect(recorded.posts).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole("button", { name: "Refresh" }));

    await waitFor(() => expect(recorded.posts).toHaveLength(1));
    expect(recorded.posts[0]?.body).toMatchObject({
      payload: { job_posting_ids: ["job-1"], force: true },
    });
  });

  it("reports successful and failed postings from one completed refresh batch", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-partial-refresh-test");
    const task = ledgerTask({
      id: "refresh-partial",
      workspace_id: "jobs-partial-refresh-test",
      task_type: "posting.refresh_batch",
      status: "completed",
      progress: {
        refreshed: 1,
        unchanged: 1,
        changed: 0,
        withdrawn_as_closed: 0,
        unverified: 0,
        skipped: [],
        failed: 1,
        failures: [
          {
            job_posting_id: "job-2",
            error_category: "IngestError",
            error_message: "The publisher returned no readable text.",
          },
        ],
      },
    });
    installApi(jobs, [], jobs.length, [], task);

    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByRole("table");
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha Manager" }));
    fireEvent.click(screen.getByRole("button", { name: "Refresh selected (1)" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Refresh" }),
    );

    expect(await screen.findByText("Re-read 1 posting: 1 unchanged. 1 failed.")).toBeTruthy();
    expect(screen.getByRole("list", { name: "Refresh failures" }).textContent).toContain(
      "job-2: The publisher returned no readable text.",
    );
  });

  it("spells out an unconfirmed refresh instead of leaving the reader the phrase", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-unverified-refresh-test");
    const task = ledgerTask({
      id: "refresh-unverified",
      workspace_id: "jobs-unverified-refresh-test",
      task_type: "posting.refresh_batch",
      status: "completed",
      progress: {
        refreshed: 3,
        unchanged: 1,
        changed: 1,
        withdrawn_as_closed: 1,
        unverified: 2,
        skipped: ["job-9"],
        failed: 0,
        failures: [],
      },
    });
    installApi(jobs, [], jobs.length, [], task);

    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByRole("table");
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha Manager" }));
    fireEvent.click(screen.getByRole("button", { name: "Refresh selected (1)" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Refresh" }),
    );

    expect(
      await screen.findByText(
        "Re-read 3 postings: 1 unchanged, 1 changed, 1 closed. 1 skipped. " +
          "The publisher answered but would not confirm 2.",
      ),
    ).toBeTruthy();
  });

  it("confirms and bulk archives the exact selected postings", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-archive-test");
    const recorded = installApi(jobs);

    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByRole("table");
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Beta Director" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Alpha Manager" }));
    fireEvent.click(screen.getByRole("button", { name: "Archive selected (2)" }));

    expect(screen.getByRole("dialog", { name: "Archive 2 selected jobs?" })).toBeTruthy();
    expect(
      screen.getByText(/evaluations, documents, and application history are preserved/),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Archive selected" }));

    await waitFor(() => expect(recorded.posts).toHaveLength(1));
    expect(recorded.posts[0]).toEqual({
      path: "/api/v1/jobs/archive?workspace_id=jobs-archive-test",
      body: { job_posting_ids: ["job-1", "job-2"] },
    });
    expect(await screen.findByText("Archived 2 selected jobs.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Archive selected (0)" })).toBeTruthy();
  });

  it("keeps policy context accessible and warns when recorded scores differ", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-policy-test");
    installApi(mixedPolicyJobs);

    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    const table = await screen.findByRole("table");

    expect(within(table).getByTitle(/ClearScout default v1/).textContent).toBe("55");
    expect(within(table).getByTitle(/ClearScout default v2/).textContent).toBe("75");
    expect(within(table).getByTitle(/weights behind this total are unknown/).textContent).toBe(
      "50",
    );

    expect(screen.getByRole("status").textContent).toContain("not directly comparable");
  });

  it("marks a closed posting beside its role and names the matched place and its countries", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-place-test");
    installApi([
      { ...jobs[0]!, posting_status: "closed", place: "Toronto, ON", countries: ["Canada"] },
      { ...jobs[1]!, posting_status: "active", place: null },
    ]);

    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    const table = await screen.findByRole("table");
    const closed = within(table).getByRole("link", { name: "Director" });
    expect(closed.getAttribute("title")).toBe("Director");
    const closedRole = closed.closest("td");
    if (!closedRole) throw new Error("Expected the closed role cell.");
    expect(within(closedRole).getByText("Posting closed").className).toContain(
      "status--tone-muted",
    );
    expect(within(closedRole).getByText("Matched saved location: Toronto, ON")).toBeTruthy();
    expect(within(closedRole).getByText("Canada")).toBeTruthy();

    const openRole = within(table).getByRole("link", { name: "Manager" }).closest("td");
    if (!openRole) throw new Error("Expected the open role cell.");
    expect(within(openRole).queryByText("Posting closed")).toBeNull();
    expect(within(openRole).queryByText(/Matched saved location/)).toBeNull();
    expect(within(table).queryByText("Active")).toBeNull();
  });

  it("asks for archived jobs on request and marks them in the ledger", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-archived-test");
    const recorded = installApi([{ ...jobs[0]!, archived_at: "2026-08-06T09:00:00Z" }]);

    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByRole("table");
    expect(recorded.queries.every((query) => !query.includes("include_archived"))).toBe(true);

    fireEvent.change(screen.getByLabelText("Archived jobs"), { target: { value: "include" } });

    await waitFor(() =>
      expect(recorded.queries.some((query) => query.includes("include_archived=true"))).toBe(true),
    );
    expect((await screen.findAllByText("Archived")).length).toBeGreaterThan(0);
  });

  it("applies the filters and sort a saved view stores", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-view-test");
    const recorded = installApi(jobs, [savedView], 100);

    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByRole("table");
    fireEvent.change(screen.getByLabelText("Saved view"), { target: { value: "view-1" } });

    await waitFor(() =>
      expect(
        recorded.queries.some(
          (query) =>
            query.includes("decision=monitor") &&
            query.includes("place=canada_remote") &&
            query.includes("employer=Alpha&employer=Beta") &&
            query.includes("include_archived=true") &&
            query.includes("sort=fit_score") &&
            query.includes("descending=false") &&
            !query.includes("posting_status"),
        ),
      ).toBe(true),
    );
  });

  it("narrows to the chosen employers with one parameter each and drops the row selection", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-employer-test");
    const recorded = installApi(jobs);

    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByRole("table");
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Beta Director" }));
    expect(screen.getByRole("button", { name: "Archive selected (1)" })).toBeTruthy();

    const employers = screen.getByRole("group", { name: "Employer" });
    fireEvent.click(within(employers).getByRole("checkbox", { name: "Beta (1)" }));
    await waitFor(() =>
      expect(recorded.queries.some((query) => query.includes("employer=Beta"))).toBe(true),
    );
    expect(screen.getByRole("button", { name: "Archive selected (0)" })).toBeTruthy();

    fireEvent.click(await within(employers).findByRole("checkbox", { name: "Alpha (1)" }));
    await waitFor(() =>
      expect(recorded.queries.some((query) => query.includes("employer=Beta&employer=Alpha"))).toBe(
        true,
      ),
    );
  });

  it("stays quiet when every row was scored the same way", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-single-policy-test");
    installApi(mixedPolicyJobs.slice(0, 1));

    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByRole("table");

    expect(screen.queryByRole("status")).toBeNull();
  });

  it("points an empty active list at discovery without blaming filters", async () => {
    window.localStorage.setItem("clearscout.workspace", "jobs-empty-test");
    installApi([]);

    render(
      <MemoryRouter>
        <AppProviders>
          <JobsPage />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(await screen.findByText(/No tracked postings yet/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "How jobs are scored" }).getAttribute("href")).toBe(
      "/find/scoring",
    );
    expect(screen.getByRole("link", { name: "Find postings" }).getAttribute("href")).toBe("/find");

    fireEvent.change(screen.getByLabelText("Decision"), { target: { value: "investigate" } });
    expect(await screen.findByText("No jobs match these filters.")).toBeTruthy();
  });
});
