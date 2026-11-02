import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { JobDetail } from "../../api/client";
import { jobDetail } from "../job-detail/jobDetailFixtures";
import { EvidenceWorkspace } from "./EvidenceWorkspace";
import type { FitRequirement } from "./evidenceWorkspaceModel";

type Evidence = JobDetail["evidence"][number];
type PostingRequirement = NonNullable<JobDetail["requirements"]>[number];

function row(id: string, overrides: Partial<Evidence>): Evidence {
  return {
    artifact_id: null,
    char_end: null,
    char_start: null,
    classification: "DIRECT",
    id,
    locator: "",
    master_cv_entry_id: null,
    observation_id: null,
    requirement_id: null,
    score_links: [],
    source_kind: "posting",
    strength: "moderate",
    text: `Evidence ${id}`,
    ...overrides,
  };
}

function requirement(id: string, text: string, position: number): PostingRequirement {
  return { importance: "must_have", position, requirement_id: id, requirement_key: id, text };
}

function renderWorkspace(
  evidence: Evidence[],
  requirements: PostingRequirement[],
  fitRequirements?: FitRequirement[],
  fitReportPending = false,
) {
  render(
    <MemoryRouter>
      <EvidenceWorkspace
        data={jobDetail({ evidence, requirements, unsupported: [] })}
        evidenceSource={{ error: null, isError: false, isLoading: false }}
        fitReportPending={fitReportPending}
        fitRequirements={fitRequirements}
        jobId="job-1"
        onSelectEvidence={() => {}}
        onToggleUnsupported={() => {}}
        selectedEvidenceId=""
        unsupportedOnly={false}
      />
    </MemoryRouter>,
  );
}

function card(heading: string): HTMLElement {
  return screen.getByRole("article", { name: heading });
}

const licenceQuote = row("ev-quote", {
  locator: "posting:hard-gate-1",
  text: "Must hold a synthetic pilot licence",
});

describe("EvidenceWorkspace", () => {
  it("shows a gate quote on the gate the fit report pairs it with, with no verdict chip", () => {
    renderWorkspace(
      [licenceQuote],
      [requirement("gate-1", "Synthetic pilot licence", 0)],
      [{ requirement_id: "gate-1", is_hard_gate: true, evidence_keys: ["ev-quote"] }],
    );

    const gate = card("Synthetic pilot licence");
    expect(within(gate).getByText("Must hold a synthetic pilot licence")).toBeTruthy();
    expect(within(gate).getByText("Hard gate")).toBeTruthy();
    expect(within(gate).queryByText(/Uncited:/)).toBeNull();
    expect(within(gate).queryByText(/posting span/)).toBeNull();
    expect(screen.queryByText(/Evidence not tied to a requirement/)).toBeNull();
  });

  it("shows a folded gate quote on the ordinary requirement that carries it", () => {
    renderWorkspace(
      [
        row("ev-quote", {
          locator: "posting:hard-gate-1",
          text: "At least five years of synthetic work is required",
        }),
        row("record-1", {
          locator: "cv-1",
          master_cv_entry_id: "cv-1",
          requirement_id: "REQ-1",
          source_kind: "candidate",
          text: "Six years of synthetic work",
        }),
      ],
      [requirement("REQ-1", "Five years of synthetic work", 0)],
      [{ requirement_id: "REQ-1", is_hard_gate: true, evidence_keys: ["record-1", "ev-quote"] }],
    );

    const carrier = card("Five years of synthetic work");
    expect(within(carrier).getByText("Six years of synthetic work")).toBeTruthy();
    expect(
      within(carrier).getByText("At least five years of synthetic work is required"),
    ).toBeTruthy();
    expect(within(carrier).getByText("Hard gate")).toBeTruthy();
    expect(screen.queryByText(/Evidence not tied to a requirement/)).toBeNull();
  });

  it("lists a gate quote as unattached while no fit report is loaded", () => {
    renderWorkspace([licenceQuote], [requirement("gate-1", "Synthetic pilot licence", 0)]);

    const gate = card("Synthetic pilot licence");
    expect(within(gate).getByText(/No posting span cites this requirement/)).toBeTruthy();
    expect(screen.getByText("Evidence not tied to a requirement (1)")).toBeTruthy();
  });

  it("holds the gate quote back while the fit report is still loading", () => {
    renderWorkspace(
      [licenceQuote],
      [requirement("gate-1", "Synthetic pilot licence", 0)],
      undefined,
      true,
    );

    const gate = card("Synthetic pilot licence");
    expect(within(gate).queryByText(/No posting span cites this requirement/)).toBeNull();
    expect(within(gate).getByText("Matching posting quotes to requirements...")).toBeTruthy();
    expect(screen.queryByText(/Evidence not tied to a requirement/)).toBeNull();
    expect(screen.queryByText("Must hold a synthetic pilot licence")).toBeNull();
  });

  it("leaves an ordinary requirement with its own span unchanged", () => {
    renderWorkspace(
      [
        row("span-1", {
          locator: "posting:req-span",
          requirement_id: "REQ-2",
          text: "Experience with synthetic dashboards",
        }),
      ],
      [requirement("REQ-2", "Synthetic dashboards", 0)],
      [{ requirement_id: "REQ-2", is_hard_gate: false, evidence_keys: ["span-1"] }],
    );

    const ordinary = card("Synthetic dashboards");
    expect(within(ordinary).getByText("Experience with synthetic dashboards")).toBeTruthy();
    expect(within(ordinary).getByText(/Uncited: /)).toBeTruthy();
    expect(within(ordinary).queryByText(/posting span/)).toBeNull();
    expect(within(ordinary).queryByText("Hard gate")).toBeNull();
  });
});
