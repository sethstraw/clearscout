import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { taskTypesHandler } from "../../test/taskTypes";
import { pendingDiscoveryRow } from "./pendingDiscoveryFixtures";
import { PendingRowTitle } from "./PendingRowTitle";

/** The bodies the title correction sent, and the conflict the server may answer with. */
function useTitleCorrection(conflict?: string): unknown[] {
  const corrections: unknown[] = [];
  server.use(
    taskTypesHandler(),
    http.patch(
      "*/api/v1/workspaces/:workspaceId/pending-discoveries/:pendingId",
      async ({ request }) => {
        corrections.push(await request.json());
        if (conflict) return HttpResponse.json({ detail: conflict }, { status: 409 });
        return HttpResponse.json(pendingDiscoveryRow({ title: "Sr Director, RWD" }));
      },
    ),
  );
  return corrections;
}

function renderRow() {
  render(
    <AppProviders>
      <PendingRowTitle
        currentTitle=""
        displayTitle="posting at northwind"
        pendingId="pd-1"
        url="https://job-boards.greenhouse.io/northwind/jobs/1234"
        workspaceId="ws-1"
      />
    </AppProviders>,
  );
}

describe("PendingRowTitle", () => {
  it("prompts for a missing title and keyboard-submits with Enter", async () => {
    const corrections = useTitleCorrection();
    renderRow();

    fireEvent.click(screen.getByRole("button", { name: "Add title for posting at northwind" }));
    const input = await screen.findByLabelText("Correct the title");
    fireEvent.change(input, { target: { value: "Sr Director, RWD" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => expect(corrections).toEqual([{ title: "Sr Director, RWD" }]));
  });

  it("cancels on Escape without calling PATCH", async () => {
    const corrections = useTitleCorrection();
    renderRow();

    fireEvent.click(screen.getByRole("button", { name: "Add title for posting at northwind" }));
    const input = await screen.findByLabelText("Correct the title");
    fireEvent.change(input, { target: { value: "wrong entry" } });
    fireEvent.keyDown(input, { key: "Escape" });

    await waitFor(() => {
      expect(screen.queryByLabelText("Correct the title")).toBeNull();
    });
    expect(corrections).toEqual([]);
  });

  it("refuses an empty title inline and does not call PATCH", async () => {
    const corrections = useTitleCorrection();
    renderRow();

    fireEvent.click(screen.getByRole("button", { name: "Add title for posting at northwind" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect((await screen.findByRole("alert")).textContent).toContain("A title is required.");
    expect(corrections).toEqual([]);
  });

  it("surfaces the server error body on 409 conflict", async () => {
    useTitleCorrection("A discovery evaluation is in flight for this row.");
    renderRow();

    fireEvent.click(screen.getByRole("button", { name: "Add title for posting at northwind" }));
    fireEvent.change(screen.getByLabelText("Correct the title"), {
      target: { value: "Sr Director, RWD" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect((await screen.findByRole("alert")).textContent).toContain(
      "A discovery evaluation is in flight for this row.",
    );
  });

  it("stops Enter propagation so an ancestor form does not submit", async () => {
    useTitleCorrection();
    const ancestor = vi.fn((event: React.FormEvent) => event.preventDefault());
    render(
      <AppProviders>
        <form onSubmit={ancestor}>
          <PendingRowTitle
            currentTitle=""
            displayTitle="posting at northwind"
            pendingId="pd-1"
            url={null}
            workspaceId="ws-1"
          />
        </form>
      </AppProviders>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Add title for posting at northwind" }));
    const input = await screen.findByLabelText("Correct the title");
    fireEvent.change(input, { target: { value: "New title" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(ancestor).not.toHaveBeenCalled();
  });
});
