import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Task, WorkspaceSummary } from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { jobSummary } from "../job-ledger/jobFixtures";
import { taskRecord } from "../job-search/pendingDiscoveryFixtures";
import { CommandCenterPage } from "./CommandCenter";
import { firstRunHandlers } from "./firstRunFixtures";

const FIXED_NOW = Date.parse("2026-09-20T12:00:00Z");

const DECIDED_ROWS = [
  jobSummary({
    job_posting_id: "job-d1",
    title: "Registry Data Lead",
    employer: "Initech",
    decision: "apply_now",
    evaluated_at: "2026-09-05T12:00:00Z",
    posting_status: "closed",
  }),
  ...Array.from({ length: 6 }, (_, index) =>
    jobSummary({
      job_posting_id: `job-d${index + 2}`,
      title: `Waiting Role ${index + 2}`,
      employer: "Globex",
      decision: "premium_target",
      evaluated_at: "2026-09-12T12:00:00Z",
      posting_status: "active",
    }),
  ),
];

const SUMMARY = {
  total_jobs: 7,
  actionable_applications: 2,
  incomplete_jobs: 1,
  application_funnel: { package_ready: 2, applied: 1, interviewing: 0, offer: 0 },
  highest_intrinsic: [
    jobSummary({
      job_posting_id: "job-1",
      title: "Synthetic Platform Lead",
      employer: "Example Systems",
      fit_score: 95,
      overall_score: 91,
      evaluation_inputs: [{ name: "cv", recorded: "cv-old", current: "cv-new", status: "changed" }],
    }),
  ],
  highest_upside: [
    jobSummary({
      job_posting_id: "job-3",
      title: "Data Ingestion Manager",
      employer: "Globex",
      fit_score: 74,
      overall_score: 62,
    }),
    jobSummary({
      job_posting_id: "job-4",
      title: "Integration Engineer",
      employer: "Northwind",
      fit_score: 81,
      overall_score: null,
    }),
  ],
  actionable: [
    jobSummary({
      job_posting_id: "job-2",
      title: "Offered Staff Engineer",
      employer: "Example Systems",
      fit_score: 70,
      overall_score: null,
      application_status: "package_ready",
    }),
    jobSummary({
      job_posting_id: "job-5",
      title: "Clinical Systems Analyst",
      employer: "Initech",
      fit_score: 68,
      overall_score: 86,
      application_status: "package_ready",
    }),
  ],
  decided_without_outcome: DECIDED_ROWS,
  decided_without_outcome_count: DECIDED_ROWS.length,
} satisfies WorkspaceSummary;

const TASKS = [
  taskRecord({
    id: "task-1",
    workspace_id: "workspace-command",
    task_type: "posting.refresh",
    status: "running",
    attempt_count: 1,
    error_message: null,
  }),
  taskRecord({
    id: "task-2",
    workspace_id: "workspace-command",
    task_type: "posting.refresh_batch",
    status: "failed",
    attempt_count: 3,
    error_message:
      "Reading https://boards.example.com/globex/engineering/openings returned 429. " +
      "Wait for the board's rate window to reset, then queue the refresh again.",
  }),
] satisfies Task[];

function renderPage(summary: WorkspaceSummary = SUMMARY, tasks: Task[] = TASKS) {
  window.localStorage.setItem("tailorcv.workspace", "workspace-command");
  server.use(
    http.get("*/api/v1/workspaces/:workspaceId/summary", () => HttpResponse.json(summary)),
    http.get("*/api/v1/tasks", () => HttpResponse.json(tasks)),
    ...firstRunHandlers(),
  );
  render(
    <AppProviders>
      <MemoryRouter>
        <CommandCenterPage />
      </MemoryRouter>
    </AppProviders>,
  );
}

afterEach(() => {
  window.localStorage.clear();
  vi.restoreAllMocks();
});

function decidedActions() {
  return screen
    .getAllByRole("button")
    .map((button) => button.textContent)
    .filter((label) => label === "Record applied" || label === "Closed before I applied");
}

