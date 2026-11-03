import { fireEvent, render, screen, within } from "@testing-library/react";
import { HttpResponse, http } from "msw/http";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import type { TitleExclusionCollateral } from "../../api/client/types";
import { MemoryRouter } from "react-router-dom";

import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import type { Draft } from "./draft";
import { Level } from "./Level";

const KNOWN_TYPES = ["full_time", "part_time", "contract"];

function baseDraft(types: string[] = []): Draft {
  return {
    locations: [],
    countries: [],
    working_time_zone: null,
    working_day_shift_hours: 0,
    arrangement: {
      remote: null,
      hybrid: null,
      on_site: null,
      maximum_office_days: null,
      maximum_travel_percent: null,
      relocation: null,
      onsite_requires_paid_travel: null,
    },
    compensation: [],
    seniority: {
      minimum: "",
      maximum: "",
      target_titles: [],
      excluded_titles: [],
      specialist_ic_role_nouns: [],
      strong_domain_tokens: [],
      title_matching: "guided",
    },
    employment: { types, excluded_types: [], payroll_arrangements: [] },
    employers: { industries: [], excluded_industries: [], excluded_employers: [], sizes: [] },
    focus: { capabilities: [], domains: [], boundaries: [] },
    excluded_conditions: [],
    score_weight_set: "",
    opportunity_thesis: "",
  };
}

function renderLevel(types: string[], patch: (changes: Partial<Draft>) => void) {
  renderDraft(baseDraft(types), patch);
}

function renderDraft(draft: Draft, patch: (changes: Partial<Draft>) => void) {
  render(
    <MemoryRouter>
      <AppProviders>
        <Level
          draft={draft}
          employmentTypes={KNOWN_TYPES}
          lanes={["specialist", "leadership"]}
          levels={["mid", "senior"]}
          patch={patch}
        />
      </AppProviders>
    </MemoryRouter>,
  );
}

describe("Level employment types", () => {
  it("checks a known type already on record", () => {
    renderLevel(["full_time"], vi.fn());

    expect(screen.getByRole<HTMLInputElement>("checkbox", { name: "Full time" }).checked).toBe(
      true,
    );
    expect(screen.getByRole<HTMLInputElement>("checkbox", { name: "Contract" }).checked).toBe(
      false,
    );
  });

  it("adds a checked type to the saved values", () => {
    const patch = vi.fn<(changes: Partial<Draft>) => void>();
    renderLevel(["full_time"], patch);

    fireEvent.click(screen.getByRole("checkbox", { name: "Part time" }));

    expect(patch).toHaveBeenCalledTimes(1);
    expect(patch.mock.calls[0]?.[0]?.employment?.types).toEqual(["full_time", "part_time"]);
  });

  it("removes a type when its checkbox is unchecked", () => {
    const patch = vi.fn<(changes: Partial<Draft>) => void>();
    renderLevel(["full_time", "contract"], patch);

    fireEvent.click(screen.getByRole("checkbox", { name: "Full time" }));

    expect(patch.mock.calls[0]?.[0]?.employment?.types).toEqual(["contract"]);
  });

  it("shows a custom file-configured value outside the picker vocabulary", () => {
    renderLevel(["fractional"], vi.fn());

    expect(screen.getByText("fractional")).toBeTruthy();
    expect(screen.queryByRole("checkbox", { name: "fractional" })).toBeNull();
    for (const label of ["Full time", "Part time", "Contract"]) {
      expect(screen.getByRole<HTMLInputElement>("checkbox", { name: label }).checked).toBe(false);
    }
  });
});

describe("Level specialist review", () => {
  it("adds a specialist role noun to the seniority policy", () => {
    const patch = vi.fn<(changes: Partial<Draft>) => void>();
    renderLevel([], patch);

    const advanced = screen.getByText("Advanced matching");
    expect(advanced.closest("details")?.open).toBe(false);
    expect(advanced.closest("summary")?.textContent).toContain(
      "role nouns, domain tokens: none saved",
    );
    expect(screen.getByText("Advanced preferences").closest("summary")?.textContent).toContain(
      "payroll, industries, employer sizes: none saved",
    );
    fireEvent.click(advanced);
    fireEvent.change(screen.getByLabelText("Specialist individual-contributor role nouns"), {
      target: { value: "Architect" },
    });
    fireEvent.keyDown(screen.getByLabelText("Specialist individual-contributor role nouns"), {
      key: "Enter",
    });

    expect(patch).toHaveBeenCalled();
    expect(patch.mock.calls.at(-1)?.[0].seniority?.specialist_ic_role_nouns).toEqual(["Architect"]);
  });
});

