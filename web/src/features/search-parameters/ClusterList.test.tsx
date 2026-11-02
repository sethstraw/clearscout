import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { components } from "../../api/schema";
import { ClusterList } from "./ClusterList";

type Cluster = components["schemas"]["Cluster"];

function renderList(
  values: Cluster[],
  onChange: (values: Cluster[]) => void,
  caps: { cap?: number; termCap?: number } = {},
) {
  return render(
    <ClusterList
      addLabel="Add a capability"
      cap={caps.cap ?? 5}
      label="Capabilities"
      noun="Capability"
      onChange={onChange}
      termCap={caps.termCap ?? 10}
      values={values}
    />,
  );
}

const three: Cluster[] = [
  { name: "First", terms: [], because: "" },
  { name: "Second", terms: [], because: "" },
  { name: "Third", terms: [], because: "" },
];

describe("ClusterList", () => {
  it("adds an empty cluster to the end", () => {
    const onChange = vi.fn<(values: Cluster[]) => void>();
    renderList([{ name: "Data platforms", terms: ["etl"], because: "core" }], onChange);

    fireEvent.click(screen.getByRole("button", { name: "Add a capability" }));

    expect(onChange).toHaveBeenCalledWith([
      { name: "Data platforms", terms: ["etl"], because: "core" },
      { name: "", terms: [], because: "" },
    ]);
  });

  it("edits the name without touching other fields", () => {
    const onChange = vi.fn<(values: Cluster[]) => void>();
    renderList([{ name: "Data", terms: ["etl"], because: "core" }], onChange);

    fireEvent.change(screen.getByLabelText("Capability name"), { target: { value: "Data eng" } });

    expect(onChange).toHaveBeenCalledWith([{ name: "Data eng", terms: ["etl"], because: "core" }]);
  });

  it("adds a term through the token field", () => {
    const onChange = vi.fn<(values: Cluster[]) => void>();
    renderList([{ name: "Data", terms: ["etl"], because: "core" }], onChange);

    const terms = screen.getByLabelText("Capability terms");
    fireEvent.change(terms, { target: { value: "warehouse" } });
    fireEvent.keyDown(terms, { key: "Enter" });

    expect(onChange).toHaveBeenCalledWith([
      { name: "Data", terms: ["etl", "warehouse"], because: "core" },
    ]);
  });

  it("edits the because field", () => {
    const onChange = vi.fn<(values: Cluster[]) => void>();
    renderList([{ name: "Data", terms: [], because: "" }], onChange);

    fireEvent.change(screen.getByLabelText("Why this capability matters"), {
      target: { value: "central to the mandate" },
    });

    expect(onChange).toHaveBeenCalledWith([
      { name: "Data", terms: [], because: "central to the mandate" },
    ]);
  });

  it("removes a cluster by position and preserves the order of the rest", () => {
    const onChange = vi.fn<(values: Cluster[]) => void>();
    renderList(three, onChange);

    fireEvent.click(screen.getAllByRole("button", { name: "Remove" })[1]!);

    expect(onChange).toHaveBeenCalledWith([
      { name: "First", terms: [], because: "" },
      { name: "Third", terms: [], because: "" },
    ]);
  });

  it("edits one row while leaving earlier rows and order intact", () => {
    const onChange = vi.fn<(values: Cluster[]) => void>();
    renderList(
      [
        { name: "First", terms: ["a"], because: "" },
        { name: "Second", terms: ["b"], because: "" },
      ],
      onChange,
    );

    fireEvent.change(screen.getAllByLabelText("Capability name")[1]!, {
      target: { value: "Second edited" },
    });

    expect(onChange).toHaveBeenCalledWith([
      { name: "First", terms: ["a"], because: "" },
      { name: "Second edited", terms: ["b"], because: "" },
    ]);
  });

  it("marks only the rows past the cap as not ranked and says how many are read", () => {
    renderList(three, vi.fn(), { cap: 2 });

    expect(screen.getByRole("status").textContent).toBe(
      "Ranking reads the first 2 of 3 capabilities; the rest still shape guided searches. Move a row into the first 2 to rank against it.",
    );
    const badges = screen.getAllByText("Not ranked");
    expect(badges).toHaveLength(1);
    expect(badges[0]!.closest("li")).toBe(screen.getAllByRole("listitem")[2]);
    expect(screen.getAllByLabelText("Capability name")[2]!.getAttribute("aria-describedby")).toBe(
      badges[0]!.id,
    );
    expect(screen.getAllByLabelText("Capability name")[0]!.hasAttribute("aria-describedby")).toBe(
      false,
    );
  });

  it("shows no cap note or badge when every cluster is read", () => {
    renderList(three, vi.fn(), { cap: 5 });

    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByText("Not ranked")).toBeNull();
  });

  it("skips blank rows when counting the ranked clusters", () => {
    const named = (name: string): Cluster => ({ name, terms: [], because: "" });
    const withBlankSecond = ["One", " ", "Three", "Four", "Five", "Six"].map(named);
    const { unmount } = renderList(withBlankSecond, vi.fn(), { cap: 5 });

    expect(screen.queryByText("Not ranked")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
    unmount();

    renderList([...withBlankSecond, named("Seven")], vi.fn(), { cap: 5 });

    const badges = screen.getAllByText("Not ranked");
    expect(badges).toHaveLength(1);
    expect(badges[0]!.closest("li")).toBe(screen.getAllByRole("listitem")[6]);
    expect(screen.getByRole("status").textContent).toContain("first 5 of 6 capabilities");
  });

  it("moves a cluster down by swapping it with the next one and follows it with focus", async () => {
    const onChange = vi.fn<(values: Cluster[]) => void>();
    renderList(three, onChange);

    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Move up First" }).disabled).toBe(
      true,
    );
    fireEvent.click(screen.getByRole("button", { name: "Move down First" }));

    expect(onChange).toHaveBeenCalledWith([three[1], three[0], three[2]]);
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "Move down Second" })),
    );
  });

  it("moves focus to the destination name when the moved row reaches the end", async () => {
    renderList(three, vi.fn());

    fireEvent.click(screen.getByRole("button", { name: "Move down Second" }));

    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getAllByLabelText("Capability name")[2]),
    );
  });

  it("stops accepting terms once a cluster reaches the term cap", () => {
    const onChange = vi.fn<(values: Cluster[]) => void>();
    renderList([{ name: "Data", terms: ["etl", "warehouse"], because: "" }], onChange, {
      termCap: 2,
    });

    const terms = screen.getByLabelText<HTMLInputElement>("Capability terms");
    expect(terms.disabled).toBe(true);
    expect(terms.placeholder).toBe("Type a term, then press Enter");
    expect(screen.getByText(/Limit of 2 reached; remove a term to add another\./)).toBeTruthy();
    fireEvent.change(terms, { target: { value: "lakehouse" } });
    fireEvent.keyDown(terms, { key: "Enter" });

    expect(onChange).not.toHaveBeenCalled();
  });
});
