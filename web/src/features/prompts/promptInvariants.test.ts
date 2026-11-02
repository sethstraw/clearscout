import { describe, expect, it } from "vitest";

import { invariantBlocks, preserveInvariantBlocks } from "./promptInvariants";

const BASELINE = [
  "Before.",
  "<!--invariant-->",
  "Keep this rule.",
  "<!--/invariant-->",
  "After.",
].join("\n");

// The server's own suite asserts these same three strings, because this editor
// decides what to protect and the server decides what to accept.
const INLINE_MARKER = ["Rule text <!--invariant-->", "Body.", "<!--/invariant-->"].join("\n");
const JOINED_OPENER = ["<!--invariant-->Body.", "<!--/invariant-->"].join("\n");
const TWO_REGIONS = [
  "<!--invariant-->",
  "First.",
  "<!--/invariant-->",
  "Between.",
  "<!--invariant-->",
  "Second.",
  "<!--/invariant-->",
].join("\n");

describe("prompt invariant regions", () => {
  it("extracts each fenced rule without losing its exact source block", () => {
    const [block] = invariantBlocks(BASELINE);

    expect(block?.body).toBe("Keep this rule.\n");
    expect(block?.raw).toBe("<!--invariant-->\nKeep this rule.\n<!--/invariant-->");
  });

  it("treats a marker that does not start its own line as ordinary text", () => {
    // The server does not treat these as invariant regions, so the editor must not either.
    expect(invariantBlocks(INLINE_MARKER)).toEqual([]);
    expect(invariantBlocks(JOINED_OPENER)).toEqual([]);
    expect(preserveInvariantBlocks("anything", INLINE_MARKER)).toBe("anything");
  });

  it("finds every marked region and takes each one verbatim", () => {
    expect(invariantBlocks(TWO_REGIONS).map((block) => block.body)).toEqual([
      "First.\n",
      "Second.\n",
    ]);
  });

  it("restores a protected edit while preserving text outside the fence", () => {
    const changed = BASELINE.replace("Before.", "Changed outside.").replace(
      "Keep this rule.",
      "Changed inside.",
    );

    expect(preserveInvariantBlocks(changed, BASELINE)).toBe(
      BASELINE.replace("Before.", "Changed outside."),
    );
  });

  it("rejects a draft that removes a protected fence", () => {
    expect(preserveInvariantBlocks("Changed outside.", BASELINE)).toBe(BASELINE);
  });
});
