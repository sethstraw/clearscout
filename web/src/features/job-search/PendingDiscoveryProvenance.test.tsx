import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { PendingDiscovery } from "../../api/client";
import { PendingDiscoveryProvenance } from "./PendingDiscoveryProvenance";

const emptyFacts = { values: [], unresolved_values: [], provenance: "", complete: false };

function discovery(freshness: "unknown" | "likely_stale" | "verified_current"): PendingDiscovery {
  return {
    id: "pending-1",
    employer: "Example Health",
    title: "Product Manager",
    location: "Northtown",
    discovery_url: "https://careers.example.com/postings/1234",
    canonical_url: "https://careers.example.com/postings/1234",
    content_status: "readable",
    content_note: "",
    reason: "Found on a publisher page.",
    first_seen_on: "2026-08-24",
    rank: 0,
    favorite: false,
    kept: false,
    keep_note: null,
    observed_publisher_employer: "",
    observed_publisher_title: "",
    recommendation: "investigate",
    recommendation_because: "Needs evaluation.",
    constraint_confidence: "investigate",
    screening_standing: "unknown",
    screening_findings: [],
    readiness: "ready",
    arrangement_categories: ["unknown"],
    arrangement_categories_source: "missing",
    staged_facts: {
      schema_version: 5,
      identity_trust: "publisher",
      freshness,
      locations: { ...emptyFacts, values: ["Northtown"] },
      arrangements: emptyFacts,
      employment_types: emptyFacts,
      compensation: emptyFacts,
    },
    current_robots_basis: null,
    staging_provenance: "board_scan",
  };
}

describe("PendingDiscoveryProvenance", () => {
  it("says a likely stale posting rests on the publisher's own expiry date", () => {
    render(<PendingDiscoveryProvenance discovery={discovery("likely_stale")} />);

    expect(screen.getByText("Likely stale")).toBeTruthy();
    expect(
      screen.getByText(/publisher's own expiry date for this posting has passed/),
    ).toBeTruthy();
    expect(screen.getByText(/has not been checked for closure/)).toBeTruthy();
  });

  it("says a verified current posting outlived the board listing that found it", () => {
    render(<PendingDiscoveryProvenance discovery={discovery("verified_current")} />);

    expect(screen.getByText("Verified current")).toBeTruthy();
    expect(
      screen.getByText(
        "The board no longer lists this posting, but a later read of the posting itself found it open.",
      ),
    ).toBeTruthy();
  });

  it("states nothing about expiry when freshness is unknown", () => {
    render(<PendingDiscoveryProvenance discovery={discovery("unknown")} />);

    expect(screen.queryByText("Likely stale")).toBeNull();
    expect(screen.queryByText(/expiry date/)).toBeNull();
  });
});
