import { describe, expect, it } from "vitest";

import { TASK_KINDS, taskKindOf, taskSpendDisclosure } from "./taskNames";

describe("split search task names", () => {
  it("names each task in the search family on its own", () => {
    expect(TASK_KINDS["search.guided_campaign"].name).toBe("Search for new jobs");
    expect(TASK_KINDS["search.guided_campaign"].detail).toContain("Search the web");
    expect(TASK_KINDS["search.vocabulary"].name).toBe("Suggest search vocabulary");
    expect(TASK_KINDS["search.vocabulary"].detail).toContain("Propose CV-grounded search terms");
    expect(TASK_KINDS["discovery.relevance_review"].name).toBe("Review discovery relevance");
    expect(TASK_KINDS["discovery.relevance_review"].detail).toContain(
      "Review selected readable discoveries",
    );
  });
});

describe("taskKindOf", () => {
  it("names an evaluation of a tracked posting as a re-evaluation", () => {
    const single = taskKindOf({ task_type: "discovery.evaluate", job_posting_ids: ["job-1"] });
    expect(single.name).toBe("Re-evaluate a tracked posting");
    expect(single.detail).toContain("Evaluate the tracked posting again");
    expect(
      taskKindOf({ task_type: "discovery.evaluate_batch", job_posting_ids: ["job-1"] }).name,
    ).toBe("Re-evaluate tracked postings");
    expect(taskKindOf({ task_type: "discovery.evaluate", job_posting_ids: [] }).name).toBe(
      "Evaluate a discovery",
    );
  });
});

describe("taskSpendDisclosure", () => {
  it("states each paid search task's real charge shape", () => {
    expect(taskSpendDisclosure("search.guided_campaign")).toContain("web-content-retrieval");
    expect(taskSpendDisclosure("search.vocabulary")).toBe(
      "One provider model-token charge per invocation. No search fee.",
    );
    expect(taskSpendDisclosure("discovery.relevance_review")).toBe(
      "Provider model-token charges may apply per reviewed batch.",
    );
  });

  it("discloses nothing for an unpaid retry or an unrelated task type", () => {
    expect(taskSpendDisclosure("search.guided_campaign_retry")).toBe("");
    expect(taskSpendDisclosure("posting.refresh")).toBe("");
  });
});
