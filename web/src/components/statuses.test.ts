import { describe, expect, it } from "vitest";

import {
  APPLICATION_EVENT_STATUS_VALUES,
  APPLICATION_STATUS_VALUES,
  PURSUED_STATUSES,
  applicationEventStatusValues,
  statusEntry,
} from "./statuses";

describe("application status vocabulary", () => {
  it("withholds package ready after the application has progressed", () => {
    expect(applicationEventStatusValues("applied")).not.toContain("package_ready");
    expect(applicationEventStatusValues("closed_before_application")).not.toContain(
      "package_ready",
    );
    expect(applicationEventStatusValues("not_applied")).toContain("package_ready");
    expect(applicationEventStatusValues("not_applied", true)).not.toContain("package_ready");
  });

  it("derives selectable values from the exhaustive toned vocabulary", () => {
    expect(APPLICATION_STATUS_VALUES.every((status) => statusEntry(status))).toBe(true);
    expect(APPLICATION_EVENT_STATUS_VALUES).toEqual(
      APPLICATION_STATUS_VALUES.filter((status) => status !== "not_applied"),
    );
    expect(PURSUED_STATUSES.every((status) => APPLICATION_STATUS_VALUES.includes(status))).toBe(
      true,
    );
  });
});
