import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";

import { ApplyPage } from "./ApplyPage";

vi.mock("./ApplicationsPage", () => ({ ApplicationsPage: () => <p>Board</p> }));
vi.mock("./DocumentsPage", () => ({ DocumentsPage: () => <p>Files</p> }));

function renderAt(search: string) {
  render(
    <MemoryRouter initialEntries={[`/apply${search}`]}>
      <ApplyPage />
    </MemoryRouter>,
  );
}

it("opens Applications help saying a prepared application is not submitted", () => {
  renderAt("");
  fireEvent.click(screen.getByRole("button", { name: "Help: Applications" }));
  expect(screen.getByRole("region", { name: "Applications" }).textContent).toContain(
    "Prepared is not submitted",
  );
});

it("follows the Documents tab with help saying every version is kept and hashed", () => {
  renderAt("");
  fireEvent.click(screen.getByRole("tab", { name: "Documents" }));
  fireEvent.click(screen.getByRole("button", { name: "Help: Documents" }));
  expect(screen.getByRole("region", { name: "Documents" }).textContent).toContain(
    "Every version is kept and hashed",
  );
});
