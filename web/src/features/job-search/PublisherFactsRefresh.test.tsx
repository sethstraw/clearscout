import { fireEvent, render, screen } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { PublisherFactsRefresh } from "./PublisherFactsRefresh";

/** Answer the enqueue with a finished task carrying the given progress counts. */
function installHandlers(progress: Record<string, unknown>) {
  const submitted: unknown[] = [];
  server.use(
    http.post("*/api/v1/tasks", async ({ request }) => {
      submitted.push(await request.json());
      return HttpResponse.json({ id: "task-1", status: "queued", progress: {} });
    }),
    http.get("*/api/v1/tasks/task-1", () =>
      HttpResponse.json({ id: "task-1", status: "completed", progress }),
    ),
  );
  return submitted;
}

function panel() {
  render(
    <AppProviders>
      <PublisherFactsRefresh pendingDiscoveryIds={["p1", "p2"]} workspaceId="workspace-1" />
    </AppProviders>,
  );
}

describe("PublisherFactsRefresh", () => {
  it("promises one refresh of the publisher facts, liveness, and posting text", async () => {
    const submitted = installHandlers({});
    panel();

    fireEvent.click(screen.getByRole("button", { name: "Refresh postings (2)" }));

    const confirmation = await screen.findByRole("dialog");
    expect(confirmation.textContent).toContain("keeps its stored facts and your corrected title");
    expect(confirmation.textContent).not.toContain("Nothing is spent");
    fireEvent.click(await screen.findByRole("button", { name: "Refresh 2" }));

    await screen.findByRole("status");
    expect(submitted).toEqual([
      {
        workspace_id: "workspace-1",
        task_type: "discovery.refresh_batch",
        payload: { pending_discovery_ids: ["p1", "p2"] },
      },
    ]);
  });

  it("reports the posting text each refreshed row reached", async () => {
    installHandlers({ refreshed: 2, content_readable: 1, content_unreadable: 1 });
    panel();

    fireEvent.click(screen.getByRole("button", { name: "Refresh postings (2)" }));
    fireEvent.click(await screen.findByRole("button", { name: "Refresh 2" }));

    const summary = await screen.findByRole("status");
    expect(summary.textContent).toContain("Refreshed 2");
    expect(summary.textContent).toContain("1 readable");
    expect(summary.textContent).toContain("1 unreadable");
  });

  it("reports not-checkable rows apart from genuine failures", async () => {
    installHandlers({ refreshed: 0, withdrawn_as_closed: 0, not_checkable: 1, failed: 1 });
    panel();

    fireEvent.click(screen.getByRole("button", { name: "Refresh postings (2)" }));
    fireEvent.click(await screen.findByRole("button", { name: "Refresh 2" }));

    const summary = await screen.findByRole("status");
    expect(summary.textContent).toContain("1 not checkable");
    expect(summary.textContent).toContain("1 failed");
  });

  it("reports rows the publisher would not confirm apart from failures", async () => {
    installHandlers({ refreshed: 1, withdrawn_as_closed: 1, unverified: 1, failed: 0 });
    panel();

    fireEvent.click(screen.getByRole("button", { name: "Refresh postings (2)" }));
    fireEvent.click(await screen.findByRole("button", { name: "Refresh 2" }));

    const summary = await screen.findByRole("status");
    expect(summary.textContent).toContain("1 closed");
    expect(summary.textContent).toContain("1 could not verify");
    expect(summary.textContent).not.toContain("failed");
  });
});
