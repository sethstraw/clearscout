import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { hasNumberErrors, type Draft } from "./draft";
import { Working } from "./Working";

function draft(): Draft {
  return {
    locations: [],
    countries: [],
    working_time_zone: "Mountain",
    working_day_shift_hours: 0,
    arrangement: {
      remote: "preferred",
      hybrid: "preferred",
      on_site: null,
      maximum_office_days: 2,
      maximum_travel_percent: 10,
      relocation: false,
      onsite_requires_paid_travel: true,
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

describe("Working form identity", () => {
  it("gives each conditional and general control a distinct id and name", () => {
    render(
      <Working
        acceptance={["preferred", "acceptable", "refused"]}
        draft={draft()}
        patch={vi.fn()}
      />,
    );

    const controls = [
      screen.getByLabelText(/^Time zone you normally work from/),
      screen.getByLabelText(/^Hours you can shift your working day/),
      screen.getByLabelText("Most days in an office per week"),
      screen.getByLabelText(/^Most travel you will take/),
      screen.getByLabelText(/^Office days only if travel is paid/),
      screen.getByLabelText("Would you relocate"),
    ] as (HTMLInputElement | HTMLSelectElement)[];

    expect(new Set(controls.map((control) => control.id)).size).toBe(controls.length);
    for (const control of controls) {
      expect(control.id).not.toBe("");
      expect(control.name).toBe(control.id);
    }
  });

  it("saves the declared working time zone as cross-location policy", () => {
    const patch = vi.fn();
    render(<Working acceptance={[]} draft={draft()} patch={patch} />);

    fireEvent.change(screen.getByLabelText(/^Time zone you normally work from/), {
      target: { value: "Eastern" },
    });

    expect(patch).toHaveBeenLastCalledWith({ working_time_zone: "Eastern" });
  });

  it("shows and saves the hours the person can shift their working day", () => {
    const patch = vi.fn();
    const { rerender } = render(
      <Working acceptance={[]} draft={{ ...draft(), working_day_shift_hours: 1 }} patch={patch} />,
    );

    const shift = screen.getByRole<HTMLInputElement>("spinbutton", {
      name: "Hours you can shift your working day",
      description:
        "How many hours earlier or later than usual you can work to match a posting's required time zone.",
    });
    expect(shift.value).toBe("1");
    expect([shift.step, shift.min, shift.max]).toEqual(["1", "0", "12"]);

    fireEvent.change(shift, { target: { value: "2" } });
    expect(patch).toHaveBeenLastCalledWith({ working_day_shift_hours: 2 });

    fireEvent.change(shift, { target: { value: "" } });
    expect(patch).toHaveBeenLastCalledWith({ working_day_shift_hours: 0 });
    rerender(
      <Working acceptance={[]} draft={{ ...draft(), working_day_shift_hours: 0 }} patch={patch} />,
    );
    expect(shift.value).toBe("");
  });

  it("asks for shift hours only once a working time zone is stated", () => {
    render(
      <Working acceptance={[]} draft={{ ...draft(), working_time_zone: null }} patch={vi.fn()} />,
    );

    expect(screen.queryByRole("spinbutton", { name: /^Hours you can shift/ })).toBeNull();
  });

  it("clears shift hours with the time zone so a hidden value never blocks a save", () => {
    const patch = vi.fn();
    render(
      <Working acceptance={[]} draft={{ ...draft(), working_day_shift_hours: 15 }} patch={patch} />,
    );

    fireEvent.change(screen.getByLabelText(/^Time zone you normally work from/), {
      target: { value: "" },
    });

    expect(patch).toHaveBeenLastCalledWith({
      working_time_zone: null,
      working_day_shift_hours: 0,
    });
  });

  it("does not hold a save on office days while that question is hidden", () => {
    const hidden = draft();
    const officeDays = { ...hidden.arrangement, hybrid: null, on_site: null };
    expect(
      hasNumberErrors({ ...hidden, arrangement: { ...officeDays, maximum_office_days: 9 } }),
    ).toBe(false);
    expect(
      hasNumberErrors({
        ...hidden,
        arrangement: { ...officeDays, hybrid: "preferred", maximum_office_days: 9 },
      }),
    ).toBe(true);
  });

  it("names each control by its label alone and describes it with its help", () => {
    render(<Working acceptance={[]} draft={draft()} patch={vi.fn()} />);

    expect(
      screen.getByRole("combobox", {
        name: "Time zone you normally work from",
        description: "Used when a posting requires work within a named North American time zone.",
      }),
    ).toBeTruthy();
    expect(
      screen.getByRole("spinbutton", {
        name: "Most travel you will take",
        description: "Percent of your time.",
      }),
    ).toBeTruthy();
    expect(
      screen.getByRole("combobox", {
        name: "Office days only if travel is paid",
        description: "The employer covers travel to the office.",
      }),
    ).toBeTruthy();
  });

  it.each([
    ["Hours you can shift your working day", "-1", "Enter a whole number from 0 to 12."],
    ["Hours you can shift your working day", "1.5", "Enter a whole number from 0 to 12."],
    ["Most travel you will take", "150", "Enter a whole number from 0 to 100."],
    ["Most days in an office per week", "8", "Enter a whole number from 0 to 7."],
  ])("flags %s set to %s beside the field before saving", (name, value, message) => {
    const values: Record<string, Partial<Draft>> = {
      "Hours you can shift your working day": { working_day_shift_hours: Number(value) },
      "Most travel you will take": {
        arrangement: { ...draft().arrangement, maximum_travel_percent: Number(value) },
      },
      "Most days in an office per week": {
        arrangement: { ...draft().arrangement, maximum_office_days: Number(value) },
      },
    };
    render(<Working acceptance={[]} draft={{ ...draft(), ...values[name] }} patch={vi.fn()} />);

    const field = screen.getByRole("spinbutton", { name, description: message });
    expect(field.getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByRole("alert").textContent).toBe(message);
  });
});
