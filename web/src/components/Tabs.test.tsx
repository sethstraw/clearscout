import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { TabPanel, Tabs } from "./Tabs";

function Example() {
  const [selected, setSelected] = useState("overview");
  return (
    <>
      <Tabs
        id="example"
        items={[
          { id: "overview", label: "Overview" },
          { id: "notes", label: "Notes" },
        ]}
        label="Example sections"
        onSelect={setSelected}
        selected={selected}
      />
      <TabPanel id="example" selected={selected}>
        {selected}
      </TabPanel>
    </>
  );
}

describe("Tabs", () => {
  it("supports roving focus, keyboard selection, and panel relationships", () => {
    render(<Example />);
    const overview = screen.getByRole("tab", { name: "Overview" });
    const notes = screen.getByRole("tab", { name: "Notes" });
    expect(overview.getAttribute("aria-controls")).toBe("example-panel-overview");
    expect(screen.getByRole("tabpanel").getAttribute("aria-labelledby")).toBe(
      "example-tab-overview",
    );
    expect(overview.tabIndex).toBe(0);
    fireEvent.keyDown(overview, { key: "ArrowRight" });
    expect(notes.getAttribute("aria-selected")).toBe("true");
    expect(notes.tabIndex).toBe(0);
    expect(document.activeElement).toBe(notes);
    expect(screen.getByRole("tabpanel").id).toBe("example-panel-notes");
  });
});
