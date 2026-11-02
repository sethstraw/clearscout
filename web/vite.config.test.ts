import { describe, expect, it } from "vitest";

import { isIneffectiveDynamicImportWarning } from "./vite.config";

describe("isIneffectiveDynamicImportWarning", () => {
  it("identifies a dynamic import that remains in its static chunk", () => {
    expect(
      isIneffectiveDynamicImportWarning(
        "module is dynamically imported but also statically imported, so dynamic import will not move it into another chunk",
      ),
    ).toBe(true);
  });

  it("leaves unrelated build warnings alone", () => {
    expect(isIneffectiveDynamicImportWarning("Generated an empty chunk.")).toBe(false);
  });
});
