import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Task, TaskBilling } from "../api/client";
import { taskRecord } from "../features/job-search/pendingDiscoveryFixtures";
import { jobDetail } from "../features/job-detail/jobDetailFixtures";
import {
  jobDetailHandlers,
  NO_SAVED_PLACES,
  type JobDetailReads,
} from "../test/msw/jobDetailHandlers";
import { AppProviders } from "../providers";
import { server } from "../test/msw/server";
import { requestLog } from "../test/requestLog";
import { evaluationPreflightHandler } from "../test/taskTypes";
import { JobPage } from "./JobPage";

const API = "*/api/v1";

function installJob(reads: JobDetailReads = {}) {
  server.use(evaluationPreflightHandler(), ...jobDetailHandlers(reads));
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

const detail = jobDetail();
const [firstEvent] = detail.application_events;
const [firstEvidence] = detail.evidence;
const latestObservation = detail.latest_observation;
if (!firstEvent || !firstEvidence || !latestObservation) {
  throw new Error("The job fixture must carry one event, one evidence row, and a latest reading.");
}

function RouteHistoryProbe() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output aria-label="Current job location">{`${location.pathname}${location.search}`}</output>
      <button onClick={() => void navigate(-1)} type="button">
        History back
      </button>
    </>
  );
}

/** Drive one dispute re-extraction to `status`, answering its billing read with `billing`. */
function stubDisputeRefresh(
  status: Task["status"],
  billing: TaskBilling | Promise<TaskBilling>,
  failureMessage = "The provider refused the request.",
  taskType: Task["task_type"] = "posting.refresh_with_reextraction",
  taskRead?: Promise<Task>,
) {
  const isRematch = taskType === "posting.refresh_with_rematch";
  const enabled = jobDetail({
    enable_dispute_reextraction: !isRematch,
    disputed_extraction_ruling_count: isRematch ? 0 : 2,
    enable_dispute_rematch: isRematch,
    disputed_fit_ruling_count: isRematch ? 1 : 0,
  });
  const task = {
    id: "task-42",
    workspace_id: "workspace-jobs",
    task_type: taskType,
    job_posting_ids: ["job-1"],
    pending_discovery_ids: [],
    source_ids: [],
    status,
    attempt_count: 0,
    progress: { observation_unchanged: true, unchanged_since: "2026-08-19T12:00:00Z" },
    cancellation_requested_at: null,
    error_category: status === "failed" ? "provider" : null,
    error_message: status === "failed" ? failureMessage : null,
    created_at: "2026-08-20T12:00:00Z",
    started_at: "2026-08-20T12:00:00Z",
    updated_at: "2026-08-20T12:01:00Z",
    completed_at: ["completed", "failed", "cancelled"].includes(status)
      ? "2026-08-20T12:01:00Z"
      : null,
  } satisfies Task;
  const log = requestLog();
  installJob({
    detail: enabled,
    enqueued: { ...task, status: "queued" },
    task: () => Promise.resolve(taskRead ? taskRead : task).then((value) => value),
    billing: () => Promise.resolve(billing),
    onRequest: log.reads,
  });
  return log;
}

/** Render the Fit workspace and open its Advanced fold, where dispute re-checks live. */
async function renderFitAdvanced() {
  render(
    <AppProviders>
      <MemoryRouter initialEntries={["/jobs/job-1?tab=evaluation"]}>
        <Routes>
          <Route path="/jobs/:jobId" element={<JobPage />} />
        </Routes>
      </MemoryRouter>
    </AppProviders>,
  );
  const summary = await screen.findByText("Advanced", { selector: "summary" });
  expect(summary.closest("details")?.open).toBe(false);
  fireEvent.click(summary);
}

async function runDisputeRefresh(kind: "extraction" | "fit" = "extraction") {
  window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
  await renderFitAdvanced();
  fireEvent.click(
    await screen.findByRole("button", {
      name: kind === "fit" ? "Refresh with fit re-match" : "Refresh with dispute re-extraction",
    }),
  );
  fireEvent.click(
    await screen.findByRole("button", {
      name: kind === "fit" ? "Confirm fit re-match" : "Refresh with re-extraction",
    }),
  );
}

