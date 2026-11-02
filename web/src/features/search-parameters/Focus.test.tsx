import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Draft } from "./draft";
import { Focus } from "./Focus";

const caps = {
  titles: 8,
  archetypes: 6,
  capabilities: 5,
  domains: 5,
  cluster_terms: 10,
};

function baseDraft(overrides: Partial<Draft> = {}): Draft {
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
    opportunity_thesis: "",
    ...overrides,
  };
}

describe("Focus capabilities and domains", () => {
  it("adds a capability cluster without touching domains", () => {
    const patch = vi.fn<(changes: Partial<Draft>) => void>();
    render(<Focus caps={caps} draft={baseDraft()} patch={patch} />);

    fireEvent.click(screen.getByRole("button", { name: "Add a capability" }));

    expect(patch.mock.calls.at(-1)?.[0].focus).toEqual({
      capabilities: [{ name: "", terms: [], because: "" }],
      domains: [],
      boundaries: [],
    });
  });

  it("edits a domain cluster name", () => {
    const patch = vi.fn<(changes: Partial<Draft>) => void>();
    render(
      <Focus
        caps={caps}
        draft={baseDraft({
          focus: {
            capabilities: [],
            domains: [{ name: "Payments", terms: [], because: "" }],
            boundaries: [],
          },
        })}
        patch={patch}
      />,
    );

    fireEvent.change(screen.getByLabelText("Domain name"), { target: { value: "Fintech" } });

    expect(patch.mock.calls.at(-1)?.[0].focus?.domains).toEqual([
      { name: "Fintech", terms: [], because: "" },
    ]);
  });

  it("adds a role boundary without touching capabilities or domains", () => {
    const patch = vi.fn<(changes: Partial<Draft>) => void>();
    render(
      <Focus
        caps={caps}
        draft={baseDraft({
          focus: { capabilities: [], domains: [], boundaries: ["sales management"] },
        })}
        patch={patch}
      />,
    );

    expect(screen.getByText("sales management")).toBeTruthy();
    const boundary = screen.getByLabelText("Role boundaries to steer away from");
    fireEvent.change(boundary, { target: { value: "line management as the defining mandate" } });
    fireEvent.keyDown(boundary, { key: "Enter" });

    expect(patch.mock.calls.at(-1)?.[0].focus?.boundaries).toEqual([
      "sales management",
      "line management as the defining mandate",
    ]);
  });
});

describe("Focus excluded conditions", () => {
  it("adds an empty condition pair", () => {
    const patch = vi.fn<(changes: Partial<Draft>) => void>();
    render(<Focus caps={caps} draft={baseDraft()} patch={patch} />);

    fireEvent.click(screen.getByRole("button", { name: "Add an excluded condition" }));

    expect(patch).toHaveBeenCalledWith({ excluded_conditions: [["", ""]] });
  });

  it("edits the condition and reason of a pair independently", () => {
    const patch = vi.fn<(changes: Partial<Draft>) => void>();
    render(
      <Focus
        caps={caps}
        draft={baseDraft({ excluded_conditions: [["on call", "burnout"]] })}
        patch={patch}
      />,
    );

    fireEvent.change(screen.getByLabelText("Condition"), { target: { value: "night shift" } });
    expect(patch.mock.calls.at(-1)?.[0].excluded_conditions).toEqual([["night shift", "burnout"]]);

    fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "sleep" } });
    expect(patch.mock.calls.at(-1)?.[0].excluded_conditions).toEqual([["on call", "sleep"]]);
  });

  it("removes a pair by position and preserves order", () => {
    const patch = vi.fn<(changes: Partial<Draft>) => void>();
    render(
      <Focus
        caps={caps}
        draft={baseDraft({
          excluded_conditions: [
            ["first", "a"],
            ["second", "b"],
            ["third", "c"],
          ],
        })}
        patch={patch}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Remove second" }));

    expect(patch.mock.calls.at(-1)?.[0].excluded_conditions).toEqual([
      ["first", "a"],
      ["third", "c"],
    ]);
  });
});
