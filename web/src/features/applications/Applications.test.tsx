import { render, screen } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { AppProviders } from "../../providers";
import { jobList, jobSummary } from "../job-ledger/jobFixtures";
import { server } from "../../test/msw/server";
import { ApplicationsPage } from "./Applications";

describe("ApplicationsPage", () => {
  it("links each application from the job projection's stored identity", async () => {
    window.localStorage.setItem("clearscout.workspace", "workspace-applications");
    server.use(
      http.get("*/api/v1/jobs", () =>
        HttpResponse.json(
          jobList([
            jobSummary({
              application_id: "application-1",
              employer: "Synthetic Systems",
              title: "Evidence Lead",
              fit_score: 88,
              evaluated_at: "2026-08-06T12:00:00Z",
              application_status: "applied",
              application_event_count: 1,
            }),
          ]),
        ),
      ),
      http.get("*/api/v1/tasks", () => HttpResponse.json([])),
    );

    render(
      <AppProviders>
        <MemoryRouter>
          <ApplicationsPage />
        </MemoryRouter>
      </AppProviders>,
    );

    expect(await screen.findByText("Evidence Lead")).toBeTruthy();
    expect(screen.getByText("Fit 88")).toBeTruthy();
    expect(screen.queryByText(/Overall not stated/)).toBeNull();
    expect(screen.getByLabelText("1 application")).toBeTruthy();
    expect(screen.getByLabelText<HTMLSelectElement>("Sort applications").value).toBe("fit_score");
    expect(screen.getByRole("link", { name: "Open application files" }).getAttribute("href")).toBe(
      "/applications/application-1",
    );
    expect(screen.getByRole("option", { name: "Closed before applying" })).toBeTruthy();
    expect(screen.queryByRole("option", { name: "Package ready" })).toBeNull();
    expect(screen.queryByRole("option", { name: "closed before application" })).toBeNull();
  });

  it("shows and sorts by the same human-overridden fit score", async () => {
    window.localStorage.setItem("clearscout.workspace", "workspace-application-review");
    server.use(
      http.get("*/api/v1/jobs", () =>
        HttpResponse.json(
          jobList([
            jobSummary({
              job_posting_id: "job-one",
              title: "Role One",
              application_status: "applied",
              fit_score: 90,
              overall_score: 95,
              recorded_fit_score: 40,
              recorded_overall_score: 50,
            }),
            jobSummary({
              job_posting_id: "job-two",
              title: "Role Two",
              application_status: "applied",
              fit_score: 70,
              overall_score: 60,
            }),
          ]),
        ),
      ),
      http.get("*/api/v1/tasks", () => HttpResponse.json([])),
    );

    render(
      <AppProviders>
        <MemoryRouter>
          <ApplicationsPage />
        </MemoryRouter>
      </AppProviders>,
    );

    expect(await screen.findByText("Role One")).toBeTruthy();
    expect(screen.getAllByText(/^Role (One|Two)$/).map((item) => item.textContent)).toEqual([
      "Role Two",
      "Role One",
    ]);
    expect(screen.getByText("Fit 40")).toBeTruthy();
    expect(screen.getByText("Overall 50")).toBeTruthy();
  });
});
