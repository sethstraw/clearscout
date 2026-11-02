import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Draft } from "./draft";
import { Places } from "./Places";

function baseDraft(overrides: Partial<Draft["locations"][number]> = {}): Draft {
  return {
    locations: [
      {
        key: "ca-mb",
        country: "Canada",
        region: "Manitoba",
        metro: "",
        eligibility: "authorized",
        basis: "",
        aliases: [],
        arrangement: null,
        resident: false,
        ...overrides,
      },
    ],
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
  };
}

describe("Places metro field", () => {
  it.each([false, true])(
    "leaves a new place's work authorization unanswered (basic: %s)",
    (basic) => {
      const patch = vi.fn<(changes: Partial<Draft>) => void>();
      const draft = baseDraft();
      const props = {
        acceptance: ["accepted", "rejected"],
        eligibility: ["authorized", "restricted"],
        basic,
        patch,
      };
      const { rerender } = render(<Places {...props} draft={draft} />);
      fireEvent.click(screen.getByRole("button", { name: "Add a place" }));
      const changes = patch.mock.calls[0]?.[0];
      expect(changes?.locations).toHaveLength(2);
      rerender(<Places {...props} draft={{ ...draft, ...changes }} />);
      const standing = screen.getAllByRole<HTMLSelectElement>("combobox", { name: /Standing/ });
      expect(standing[0]?.value).toBe("authorized");
      expect(standing[1]?.value).toBe("");
    },
  );

  it("renders a metro input that seeds from the stored value and reports edits", () => {
    const patch = vi.fn<(changes: Partial<Draft>) => void>();
    const { rerender } = render(
      <Places
        acceptance={["accepted", "rejected"]}
        draft={baseDraft({ metro: "Brandon" })}
        eligibility={["authorized", "restricted"]}
        patch={patch}
      />,
    );

    const metroInput = screen.getByRole<HTMLInputElement>("textbox", { name: "Metro (ca-mb)" });
    expect(metroInput.value).toBe("Brandon");
    expect(metroInput.id).not.toBe("");
    expect(metroInput.name).toBe(metroInput.id);
    expect(metroInput.closest("label")?.htmlFor).toBe(metroInput.id);
    expect(screen.getByRole("button", { name: "Add a place" }).classList).toContain(
      "button--quiet",
    );

    fireEvent.change(metroInput, { target: { value: "Moncton" } });
    expect(patch).toHaveBeenCalledTimes(1);
    const changes = patch.mock.calls[0]?.[0];
    expect(changes?.locations?.[0]?.metro).toBe("Moncton");
    expect(changes?.locations?.[0]?.region).toBe("Manitoba");

    rerender(
      <Places
        acceptance={["accepted", "rejected"]}
        draft={baseDraft({ metro: "Moncton" })}
        eligibility={["authorized", "restricted"]}
        patch={patch}
      />,
    );
    expect(screen.getByRole<HTMLInputElement>("textbox", { name: "Metro (ca-mb)" }).value).toBe(
      "Moncton",
    );
  });

  it("gives every place field a distinct form identity", () => {
    render(
      <Places
        acceptance={["accepted", "rejected"]}
        draft={baseDraft()}
        eligibility={["authorized", "restricted"]}
        patch={vi.fn()}
      />,
    );

    const controls = [
      screen.getByRole("textbox", { name: "Key (ca-mb)" }),
      screen.getByRole("combobox", { name: "Country (ca-mb)" }),
      screen.getByRole("textbox", { name: "Region (ca-mb)" }),
      screen.getByRole("textbox", { name: "Metro (ca-mb)" }),
      screen.getByRole("combobox", { name: "Standing (ca-mb)" }),
      screen.getByRole("textbox", { name: "Why, in your own words (ca-mb)" }),
      screen.getByRole("textbox", {
        name: "Publisher-specific aliases, comma separated (ca-mb)",
      }),
      screen.getByRole("checkbox", { name: "I live here (ca-mb)" }),
      screen.getByRole("spinbutton", { name: "Most days in an office per week (ca-mb)" }),
    ] as HTMLInputElement[];

    expect(new Set(controls.map((control) => control.id)).size).toBe(controls.length);
    for (const control of controls) {
      expect(control.id).not.toBe("");
      expect(control.name).toBe(control.id);
    }
  });

  it("marks only the place the person lives in as their residence", () => {
    const patch = vi.fn<(changes: Partial<Draft>) => void>();
    const draft = baseDraft();
    draft.locations.push({ ...draft.locations[0]!, key: "ca-on", region: "Ontario" });
    render(
      <Places
        acceptance={["accepted", "rejected"]}
        draft={draft}
        eligibility={["authorized", "restricted"]}
        patch={patch}
      />,
    );

    const manitoba = screen.getByRole<HTMLInputElement>("checkbox", {
      name: "I live here (ca-mb)",
    });
    const ontario = screen.getByRole<HTMLInputElement>("checkbox", { name: "I live here (ca-on)" });
    expect([manitoba.checked, ontario.checked]).toEqual([false, false]);
    fireEvent.click(ontario);

    const changes = patch.mock.calls[0]?.[0];
    expect(changes?.locations?.map((place) => [place.key, place.resident])).toEqual([
      ["ca-mb", false],
      ["ca-on", true],
    ]);
  });

  it("describes the residence checkbox with the relocation answer it depends on", () => {
    render(
      <Places
        acceptance={["accepted", "rejected"]}
        draft={baseDraft()}
        eligibility={["authorized", "restricted"]}
        patch={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("checkbox", {
        name: "I live here (ca-mb)",
        description:
          "A posting that requires living in another country is refused unless you would " +
          "relocate (Would you relocate, under How you want to work).",
      }),
    ).toBeTruthy();
  });

  it("names each row's remove button by its place, or its position when it has no key", () => {
    const patch = vi.fn<(changes: Partial<Draft>) => void>();
    const draft = baseDraft();
    draft.locations.push({ ...draft.locations[0]!, key: "", region: "Ontario" });
    render(
      <Places
        acceptance={["accepted", "rejected"]}
        draft={draft}
        eligibility={["authorized", "restricted"]}
        patch={patch}
      />,
    );

    expect(screen.getByRole("textbox", { name: "Key (place 2)" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Remove ca-mb" }));

    expect(patch.mock.calls[0]?.[0].locations?.map((place) => place.region)).toEqual(["Ontario"]);
  });

  it("keeps the residence checkbox controlled for a place drafted without the answer", () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      const older = baseDraft();
      delete (older.locations[0] as Partial<Draft["locations"][number]>).resident;
      const { rerender } = render(
        <Places
          acceptance={["accepted", "rejected"]}
          draft={older}
          eligibility={["authorized", "restricted"]}
          patch={vi.fn()}
        />,
      );
      expect(
        screen.getByRole<HTMLInputElement>("checkbox", { name: "I live here (ca-mb)" }).checked,
      ).toBe(false);

      rerender(
        <Places
          acceptance={["accepted", "rejected"]}
          draft={baseDraft({ resident: true })}
          eligibility={["authorized", "restricted"]}
          patch={vi.fn()}
        />,
      );

      expect(
        screen.getByRole<HTMLInputElement>("checkbox", { name: "I live here (ca-mb)" }).checked,
      ).toBe(true);
      expect(errors).not.toHaveBeenCalled();
    } finally {
      errors.mockRestore();
    }
  });
});
