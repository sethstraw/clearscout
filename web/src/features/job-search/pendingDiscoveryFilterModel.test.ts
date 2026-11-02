import { describe, expect, it } from "vitest";

import {
  DEFAULT_QUEUE_FILTERS,
  MAX_SELECTION_SIZE,
  UNFILTERED_QUEUE,
  pendingQueueQuery,
  contentNeedsRecovery,
  selectAllRefusal,
} from "./pendingDiscoveryFilterModel";
import { pendingDiscoveryRow } from "./pendingDiscoveryFixtures";

const sent = (query: object) =>
  Object.fromEntries(Object.entries(query).filter(([, value]) => value !== undefined));

describe("pendingQueueQuery", () => {
  it("keeps a readable screening refusal out of posting recovery", () => {
    expect(contentNeedsRecovery(pendingDiscoveryRow({ readiness: "screening_refused" }))).toBe(
      false,
    );
  });
  it("includes every content state among not excluded rows by default", () => {
    expect(sent(pendingQueueQuery(DEFAULT_QUEUE_FILTERS))).toEqual({
      constraint: "not_excluded",
    });
    expect(sent(pendingQueueQuery(UNFILTERED_QUEUE))).toEqual({});
  });

  it("sends pressed markers, chosen employers and a trimmed search", () => {
    const query = pendingQueueQuery({
      ...UNFILTERED_QUEUE,
      employers: ["Globex", "Initech"],
      markers: { favorite: true, kept: false },
      search: "  data lead ",
    });

    expect(sent(query)).toEqual({
      employer: ["Globex", "Initech"],
      favorite: true,
      search: "data lead",
    });
  });
});

describe("selectAllRefusal", () => {
  it("allows a selection that lands exactly on the bound", () => {
    expect(selectAllRefusal(0, MAX_SELECTION_SIZE)).toBe("");
    expect(selectAllRefusal(230, 20)).toBe("");
  });

  it("points at a smaller page when nothing is selected yet", () => {
    expect(selectAllRefusal(0, 312)).toBe(
      "Select at most 250; 312 of the rows loaded on this page match the filters. " +
        "Set Rows per page to 50 and select a page at a time; your selection is kept across pages.",
    );
  });

  it("counts what is already held and points at it, since paging down cannot help", () => {
    expect(selectAllRefusal(230, 40)).toBe(
      "Select at most 250; 230 already selected and 40 more of the rows loaded on this page match the filters. " +
        "Act on those 230 or clear them first.",
    );
  });
});
