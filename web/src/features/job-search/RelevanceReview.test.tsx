import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw/http";
import { describe, expect, it } from "vitest";

import type { RelevanceReview as RelevanceReviewData } from "../../api/client";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { RelevanceReview } from "./RelevanceReview";

const WORKSPACE_ID = "workspace-1";

function reviewData(overrides: Partial<RelevanceReviewData> = {}): RelevanceReviewData {
  return {
    id: "review-1",
    pending_discovery_id: "pending-1",
    employer: "Northwind Research",
    title: "Platform Lead",
    outcome: "judged",
    recommendation: "investigate",
    summary: "The platform scope is relevant, while the reporting line is unclear.",
    core_work_alignment: "direct",
    core_qualification_alignment: "established",
    hard_condition: "unresolved",
    decisive_basis: "logistics_unknown",
    posting_citations: [
      {
        span_id: "pending-1:span-0001",
        text: "Own the platform roadmap and delivery outcomes.",
        char_start: 0,
        char_end: 47,
      },
    ],
    master_cv_evidence_ids: ["work-1"],
    unresolved_questions: ["Who does this role report to?"],
    fetched_at: "2026-08-28T00:00:00Z",
    task_id: "task-1",
    master_cv_version_id: "cv-1",
    search_parameter_set_id: "parameters-1",
    out_of_date_because: [],
    reviewed_at: "2026-08-28T00:01:00Z",
    matched_exclusion: null,
    ...overrides,
  } satisfies RelevanceReviewData;
}

function card(review: RelevanceReviewData) {
  return render(
    <AppProviders>
      <RelevanceReview review={review} workspaceId={WORKSPACE_ID} />
    </AppProviders>,
  );
}

describe("RelevanceReview", () => {
  it("shows automated relevance evidence and offers one per-row dispute", () => {
    card(reviewData());

    expect(screen.getByText(/Automated relevance review: Needs closer look/)).toBeTruthy();
    expect(screen.queryByText("Review is out of date.")).toBeNull();
    expect(screen.getByText(/platform scope is relevant/)).toBeTruthy();
    expect(screen.getByText("Review relevance evidence and unresolved questions")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Dispute this verdict" })).toBeTruthy();
    expect(screen.queryByText("Disputed by you.")).toBeNull();
  });

  it("names each changed input on an out-of-date review", () => {
    card(reviewData({ out_of_date_because: ["search_parameters", "master_cv"] }));

    expect(screen.getByText("Review is out of date.")).toBeTruthy();
    const reasons = screen.getByRole("list", { name: "Changed since this review" });
    expect(
      within(reasons)
        .getAllByRole("listitem")
        .map((tag) => tag.textContent),
    ).toEqual(["Master CV changed", "Search parameters changed"]);
  });

  it("names changed review instructions on an out-of-date review", () => {
    card(reviewData({ out_of_date_because: ["review_instructions"] }));

    expect(screen.getByText("Review is out of date.")).toBeTruthy();
    expect(screen.getByText("Review instructions changed")).toBeTruthy();
  });

  it("shows no out-of-date banner or tags on a current review", () => {
    card(reviewData({ out_of_date_because: [] }));

    expect(screen.queryByText("Review is out of date.")).toBeNull();
    expect(screen.queryByRole("list", { name: "Changed since this review" })).toBeNull();
    expect(screen.queryByText(/changed$/)).toBeNull();
  });

  it("names the excluded condition a hard conflict matched", () => {
    card(
      reviewData({
        recommendation: "unlikely",
        hard_condition: "conflicts",
        decisive_basis: "hard_conflict",
        matched_exclusion: "Requires living in Ontario",
      }),
    );

    expect(
      screen.getByText("Matches your excluded condition: Requires living in Ontario"),
    ).toBeTruthy();
  });

  it("shows no conflict line when no exclusion matched", () => {
    card(reviewData({ matched_exclusion: null }));

    expect(screen.queryByText(/Matches your excluded condition/)).toBeNull();
  });

  it("shows posting text too short to review without a verdict or classes", () => {
    card(
      reviewData({
        outcome: "insufficient_posting_text",
        recommendation: null,
        core_work_alignment: null,
        core_qualification_alignment: null,
        hard_condition: null,
        decisive_basis: null,
        summary: "Only the listing excerpt was available.",
      }),
    );

    expect(
      screen.getByText(/Automated relevance review: Posting text too short to review/),
    ).toBeTruthy();
    expect(screen.queryByText("Decisive basis")).toBeNull();
  });

  it("sends the disputed verdict's own review id with the person's note", async () => {
    const sent: unknown[] = [];
    server.use(
      http.put("*/api/v1/workspaces/:workspaceId/relevance-disputes/:pendingId", async (info) => {
        sent.push({ pendingId: info.params.pendingId, body: await info.request.json() });
        return HttpResponse.json({
          pending_discovery_id: "pending-1",
          relevance_review_id: "review-1",
          disposition: "set_aside",
          note: "The reporting line is stated in the posting.",
          updated_at: "2026-09-17T00:00:00Z",
        });
      }),
    );
    card(reviewData());

    fireEvent.click(screen.getByRole("button", { name: "Dispute this verdict" }));
    fireEvent.change(screen.getByLabelText("Why this verdict is wrong (optional)"), {
      target: { value: "The reporting line is stated in the posting." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Set this verdict aside" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toEqual({
      pendingId: "pending-1",
      body: {
        relevance_review_id: "review-1",
        note: "The reporting line is stated in the posting.",
      },
    });
  });

  it("keeps the model's verdict readable beside a dispute and offers to restore it", async () => {
    const cleared: string[] = [];
    server.use(
      http.delete("*/api/v1/workspaces/:workspaceId/relevance-disputes/:pendingId", (info) => {
        cleared.push(String(info.params.pendingId));
        return HttpResponse.json({ pending_discovery_id: "pending-1", cleared: true });
      }),
    );
    card(
      reviewData({
        disputed_at: "2026-09-17T00:00:00Z",
        dispute_note: "The platform scope is on my record.",
      }),
    );

    expect(screen.getByText(/Automated relevance review: Needs closer look/)).toBeTruthy();
    expect(screen.getByText("Disputed by you.")).toBeTruthy();
    expect(screen.getByText("The platform scope is on my record.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Restore the model verdict" }));

    await waitFor(() => expect(cleared).toEqual(["pending-1"]));
  });
});
