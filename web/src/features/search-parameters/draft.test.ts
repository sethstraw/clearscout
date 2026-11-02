import { describe, expect, it } from "vitest";

import type { JobSearchParameters } from "../../api/client";
import { normalize } from "./draft";

describe("normalize", () => {
  it("reads a place saved without a residence answer as not lived in", () => {
    const older = {
      locations: [{ key: "ca-mb", country: "Canada", eligibility: "authorized" }],
    } as unknown as JobSearchParameters;

    expect(normalize(older).locations.map((place) => [place.key, place.resident])).toEqual([
      ["ca-mb", false],
    ]);
  });

  it("keeps a stated residence", () => {
    const stated = {
      locations: [{ key: "ca-on", country: "Canada", eligibility: "authorized", resident: true }],
    } as unknown as JobSearchParameters;

    expect(normalize(stated).locations[0]?.resident).toBe(true);
  });
});
