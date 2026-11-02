import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DiscoveryScreeningDetails } from "./DiscoveryScreeningDetails";

describe("DiscoveryScreeningDetails", () => {
  it("keeps incomplete publisher evidence visibly unknown", () => {
    render(
      <DiscoveryScreeningDetails
        discovery={{
          screening_findings: [
            {
              code: "location_unresolved",
              dimension: "location",
              standing: "unknown",
              subject: "",
            },
          ],
          screening_standing: "unknown",
          staged_facts: {
            schema_version: 5,
            identity_trust: "publisher",
            role_aliases: {
              values: ["Data Team Lead"],
              unresolved_values: [],
              provenance: "workday.job_description",
              complete: true,
            },
            locations: {
              values: [],
              unresolved_values: ["4 Locations"],
              provenance: "synthetic_board.location",
              complete: false,
            },
            arrangements: { values: [], unresolved_values: [], provenance: "", complete: false },
            employment_types: {
              values: [],
              unresolved_values: [],
              provenance: "",
              complete: false,
            },
            compensation: { values: [], unresolved_values: [], provenance: "", complete: false },
            freshness: "unknown",
          },
        }}
      />,
    );

    expect(screen.getByText(/location unresolved/)).toBeTruthy();
    expect(screen.getByText(/came from the publisher/)).toBeTruthy();
    expect(screen.getByText(/unresolved: 4 Locations/)).toBeTruthy();
    expect(screen.getByText("Data Team Lead")).toBeTruthy();
    expect(screen.getByText(/workday.job_description/)).toBeTruthy();
    expect(screen.getAllByText(/publisher answer incomplete/)).toHaveLength(4);
  });

  it("labels search identity text as unverified", () => {
    render(
      <DiscoveryScreeningDetails
        discovery={{
          screening_findings: [
            {
              code: "title_unverified",
              dimension: "title",
              standing: "unknown",
              subject: "",
            },
          ],
          screening_standing: "unknown",
          staged_facts: {
            schema_version: 5,
            identity_trust: "unverified_hint",
            locations: { values: [], unresolved_values: [], provenance: "", complete: false },
            arrangements: { values: [], unresolved_values: [], provenance: "", complete: false },
            employment_types: {
              values: [],
              unresolved_values: [],
              provenance: "",
              complete: false,
            },
            compensation: { values: [], unresolved_values: [], provenance: "", complete: false },
            freshness: "unknown",
          },
        }}
      />,
    );

    expect(screen.getByText(/stored identity value remains an unverified hint/)).toBeTruthy();
    expect(screen.getByText(/title unverified/)).toBeTruthy();
  });
});
