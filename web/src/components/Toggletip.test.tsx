import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { Toggletip } from "./Toggletip";

it("opens on click, names its content, and closes with Escape", async () => {
  render(<Toggletip label="About fresh details">Fresh details re-read every posting.</Toggletip>);
  const button = screen.getByRole("button", { name: "About fresh details" });
  expect(screen.queryByText("Fresh details re-read every posting.")).toBeNull();

  fireEvent.click(button);
  const tip = await screen.findByRole("dialog", { name: "About fresh details" });
  expect(tip.textContent).toBe("Fresh details re-read every posting.");
  expect(button.getAttribute("aria-expanded")).toBe("true");

  fireEvent.keyDown(document.activeElement ?? tip, { key: "Escape" });
  await screen.findByRole("button", { name: "About fresh details", expanded: false });
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("portals the open panel out of the paragraph that holds its button", async () => {
  const errors = vi.spyOn(console, "error").mockImplementation(() => {});
  render(
    <p data-testid="host">
      Fresh details <Toggletip label="About fresh details">Re-reads every posting.</Toggletip>
    </p>,
  );
  fireEvent.click(screen.getByRole("button", { name: "About fresh details" }));
  const tip = await screen.findByRole("dialog", { name: "About fresh details" });
  expect(screen.getByTestId("host").contains(tip)).toBe(false);
  expect(errors).not.toHaveBeenCalled();
  errors.mockRestore();
});
