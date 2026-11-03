import { fireEvent, render, screen } from "@testing-library/react";
import { HttpResponse, http } from "msw/http";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { FREE_MODE_REASON, PROVIDER_SETTINGS_PATH } from "../../components/freeMode";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import type { components } from "../schema";
import { useCreateNote } from "./records";

function NoteForm() {
  const { submit } = useCreateNote("workspace-1", { entityType: "workspace", entityId: null });
  return (
    <button onClick={submit} type="button">
      Save note
    </button>
  );
}

function refuseWith(problem: components["schemas"]["Problem"]) {
  server.use(http.post("*/api/v1/notes", () => HttpResponse.json(problem, { status: 409 })));
  render(
    <MemoryRouter>
      <AppProviders>
        <NoteForm />
      </AppProviders>
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Save note" }));
}

describe("useWorkspaceMutation errors", () => {
  it("shows the shared free-mode copy with a way to provider settings", async () => {
    refuseWith({
      type: "urn:clearscout:problem:free-mode",
      title: "Free mode",
      status: 409,
      detail: "No provider API key is set, so paid actions are off.",
      correlation_id: "correlation-1",
      hint: "Add an API key in Settings > Provider.",
    });

    const toast = await screen.findByRole("alert");
    expect(toast.textContent).toContain(FREE_MODE_REASON);
    expect(toast.textContent).not.toContain("No provider API key is set");
    const link = screen.getByRole("link", { name: "Open provider settings" });
    expect(link.getAttribute("href")).toBe(PROVIDER_SETTINGS_PATH);
  });

  it("keeps any other conflict's own message", async () => {
    refuseWith({
      type: "urn:clearscout:problem:conflict",
      title: "Conflict",
      status: 409,
      detail: "That note title is already in use.",
      correlation_id: "correlation-2",
    });

    const toast = await screen.findByRole("alert");
    expect(toast.textContent).toContain("That note title is already in use.");
    expect(screen.queryByRole("link", { name: "Open provider settings" })).toBeNull();
  });
});
