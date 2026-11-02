import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { JobDetail } from "../../api/client";
import { JobEvaluation } from "./JobEvaluation";
import { jobDetail } from "./jobDetailFixtures";

function scored(evidence_links: JobDetail["score_arithmetic"][number]["evidence_links"]) {
  return jobDetail({
    score_arithmetic: [
      {
        component: "fit",
        score: 83,
        weight: 0.35,
        product: 29.05,
        finding_state: "supported",
        standing_source: null,
        reason: "",
        evidence_links,
      },
    ],
  });
}

function renderEvaluation(data: JobDetail) {
  return render(
    <JobEvaluation
      data={data}
      evaluationDirection="asc"
      evaluationOffset={0}
      evaluationSort="version_number"
      onEvaluationPageChange={vi.fn()}
      onOpenStudio={vi.fn()}
      onShowEvidence={vi.fn()}
      onSortEvaluations={vi.fn()}
      onSortScores={vi.fn()}
      pageSize={10}
      scoreDirection="asc"
      scoreSort="component"
      workspaceId="workspace-1"
    />,
  );
}

describe("the evidence count on a score", () => {
  it("names the direction when every citation limits the score", async () => {
    renderEvaluation(scored([{ evidence_id: "evidence-1", direction: "limits" }]));

    expect(await screen.findByRole("button", { name: "1 source (limits)" })).toBeTruthy();
  });

  it("counts the citations that do not support the score", async () => {
    renderEvaluation(
      scored([
        { evidence_id: "evidence-1", direction: "supports" },
        { evidence_id: "evidence-2", direction: "limits" },
        { evidence_id: "evidence-3", direction: "contradicts" },
      ]),
    );

    expect(
      await screen.findByRole("button", { name: "3 sources (2 contradicts and limits)" }),
    ).toBeTruthy();
  });

  it("says nothing extra when every citation supports the score", async () => {
    renderEvaluation(
      scored([
        { evidence_id: "evidence-1", direction: "supports" },
        { evidence_id: "evidence-2", direction: "supports" },
      ]),
    );

    expect(await screen.findByRole("button", { name: "2 sources" })).toBeTruthy();
  });
});
