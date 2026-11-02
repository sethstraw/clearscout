import { describe, expect, it } from "vitest";

import { postingHost, postingLabel, titleFallback } from "./fallbacks";

describe("postingHost", () => {
  it("drops a leading www for display", () => {
    expect(postingHost("https://www.example.com/jobs/1")).toBe("example.com");
  });

  it("returns an empty host for a missing or unparseable url", () => {
    expect(postingHost(null)).toBe("");
    expect(postingHost("not a url")).toBe("");
  });
});

describe("titleFallback", () => {
  it("returns a generic phrase when the url is null", () => {
    expect(titleFallback(null)).toBe("this posting");
  });

  it("names the host, never an ATS tenant slug", () => {
    expect(titleFallback("https://jobs.lever.co/northwind/abc")).toBe("posting at jobs.lever.co");
    expect(titleFallback("https://job-boards.greenhouse.io/initechhealth/jobs/999")).toBe(
      "posting at job-boards.greenhouse.io",
    );
  });

  it("names the host when the url is a plain domain", () => {
    expect(titleFallback("https://careers.example.com/roles/1")).toBe(
      "posting at careers.example.com",
    );
  });

  it("returns the generic phrase when the url is unparseable", () => {
    expect(titleFallback("not a url")).toBe("this posting");
  });
});

describe("postingLabel", () => {
  const url = "https://jobs.lever.co/northwind/abc";

  it("names the stored employer", () => {
    expect(postingLabel("Data Steward", "Globex", url)).toBe("Data Steward at Globex");
  });

  it("names the listing host as the source when the employer is not stated", () => {
    expect(postingLabel("Data Steward", "", url)).toBe(
      "Data Steward, employer not stated, listed on jobs.lever.co",
    );
  });

  it("names the host once when it already stands in for the title", () => {
    expect(postingLabel("", null, url)).toBe("posting at jobs.lever.co, employer not stated");
    expect(postingLabel("", null, null)).toBe("this posting, employer not stated");
  });
});
