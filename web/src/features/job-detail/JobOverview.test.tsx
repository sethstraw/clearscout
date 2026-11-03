import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw/http";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { AppProviders } from "../../providers";
import { jobDetailHandlers } from "../../test/msw/jobDetailHandlers";
import { server } from "../../test/msw/server";
import { jobDetail } from "./jobDetailFixtures";
import { JobOverview } from "./JobOverview";
import type { JobDetail } from "../../api/client";

function overview(data: JobDetail) {
  return (
    <AppProviders>
      <MemoryRouter>
        <JobOverview
          data={data}
          hasCorrection={false}
          jobId={data.job_posting_id}
          onOpenApplicationHistory={() => {}}
          onOpenPosting={() => {}}
          workspaceId="workspace-jobs"
        />
      </MemoryRouter>
    </AppProviders>
  );
}

describe("JobOverview closing dates", () => {
  it("saves a missing date through its free endpoint while preserving a review draft", async () => {
    window.localStorage.setItem("clearscout.workspace", "workspace-jobs");
    const job = jobDetail({ recorded_deadline: "After the panel, before 17:00 MDT" });
    const writes: { path: string; body: unknown }[] = [];
    const paid: unknown[] = [];
    server.use(
      ...jobDetailHandlers({
        detail: job,
        onEnqueue: async (request) => {
          paid.push(await request.json());
        },
      }),
      http.put("*/api/v1/jobs/:jobId/closing-date", async ({ request }) => {
        const url = new URL(request.url);
        writes.push({ path: `${url.pathname}${url.search}`, body: await request.json() });
        return HttpResponse.json({
          ...job,
          closing_date_override: "2026-11-09",
        } satisfies JobDetail);
      }),
      http.put("*/api/v1/jobs/:jobId/manual-review", async ({ request }) => {
        writes.push({ path: new URL(request.url).pathname, body: await request.json() });
        return HttpResponse.json(job);
      }),
    );
    const view = render(overview(job));
    const dates = screen.getByRole("group", { name: "Closing dates" });
    expect(within(dates).getByText("Not stated")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Your note"), {
      target: { value: "Unsaved review draft" },
    });
    fireEvent.click(within(dates).getByText("Edit closing date"));
    fireEvent.change(within(dates).getByLabelText("Your closing date"), {
      target: { value: "2026-11-09" },
    });
    fireEvent.click(within(dates).getByRole("button", { name: "Save date" }));
    await within(dates).findByText("Your closing date was saved.");
    expect(writes).toEqual([
      {
        path: `/api/v1/jobs/${job.job_posting_id}/closing-date?workspace_id=workspace-jobs`,
        body: { closing_date_override: "2026-11-09" },
      },
    ]);

    view.rerender(
      overview({ ...job, closing_date_override: "2026-11-09", manual_note: "Server review" }),
    );
    expect(within(dates).getByText("2026-11-09")).toBeTruthy();
    expect(screen.getByLabelText("Your note")).toHaveProperty("value", "Unsaved review draft");
    expect(screen.getByLabelText("Recorded deadline")).toHaveProperty(
      "value",
      "After the panel, before 17:00 MDT",
    );
    fireEvent.click(screen.getByRole("button", { name: "Save your review" }));
    await waitFor(() => expect(writes).toHaveLength(2));
    expect(writes[1]?.body).toMatchObject({
      note: "Unsaved review draft",
      recorded_deadline: "After the panel, before 17:00 MDT",
    });
    expect(writes[1]?.body).not.toHaveProperty("closing_date_override");
    expect(paid).toEqual([]);
  });

  it("preserves a failed date for retry and keeps publisher facts when resetting", async () => {
    window.localStorage.setItem("clearscout.workspace", "workspace-jobs");
    const publisher = {
      needs_clarification: false,
      normalized_value: "2026-11-12",
      precision: "date" as const,
      source_url: "https://example.org/jobs/closing-date",
      observed_at: "2026-10-01T10:00:00Z",
      statements: [
        {
          basis: "posting_text",
          wording: "Applications close November 12, 2026",
          clarification: "",
        },
      ],
    };
    const job = jobDetail({
      publisher_closing_date: publisher,
      closing_date_override: "2026-11-10",
    });
    const bodies: unknown[] = [];
    server.use(
      ...jobDetailHandlers({ detail: job }),
      http.put("*/api/v1/jobs/:jobId/closing-date", async ({ request }) => {
        const body = await request.json();
        bodies.push(body);
        if (bodies.length === 1)
          return HttpResponse.json({ detail: "Synthetic save failure" }, { status: 503 });
        return HttpResponse.json(job);
      }),
    );
    const view = render(overview(job));
    const dates = screen.getByRole("group", { name: "Closing dates" });
    fireEvent.click(within(dates).getByText("Edit closing date"));
    fireEvent.change(within(dates).getByLabelText("Your closing date"), {
      target: { value: "2026-11-09" },
    });
    fireEvent.click(within(dates).getByRole("button", { name: "Save date" }));
    expect((await within(dates).findByRole("alert")).textContent).toContain(
      "Synthetic save failure",
    );
    expect(within(dates).getByLabelText("Your closing date")).toHaveProperty("value", "2026-11-09");
    fireEvent.click(within(dates).getByRole("button", { name: "Save date" }));
    await within(dates).findByText("Your closing date was saved.");
    const updatedPublisher = {
      ...publisher,
      normalized_value: "2026-11-13",
      statements: [
        {
          basis: "posting_text",
          wording: "Applications close November 13, 2026",
          clarification: "",
        },
      ],
    };
    view.rerender(
      overview({
        ...job,
        closing_date_override: "2026-11-09",
        publisher_closing_date: updatedPublisher,
      }),
    );
    expect(within(dates).getByText("2026-11-13")).toBeTruthy();
    expect(within(dates).getByText("Applications close November 13, 2026")).toBeTruthy();
    expect(within(dates).getByText("Observed 2026-10-01T10:00:00Z")).toBeTruthy();
    fireEvent.click(within(dates).getByText("Edit closing date"));
    fireEvent.click(within(dates).getByRole("button", { name: "Use publisher date" }));
    await within(dates).findByText("Using the publisher closing date.");
    expect(bodies).toEqual([
      { closing_date_override: "2026-11-09" },
      { closing_date_override: "2026-11-09" },
      { closing_date_override: null },
    ]);
    view.rerender(
      overview({ ...job, closing_date_override: null, publisher_closing_date: updatedPublisher }),
    );
    expect(within(dates).getByText("Not set; using publisher information")).toBeTruthy();
    expect(within(dates).getByText("2026-11-13")).toBeTruthy();
  });
});

