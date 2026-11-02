import { describe, expect, it } from "vitest";

import { storedFileName } from "./documentNames";

const RUN_ROOT = "applications/_workbench/tailorcv/Globex_Platform_Director--gh-100/runs";

describe("the name a stored file shows", () => {
  it("separates two run records that share the file name, by the run folder", () => {
    const first = storedFileName({
      relative_path: `${RUN_ROOT}/2026-08-05T173319Z-8e0253b4-054598d5/run.json`,
    });
    const second = storedFileName({
      relative_path: `${RUN_ROOT}/2026-08-05T174107Z-8e0253b4-054598d5-letter/run.json`,
    });

    expect(first).toBe("2026-08-05T173319Z-8e0253b4-054598d5/run.json");
    expect(second).toBe("2026-08-05T174107Z-8e0253b4-054598d5-letter/run.json");
  });

  it("leaves a file the import already named to itself", () => {
    expect(
      storedFileName({
        relative_path:
          "applications/2026/Initech_Data_Engineer/Resume_2026_Initech_Data_Engineer.pdf",
      }),
    ).toBe("Resume_2026_Initech_Data_Engineer.pdf");
    expect(storedFileName({ relative_path: `${RUN_ROOT}/index.json` })).toBe("index.json");
    expect(storedFileName({})).toBe("");
  });
});
