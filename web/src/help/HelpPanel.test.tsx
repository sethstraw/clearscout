import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { FIND_TOUR, HELP, USER_GUIDE_URL } from "./content";
import { HelpPanel } from "./HelpPanel";

it("links the entry to its user guide section", () => {
  render(<HelpPanel helpKey="settings" id="help" onClose={() => undefined} />);
  const link = screen.getByRole("link", { name: "Read more in the user guide" });
  expect(link.getAttribute("href")).toBe(`${USER_GUIDE_URL}#${HELP.settings.docsAnchor}`);
  expect(screen.queryByRole("button", { name: "Take the tour" })).toBeNull();
});

it("starts the Find jobs tour from the panel and closes the panel", async () => {
  const onClose = vi.fn();
  render(<HelpPanel helpKey="find.boards" id="help" onClose={onClose} />);
  fireEvent.click(screen.getByRole("button", { name: "Take the tour" }));
  expect(onClose).toHaveBeenCalledOnce();
  expect(await screen.findByText(FIND_TOUR[0].title)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  expect(await screen.findByText(FIND_TOUR[1].title)).toBeTruthy();
});
