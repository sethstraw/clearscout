import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { DiscoveryFunnelAudit } from "./DiscoveryFunnelAudit";

describe("DiscoveryFunnelAudit", () => {
  it("shows why each candidate stopped and links its posting", () => {
    render(
      <MemoryRouter>
        <DiscoveryFunnelAudit
          value={{
            counts: { proposed: 2, screened: 1, staged: 0, rejected: 2 },
            total_outcomes: 2,
            omitted_outcomes: 1,
            omission_reason: "Only the first outcome is retained in task history.",
            outcomes: [
              {
                kind: "board_listing",
                employer: "Globex",
                title: "Warehouse Supervisor",
                location: "Northtown",
                url: "https://jobs.example.test/1",
                source: "Synthetic board",
                stages: ["proposed", "screened", "rejected"],
                reason: "title_not_wanted",
                detail: "",
                screening_findings: [
                  {
                    dimension: "title",
                    code: "title_mismatch",
                    subject: "Data Platform Manager",
                  },
                  {
                    dimension: "title",
                    code: "title_specialist_ic_review",
                    subject: "Data systems architect",
                  },
                ],
              },
            ],
          }}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText(/2 candidates, 0 staged, 2 rejected/)).toBeTruthy();
    expect(screen.getByText("proposed -> screened -> rejected")).toBeTruthy();
    expect(screen.getByRole("note").textContent).toMatch(/1 outcome was omitted/);
    const link = screen.getByRole("link", { name: "Warehouse Supervisor" });
    expect(link.getAttribute("href")).toBe("https://jobs.example.test/1");
    expect(screen.getByText("title not wanted")).toBeTruthy();
    expect(screen.getByText(/title: title mismatch: Data Platform Manager;/)).toBeTruthy();
    expect(
      screen.getByRole("link", { name: "Edit specialist IC review settings" }).getAttribute("href"),
    ).toBe("/settings?tab=search");
  });

  it("names each reason's destination in the summary and renders a reasons table", () => {
    render(
      <MemoryRouter>
        <DiscoveryFunnelAudit
          value={{
            counts: { proposed: 4088, staged: 0, rejected: 2 },
            total_outcomes: 4088,
            omitted_outcomes: 0,
            omission_reason: "",
            outcomes: [],
            reason_counts: {
              already_known: 1417,
              insufficient_title_evidence: 2588,
              seniority_outside_band: 4,
              location_outside_saved_places: 3,
              hard_constraint_excluded: 9,
              fetch_failed: 2,
            },
          }}
        />
      </MemoryRouter>,
    );

    expect(
      screen.getByText(
        /Discovery funnel: 4088 candidates - 0 staged, 1417 already known, 2588 screened on title evidence, 4 screened on seniority, 3 screened on location, 9 excluded by hard constraints, 2 failed to read/,
      ),
    ).toBeTruthy();
    const reasonsTable = screen.getByRole("table", { name: "All-candidate reasons" });
    expect(within(reasonsTable).getByRole("columnheader", { name: "Reason" })).toBeTruthy();
    expect(within(reasonsTable).getByRole("columnheader", { name: "Candidates" })).toBeTruthy();
    expect(within(reasonsTable).getByText("already known")).toBeTruthy();
    expect(within(reasonsTable).getByText("1417")).toBeTruthy();
  });

  it("says what a campaign read with no candidates", () => {
    const { container } = render(
      <MemoryRouter>
        <DiscoveryFunnelAudit
          discoveryKind="guided_campaign"
          value={{
            counts: { proposed: 0, staged: 0, rejected: 0 },
            total_outcomes: 0,
            omitted_outcomes: 0,
            omission_reason: "",
            outcomes: [],
            reason_counts: {},
          }}
        />
      </MemoryRouter>,
    );

    expect(container.textContent).toBe("This campaign read no candidates.");
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("leaves a campaign's candidate rows to the ledger and keeps its reasons table", () => {
    render(
      <MemoryRouter>
        <DiscoveryFunnelAudit
          discoveryKind="guided_campaign"
          value={{
            counts: { proposed: 2, staged: 1, rejected: 1 },
            total_outcomes: 2,
            omitted_outcomes: 3,
            omission_reason: "Only the first outcomes are retained in task history.",
            reason_counts: { staged: 1, already_known: 1 },
            outcomes: [
              {
                kind: "board_listing",
                employer: "Globex",
                title: "Warehouse Supervisor",
                location: "Northtown",
                url: "https://jobs.example.test/1",
                source: "Synthetic board",
                stages: ["proposed", "screened", "rejected"],
                reason: "already_known",
                detail: "",
              },
              {
                kind: "direct_posting",
                employer: "Northwind",
                title: "Platform Lead",
                location: "Northtown",
                url: "https://jobs.example.test/2",
                source: "Synthetic board",
                stages: ["proposed", "staged"],
                reason: "staged",
                detail: "",
              },
            ],
          }}
        />
      </MemoryRouter>,
    );

    expect(screen.queryByRole("table", { name: "Discovery funnel outcomes" })).toBeNull();
    expect(screen.queryByRole("note")).toBeNull();
    expect(
      screen.getByText(
        "Discovery funnel: 2 candidates the campaign read - 1 staged, 1 already known",
      ),
    ).toBeTruthy();
    const reasonsTable = screen.getByRole("table", { name: "All-candidate reasons" });
    expect(within(reasonsTable).getByText("already known")).toBeTruthy();
    expect(within(reasonsTable).getByText("staged")).toBeTruthy();
  });

  it("counts a campaign's candidates when stored progress carries no total", () => {
    render(
      <MemoryRouter>
        <DiscoveryFunnelAudit
          discoveryKind="guided_campaign"
          value={{
            counts: {},
            omitted_outcomes: 0,
            omission_reason: "",
            reason_counts: { already_known: 2 },
            outcomes: [
              {
                kind: "board_listing",
                employer: "Globex",
                title: "Warehouse Supervisor",
                location: "Northtown",
                url: "https://jobs.example.test/1",
                source: "Synthetic board",
                stages: ["proposed", "screened"],
                reason: "already_known",
                detail: "",
              },
              {
                kind: "direct_posting",
                employer: "Northwind",
                title: "Platform Lead",
                location: "Northtown",
                url: "https://jobs.example.test/2",
                source: "Synthetic board",
                stages: ["proposed", "screened"],
                reason: "already_known",
                detail: "",
              },
            ],
          }}
        />
      </MemoryRouter>,
    );

    expect(screen.queryByText("This campaign read no candidates.")).toBeNull();
    expect(
      screen.getByText(
        "Discovery funnel: 2 candidates the campaign read - 0 staged, 2 already known",
      ),
    ).toBeTruthy();
  });

  it("counts staged once and says a campaign total covers only what it read", () => {
    render(
      <MemoryRouter>
        <DiscoveryFunnelAudit
          discoveryKind="guided_campaign"
          value={{
            counts: { proposed: 378, staged: 12, rejected: 366 },
            total_outcomes: 378,
            omitted_outcomes: 0,
            omission_reason: "",
            outcomes: [],
            reason_counts: {
              staged: 12,
              already_known: 150,
              insufficient_title_evidence: 214,
              fetch_failed: 2,
            },
          }}
        />
      </MemoryRouter>,
    );

    const summary = screen.getByText(/^Discovery funnel:/);
    expect(summary.textContent).toBe(
      "Discovery funnel: 378 candidates the campaign read - 12 staged, 150 already known, " +
        "214 screened on title evidence, 2 failed to read",
    );
  });
});
