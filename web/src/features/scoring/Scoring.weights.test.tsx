import { render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw/http";
import { afterEach, describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";

import type { Job, ScoreWeightSet, SearchParameterVersion } from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { jobSummary } from "../job-ledger/jobFixtures";
import { searchParametersState } from "../search-parameters/searchParameterFixtures";
import { ScoringPage } from "./Scoring";

/** Keep browser score previews aligned with server weighting rules. */

const POLICIES = [
  {
    id: "policy-2",
    name: "ClearScout default",
    version: 2,
    effective_at: "2026-08-07T00:00:00Z",
    is_shipped_default: true,
    is_active: true,
    weights: [
      { component: "compensation", weight: 0.25 },
      { component: "eligibility", weight: 0.25 },
      { component: "evidence", weight: 0 },
      { component: "fit", weight: 0.4 },
      { component: "opportunity", weight: 0.1 },
    ],
  },
] satisfies ScoreWeightSet[];

const completeJob = {
  rationale: "",
  scope_summary: "",
  qualifications_summary: "",
  completeness: { status: "complete", missing: [], checks: [] },
} satisfies Partial<Job>;

const JOBS = [
  jobSummary({
    ...completeJob,
    job_posting_id: "job-1",
    employer: "Beta",
    title: "Director",
    fit_score: 90,
    compensation_score: 80,
    eligibility_score: 100,
    evidence_score: 100,
    opportunity_score: 70,
    overall_score: 88,
  }),
  jobSummary({
    ...completeJob,
    job_posting_id: "job-2",
    employer: "Alpha",
    title: "Manager",
    fit_score: 50,
    compensation_score: 50,
    eligibility_score: 50,
    evidence_score: null,
    opportunity_score: 50,
    overall_score: 50,
  }),
  jobSummary({
    ...completeJob,
    job_posting_id: "job-3",
    employer: "Gamma",
    title: "Lead",
    fit_score: null,
    compensation_score: 90,
    eligibility_score: 90,
    evidence_score: 90,
    opportunity_score: 90,
    overall_score: null,
  }),
] satisfies Job[];

function installApi() {
  server.use(
    http.get("*/api/v1/workspaces/:workspaceId/score-weight-sets", () =>
      HttpResponse.json(POLICIES),
    ),
    http.get("*/api/v1/jobs", () =>
      HttpResponse.json({ items: JOBS, total: JOBS.length, offset: 0, limit: 250 }),
    ),
    http.get("*/api/v1/job-search-parameters", () => HttpResponse.json(searchParametersState())),
    http.get("*/api/v1/job-search-parameters/history", () =>
      HttpResponse.json([] satisfies SearchParameterVersion[]),
    ),
  );
}

afterEach(() => window.localStorage.clear());

describe("scoring weight editor", () => {
  it("previews a reorder using the same rule the server applies", async () => {
    window.localStorage.setItem("clearscout.workspace", "weights-test");
    installApi();

    render(
      <MemoryRouter>
        <AppProviders>
          <ScoringPage />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(screen.getByText("Decide")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Back to evaluated jobs" }).getAttribute("href")).toBe(
      "/jobs",
    );
    const table = await screen.findByRole("table");
    await waitFor(() => expect(within(table).getAllByRole("row").length).toBeGreaterThan(1));
    const rows = within(table).getAllByRole("row").slice(1);

    const computed = (employer: string) => {
      const row = rows.find((item) => item.querySelector("th")?.textContent === employer);
      return row?.querySelectorAll("td")[2]?.textContent;
    };

    expect(rows[0]?.querySelector("th")?.textContent).toBe("Beta");
    expect(computed("Beta")).toBe("88");

    expect(computed("Alpha")).toBe("50");

    expect(computed("Gamma")).toBe("-");
  });

  it("says how much of the ledger the preview actually covers", async () => {
    window.localStorage.setItem("clearscout.workspace", "weights-count-test");
    installApi();

    render(
      <MemoryRouter>
        <AppProviders>
          <ScoringPage />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(await screen.findByText(/top 10 of 3 evaluated jobs/i)).toBeTruthy();
  });

  it("refuses to save weights that do not total one", async () => {
    window.localStorage.setItem("clearscout.workspace", "weights-invalid-test");
    installApi();

    render(
      <MemoryRouter>
        <AppProviders>
          <ScoringPage />
        </AppProviders>
      </MemoryRouter>,
    );

    const fit = await screen.findByLabelText("Fit");
    fit.focus();
    const value = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value");
    value?.set?.call(fit, "0.90");
    fit.dispatchEvent(new Event("input", { bubbles: true }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Save as a new policy/ })).toHaveProperty(
        "disabled",
        true,
      ),
    );
    expect(screen.getByText(/have to sum to exactly 1.00/)).toBeTruthy();
  });
});
