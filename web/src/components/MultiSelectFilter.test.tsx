import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { MultiSelectFilter } from "./MultiSelectFilter";

const options = [
  { value: "Northwind Health", label: "Northwind Health", count: 3 },
  { value: "Acme Analytics", label: "Acme Analytics", count: 1 },
];

function renderFilter(selected: string[], onChange = vi.fn()) {
  render(
    <MultiSelectFilter
      allLabel="All employers"
      label="Employer"
      onChange={onChange}
      options={options}
      selected={selected}
    />,
  );
  return onChange;
}

describe("MultiSelectFilter", () => {
  it("offers each option with its count and adds a checked one to the selection", () => {
    const onChange = renderFilter(["Acme Analytics"]);

    expect(screen.getByText("Acme Analytics", { selector: "summary" })).toBeTruthy();
    expect(screen.getByRole("checkbox", { name: "Acme Analytics (1)" })).toHaveProperty(
      "checked",
      true,
    );
    fireEvent.click(screen.getByRole("checkbox", { name: "Northwind Health (3)" }));

    expect(onChange).toHaveBeenCalledWith(["Acme Analytics", "Northwind Health"]);
  });

  it("keeps a chosen value the counts no longer include so it can be unchecked", () => {
    const onChange = renderFilter(["Globex", "Acme Analytics"]);

    expect(screen.getByText("2 selected", { selector: "summary" })).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox", { name: "Globex (0)" }));

    expect(onChange).toHaveBeenCalledWith(["Acme Analytics"]);
  });

  it("clears the whole selection at once", () => {
    const onChange = renderFilter(["Acme Analytics"]);

    fireEvent.click(screen.getByRole("button", { name: "Clear employer filter" }));

    expect(onChange).toHaveBeenCalledWith([]);
    // The clear button leaves with the selection, so focus stays where Escape still works.
    expect(document.activeElement).toBe(
      screen.getByText("Acme Analytics", { selector: "summary" }),
    );
  });

  it("closes the open panel on Escape and returns focus to its toggle", () => {
    renderFilter([]);
    const toggle = screen.getByText("All employers", { selector: "summary" });
    const disclosure = toggle.closest("details") as HTMLDetailsElement;
    disclosure.open = true;

    fireEvent.keyDown(screen.getByRole("checkbox", { name: "Acme Analytics (1)" }), {
      key: "Escape",
    });

    expect(disclosure.open).toBe(false);
    expect(document.activeElement).toBe(toggle);
  });

  it("closes the open panel when the person presses outside it", () => {
    renderFilter([]);
    const disclosure = screen
      .getByText("All employers", { selector: "summary" })
      .closest("details") as HTMLDetailsElement;
    fireEvent.click(screen.getByText("All employers", { selector: "summary" }));
    expect(disclosure.open).toBe(true);

    fireEvent.pointerDown(screen.getByRole("checkbox", { name: "Acme Analytics (1)" }));
    expect(disclosure.open).toBe(true);
    fireEvent.pointerDown(document.body);

    expect(disclosure.open).toBe(false);
  });

  it("names the unfiltered state and offers no clear button", () => {
    renderFilter([]);

    expect(screen.getByText("All employers", { selector: "summary" })).toBeTruthy();
    expect(screen.getByRole("group", { name: "Employer" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Clear/ })).toBeNull();
  });
});
