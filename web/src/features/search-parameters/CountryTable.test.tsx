import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CountryTable } from "./CountryTable";
import type { Draft } from "./draft";

function baseDraft(countries: Draft["countries"] = []): Draft {
  return {
    locations: [],
    countries,
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
    employment: { types: [], excluded_types: [], payroll_arrangements: [] },
    employers: { industries: [], excluded_industries: [], excluded_employers: [], sizes: [] },
    focus: { capabilities: [], domains: [], boundaries: [] },
    excluded_conditions: [],
    score_weight_set: "",
    opportunity_thesis: "",
  };
}

describe("CountryTable", () => {
  it("adds an empty country row", () => {
    const patch = vi.fn<(changes: Partial<Draft>) => void>();
    render(<CountryTable draft={baseDraft()} patch={patch} />);

    fireEvent.click(screen.getByRole("button", { name: "Add a country" }));

    expect(patch).toHaveBeenCalledWith({ countries: [{ country: "", spellings: [] }] });
  });

  it("edits a country name", () => {
    const patch = vi.fn<(changes: Partial<Draft>) => void>();
    render(
      <CountryTable draft={baseDraft([{ country: "Canada", spellings: [] }])} patch={patch} />,
    );

    fireEvent.change(screen.getByLabelText("Country"), { target: { value: "United States" } });

    expect(patch).toHaveBeenCalledWith({
      countries: [{ country: "United States", spellings: [] }],
    });
  });

  it("adds a spelling to a country", () => {
    const patch = vi.fn<(changes: Partial<Draft>) => void>();
    render(
      <CountryTable
        draft={baseDraft([{ country: "United States", spellings: [] }])}
        patch={patch}
      />,
    );

    const spellings = screen.getByLabelText("Spellings for United States");
    fireEvent.change(spellings, { target: { value: "USA" } });
    fireEvent.keyDown(spellings, { key: "Enter" });

    expect(patch).toHaveBeenCalledWith({
      countries: [{ country: "United States", spellings: ["USA"] }],
    });
  });

  it("removes a country by position and preserves order", () => {
    const patch = vi.fn<(changes: Partial<Draft>) => void>();
    render(
      <CountryTable
        draft={baseDraft([
          { country: "Canada", spellings: [] },
          { country: "United States", spellings: [] },
          { country: "Ireland", spellings: [] },
        ])}
        patch={patch}
      />,
    );

    fireEvent.click(screen.getAllByRole("button", { name: "Remove" })[1]!);

    expect(patch).toHaveBeenCalledWith({
      countries: [
        { country: "Canada", spellings: [] },
        { country: "Ireland", spellings: [] },
      ],
    });
  });
});
