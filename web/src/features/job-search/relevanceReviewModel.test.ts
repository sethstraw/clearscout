import { describe, expect, it } from "vitest";

import type { RelevanceReview, RelevanceReviewRejection } from "../../api/client";
import {
  RELEVANCE_ORDER,
  STALE_INPUT_ORDER,
  discoveryRelevanceKey,
  modelRelevanceKey,
  rejectionFollowsReview,
  rejectionReasonLabel,
  relevanceLabel,
  relevanceRank,
  relevanceReviewIsOutOfDate,
  relevanceReviewsByPending,
  staleInputLabel,
  staleInputTone,
} from "./relevanceReviewModel";

function review(overrides: Partial<RelevanceReview> = {}): RelevanceReview {
  return {
    id: "review-1",
    pending_discovery_id: "pending-1",
    employer: "Globex",
    title: "Role Alpha",
    outcome: "judged",
    recommendation: "review",
    summary: "A relevant match.",
    core_work_alignment: "direct",
    core_qualification_alignment: "established",
    hard_condition: "passes",
    decisive_basis: "core_match",
    posting_citations: [],
    master_cv_evidence_ids: [],
    unresolved_questions: [],
    fetched_at: "2026-08-28T00:00:00Z",
    task_id: "task-1",
    master_cv_version_id: "cv-current",
    search_parameter_set_id: "parameters-current",
    out_of_date_because: [],
    reviewed_at: "2026-08-28T00:01:00Z",
    matched_exclusion: null,
    ...overrides,
  };
}

function rejection(overrides: Partial<RelevanceReviewRejection> = {}): RelevanceReviewRejection {
  return {
    pending_discovery_id: "pending-1",
    failure_code: "invalid_cv_evidence",
    detail: "The result cited evidence outside the selected master CV.",
    rejected_at: "2026-09-20T00:00:00Z",
    task_id: "task-9",
    ...overrides,
  };
}

describe("relevance review projection", () => {
  it("keys the authoritative current review by pending discovery", () => {
    const current = review();

    expect(relevanceReviewsByPending([current]).get("pending-1")).toBe(current);
  });

  it("uses relevance language for every review verdict", () => {
    expect(relevanceLabel("review")).toBe("High relevance");
    expect(relevanceLabel("investigate")).toBe("Needs closer look");
    expect(relevanceLabel("unlikely")).toBe("Low relevance");
    expect(relevanceLabel("insufficient_evidence")).toBe("Insufficient evidence");
  });
});

describe("relevance projection for the queue", () => {
  it("projects a missing review as the not-yet-reviewed state", () => {
    expect(discoveryRelevanceKey(undefined)).toBe("not_reviewed");
    expect(relevanceLabel("not_reviewed")).toBe("Not yet reviewed");
  });

  it("labels a present review with its verdict language", () => {
    expect(discoveryRelevanceKey(review({ recommendation: "investigate" }))).toBe("investigate");
    expect(relevanceLabel("investigate")).toBe("Needs closer look");
  });

  it("keys posting text too short to review apart from every verdict", () => {
    const stub = review({
      outcome: "insufficient_posting_text",
      recommendation: null,
      core_work_alignment: null,
      core_qualification_alignment: null,
      hard_condition: null,
      decisive_basis: null,
    });

    expect(discoveryRelevanceKey(stub)).toBe("insufficient_posting_text");
    expect(relevanceLabel("insufficient_posting_text")).toBe("Posting text too short to review");
  });

  it("orders High relevance, Needs closer look, Disputed, Rejected, Not yet reviewed, Insufficient, Low", () => {
    expect(RELEVANCE_ORDER).toEqual([
      "review",
      "investigate",
      "disputed",
      "review_rejected",
      "not_reviewed",
      "insufficient_posting_text",
      "insufficient_evidence",
      "unlikely",
    ]);
    const order = RELEVANCE_ORDER.map(relevanceRank);
    expect(order).toEqual([...order].sort((a, b) => a - b));
    expect(relevanceRank("review")).toBeLessThan(relevanceRank("unlikely"));
    expect(relevanceRank("not_reviewed")).toBeLessThan(relevanceRank("insufficient_evidence"));
  });

  it("keys a disputed verdict apart from the verdict the model recorded", () => {
    const disputed = review({ recommendation: "unlikely", disputed_at: "2026-09-17T00:00:00Z" });

    expect(discoveryRelevanceKey(disputed)).toBe("disputed");
    expect(modelRelevanceKey(disputed)).toBe("unlikely");
    expect(relevanceLabel("disputed")).toBe("Disputed by you");
  });

  it("ranks a dispute above the unreviewed backlog and below every model verdict", () => {
    expect(relevanceRank("investigate")).toBeLessThan(relevanceRank("disputed"));
    expect(relevanceRank("review")).toBeLessThan(relevanceRank("disputed"));
    expect(relevanceRank("disputed")).toBeLessThan(relevanceRank("not_reviewed"));
  });

  it("keys a row whose paid review was rejected apart from one never reviewed", () => {
    expect(discoveryRelevanceKey(undefined, rejection())).toBe("review_rejected");
    expect(discoveryRelevanceKey(undefined, null)).toBe("not_reviewed");
    expect(relevanceLabel("review_rejected")).toBe("Paid review returned nothing usable");
    expect(rejectionReasonLabel(rejection())).toBe(
      "the answer cited CV evidence outside your selected CV",
    );
  });

  it("ranks a rejected review above the unreviewed backlog", () => {
    expect(relevanceRank("review_rejected")).toBeLessThan(relevanceRank("not_reviewed"));
    expect(relevanceRank("disputed")).toBeLessThan(relevanceRank("review_rejected"));
  });

  it("keeps the stored verdict as the badge when a later paid review recorded nothing", () => {
    const stored = review({ recommendation: "unlikely", reviewed_at: "2026-09-18T00:00:00Z" });
    const later = rejection({ rejected_at: "2026-09-20T00:00:00Z" });

    expect(discoveryRelevanceKey(stored, later)).toBe("unlikely");
    expect(rejectionFollowsReview(stored, later)).toBe(true);
  });

  it("reports a mark on a row with no stored verdict as the newest paid review", () => {
    expect(rejectionFollowsReview(undefined, rejection())).toBe(true);
  });

  it("does not report a mark older than the stored verdict as the newest paid review", () => {
    const stored = review({ reviewed_at: "2026-09-21T00:00:00Z" });

    expect(rejectionFollowsReview(stored, rejection({ rejected_at: "2026-09-20T00:00:00Z" }))).toBe(
      false,
    );
  });
});

describe("relevance review staleness", () => {
  it("is out of date exactly when the server names a changed input", () => {
    expect(relevanceReviewIsOutOfDate(review())).toBe(false);
    expect(relevanceReviewIsOutOfDate(review({ out_of_date_because: ["master_cv"] }))).toBe(true);
    expect(
      relevanceReviewIsOutOfDate(review({ out_of_date_because: ["review_instructions"] })),
    ).toBe(true);
  });

  it("names each changed input with its own label and tone, in a fixed order", () => {
    expect(STALE_INPUT_ORDER.map(staleInputLabel)).toEqual([
      "Master CV changed",
      "Search parameters changed",
      "Review instructions changed",
      "Your keep note changed",
    ]);
    expect(STALE_INPUT_ORDER.map(staleInputTone)).toEqual(["accent", "info", "neutral", "warning"]);
  });
});