describe("CommandCenterPage", () => {
  it("keeps an unevaluated tracked job out of CV fit ranking and historical score warnings", async () => {
    const unevaluated = jobSummary({
      title: "Tracked Data Engineer",
      evaluation_id: null,
      evaluation_version: null,
      evaluated_at: null,
      evaluation_inputs: [],
      application_status: "package_ready",
    });
    renderPage({
      ...SUMMARY,
      total_jobs: 1,
      actionable_applications: 1,
      highest_intrinsic: [unevaluated],
      highest_upside: [],
      actionable: [unevaluated],
      decided_without_outcome: [],
      decided_without_outcome_count: 0,
    });

    const fit = (await screen.findByRole("heading", { name: "Best fit with your CV" })).closest(
      "section",
    )!;
    expect(within(fit).queryByRole("link", { name: /Tracked Data Engineer/ })).toBeNull();
    expect(within(fit).getByText("No evaluated jobs with a fit score yet.")).toBeTruthy();
    expect(screen.getByRole("link", { name: /Tracked Data Engineer/ })).toBeTruthy();
    expect(screen.getByText("Not evaluated")).toBeTruthy();
    expect(screen.queryByText(/Historical score/)).toBeNull();
    const tracked = screen.getByText("Tracked postings").closest("article")!;
    expect(within(tracked).getByText("1")).toBeTruthy();
  });

  it("ranks only evaluated jobs with stated fit while retaining saved score provenance", async () => {
    renderPage({
      ...SUMMARY,
      highest_intrinsic: [
        SUMMARY.highest_intrinsic[0]!,
        jobSummary({
          job_posting_id: "unevaluated",
          title: "Manually Reviewed Posting",
          evaluation_id: null,
          evaluation_version: null,
          evaluated_at: null,
          recorded_fit_score: 99,
        }),
        jobSummary({ job_posting_id: "unknown-fit", title: "Unstated Fit", fit_score: null }),
        jobSummary({ job_posting_id: "zero-fit", title: "Zero Fit", fit_score: 0 }),
      ],
    });

    const fit = (await screen.findByRole("heading", { name: "Best fit with your CV" })).closest(
      "section",
    )!;
    expect(
      within(fit)
        .getAllByRole("link")
        .map((link) => link.getAttribute("href")),
    ).toEqual(["/jobs/job-1", "/jobs/zero-fit"]);
    expect(within(fit).getByText("95")).toBeTruthy();
    expect(within(fit).getByText("0")).toBeTruthy();
    expect(within(fit).getByText("Historical score: CV changed")).toBeTruthy();
  });

  it("shows the same badges for pursued jobs sharing an application state", async () => {
    renderPage();

    expect(await screen.findByText("Offered Staff Engineer")).toBeTruthy();
    expect(screen.getByText("Historical score: CV changed")).toBeTruthy();
    expect(screen.getByText("Clinical Systems Analyst")).toBeTruthy();
    expect(screen.getAllByText("Package ready")).toHaveLength(2);
    expect(screen.getByText("86")).toBeTruthy();
    expect(screen.getAllByText("Not scored")).toHaveLength(2);
  });

  it("shows human review scores in ranked jobs without changing evaluation values", async () => {
    renderPage({
      ...SUMMARY,
      highest_intrinsic: [
        jobSummary({
          title: "Reviewed Role",
          fit_score: 95,
          recorded_fit_score: 45,
        }),
      ],
      highest_upside: [
        jobSummary({
          title: "Reviewed Opportunity",
          overall_score: 91,
          recorded_overall_score: 55,
        }),
      ],
    });

    expect(await screen.findByText("Reviewed Role")).toBeTruthy();
    expect(screen.getByText("45")).toBeTruthy();
    expect(screen.getByText("55")).toBeTruthy();
    expect(screen.getByText("fit (your review)")).toBeTruthy();
    expect(screen.getByText("overall (your review)")).toBeTruthy();
  });

  it("states the ordering the revisit list actually uses", async () => {
    renderPage();

    expect(
      await screen.findByText(
        "Jobs marked investigate or monitor. Highest overall score first; jobs with no overall " +
          "score come last, by employer then title.",
      ),
    ).toBeTruthy();
    expect(screen.getByText("62")).toBeTruthy();
    expect(screen.getByText("Integration Engineer")).toBeTruthy();
  });

  it("names the unit the first metric counts and what it leaves out", async () => {
    renderPage();

    expect(await screen.findByText("Tracked postings")).toBeTruthy();
    expect(screen.getByText("Job postings in the ledger, archived ones excluded")).toBeTruthy();
    expect(screen.getByText("7")).toBeTruthy();
  });

  it("keeps the attempt count on a failed task and shows its reason in full", async () => {
    renderPage();

    expect(await screen.findByText("Refresh a posting")).toBeTruthy();
    expect(screen.getByText("1 attempt")).toBeTruthy();
    expect(screen.getByText("3 attempts")).toBeTruthy();
    expect(
      screen.getByText(
        "Reading https://boards.example.com/globex/engineering/openings returned 429. " +
          "Wait for the board's rate window to reset, then queue the refresh again.",
      ),
    ).toBeTruthy();
  });

  it("names an evaluation batch over tracked postings as a re-evaluation", async () => {
    const rerun = taskRecord({
      id: "task-3",
      workspace_id: "workspace-command",
      task_type: "discovery.evaluate_batch",
      status: "running",
      attempt_count: 1,
      error_message: null,
      job_posting_ids: ["job-1", "job-2"],
    });
    renderPage(SUMMARY, [rerun]);

    expect(await screen.findByText("Re-evaluate tracked postings")).toBeTruthy();
    expect(screen.queryByText("Evaluate discoveries")).toBeNull();
  });

  it("shows six waiting decisions and reveals the rest without leaving the page", async () => {
    renderPage();

    expect(await screen.findByText("Registry Data Lead")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: /^Record applied for / })).toHaveLength(6);
    expect(screen.queryByText("Waiting Role 7")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Show all 7" }));

    expect(screen.getAllByRole("button", { name: /^Record applied for / })).toHaveLength(7);
    expect(screen.getByText("Waiting Role 7")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Home" })).toBeTruthy();
  });

  it("flags a posting that closed while waiting and offers its closure first", async () => {
    vi.spyOn(Date, "now").mockReturnValue(FIXED_NOW);
    renderPage();

    expect(await screen.findByText("Closed at publisher")).toBeTruthy();
    expect(screen.getByText("Decided 15 days ago")).toBeTruthy();
    expect(decidedActions().slice(0, 2)).toEqual(["Closed before I applied", "Record applied"]);
  });

  it("records an applied event against the job whose button was pressed", async () => {
    const posted: { jobId: string; status: unknown }[] = [];
    renderPage();
    server.use(
      http.post("*/api/v1/jobs/:jobId/application-events", async ({ params, request }) => {
        const body = (await request.json()) as { status: string };
        posted.push({ jobId: String(params.jobId), status: body.status });
        return HttpResponse.json({ id: "event-1" });
      }),
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "Record applied for Registry Data Lead" }),
    );

    await waitFor(() => expect(posted).toEqual([{ jobId: "job-d1", status: "applied" }]));
  });

  it("says nothing is waiting when every strong decision has an outcome", async () => {
    renderPage({ ...SUMMARY, decided_without_outcome: [], decided_without_outcome_count: 0 });

    expect(await screen.findByText("Every strong decision has a recorded outcome.")).toBeTruthy();
  });

  it("renders the overview shell", async () => {
    renderPage();

    expect(await screen.findByText("Synthetic Platform Lead")).toBeTruthy();
    expect(screen.getByText("95")).toBeTruthy();
    expect(screen.getByText("Best fit with your CV")).toBeTruthy();
    expect(screen.getByText("Waiting on you")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Review applications" }).getAttribute("href")).toBe(
      "/apply",
    );
    expect(await screen.findByRole("region", { name: "Get started" })).toBeTruthy();
    expect(
      screen.getByRole("img", {
        name: "Application pipeline: Ready 2, Applied 1, Interview 0, Offer 0",
      }),
    ).toBeTruthy();
  });
});
