import { describe, expect, it } from "vitest";

import { importCostNote } from "./importCost";

describe("importCostNote", () => {
  it("warns that a format the provider must standardize may be charged for", () => {
    for (const name of ["CV.docx", "CV.pdf", "CV.txt", "scan.png", "CV"]) {
      expect(importCostNote(name)).toContain("model-token charges");
      expect(importCostNote(name)).toContain("standardized by the configured provider");
    }
  });

  it("does not promise Markdown is free, because only the server can parse it", () => {
    const note = importCostNote("CV_Master.md");
    expect(note).toContain("adopted without a provider call");
    expect(note).toContain("model-token charges");
  });

  it("reads the suffix regardless of case", () => {
    expect(importCostNote("CV.MD")).toEqual(importCostNote("cv.md"));
  });
});
