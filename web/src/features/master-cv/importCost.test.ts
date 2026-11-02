import { describe, expect, it } from "vitest";

import { needsModelRead } from "./importCost";

describe("needsModelRead", () => {
  it("sends every non-text format to the model", () => {
    for (const name of ["CV.docx", "CV.pdf", "scan.png", "CV"]) {
      expect(needsModelRead(name)).toBe(true);
    }
  });

  it("lets the server adopt Markdown and plain text without a model call", () => {
    for (const name of ["CV_Master.md", "CV.markdown", "CV.txt"]) {
      expect(needsModelRead(name)).toBe(false);
    }
  });

  it("reads the suffix regardless of case", () => {
    expect(needsModelRead("CV.MD")).toBe(false);
    expect(needsModelRead("CV.TXT")).toBe(false);
  });
});