describe("Level scoring policy", () => {
  it("leaves the scoring policy choice to the Scoring screen", () => {
    renderDraft({ ...baseDraft(), score_weight_set: "Synthetic policy" }, vi.fn());

    for (const details of document.querySelectorAll("details")) details.open = true;
    expect(screen.queryByRole("combobox", { name: /Scoring policy/ })).toBeNull();
    expect(screen.getByRole("link", { name: "How jobs are scored" }).getAttribute("href")).toBe(
      "/find/scoring",
    );
  });
});

describe("Level retained search preferences", () => {
  it("makes retained role, employment, and employer preferences editable", () => {
    const patch = vi.fn<(changes: Partial<Draft>) => void>();
    renderDraft(
      {
        ...baseDraft(),
        employment: {
          types: [],
          excluded_types: ["contract"],
          payroll_arrangements: ["employee payroll"],
        },
        employers: {
          industries: ["public services"],
          excluded_industries: ["tobacco"],
          excluded_employers: [],
          sizes: ["enterprise"],
        },
      },
      patch,
    );

    const summary = screen.getByText("Advanced preferences");
    const advanced = summary.closest("details");
    expect(advanced?.open).toBe(false);
    expect(summary.closest("summary")?.textContent).toContain(
      "payroll, industries, employer sizes: 4 of 4 saved",
    );
    fireEvent.click(summary);
    expect(advanced?.open).toBe(true);
    for (const value of [
      "contract",
      "employee payroll",
      "public services",
      "tobacco",
      "enterprise",
    ]) {
      expect(screen.getByText(value)).toBeTruthy();
    }
    for (const label of [
      "Payroll arrangements you can accept",
      "Industries of interest",
      "Industries to exclude",
      "Employer sizes to consider",
    ]) {
      expect(advanced?.contains(screen.getByLabelText(label))).toBe(true);
    }

    const payroll = screen.getByLabelText("Payroll arrangements you can accept");
    fireEvent.change(payroll, { target: { value: "incorporated contractor" } });
    fireEvent.keyDown(payroll, { key: "Enter" });

    expect(patch.mock.calls.at(-1)?.[0].employment?.payroll_arrangements).toEqual([
      "employee payroll",
      "incorporated contractor",
    ]);
  });
});

const WORKSPACE = "*/api/v1/workspaces/:workspaceId";

/** Serve the queue-count read and the two workspace reads a workspace-bound render makes. */
function installCollateral(body: TitleExclusionCollateral): URL[] {
  const asked: URL[] = [];
  window.localStorage.setItem("clearscout.workspace", "workspace-level");
  server.use(
    http.get(`${WORKSPACE}/pending-discoveries/title-exclusion-collateral`, ({ request }) => {
      asked.push(new URL(request.url));
      return HttpResponse.json(body);
    }),
    http.get("*/api/v1/tasks", () => HttpResponse.json([])),
  );
  return asked;
}

function draftExcluding(...excluded: string[]): Draft {
  const base = baseDraft();
  return { ...base, seniority: { ...base.seniority, excluded_titles: excluded } };
}

describe("Level title exclusion collateral", () => {
  it("tables what each excluded title would hide and says the count is of the queue today", async () => {
    const asked = installCollateral({
      queue_count: 12,
      phrases: [
        { phrase: "Delivery Lead", matched_count: 3 },
        { phrase: "Archivist", matched_count: 0 },
      ],
    });
    renderDraft(draftExcluding("Delivery Lead", "Archivist"), vi.fn());

    const table = await screen.findByRole("table", {
      name: "Titles to exclude and what each would hide",
    });
    const rows = within(table)
      .getAllByRole("row")
      .slice(1)
      .map((row) => [
        within(row).getByRole("rowheader").textContent,
        within(row).getByRole("cell").textContent,
      ]);
    expect(rows).toEqual([
      ["Delivery Lead", "3"],
      ["Archivist", "0"],
    ]);
    expect(
      screen.getByText(/Counted against the 12 rows in the Pending queue right now/),
    ).toBeTruthy();
    expect(screen.getByText(/Postings found after this is saved are not counted/)).toBeTruthy();
    expect(asked.at(-1)?.searchParams.getAll("phrase")).toEqual(["Delivery Lead", "Archivist"]);
  });

  it("shows no queue count at all when nothing is excluded", () => {
    renderLevel([], vi.fn());

    expect(screen.queryByText("What these exclusions would hide")).toBeNull();
    expect(screen.queryByText(/rows in the Pending queue right now/)).toBeNull();
  });
});

