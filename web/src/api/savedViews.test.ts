import { describe, expect, it } from "vitest";

import type { SavedView } from "./client";
import { VIEW_KEYS, viewFilters, viewSort, viewText } from "./savedViews";

/** A stored view is free-form JSON, so the fixture takes the configuration and
 *  fills in the record fields the API always sends. */
function makeView(configuration: SavedView["configuration"]): SavedView {
  return {
    id: "view-1",
    workspace_id: "workspace-1",
    resource: "jobs",
    name: "Stored view",
    configuration,
    created_at: "2026-08-01T12:00:00Z",
    archived_at: null,
  };
}

describe("viewFilters", () => {
  it("reads a dict of filters", () => {
    const view = makeView({ filters: { decision: "monitor" }, sort: {} });
    expect(viewFilters(view)).toEqual({ decision: "monitor" });
  });

  it("treats a list of filters as no filters, because it names no keys", () => {
    const view = makeView({ filters: ["decision", "monitor"], sort: {} });
    expect(viewFilters(view)).toEqual({});
  });
});

describe("viewText", () => {
  it("returns a stored string", () => {
    expect(viewText({ decision: "monitor" }, "decision")).toBe("monitor");
  });

  it("clears a key the view does not set", () => {
    expect(viewText({ decision: "monitor" }, "place")).toBe("");
  });

  it("ignores a key whose value is not text", () => {
    expect(viewText({ include_archived: true, status: 3 }, "include_archived")).toBe("");
    expect(viewText({ include_archived: true, status: 3 }, "status")).toBe("");
  });
});

describe("viewSort", () => {
  it("reads a clause stored as a dict", () => {
    expect(viewSort({ key: "fit_score", descending: false })).toEqual({
      key: "fit_score",
      descending: false,
    });
  });

  it("reads the first clause of a list, because the ledger sorts by one column", () => {
    expect(viewSort([{ key: "employer" }, { key: "title" }])).toEqual({
      key: "employer",
      descending: true,
    });
  });

  it("keeps older review sorts when applying a saved view", () => {
    expect(viewSort({ key: "manual_fit", descending: false })).toEqual({
      key: "manual_fit",
      descending: false,
    });
    expect(viewSort({ key: "recorded_fit_score", descending: false })).toEqual({
      key: "fit_score",
      descending: false,
    });
    expect(viewSort({ key: "recorded_compensation_score" })).toEqual({
      key: "compensation_score",
      descending: true,
    });
    expect(viewSort({ key: "tracker_composite_score" })).toEqual({
      key: "recorded_score",
      descending: true,
    });
  });

  it("returns null for anything it cannot obey", () => {
    expect(viewSort({})).toBeNull();
    expect(viewSort([])).toBeNull();
    expect(viewSort({ key: "mood" })).toBeNull();
    expect(viewSort({ key: 7 })).toBeNull();
    expect(viewSort(["fit_score"])).toBeNull();
  });
});

describe("VIEW_KEYS", () => {
  it("names the keys the ledger reads", () => {
    expect(VIEW_KEYS.split(", ")).toContain("include_archived");
  });
});
