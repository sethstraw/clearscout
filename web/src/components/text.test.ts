import { describe, expect, it } from "vitest";

import { employmentTypeLabel } from "./text";

describe("employmentTypeLabel", () => {
  it.each([
    ["FULL_TIME", "Full time"],
    ["Full time", "Full time"],
    ["FullTime", "Full time"],
    ["Full Time", "Full time"],
    ["full-time", "Full time"],
    ["PART_TIME", "Part time"],
    ["CONTRACTOR", "Contract"],
    ["contract", "Contract"],
    ["TEMPORARY", "Temporary"],
    ["INTERN", "Internship"],
    ["Internship", "Internship"],
  ])("reads %s as %s", (stored, label) => {
    expect(employmentTypeLabel(stored)).toBe(label);
  });

  it("passes an unknown value through readably", () => {
    expect(employmentTypeLabel("PER_DIEM")).toBe("Per diem");
    expect(employmentTypeLabel("  seasonal  ")).toBe("Seasonal");
  });
});