describe("Level saved lists", () => {
  it("lists every saved title and accepted employer type without calling any unused", () => {
    renderDraft(
      {
        ...baseDraft(),
        seniority: {
          ...baseDraft().seniority,
          target_titles: Array.from({ length: 9 }, (_, index) => ({
            title: `Title ${index}`,
            lane: null,
          })),
        },
        employers: {
          ...baseDraft().employers,
          archetypes: Array.from({ length: 7 }, (_, index) => ({
            name: `Archetype ${index}`,
            because: "",
          })),
        },
      },
      vi.fn(),
    );

    expect(screen.getByText("Archetype 0")).toBeTruthy();
    expect(screen.getByText("Archetype 6")).toBeTruthy();
    expect(screen.queryByText(/saved but unused/)).toBeNull();
  });

  it("lists only the named employer archetypes", () => {
    renderDraft(
      {
        ...baseDraft(),
        employers: {
          ...baseDraft().employers,
          archetypes: [
            { name: "Regional freight operator", because: "" },
            { name: "   ", because: "" },
          ],
        },
      },
      vi.fn(),
    );

    expect(screen.getByText("Regional freight operator")).toBeTruthy();
    expect(screen.getByText("Unfinished employer type 2")).toBeTruthy();
  });

  it("says no employer archetypes are saved yet when the list is empty", () => {
    renderLevel([], vi.fn());

    expect(
      screen.getByText("None saved yet. Add one here or accept a search suggestion."),
    ).toBeTruthy();
  });
});

describe("Level explicit employer associations", () => {
  it("does not transfer an unfinished employer entry when a different type is removed", () => {
    function EditableLevel() {
      const [draft, setDraft] = useState<Draft>({
        ...baseDraft(),
        employers: {
          ...baseDraft().employers,
          archetypes: [
            { name: "Clinical data", because: "", employer_names: [] },
            { name: "Scientific publishing", because: "", employer_names: [] },
          ],
        },
      });
      return (
        <Level
          draft={draft}
          patch={(changes) => setDraft((saved) => ({ ...saved, ...changes }))}
          employmentTypes={KNOWN_TYPES}
          lanes={[]}
          levels={[]}
        />
      );
    }
    render(
      <MemoryRouter>
        <AppProviders>
          <EditableLevel />
        </AppProviders>
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("Clinical data", { selector: "summary" }));
    fireEvent.change(screen.getByLabelText("Employers in type 1"), { target: { value: "Acme" } });
    fireEvent.click(screen.getByText("Scientific publishing", { selector: "summary" }));
    fireEvent.change(screen.getByLabelText("Employers in type 2"), { target: { value: "Globex" } });
    fireEvent.click(screen.getByRole("button", { name: "Remove employer type 1" }));
    const remaining = screen.getByLabelText("Employers in type 1");
    expect(remaining).toHaveProperty("value", "Globex");
    fireEvent.keyDown(remaining, { key: "Enter" });
    expect(screen.queryByRole("button", { name: "Remove Acme" })).toBeNull();
    expect(screen.getByRole("button", { name: "Remove Globex" })).toBeTruthy();
  });
  it("adds a manual employer type without a provider or CV", () => {
    const patch = vi.fn<(changes: Partial<Draft>) => void>();
    renderLevel([], patch);
    fireEvent.click(screen.getByRole("button", { name: "Add employer type" }));
    expect(patch.mock.calls.at(-1)?.[0].employers?.archetypes).toEqual([
      { name: "", because: "", employer_names: [] },
    ]);
  });

  it("edits membership while preserving the type and reason", () => {
    const patch = vi.fn<(changes: Partial<Draft>) => void>();
    renderDraft(
      {
        ...baseDraft(),
        employers: {
          ...baseDraft().employers,
          archetypes: [
            { name: "Clinical data", because: "Medication information", employer_names: ["Acme"] },
          ],
        },
      },
      patch,
    );
    const input = screen.getByLabelText("Employers in type 1");
    fireEvent.click(screen.getByText("Clinical data", { selector: "summary" }));
    fireEvent.change(input, { target: { value: "Globex" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(patch.mock.calls.at(-1)?.[0].employers?.archetypes).toEqual([
      {
        name: "Clinical data",
        because: "Medication information",
        employer_names: ["Acme", "Globex"],
      },
    ]);
    expect(screen.getByText(/does not establish eligibility or override exclusions/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Remove employer type 1" }));
    expect(patch.mock.calls.at(-1)?.[0].employers?.archetypes).toEqual([]);
  });
});
