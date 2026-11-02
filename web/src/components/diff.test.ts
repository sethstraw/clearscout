import { describe, expect, it } from "vitest";

import { unifiedDiff } from "./diff";

describe("unifiedDiff", () => {
  it("returns nothing when the two sides are identical", () => {
    expect(unifiedDiff("same\ntext", "same\ntext")).toBe("");
  });

  it("emits one hunk for a one-line edit, with the surrounding context", () => {
    const before = ["a", "b", "c", "d", "e", "f", "g", "h", "i"].join("\n");
    const after = ["a", "b", "c", "d", "CHANGED", "f", "g", "h", "i"].join("\n");

    expect(unifiedDiff(before, after)).toBe(
      ["@@ -2,7 +2,7 @@", " b", " c", " d", "-e", "+CHANGED", " f", " g", " h"].join("\n"),
    );
  });

  it("leaves untouched lines outside the context window out of the diff", () => {
    const before = Array.from({ length: 40 }, (_, index) => `line ${index}`).join("\n");
    const after = before.replace("line 20", "line twenty");
    const lines = unifiedDiff(before, after).split("\n");

    expect(lines[0]).toBe("@@ -18,7 +18,7 @@");
    expect(lines).toContain("-line 20");
    expect(lines).toContain("+line twenty");
    expect(lines).not.toContain(" line 5");
  });

  it("pairs lines by longest common subsequence rather than by position", () => {
    const diff = unifiedDiff("keep\ndrop\nkeep two", "keep\nkeep two");

    expect(diff.split("\n")).toEqual(["@@ -1,3 +1,2 @@", " keep", "-drop", " keep two"]);
  });

  it("counts an insertion at the top from line 1 on both sides", () => {
    const diff = unifiedDiff("body", "heading\nbody");

    expect(diff.split("\n")).toEqual(["@@ -1,1 +1,2 @@", "+heading", " body"]);
  });

  it("treats an empty side as a wholesale replacement", () => {
    expect(unifiedDiff("", "new")).toBe(["@@ -1,1 +1,1 @@", "-", "+new"].join("\n"));
  });

  it("emits a separate hunk per change when the changes are far apart", () => {
    const before = Array.from({ length: 40 }, (_, index) => `line ${index}`).join("\n");
    const after = before.replace("line 5", "five").replace("line 30", "thirty");
    const headers = unifiedDiff(before, after)
      .split("\n")
      .filter((line) => line.startsWith("@@"));

    expect(headers).toHaveLength(2);
  });
});
