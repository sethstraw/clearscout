import { describe, expect, it } from "vitest";

import { compareValues, nextSort } from "./sorting";

describe("sorting helpers", () => {
  it("toggles an active column and starts a new column ascending", () => {
    expect(nextSort("fit", "asc", "fit")).toEqual({ key: "fit", direction: "desc" });
    expect(nextSort("fit", "desc", "overall")).toEqual({
      key: "overall",
      direction: "asc",
    });
  });

  it("keeps missing values last in either direction", () => {
    const values = [null, 20, 10];
    expect([...values].sort((left, right) => compareValues(left, right, "asc"))).toEqual([
      10,
      20,
      null,
    ]);
    expect([...values].sort((left, right) => compareValues(left, right, "desc"))).toEqual([
      20,
      10,
      null,
    ]);
  });
});