describe("JobOverview recorded review", () => {
  it("edits and clears source values on an archived job without changing evaluated scores", async () => {
    window.localStorage.setItem("clearscout.workspace", "workspace-jobs");
    const job = jobDetail({
      archived_at: "2026-08-10T12:00:00Z",
      recorded_fit_score: 77,
      recorded_overall_score: 79,
      recorded_compensation: "USD 120,000 annually",
      recorded_eligibility: "Synthetic assessment",
      recorded_source: "Synthetic source record",
    });
    const saved: Record<string, unknown>[] = [];
    server.use(
      ...jobDetailHandlers({ detail: job }),
      http.put("*/api/v1/jobs/:jobId/manual-review", async ({ request }) => {
        saved.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json(job);
      }),
    );

    render(
      <AppProviders>
        <MemoryRouter>
          <JobOverview
            data={job}
            hasCorrection={false}
            jobId={job.job_posting_id}
            onOpenApplicationHistory={() => {}}
            onOpenPosting={() => {}}
            workspaceId="workspace-jobs"
          />
        </MemoryRouter>
      </AppProviders>,
    );

    expect(screen.getByLabelText<HTMLInputElement>("Fit score").value).toBe("77");
    expect(screen.getByLabelText<HTMLInputElement>("Overall score").value).toBe("79");
    fireEvent.change(screen.getByLabelText("Fit score"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Compensation score"), {
      target: { value: "68" },
    });
    fireEvent.change(screen.getByLabelText("Overall score"), {
      target: { value: "81" },
    });
    fireEvent.change(screen.getByLabelText("Compensation information"), {
      target: { value: "USD 125,000 annually" },
    });
    fireEvent.change(screen.getByLabelText("Next action"), {
      target: { value: "Follow up" },
    });
    fireEvent.change(screen.getByLabelText("Recorded work model"), {
      target: { value: "Remote in Canada" },
    });
    fireEvent.change(screen.getByLabelText("Recorded eligibility assessment"), {
      target: { value: "" },
    });
    fireEvent.change(screen.getByLabelText("Recorded deadline"), {
      target: { value: "2026-10-20 17:00 MDT" },
    });
    fireEvent.change(screen.getByLabelText("Recorded source"), {
      target: { value: "Synthetic corrected source" },
    });
    fireEvent.change(screen.getByLabelText("Your priority"), { target: { value: "high" } });
    fireEvent.change(screen.getByLabelText("Your fit judgment"), {
      target: { value: "possible" },
    });
    fireEvent.change(screen.getByLabelText("Your note"), {
      target: { value: "Checked source values." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save your review" }));

    await waitFor(() => expect(saved).toHaveLength(1));
    expect(saved[0]).toMatchObject({
      recorded_fit_score: null,
      recorded_compensation_score: 68,
      recorded_overall_score: 81,
      recorded_compensation: "USD 125,000 annually",
      next_action: "Follow up",
      recorded_work_model: "Remote in Canada",
      recorded_eligibility: "",
      recorded_deadline: "2026-10-20 17:00 MDT",
      recorded_source: "Synthetic corrected source",
      priority: "high",
      fit: "possible",
      note: "Checked source values.",
    });
    expect(saved[0]).not.toHaveProperty("fit_score");
  });
});
