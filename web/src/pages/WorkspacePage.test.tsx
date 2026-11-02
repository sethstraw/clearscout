import { render, screen } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { afterEach, describe, expect, it } from "vitest";

import type { Note } from "../api/client/types";
import { AppProviders } from "../providers";
import { server } from "../test/msw/server";
import { WorkspacePage } from "./WorkspacePage";

afterEach(() => {
  window.localStorage.clear();
});

const userNotes = [
  {
    id: "note-1",
    workspace_id: "workspace-records",
    entity_type: "workspace",
    entity_id: null,
    title: "Synthetic follow-up",
    body: "A user-authored working note.",
    pinned: false,
    created_at: "2026-08-06T12:00:00Z",
    updated_at: "2026-08-06T12:00:00Z",
    archived_at: null,
    evidence_eligible: false,
  },
] satisfies Note[];

describe("WorkspacePage", () => {
  it("marks user notes as non-evidence while preserving their editable record", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-records");
    server.use(http.get("*/api/v1/notes", () => HttpResponse.json(userNotes)));

    render(
      <AppProviders>
        <WorkspacePage />
      </AppProviders>,
    );

    expect(await screen.findByText("Synthetic follow-up")).toBeTruthy();
    expect(screen.getByText("Not evidence")).toBeTruthy();
    expect(screen.getByText(/Nothing written here can justify a score/)).toBeTruthy();
  });
});
