import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw/http";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { FREE_MODE_REASON, PROVIDER_SETTINGS_PATH } from "../../components/freeMode";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import type { Note, Task } from "../client/types";
import type { components } from "../schema";
import { taskRecord } from "../../features/job-search/pendingDiscoveryFixtures";
import { useCreateNote } from "./records";
import { useRetryTask } from "./tasks";

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

function BusyNoteForm() {
  const { submit, mutation } = useCreateNote("workspace-1", {
    entityType: "workspace",
    entityId: null,
  });
  return (
    <button aria-busy={mutation.isPending} onClick={submit} type="button">
      Save note
    </button>
  );
}

describe("useWorkspaceMutation refetch", () => {
  it("frees the button while the workspace task list is still refetching", async () => {
    window.localStorage.setItem("clearscout.workspace", "workspace-1");
    let taskReads = 0;
    let releaseTasks = () => {};
    const tasksHeld = new Promise<void>((resolve) => {
      releaseTasks = resolve;
    });
    server.use(
      http.get("*/api/v1/tasks", async () => {
        taskReads += 1;
        if (taskReads > 1) await tasksHeld;
        return HttpResponse.json([] satisfies Task[]);
      }),
      http.post("*/api/v1/notes", () =>
        HttpResponse.json({
          id: "note-1",
          workspace_id: "workspace-1",
          entity_type: "workspace",
          entity_id: null,
          title: "",
          body: "",
          pinned: false,
          evidence_eligible: false,
          archived_at: null,
          created_at: "2026-10-01T12:00:00Z",
          updated_at: "2026-10-01T12:00:00Z",
        } satisfies Note),
      ),
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <BusyNoteForm />
        </AppProviders>
      </MemoryRouter>,
    );
    await waitFor(() => expect(taskReads).toBe(1));
    const button = screen.getByRole("button", { name: "Save note" });
    fireEvent.click(button);

    await waitFor(() => expect(taskReads).toBe(2));
    await waitFor(() => expect(button.getAttribute("aria-busy")).toBe("false"));
    releaseTasks();
  });

  it("keeps a task action busy until the task list it changed is read again", async () => {
    window.localStorage.setItem("clearscout.workspace", "workspace-1");
    let taskReads = 0;
    let releaseTasks = () => {};
    const tasksHeld = new Promise<void>((resolve) => {
      releaseTasks = resolve;
    });
    server.use(
      http.get("*/api/v1/tasks", async () => {
        taskReads += 1;
        if (taskReads > 1) await tasksHeld;
        return HttpResponse.json([] satisfies Task[]);
      }),
      http.post("*/api/v1/tasks/:taskId/retry", () =>
        HttpResponse.json(taskRecord({ workspace_id: "workspace-1" })),
      ),
    );
    function RetryButton() {
      const retry = useRetryTask("workspace-1");
      return (
        <button aria-busy={retry.isPending} onClick={() => retry.mutate("task-1")} type="button">
          Retry processing
        </button>
      );
    }
    render(
      <MemoryRouter>
        <AppProviders>
          <RetryButton />
        </AppProviders>
      </MemoryRouter>,
    );
    await waitFor(() => expect(taskReads).toBe(1));
    const button = screen.getByRole("button", { name: "Retry processing" });
    fireEvent.click(button);

    await waitFor(() => expect(taskReads).toBe(2));
    expect(button.getAttribute("aria-busy")).toBe("true");
    releaseTasks();
    await waitFor(() => expect(button.getAttribute("aria-busy")).toBe("false"));
  });
});
