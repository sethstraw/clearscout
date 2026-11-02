import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { jobDetail } from "./jobDetailFixtures";
import { JobSummary } from "./JobSummary";

describe("JobSummary", () => {
  it("shows human scores for a job without an evaluation", () => {
    render(
      <MemoryRouter>
        <JobSummary
          data={jobDetail({
            evaluation_id: null,
            evaluation_version: null,
            decision: null,
            fit_score: null,
            compensation_score: null,
            recorded_fit_score: 52,
            recorded_compensation_score: 0,
            recorded_overall_score: 61,
          })}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("Not evaluated")).toBeTruthy();
    expect(screen.getByText("52")).toBeTruthy();
    expect(screen.getByText("0")).toBeTruthy();
    expect(screen.getByText("61")).toBeTruthy();
    expect(screen.getAllByText("your review")).toHaveLength(3);
  });
});