describe("JobPage", () => {
  it("records an earlier application time and offers a valid next status", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    let current = jobDetail({ application_status: "package_ready", application_event_count: 1 });
    const submitted: { status: string; occurred_at: string }[] = [];
    installJob({ detail: () => current, parameters: NO_SAVED_PLACES });
    server.use(
      http.post(`${API}/jobs/:jobId/application-events`, async ({ request }) => {
        const body = (await request.json()) as {
          status: "applied" | "interviewing";
          occurred_at: string;
        };
        submitted.push(body);
        current = jobDetail({
          application_status: body.status,
          application_event_count: current.application_event_count + 1,
        });
        return HttpResponse.json({ id: `event-${submitted.length}`, status: body.status });
      }),
    );
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/jobs/job-1?tab=application"]}>
          <Routes>
            <Route path="/jobs/:jobId" element={<JobPage />} />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );

    const when = await screen.findByLabelText<HTMLInputElement>("When it happened (local time)");
    fireEvent.change(when, { target: { value: "2026-09-30T14:30" } });
    fireEvent.click(screen.getByRole("button", { name: "Append event" }));
    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(submitted[0]?.occurred_at).toBe(new Date("2026-09-30T14:30").toISOString());
    await waitFor(() =>
      expect(screen.getByLabelText<HTMLSelectElement>("Status").value).toBe("interviewing"),
    );
    expect(screen.getByLabelText<HTMLInputElement>("When it happened (local time)").value).not.toBe(
      "",
    );
    fireEvent.click(screen.getByRole("button", { name: "Append event" }));
    await waitFor(() => expect(submitted).toHaveLength(2));
    expect(submitted[1]?.status).toBe("interviewing");
  });

  it("keeps concrete fit-section links in browser history under one Fit workspace", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    installJob({ detail: detail, parameters: NO_SAVED_PLACES });
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/jobs/job-1?tab=evaluation"]}>
          <Routes>
            <Route
              path="/jobs/:jobId"
              element={
                <>
                  <JobPage />
                  <RouteHistoryProbe />
                </>
              }
            />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );

    expect(await screen.findByText(/TailorCV default/)).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Fit" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("tab", { name: "Evaluation" }).getAttribute("aria-selected")).toBe(
      "true",
    );
    fireEvent.click(screen.getByRole("tab", { name: "Evidence & gaps" }));
    expect(screen.getByLabelText("Current job location").textContent).toBe(
      "/jobs/job-1?tab=evidence",
    );
    fireEvent.click(screen.getByRole("button", { name: "History back" }));
    expect(await screen.findByText(/TailorCV default/)).toBeTruthy();
    expect(screen.getByLabelText("Current job location").textContent).toBe(
      "/jobs/job-1?tab=evaluation",
    );
  });

  it("explains append-only status changes before writing one", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    const correctedDetail = jobDetail({
      application_status: "not_applied",
      application_events: [
        {
          ...firstEvent,
          status: "not_applied",
          event_type: "correction",
          corrects_event_id: "event-applied",
        },
      ],
    });
    const events = requestLog();
    installJob({
      detail: correctedDetail,
      parameters: NO_SAVED_PLACES,
      onApplicationEvent: events.record,
    });
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/jobs/job-1"]}>
          <Routes>
            <Route path="/jobs/:jobId" element={<JobPage />} />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );
    expect(await screen.findByText("Synthetic candidate evidence supports the role.")).toBeTruthy();
    expect(screen.queryByText("Overall")).toBeNull();
    expect(screen.getByText("Opportunity")).toBeTruthy();
    expect(screen.getAllByRole("tab").map((item) => item.textContent)).toEqual([
      "Overview",
      "Fit",
      "Tailor",
      "Posting",
      "Application",
    ]);
    expect(screen.getByText("Against canada_remote")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Help: Job" })).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Tailor" }));
    expect(screen.getByRole("tab", { name: "Files" })).toBeTruthy();
    const studioLink = screen.getByRole("link", { name: "evaluation button in the page header" });
    const target = document.getElementById(studioLink.getAttribute("href")!.slice(1));
    expect(target?.querySelector("button.button--paid")).toBeTruthy();
    expect(screen.getByRole("tablist", { name: "Tailoring workspace sections" })).toBeTruthy();
    const exportUrl = "/api/v1/jobs/job-1/fit-report/export?workspace_id=workspace-jobs";
    expect(screen.queryByRole("link", { name: "Download fit report" })).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "Fit" }));
    expect(screen.getByRole("tablist", { name: "Fit workspace sections" })).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Fit report" }));
    expect(screen.getByRole("link", { name: "Export Markdown" }).getAttribute("href")).toBe(
      exportUrl,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Application" }));
    expect(screen.getByRole("tablist", { name: "Application workspace sections" })).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Application history" }));
    expect(
      screen.getByText("This creates a new event. It never edits the timeline below."),
    ).toBeTruthy();
    const status = screen.getByLabelText<HTMLSelectElement>("Status");
    expect(status.value).toBe("applied");
    expect(screen.queryByRole("option", { name: "Package ready" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Append event" }));
    await waitFor(() => expect(events.requests).toHaveLength(1));
    const post = events.requests[0];
    expect(post?.url.pathname).toBe("/api/v1/jobs/job-1/application-events");
    expect(post?.url.searchParams.get("workspace_id")).toBe("workspace-jobs");
    expect(post?.body).toMatchObject({ status: "applied" });
  });

  it("shows authoritative latest facts outside the loaded observation page", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    installJob({ detail: detail, parameters: NO_SAVED_PLACES });
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/jobs/job-1"]}>
          <Routes>
            <Route path="/jobs/:jobId" element={<JobPage />} />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );

    expect(await screen.findByText("Latest posting facts")).toBeTruthy();
    expect(screen.getByText("Brandon")).toBeTruthy();
    expect(screen.getByText("Manitoba")).toBeTruthy();
    expect(screen.getByText("Employment type").parentElement?.textContent).toBe(
      "Employment typeFull time",
    );
    expect(screen.getByText("Payroll arrangement").parentElement?.textContent).toBe(
      "Payroll arrangementemployee",
    );
    expect(screen.getByText(/1 earlier reading of this posting/)).toBeTruthy();
  });

  it("marks the current evaluation as describing an earlier posting when the body changed", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    const changedPosting = jobDetail({
      evaluation_inputs: [
        {
          name: "posting",
          recorded: "observation-old",
          current: "observation-new",
          status: "changed",
        },
      ],
      evaluation_review: { status: "needs_review", reason: "posting_changed", reviewed_at: null },
      latest_observation: {
        ...latestObservation,
        body_artifact_id: "artifact-new-body",
      },
    });
    installJob({ detail: changedPosting, parameters: NO_SAVED_PLACES });

    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/jobs/job-1"]}>
          <Routes>
            <Route path="/jobs/:jobId" element={<JobPage />} />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );

    expect(
      await screen.findByText(
        "Compare the change first. Re-run the evaluation only if it could change your decision; otherwise mark this review complete.",
      ),
    ).toBeTruthy();
    expect(screen.getByRole("link", { name: "Compare versions" }).getAttribute("href")).toBe(
      "/jobs/job-1?tab=posting&compare=observation-old",
    );
  });

  it("withholds a location that cannot be a place and shows the stated residence rule", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    const residence =
      "Applicants must be authorized to work in the U.S. and reside in one of the states where the company is registered as an employer.";
    const prose = jobDetail({
      latest_observation: {
        ...latestObservation,
        location:
          "Role Overview We are hiring a Product Manager to own the supply side of the marketplace.",
        residence_rule: residence,
      },
    });
    installJob({ detail: prose, parameters: NO_SAVED_PLACES });
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/jobs/job-1"]}>
          <Routes>
            <Route path="/jobs/:jobId" element={<JobPage />} />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );
    expect(await screen.findByText("Latest posting facts")).toBeTruthy();
    expect(screen.queryByText(/Role Overview/)).toBeNull();
    expect(screen.getAllByText("Not recorded readably").length).toBe(1);
    expect(screen.getByText(residence)).toBeTruthy();
  });

  it("names an eligibility reading only while the workspace still states its place", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    const readings = jobDetail({
      eligibility_records: [
        ...detail.eligibility_records,
        {
          workspace_location_key: "retired_market",
          region_rule: "Ontario",
          arrangement: "hybrid",
          details: "Two office days a week",
        },
      ],
    });
    installJob({ detail: readings });
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/jobs/job-1"]}>
          <Routes>
            <Route path="/jobs/:jobId" element={<JobPage />} />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );
    expect(await screen.findByText("Latest posting facts")).toBeTruthy();
    // The second reading's place is no longer stated, so it keeps the stored key.
    await waitFor(() =>
      expect(screen.getAllByRole("heading", { level: 4 }).map((node) => node.textContent)).toEqual([
        "Against Ontario, Canada",
        "Against retired_market",
      ]),
    );
  });

  it("leads an uncited requirement with its own text and qualifies the verdict chip", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    const uncited = jobDetail({
      unsupported: [],
      evidence: [
        {
          ...firstEvidence,
          id: "posting-9",
          text: "Comfortable owning a regulated data platform end to end",
          classification: "TAILORING_ONLY",
          locator: "characters 0-55",
          source_kind: "posting",
          master_cv_entry_id: null,
          requirement_id: "req-615ccd56e4ac",
          strength: "moderate",
          observation_id: "observation-1",
        },
      ],
    });
    installJob({ detail: uncited });
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/jobs/job-1?tab=evidence"]}>
          <Routes>
            <Route path="/jobs/:jobId" element={<JobPage />} />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );
    const card = (await screen.findByText("req-615ccd56e4ac")).closest("article");
    expect(card?.querySelector("header strong")?.textContent).toBe(
      "Comfortable owning a regulated data platform end to end",
    );
    expect(card?.textContent).toContain("Nothing in the record is cited");
    expect(card?.textContent).toContain("Uncited: Covered, worded differently");
  });

  it("places a hard-gate quote on the gate card the fit report pairs it with", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    const gated = jobDetail({
      unsupported: [],
      requirements: [
        {
          requirement_id: "gate-1",
          requirement_key: "key-gate-1",
          text: "Synthetic pilot licence",
          importance: "must_have",
          position: 0,
        },
      ],
      evidence: [
        {
          ...firstEvidence,
          id: "evidence-quote",
          text: "Must hold a synthetic pilot licence",
          locator: "posting:hard-gate-1",
          source_kind: "posting",
          master_cv_entry_id: null,
          requirement_id: null,
          score_links: [],
        },
      ],
    });
    installJob({
      detail: gated,
      fitReport: {
        job_posting_id: "job-1",
        evaluation_id: "evaluation-2",
        evaluation_version: 2,
        artifact_id: "report-2",
        sha256: "a".repeat(64),
        markdown: "# Stored report",
        requirements: [
          {
            requirement_id: "gate-1",
            requirement_key: "key-gate-1",
            importance: "must_have",
            text: "Synthetic pilot licence",
            evidence_keys: ["evidence-quote"],
            span_status: "verified",
            established: true,
            unmet: false,
            dimension: "hard",
            is_hard_gate: true,
          },
        ],
      },
    });
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/jobs/job-1?tab=evidence"]}>
          <Routes>
            <Route path="/jobs/:jobId" element={<JobPage />} />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );

    const card = (await screen.findByText("gate-1")).closest("article");
    await waitFor(() => expect(card?.textContent).toContain("Must hold a synthetic pilot licence"));
    expect(screen.queryByText(/Evidence not tied to a requirement/)).toBeNull();
  });

  it("shows reconstructable arithmetic and makes unsupported findings loud", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    installJob({ detail: detail });
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/jobs/job-1?tab=evaluation"]}>
          <Routes>
            <Route path="/jobs/:jobId" element={<JobPage />} />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );
    expect(await screen.findByText(/TailorCV default/)).toBeTruthy();
    expect(screen.getByText("31.50")).toBeTruthy();
    expect(screen.getByText("v2").textContent).toContain("current");
    expect(screen.getByText("v1")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Sort by Overall/ }));
    fireEvent.click(screen.getByRole("tab", { name: "Evidence & gaps" }));
    expect((await screen.findByRole("alert")).textContent).toContain("1 unsupported item");
    fireEvent.click(screen.getByRole("button", { name: "Show unsupported only" }));
    expect(screen.getByText("Domain ramp-up")).toBeTruthy();
  });

  it("pages retained application events without loading the whole timeline", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    const jobNote = {
      archived_at: null,
      body: "Interview preparation",
      created_at: "2026-08-06T12:00:00Z",
      entity_id: "job-1",
      entity_type: "job_posting",
      evidence_eligible: false,
      id: "note-1",
      pinned: false,
      title: "Prepare",
      updated_at: "2026-08-06T12:00:00Z",
      workspace_id: "workspace-jobs",
    };
    const earlierEvent = {
      ...firstEvent,
      id: "event-51",
      notes: "Earliest retained application event",
    };
    installJob({
      onDetailRequest: (url) =>
        HttpResponse.json({
          ...detail,
          application_event_count: 51,
          application_events:
            url.searchParams.get("application_event_offset") === "50"
              ? [earlierEvent]
              : detail.application_events,
        }),
    });
    server.use(http.get(`${API}/notes`, () => HttpResponse.json([jobNote])));
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/jobs/job-1?tab=application"]}>
          <Routes>
            <Route path="/jobs/:jobId" element={<JobPage />} />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );
    expect(await screen.findByText("Initial state")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Job notes" })).toBeTruthy();
    expect(screen.getByText("Interview preparation")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("Earliest retained application event")).toBeTruthy();
    expect(screen.queryByText("Initial state")).toBeNull();
    expect(screen.getByText("Interview preparation")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Manage notes" }).getAttribute("href")).toBe(
      "/jobs/job-1?tab=notes",
    );
  });

  it("pairs each requirement with its evidence, unanswered first, and counts the coverage", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    const longAsk = `Deep experience with distributed systems. ${"Detail that keeps going. ".repeat(20)}`;
    const paired = jobDetail({
      unsupported: [],
      evidence: [
        {
          id: "posting-1",
          text: "Own a data platform",
          classification: "DIRECT",
          artifact_id: "artifact-p1",
          char_start: 10,
          char_end: 29,
          locator: "characters 10-29",
          source_kind: "posting",
          master_cv_entry_id: null,
          requirement_id: "REQ-1",
          strength: "strong",
          observation_id: "observation-1",
          score_links: [],
        },
        {
          id: "record-1",
          text: "Ran the platform team",
          classification: "DIRECT",
          artifact_id: "artifact-c1",
          char_start: 0,
          char_end: 21,
          locator: "CV entry cv-1",
          source_kind: "candidate",
          master_cv_entry_id: "cv-1",
          requirement_id: "REQ-1",
          strength: "strong",
          observation_id: null,
          score_links: [],
        },
        {
          id: "posting-2",
          text: longAsk,
          classification: "GAP",
          artifact_id: "artifact-p2",
          char_start: 40,
          char_end: 80,
          locator: "characters 40-80",
          source_kind: "posting",
          master_cv_entry_id: null,
          requirement_id: "REQ-2",
          strength: "none",
          observation_id: "observation-1",
          score_links: [],
        },
        {
          id: "posting-3",
          text: "Hold a current licence",
          classification: "ASSERTED",
          artifact_id: "artifact-p3",
          char_start: 90,
          char_end: 112,
          locator: "characters 90-112",
          source_kind: "posting",
          master_cv_entry_id: null,
          requirement_id: "REQ-3",
          strength: "none",
          observation_id: "observation-1",
          score_links: [],
        },
        {
          id: "loose-1",
          text: "Filed without a requirement",
          classification: "DIRECT",
          artifact_id: "artifact-c2",
          char_start: 0,
          char_end: 27,
          locator: "CV entry cv-2",
          source_kind: "candidate",
          master_cv_entry_id: "cv-2",
          requirement_id: null,
          strength: "moderate",
          observation_id: null,
          score_links: [],
        },
      ],
    });
    const sources = requestLog();
    installJob({
      detail: paired,
      onRequest: sources.reads,
      evidenceSource: {
        source_kind: "candidate",
        body: "Filed without a requirement",
        char_start: 0,
        char_end: 27,
        locator: "CV entry cv-2",
        sha256: "a".repeat(64),
      },
    });
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/jobs/job-1?tab=evidence"]}>
          <Routes>
            <Route path="/jobs/:jobId" element={<JobPage />} />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );

    const count = (label: string) =>
      screen.getByText(label).closest("div")?.querySelector("strong")?.textContent;
    expect(await screen.findByText("requirements read from the posting")).toBeTruthy();
    expect(count("requirements read from the posting")).toBe("3");
    expect(count("answered by an entry in the record")).toBe("1");
    expect(count("answered by nothing")).toBe("2");

    expect(screen.getAllByText(/^REQ-/).map((node) => node.textContent)).toEqual([
      "REQ-2",
      "REQ-3",
      "REQ-1",
    ]);
    expect(screen.getAllByText("No candidate-record match is stored.").length).toBe(2);

    const answered = screen.getByText("REQ-1").closest("article");
    expect(answered?.textContent).toContain("Own a data platform");
    expect(answered?.textContent).toContain("Ran the platform team");
    expect(answered?.textContent).toContain("strong strength");
    expect(answered?.querySelector("a")?.getAttribute("href")).toContain(
      "tab=posting&observation=observation-1&start=10&end=29",
    );

    expect(screen.getByText("Evidence not tied to a requirement (1)")).toBeTruthy();
    expect(screen.getByText("Filed without a requirement")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Show full text" }));
    expect(screen.getByRole("button", { name: "Show less" }).getAttribute("aria-expanded")).toBe(
      "true",
    );

    const loose = screen.getByText("Filed without a requirement").closest("blockquote");
    fireEvent.click(loose?.querySelector("button") as HTMLButtonElement);
    await waitFor(() =>
      expect(
        sources.requests.some(({ url }) => url.pathname.endsWith("/evidence/loose-1/source")),
      ).toBe(true),
    );
    expect(await screen.findByText("Verified Master CV source")).toBeTruthy();
    expect(document.body.textContent).not.toContain("source_kind");
  });

  it("names the job id and offers a way back when the id is not in this workspace", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    installJob({
      onDetailRequest: () => HttpResponse.json({ detail: "job not found" }, { status: 404 }),
    });
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/jobs/missing-job"]}>
          <Routes>
            <Route path="/jobs/:jobId" element={<JobPage />} />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );
    // The job query retries once after react-query's default 1000ms backoff
    // before the not-found message shows, so drive that wait with fake timers.
    vi.useFakeTimers();
    try {
      await vi.advanceTimersByTimeAsync(1_100);
    } finally {
      vi.useRealTimers();
    }
    expect(await screen.findByText("No job with this id in this workspace.")).toBeTruthy();
    expect(screen.getByText("missing-job")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Back to jobs" }).getAttribute("href")).toBe("/jobs");
  });

  it("hides the paid re-check button when the workspace has not opted in", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    const disabled = jobDetail({
      enable_dispute_reextraction: false,
      disputed_extraction_ruling_count: 3,
    });
    installJob({ detail: disabled });
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/jobs/job-1?tab=evaluation"]}>
          <Routes>
            <Route path="/jobs/:jobId" element={<JobPage />} />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );
    expect(await screen.findByRole("button", { name: "Refresh posting" })).toBeTruthy();
    expect(screen.getByRole("tablist", { name: "Fit workspace sections" })).toBeTruthy();
    expect(screen.queryByText("Advanced", { selector: "summary" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Refresh with dispute re-extraction" })).toBeNull();
  });

  it("hides the paid re-check button when no disputes are active", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    const empty = jobDetail({
      enable_dispute_reextraction: true,
      disputed_extraction_ruling_count: 0,
    });
    installJob({ detail: empty });
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/jobs/job-1?tab=evaluation"]}>
          <Routes>
            <Route path="/jobs/:jobId" element={<JobPage />} />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );
    expect(await screen.findByRole("button", { name: "Refresh posting" })).toBeTruthy();
    expect(screen.getByRole("tablist", { name: "Fit workspace sections" })).toBeTruthy();
    expect(screen.queryByText("Advanced", { selector: "summary" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Refresh with dispute re-extraction" })).toBeNull();
  });

  it("enqueues the paid re-check when both flag and count qualify", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    const enabled = jobDetail({
      enable_dispute_reextraction: true,
      disputed_extraction_ruling_count: 2,
    });
    const enqueues = requestLog();
    installJob({
      detail: enabled,
      onEnqueue: enqueues.record,
      enqueued: taskRecord({
        id: "task-42",
        workspace_id: "workspace-jobs",
        task_type: "posting.refresh_with_reextraction",
        job_posting_ids: ["job-1"],
        status: "queued",
        attempt_count: 0,
        started_at: null,
        created_at: "2026-08-20T12:00:00Z",
        updated_at: "2026-08-20T12:00:00Z",
      }),
    });
    await renderFitAdvanced();
    const paid = await screen.findByRole("button", {
      name: "Refresh with dispute re-extraction",
    });
    fireEvent.click(paid);
    fireEvent.click(await screen.findByRole("button", { name: "Refresh with re-extraction" }));
    await waitFor(() => {
      const post = enqueues.requests.at(-1);
      expect(post?.url.pathname).toBe("/api/v1/tasks");
      expect(post?.body).toMatchObject({ task_type: "posting.refresh_with_reextraction" });
    });
  });

  it("enqueues fit re-match only from its own flag and count", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    const enabled = jobDetail({
      enable_dispute_rematch: true,
      disputed_fit_ruling_count: 1,
      enable_dispute_reextraction: false,
      disputed_extraction_ruling_count: 2,
    });
    const enqueues = requestLog();
    installJob({
      detail: enabled,
      onEnqueue: enqueues.record,
      enqueued: taskRecord({
        id: "task-rematch",
        workspace_id: "workspace-jobs",
        task_type: "posting.refresh_with_rematch",
        job_posting_ids: ["job-1"],
        status: "queued",
        attempt_count: 0,
        started_at: null,
        created_at: "2026-08-20T12:00:00Z",
        updated_at: "2026-08-20T12:00:00Z",
      }),
    });
    await renderFitAdvanced();

    expect(screen.queryByRole("button", { name: "Refresh with dispute re-extraction" })).toBeNull();
    fireEvent.click(await screen.findByRole("button", { name: "Refresh with fit re-match" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm fit re-match" }));

    await waitFor(() =>
      expect(enqueues.requests.at(-1)?.body).toMatchObject({
        task_type: "posting.refresh_with_rematch",
        payload: { job_posting_id: "job-1" },
      }),
    );
  });

  it("states what a completed dispute re-extraction cost", async () => {
    stubDisputeRefresh("completed", {
      task_id: "task-42",
      status: "priced",
      call_count: 2,
      unpriced_call_count: 0,
      resent_on_standard_count: 0,
      actual_cost_usd: "0.0413118",
    });
    await runDisputeRefresh();

    expect(await screen.findByText("Unchanged since 2026-08-19T12:00:00Z.")).toBeTruthy();
    const billingStatus = await screen.findByRole("status", { name: "Dispute refresh billing" });
    expect(billingStatus.textContent).toBe(
      "Recorded cost for this task: $0.0413 across 2 model calls.",
    );
    const retry = screen.getByRole("button", { name: "Refresh with dispute re-extraction" });
    expect(
      billingStatus.compareDocumentPosition(retry) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("says a failed dispute re-extraction recorded no model charge", async () => {
    stubDisputeRefresh("failed", {
      task_id: "task-42",
      status: "no_model_calls",
      call_count: 0,
      unpriced_call_count: 0,
      resent_on_standard_count: 0,
      actual_cost_usd: null,
    });
    await runDisputeRefresh();

    expect(
      await screen.findByText("Refresh failed: The provider refused the request."),
    ).toBeTruthy();
    expect(await screen.findByRole("status", { name: "Dispute refresh billing" })).toHaveProperty(
      "textContent",
      "No model charge was recorded for this task.",
    );
    expect(document.body.textContent).not.toContain("$0.0000");
  });

  it("closes an unpunctuated failure reason before naming the cost", async () => {
    stubDisputeRefresh(
      "failed",
      {
        task_id: "task-42",
        status: "no_model_calls",
        call_count: 0,
        unpriced_call_count: 0,
        resent_on_standard_count: 0,
        actual_cost_usd: null,
      },
      "The read operation timed out",
    );
    await runDisputeRefresh();

    expect(await screen.findByText("Refresh failed: The read operation timed out.")).toBeTruthy();
    expect(await screen.findByRole("status", { name: "Dispute refresh billing" })).toHaveProperty(
      "textContent",
      "No model charge was recorded for this task.",
    );
  });

  it("waits for terminal dispute billing before offering another paid refresh", async () => {
    let finishBilling: (value: TaskBilling) => void = () => {};
    const billing = new Promise<TaskBilling>((resolve) => {
      finishBilling = resolve;
    });
    stubDisputeRefresh("completed", billing);
    await runDisputeRefresh();

    const billingStatus = await screen.findByRole("status", { name: "Dispute refresh billing" });
    expect(billingStatus.textContent).toBe("Checking recorded cost for this task.");
    const retry = screen.getByRole("button", { name: "Refresh with dispute re-extraction" });
    const freeRefresh = screen.getByRole("button", { name: "Refresh posting" });
    expect(retry).toHaveProperty("disabled", true);
    expect(freeRefresh).toHaveProperty("disabled", true);
    expect(
      billingStatus.compareDocumentPosition(retry) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    finishBilling({
      task_id: "task-42",
      status: "no_model_calls",
      call_count: 0,
      unpriced_call_count: 0,
      resent_on_standard_count: 0,
      actual_cost_usd: null,
    });
    await waitFor(() => {
      expect(retry).toHaveProperty("disabled", false);
      expect(freeRefresh).toHaveProperty("disabled", false);
    });
  });

  it("shows a billing-read warning before restoring dispute refresh actions", async () => {
    let failBilling: (reason: Error) => void = () => {};
    const billing = new Promise<TaskBilling>((_, reject) => {
      failBilling = reject;
    });
    stubDisputeRefresh("completed", billing);
    await runDisputeRefresh();

    expect(await screen.findByRole("status", { name: "Dispute refresh billing" })).toHaveProperty(
      "textContent",
      "Checking recorded cost for this task.",
    );
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
      expect(screen.getByRole("status", { name: "Dispute refresh billing" })).toHaveProperty(
        "textContent",
        "Recorded cost for this task is unavailable; review Spend before retrying.",
      ),
    );
    expect(
      screen.getByRole("button", { name: "Refresh with dispute re-extraction" }),
    ).toHaveProperty("disabled", false);
    expect(screen.getByRole("button", { name: "Refresh posting" })).toHaveProperty(
      "disabled",
      false,
    );
  });

  it("keeps refresh controls unavailable until durable task status arrives", async () => {
    let finishTask: (task: Task) => void = () => {};
    const taskRead = new Promise<Task>((resolve) => {
      finishTask = resolve;
    });
    const log = stubDisputeRefresh(
      "completed",
      {
        task_id: "task-42",
        status: "no_model_calls",
        call_count: 0,
        unpriced_call_count: 0,
        resent_on_standard_count: 0,
        actual_cost_usd: null,
      },
      "",
      "posting.refresh_with_reextraction",
      taskRead,
    );
    await runDisputeRefresh();

    const paid = screen.getByRole("button", { name: "Refresh with dispute re-extraction" });
    await waitFor(() => {
      expect(log.requests.some(({ url }) => url.pathname.endsWith("/tasks/task-42"))).toBe(true);
      expect(paid).toHaveProperty("disabled", true);
    });
    expect(screen.getByRole("button", { name: "Checking refresh status" })).toHaveProperty(
      "disabled",
      true,
    );
    expect(screen.getByRole("status")).toHaveProperty("textContent", "Checking refresh status");
    expect(screen.queryByText("Refresh in progress")).toBeNull();

    finishTask({
      id: "task-42",
      workspace_id: "workspace-jobs",
      task_type: "posting.refresh_with_reextraction",
      job_posting_ids: ["job-1"],
      pending_discovery_ids: [],
      source_ids: [],
      status: "completed",
      attempt_count: 0,
      progress: { observation_unchanged: true, unchanged_since: "2026-08-19T12:00:00Z" },
      cancellation_requested_at: null,
      error_category: null,
      error_message: null,
      created_at: "2026-08-20T12:00:00Z",
      started_at: "2026-08-20T12:00:00Z",
      updated_at: "2026-08-20T12:01:00Z",
      completed_at: "2026-08-20T12:01:00Z",
    });
    await waitFor(() => expect(paid).toHaveProperty("disabled", false));
    expect(screen.getByRole("button", { name: "Refresh posting" })).toHaveProperty(
      "disabled",
      false,
    );
  });

  it("keeps refresh controls unavailable when durable task status cannot be read", async () => {
    let failTask: (reason: Error) => void = () => {};
    const taskRead = new Promise<Task>((_, reject) => {
      failTask = reject;
    });
    const log = stubDisputeRefresh(
      "completed",
      {
        task_id: "task-42",
        status: "no_model_calls",
        call_count: 0,
        unpriced_call_count: 0,
        resent_on_standard_count: 0,
        actual_cost_usd: null,
      },
      "",
      "posting.refresh_with_reextraction",
      taskRead,
    );
    await runDisputeRefresh();

    await waitFor(() =>
      expect(log.requests.some(({ url }) => url.pathname.endsWith("/tasks/task-42"))).toBe(true),
    );
    failTask(new Error("Task status unavailable"));

    // The task query retries once after react-query's default 1000ms backoff
    // before the error shows, so drive that wait with fake timers.
    vi.useFakeTimers();
    try {
      await vi.advanceTimersByTimeAsync(1_100);
    } finally {
      vi.useRealTimers();
    }
    const error = await screen.findByText(
      "Refresh status is unavailable. Reload or open Activity before starting another refresh.",
    );
    expect(error.getAttribute("role")).toBe("alert");
    expect(
      screen.getByRole("button", { name: "Refresh with dispute re-extraction" }),
    ).toHaveProperty("disabled", true);
    expect(screen.getByRole("button", { name: "Refresh unavailable" })).toHaveProperty(
      "disabled",
      true,
    );
    expect(screen.queryByText("Refresh in progress")).toBeNull();
    expect(screen.queryByText("Checking refresh status")).toBeNull();
  });

  it("reports billing for a cancelled paid dispute refresh", async () => {
    stubDisputeRefresh(
      "cancelled",
      {
        task_id: "task-42",
        status: "priced",
        call_count: 1,
        unpriced_call_count: 0,
        resent_on_standard_count: 0,
        actual_cost_usd: "0.0182",
      },
      "",
      "posting.refresh_with_rematch",
    );
    await runDisputeRefresh("fit");

    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Dispute refresh billing" })).toHaveProperty(
        "textContent",
        "Recorded cost for this task: $0.0182 across 1 model call.",
      ),
    );
  });

  it("withholds a dispute re-extraction cost while the task is still running", async () => {
    const log = stubDisputeRefresh("running", {
      task_id: "task-42",
      status: "priced",
      call_count: 2,
      unpriced_call_count: 0,
      resent_on_standard_count: 0,
      actual_cost_usd: "0.0413118",
    });
    await runDisputeRefresh();

    expect(await screen.findByText("Refresh in progress")).toBeTruthy();
    expect(document.body.textContent).not.toContain("Recorded cost for this task");
    expect(log.requests.some(({ url }) => url.pathname.endsWith("/tasks/task-42/billing"))).toBe(
      false,
    );
  });

  it("does not request billing for a completed free posting refresh", async () => {
    const log = stubDisputeRefresh(
      "completed",
      {
        task_id: "task-42",
        status: "no_model_calls",
        call_count: 0,
        unpriced_call_count: 0,
        resent_on_standard_count: 0,
        actual_cost_usd: null,
      },
      "",
      "posting.refresh",
    );
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/jobs/job-1"]}>
          <Routes>
            <Route path="/jobs/:jobId" element={<JobPage />} />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Refresh posting" }));

    expect(await screen.findByText("Unchanged since 2026-08-19T12:00:00Z.")).toBeTruthy();
    expect(log.requests.some(({ url }) => url.pathname.endsWith("/tasks/task-42/billing"))).toBe(
      false,
    );
    expect(screen.queryByRole("status", { name: "Dispute refresh billing" })).toBeNull();
  });

  it("enqueues a paid re-run evaluation from the page actions", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    const evaluation = (status: Task["status"]) =>
      taskRecord({
        id: "task-eval",
        workspace_id: "workspace-jobs",
        job_posting_ids: ["job-1"],
        status,
        attempt_count: 0,
        completed_at: status === "completed" ? "2026-08-20T12:01:00Z" : null,
        started_at: status === "queued" ? null : "2026-08-20T12:00:00Z",
        created_at: "2026-08-20T12:00:00Z",
        updated_at: "2026-08-20T12:01:00Z",
      });
    const enqueues = requestLog();
    installJob({
      onEnqueue: enqueues.record,
      enqueued: evaluation("queued"),
      task: () => Promise.resolve(evaluation("completed")),
      billing: () =>
        Promise.resolve({
          task_id: "task-eval",
          status: "priced",
          call_count: 1,
          unpriced_call_count: 0,
          resent_on_standard_count: 0,
          actual_cost_usd: "0.0209372",
        }),
    });
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/jobs/job-1"]}>
          <Routes>
            <Route path="/jobs/:jobId" element={<JobPage />} />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );
    const trigger = await screen.findByRole("button", { name: "Re-run evaluation" });
    fireEvent.click(trigger);
    const confirm = await screen.findByRole("button", { name: "Confirm re-evaluation" });
    fireEvent.click(confirm);
    await waitFor(() => {
      const post = enqueues.requests.at(-1);
      expect(post?.url.pathname).toBe("/api/v1/tasks");
      expect(post?.body).toMatchObject({
        task_type: "discovery.evaluate",
        payload: { job_posting_id: "job-1", fresh: true },
      });
    });
    expect(
      await screen.findByText(
        "Re-evaluation completed. Recorded cost for this task: $0.0209 across 1 model call. The current Fit report is ready to review.",
      ),
    ).toBeTruthy();
    await waitFor(() => {
      expect(screen.getByRole("tab", { name: "Fit report" }).getAttribute("aria-selected")).toBe(
        "true",
      );
    });
  });

  it("announces why a paid re-run evaluation failed", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    const evaluation = (status: Task["status"], error: string | null) =>
      taskRecord({
        id: "task-eval",
        workspace_id: "workspace-jobs",
        job_posting_ids: ["job-1"],
        status,
        error_category: error ? "provider" : null,
        error_message: error,
        completed_at: "2026-08-20T12:01:00Z",
        started_at: "2026-08-20T12:00:00Z",
        created_at: "2026-08-20T12:00:00Z",
        updated_at: "2026-08-20T12:01:00Z",
      });
    installJob({
      enqueued: evaluation("queued", null),
      task: () =>
        Promise.resolve(evaluation("failed", "The provider refused the request: quota exhausted.")),
      billing: () =>
        Promise.resolve({
          task_id: "task-eval",
          status: "unpriced",
          call_count: 1,
          unpriced_call_count: 1,
          resent_on_standard_count: 0,
          actual_cost_usd: null,
        }),
    });
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/jobs/job-1"]}>
          <Routes>
            <Route path="/jobs/:jobId" element={<JobPage />} />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Re-run evaluation" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm re-evaluation" }));
    await waitFor(() => {
      const announced = screen.getAllByRole("alert");
      expect(
        announced.some((node) =>
          node.textContent?.includes(
            "Re-evaluation failed: The provider refused the request: quota exhausted.",
          ),
        ),
      ).toBe(true);
      expect(document.body.textContent).toContain(
        "1 model call was recorded for this task, but the exact cost is unavailable. A retry can add another charge.",
      );
    });
  });

  it("names the re-run when its enqueue is refused outright", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    installJob({
      onEnqueueRefused: () =>
        HttpResponse.json(
          { detail: "The workspace spend limit is already reached." },
          { status: 402 },
        ),
    });
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/jobs/job-1"]}>
          <Routes>
            <Route path="/jobs/:jobId" element={<JobPage />} />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Re-run evaluation" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm re-evaluation" }));
    await waitFor(() => {
      const announced = screen.getAllByRole("alert");
      expect(announced.some((node) => node.textContent?.startsWith("Re-evaluation failed:"))).toBe(
        true,
      );
    });
  });

  it("hides Re-run evaluation for an archived job", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    const archived = { ...detail, archived_at: "2026-08-10T12:00:00Z" };
    installJob({ detail: archived });
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/jobs/job-1"]}>
          <Routes>
            <Route path="/jobs/:jobId" element={<JobPage />} />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );
    expect(await screen.findByRole("button", { name: "Archive" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Re-run evaluation" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Refresh posting" })).toBeNull();
  });

  it("shows orphaned rulings when a re-evaluation dropped their requirement", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    const withOrphan = jobDetail({
      orphaned_ruling_count: 1,
      orphaned_rulings: [
        {
          requirement_key: "orphan-key-0001",
          ruling: "disputed_fit",
          note: "Two prior roles combined satisfy this.",
          requirement_text: "5+ years of dedicated product management experience.",
          updated_at: "2026-08-20T12:00:00Z",
        },
      ],
    });
    installJob({ detail: withOrphan });
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/jobs/job-1"]}>
          <Routes>
            <Route path="/jobs/:jobId" element={<JobPage />} />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );

    await screen.findByText(/does not match a requirement in the current evaluation/);
    screen.getByText("Disputed (fit)");
    screen.getByText("5+ years of dedicated product management experience.");
    screen.getByText("Two prior roles combined satisfy this.");
  });

  const renderRerunConfirmation = async (observedAt: string) => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-jobs");
    const localDetail = jobDetail({
      latest_observation: { ...latestObservation, observed_at: observedAt },
    });
    installJob({ detail: localDetail });
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/jobs/job-1"]}>
          <Routes>
            <Route path="/jobs/:jobId" element={<JobPage />} />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Re-run evaluation" }));
    await screen.findByRole("button", { name: "Confirm re-evaluation" });
  };

  it("names the stored body when the latest observation is under the freshness threshold", async () => {
    const recent = new Date(Date.now() - 60 * 60 * 1000).toISOString();
    await renderRerunConfirmation(recent);
    expect(
      screen.getByText(/Uses stored body; run Refresh posting first to re-fetch\./),
    ).toBeTruthy();
  });

  it("names the fresh fetch when the latest observation is past the freshness threshold", async () => {
    const stale = new Date(Date.now() - 30 * 60 * 60 * 1000).toISOString();
    await renderRerunConfirmation(stale);
    expect(screen.getByText(/a fresh fetch will run before evaluation\./)).toBeTruthy();
  });
});
