import { render, screen } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { JobDetail } from "../../api/client";
import type { PostingObservationBody } from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { requestLog } from "../../test/requestLog";
import { PostingReader } from "./PostingReader";

const observations: JobDetail["observations"] = [
  {
    body_artifact_id: "artifact-1",
    employment_type: "full-time",
    extraction_method: "jsonld",
    fetched_at: "2026-08-10T12:00:00Z",
    http_status: 200,
    id: "observation-1",
    location: "Remote",
    observed_at: "2026-08-10T12:00:00Z",
    payroll_arrangement: "employee",
    posting_status: "active",
    residence_rule: "Canada",
    robots_basis: "allowed",
  },
];

function renderReader(
  sourceUrl: string,
  initialEntry = "/jobs/job-1",
  robotsBasis: "allowed" | "single_posting_read" | null = "allowed",
) {
  const log = requestLog();
  const body = {
    body: "Synthetic posting body",
    body_artifact_id: "artifact-older",
    employment_type: "full-time",
    extraction_method: "html",
    fetched_at: "2026-07-01T12:00:00Z",
    http_status: 200,
    id: "observation-older",
    job_posting_id: "job-1",
    location: "Remote",
    logical_type: "posting.body.html",
    mime_type: "text/html",
    observed_at: "2026-08-10T12:00:00Z",
    payroll_arrangement: "employee",
    posting_status: "active",
    rendered_text: "Synthetic posting body",
    residence_rule: "Canada",
    robots_basis: robotsBasis,
    sha256: "0".repeat(64),
    source_url: sourceUrl,
    workspace_id: "workspace-1",
  } satisfies PostingObservationBody;
  server.use(
    http.get("*/api/v1/posting-observations/:observationId", ({ request }) => {
      log.reads(new URL(request.url));
      return HttpResponse.json(body);
    }),
  );
  render(
    <AppProviders>
      <MemoryRouter initialEntries={[initialEntry]}>
        <PostingReader
          jobId="job-1"
          latestObservation={observations[0] ?? null}
          observationCount={observations.length}
          observationOffset={0}
          observations={observations}
          onObservationPageChange={() => undefined}
          pageSize={50}
          workspaceId="workspace-1"
        />
      </MemoryRouter>
    </AppProviders>,
  );
  return log.requests;
}

describe("PostingReader", () => {
  it("links to an HTTP source", async () => {
    renderReader("https://jobs.example.test/openings/1");

    const source = await screen.findByRole("link", { name: "Source" });
    expect(source.getAttribute("href")).toBe("https://jobs.example.test/openings/1");
    expect(screen.getByText("HTTP status")).toBeTruthy();
    expect(screen.getByText("200")).toBeTruthy();
    expect(screen.getByText("Extraction")).toBeTruthy();
    expect(screen.getByText("jsonld")).toBeTruthy();
    expect(screen.getByRole("link", { name: "artifact-1" }).getAttribute("href")).toContain(
      "/artifacts/artifact-1/content",
    );
  });

  it("credits Himalayas when the observed source is a Himalayas address", async () => {
    const applicationLink = "https://himalayas.app/companies/initech/jobs/data-analyst";
    renderReader(applicationLink);

    const credit = await screen.findByRole("link", { name: "Job data from Himalayas" });
    expect(credit.getAttribute("href")).toBe(applicationLink);
    expect(screen.getByRole("link", { name: "Source" }).getAttribute("href")).toBe(applicationLink);
  });

  it("does not make an active-scheme source clickable", async () => {
    renderReader("javascript:alert(1)");

    expect(await screen.findByText("Original posting")).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Source" })).toBeNull();
  });

  it("exposes the rendered and raw controls as one pressed-state group", async () => {
    renderReader("");

    await screen.findByText("Original posting");
    const group = screen.getByRole("group", { name: "Posting display mode" });
    expect(group).toBeTruthy();
    expect(screen.getByRole("button", { name: "Rendered", pressed: true })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Raw", pressed: false })).toBeTruthy();
  });

  it("keeps an observation deep link outside the loaded history page", async () => {
    const requests = renderReader(
      "https://jobs.example.test/openings/older",
      "?observation=observation-older",
    );

    expect(await screen.findByText("Synthetic posting body")).toBeTruthy();
    const observation = screen.getByLabelText<HTMLSelectElement>("Observation");
    expect(observation.value).toBe("observation-older");
    expect(screen.getByRole("option", { name: /outside loaded page/ })).toBeTruthy();
    const first = requests[0]?.url;
    expect(`${first?.pathname}${first?.search}`).toBe(
      "/api/v1/posting-observations/observation-older?workspace_id=workspace-1",
    );
  });

  it("notes a single posting read when robots.txt disallowed crawling", async () => {
    renderReader(
      "https://jobs.example.test/openings/older",
      "?observation=observation-older",
      "single_posting_read",
    );

    expect(
      await screen.findByText("single page read; robots.txt disallows crawling", {
        exact: false,
      }),
    ).toBeTruthy();
  });

  it("does not note a single posting read when robots.txt allowed the crawl", async () => {
    renderReader(
      "https://jobs.example.test/openings/older",
      "?observation=observation-older",
      "allowed",
    );

    await screen.findByText("Synthetic posting body");
    expect(
      screen.queryByText("single page read; robots.txt disallows crawling", { exact: false }),
    ).toBeNull();
  });
});
