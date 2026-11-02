import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Draft } from "./draft";
import { OpportunityThesis } from "./OpportunityThesis";

function baseDraft(thesis: string): Draft {
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
    employment: { types: [], excluded_types: [], payroll_arrangements: [] },
    employers: { industries: [], excluded_industries: [], excluded_employers: [], sizes: [] },
    focus: { capabilities: [], domains: [], boundaries: [] },
    excluded_conditions: [],
    score_weight_set: "",
    opportunity_thesis: thesis,
  };
}

describe("OpportunityThesis", () => {
  it("shows the stored thesis and reports edits without touching other fields", () => {
    const patch = vi.fn<(changes: Partial<Draft>) => void>();
    render(<OpportunityThesis draft={baseDraft("first mandate")} patch={patch} />);

    const textarea = screen.getByLabelText<HTMLTextAreaElement>(/opportunity thesis/i);
    expect(textarea.value).toBe("first mandate");
    expect(textarea.id).not.toBe("");
    expect(textarea.name).toBe(textarea.id);
    expect(textarea.closest("label")?.htmlFor).toBe(textarea.id);

    fireEvent.change(textarea, { target: { value: "second mandate" } });

    expect(patch).toHaveBeenCalledTimes(1);
    expect(patch.mock.calls[0]?.[0]).toEqual({ opportunity_thesis: "second mandate" });
  });

  it("renders an empty textarea when no thesis is stored", () => {
    render(<OpportunityThesis draft={baseDraft("")} patch={vi.fn()} />);

    expect(screen.getByLabelText<HTMLTextAreaElement>(/opportunity thesis/i).value).toBe("");
  });

  it("keeps identities unique across component instances", () => {
    render(
      <>
        <OpportunityThesis draft={baseDraft("first")} patch={vi.fn()} />
        <OpportunityThesis draft={baseDraft("second")} patch={vi.fn()} />
      </>,
    );

    const textareas = screen.getAllByLabelText<HTMLTextAreaElement>(/opportunity thesis/i);
    expect(textareas[0]?.id).not.toBe(textareas[1]?.id);
    expect(textareas[0]?.name).not.toBe(textareas[1]?.name);
  });

  it("shows the structured level bounds and updates when the draft changes", () => {
    const draft = baseDraft("consider associate through director");
    draft.seniority = { ...draft.seniority, minimum: "senior", maximum: "" };
    const { rerender } = render(<OpportunityThesis draft={draft} patch={vi.fn()} />);

    expect(screen.getByText(/senior to Not stated/)).toBeTruthy();

    const updated = { ...draft, seniority: { ...draft.seniority, minimum: "mid", maximum: "vp" } };
    rerender(<OpportunityThesis draft={updated} patch={vi.fn()} />);

    expect(screen.getByText(/mid to vp/)).toBeTruthy();
  });
});
