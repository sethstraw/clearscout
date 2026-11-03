import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DiscoveryTable } from "./DiscoveryTable";

describe("DiscoveryTable", () => {
  it("renders the shared discovery columns and external posting link", () => {
    render(
      <DiscoveryTable
        discoveries={[
          {
            employer: "Acme O'Hara Systems",
            location: "Remote",
            note: "Publisher reports this posting has closed.",
            title: "Senior Data Platform Engineer",
            url: "https://example.com/job",
            constraint_confidence: "pursue",
          },
        ]}
      />,
    );

    expect(screen.queryByRole("columnheader", { name: "Source" })).toBeNull();
    expect(
      screen.getByRole("link", { name: "Senior Data Platform Engineer" }).getAttribute("href"),
    ).toBe("https://example.com/job");
    expect(screen.getByText("Acme O'Hara Systems")).toBeTruthy();
    expect(screen.getByText("Remote")).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "Publisher note" })).toBeTruthy();
    expect(screen.getByText("Publisher reports this posting has closed.")).toBeTruthy();
    expect(screen.getByText("Pursue").getAttribute("title")).toContain("known hard");
  });

  it("drops the confidence column when no row reports a route", () => {
    render(
      <DiscoveryTable
        discoveries={[
          {
            employer: "Northwind Research",
            location: "Chennai, Tamil Nadu, India",
            title: "Director - Infrastructure Security Engineering",
            url: "https://example.com/job-1",
          },
        ]}
      />,
    );

    expect(screen.queryByRole("columnheader", { name: "Local confidence route" })).toBeNull();
    expect(screen.queryByText("Not reported")).toBeNull();
    expect(screen.getByText("Northwind Research")).toBeTruthy();
  });

  it("explains the local confidence routes without turning them into rankings", () => {
    render(
      <DiscoveryTable
        discoveries={[
          {
            employer: "Northwind",
            location: "Remote",
            title: "Data Lead",
            url: "https://example.com/northwind",
            constraint_confidence: "needs_verification",
          },
          {
            employer: "Globex",
            location: "Remote",
            title: "Platform Lead",
            url: "https://example.com/globex",
            constraint_confidence: "uncertain_comp",
          },
          {
            employer: "Initech",
            location: "Elsewhere",
            title: "Data Architect",
            url: "https://example.com/initech",
            constraint_confidence: "excluded",
          },
        ]}
      />,
    );

    expect(screen.getByRole("columnheader", { name: "Local confidence route" })).toBeTruthy();
    expect(screen.getByText("Needs verification").getAttribute("title")).toContain(
      "eligibility or location",
    );
    expect(screen.getByText("Compensation uncertain").getAttribute("title")).toContain(
      "compensation constraint",
    );
    expect(screen.getByText("Excluded").getAttribute("title")).toContain("hard search constraint");
  });

  it("does not make an active-scheme URL clickable", () => {
    render(
      <DiscoveryTable
        discoveries={[
          {
            employer: "Acme O'Hara Systems",
            location: "",
            title: "Senior Data Platform Engineer",
            url: "javascript:alert(1)",
          },
        ]}
      />,
    );

    expect(screen.queryByRole("link", { name: "Senior Data Platform Engineer" })).toBeNull();
    expect(screen.getByText("Senior Data Platform Engineer")).toBeTruthy();
  });

  it("credits only the rows that name an aggregator", () => {
    render(
      <DiscoveryTable
        discoveries={[
          {
            aggregator: {
              credit_url: "https://listings.example.test/",
              name: "Example Listings",
              opens_discovery_url: false,
            },
            employer: "Contoso",
            location: "Berlin",
            title: "Analyst",
            url: "https://jobs.example.test/contoso/5",
          },
          {
            aggregator: null,
            employer: "Northwind Research",
            location: "Remote",
            title: "Engineer",
            url: "https://jobs.example.test/1",
          },
        ]}
      />,
    );

    expect(screen.getByRole("link", { name: "Analyst" }).getAttribute("href")).toBe(
      "https://jobs.example.test/contoso/5",
    );
    expect(
      screen.getByRole("link", { name: "Job data from Example Listings" }).getAttribute("href"),
    ).toBe("https://listings.example.test/");
    expect(screen.getAllByRole("link", { name: /^Job data from/ })).toHaveLength(1);
  });
});
