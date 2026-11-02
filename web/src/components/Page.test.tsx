import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { HELP } from "../help/content";
import { Page } from "./Page";

it("opens the page help from the header and closes it with Escape", () => {
  render(
    <Page eyebrow="Find" title="Find jobs" description="Scan boards." help="find.boards">
      <p>Body</p>
    </Page>,
  );
  const button = screen.getByRole("button", { name: "Help: Boards" });
  expect(button.getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByRole("region", { name: "Boards" })).toBeNull();

  fireEvent.click(button);
  const panel = screen.getByRole("region", { name: "Boards" });
  expect(button.getAttribute("aria-expanded")).toBe("true");
  expect(panel.textContent).toContain(HELP["find.boards"].body);
  expect(document.activeElement).toBe(panel);

  fireEvent.keyDown(panel, { key: "Escape" });
  expect(screen.queryByRole("region", { name: "Boards" })).toBeNull();
  expect(document.activeElement).toBe(button);
});

it("never starts the tour on its own", () => {
  render(
    <Page eyebrow="Find" title="Find jobs" description="Scan boards." help="find.boards">
      <p>Body</p>
    </Page>,
  );
  expect(document.querySelector(".driver-popover")).toBeNull();
});

it("shows no help button when the page names no help topic", () => {
  render(
    <Page eyebrow="Jobs" title="Jobs" description="Tracked postings.">
      <p>Body</p>
    </Page>,
  );
  expect(screen.queryByRole("button", { name: /^Help/ })).toBeNull();
});
