import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import type { PendingDiscovery, RelevanceReviewConfiguration } from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { dispositionHandlers, pendingQueueHandlers } from "../../test/msw/pendingDiscoveryHandlers";
import { requestLog } from "../../test/requestLog";
import { evaluationPreflightHandler } from "../../test/taskTypes";
import { pendingDiscoveryRow } from "./pendingDiscoveryFixtures";
import { PendingDiscoveries } from "./PendingDiscoveries";

afterEach(() => {
  window.localStorage.clear();
});

// Role 0 sorts first (higher rank), so the cursor starts on it.
const defaultRows = [
  pendingDiscoveryRow({ id: "pending-0", title: "Role 0", location: "Northtown", rank: 2 }),
  pendingDiscoveryRow({ id: "pending-1", title: "Role 1", location: "Northtown", rank: 1 }),
];

type Written = { path: string; body: Record<string, unknown> };

function writes(log: ReturnType<typeof requestLog>): Written[] {
  return log.requests
    .filter((request) => request.method !== "GET")
    .map((request) => ({
      path: request.url.pathname,
      body: (request.body ?? {}) as Record<string, unknown>,
    }));
}

function installQueue(
  rows: PendingDiscovery[] = defaultRows,
  reviewConfiguration?: RelevanceReviewConfiguration,
) {
  const log = requestLog();
  server.use(
    evaluationPreflightHandler(),
    http.post("*/api/v1/tasks", async ({ request }) => {
      await log.record(request);
      return HttpResponse.json({ id: "task-1", status: "queued" });
    }),
    ...dispositionHandlers({ onPreview: log.record, onApply: log.record }),
    ...pendingQueueHandlers({ page: rows, relevanceReviewConfiguration: reviewConfiguration }),
  );
  return log;
}

function renderQueue() {
  return render(
    <MemoryRouter>
      <AppProviders>
        <PendingDiscoveries workspaceId="workspace-1" />
      </AppProviders>
    </MemoryRouter>,
  );
}

async function focusedRegion() {
  const region = await screen.findByRole("group", { name: /keyboard triage/i });
  fireEvent.focus(region);
  return region;
}

const rowOf = (title: string) => screen.getByLabelText(`Select ${title} at Globex`).closest("tr");
const isCursor = (title: string) => rowOf(title)?.getAttribute("aria-current") === "true";

const enqueued = (written: Written[], taskType: string) =>
  written.filter((call) => call.body.task_type === taskType);

describe("pending queue keyboard triage", () => {
  it("moves a visible row cursor with j and k", async () => {
    installQueue();
    renderQueue();
    await screen.findByText("Role 0");
    await focusedRegion();

    expect(isCursor("Role 0")).toBe(true);
    expect(isCursor("Role 1")).toBe(false);

    fireEvent.keyDown(await focusedRegion(), { key: "j" });
    expect(isCursor("Role 1")).toBe(true);
    expect(isCursor("Role 0")).toBe(false);

    fireEvent.keyDown(await focusedRegion(), { key: "k" });
    expect(isCursor("Role 0")).toBe(true);
  });

  it("ignores navigation keys while focus is inside a field", async () => {
    installQueue();
    renderQueue();
    await screen.findByText("Role 0");
    const checkbox = screen.getByLabelText("Select Role 0 at Globex");
    fireEvent.focus(checkbox);
    fireEvent.keyDown(checkbox, { key: "j" });

    expect(isCursor("Role 1")).toBe(false);
  });

  it("dismisses the focused row through the disposition path and offers undo", async () => {
    const log = installQueue();
    renderQueue();
    await screen.findByText("Role 0");
    fireEvent.keyDown(await focusedRegion(), { key: "x" });

    await waitFor(() =>
      expect(writes(log).some((call) => call.path.endsWith("/pending-dispositions/apply"))).toBe(
        true,
      ),
    );
    const dismiss = writes(log).find((call) => call.path.endsWith("/pending-dispositions/preview"));
    expect(dismiss?.body).toMatchObject({
      action: "dismiss",
      pending_discovery_ids: ["pending-0"],
    });
    // No pre-dismiss confirmation; the undo affordance is the safety net.
    expect(screen.queryByRole("dialog")).toBeNull();

    const undo = await screen.findByRole("button", { name: "Undo" });
    fireEvent.click(undo);

    await waitFor(() =>
      expect(
        writes(log).some(
          (call) =>
            call.path.endsWith("/pending-dispositions/preview") && call.body.action === "restore",
        ),
      ).toBe(true),
    );
  });

  it("routes e through the same spend confirmation and enqueues one evaluation", async () => {
    const log = installQueue();
    renderQueue();
    await screen.findByText("Role 0");
    fireEvent.keyDown(await focusedRegion(), { key: "e" });

    const dialog = within(await screen.findByRole("dialog"));
    fireEvent.click(dialog.getByRole("button", { name: "Run full evaluation" }));

    await waitFor(() =>
      expect(enqueued(writes(log), "discovery.evaluate_batch")).not.toHaveLength(0),
    );
    expect(enqueued(writes(log), "discovery.evaluate_batch")[0]?.body).toMatchObject({
      task_type: "discovery.evaluate_batch",
      payload: { pending_discovery_ids: ["pending-0"] },
    });
  });

  it("does not open or enqueue evaluation for an unread cursor row", async () => {
    const log = installQueue([
      pendingDiscoveryRow({
        id: "pending-0",
        title: "Role 0",
        location: "Northtown",
        rank: 2,
        readiness: "no_body",
        content_status: "not_yet_read",
        retained_content_id: null,
        current_content_id: null,
      }),
    ]);
    renderQueue();
    fireEvent.change(await screen.findByLabelText("Content readiness"), {
      target: { value: "all" },
    });
    await screen.findByText("Role 0");
    fireEvent.keyDown(await focusedRegion(), { key: "e" });

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByText("Refresh this posting first.")).toBeTruthy();
    expect(enqueued(writes(log), "discovery.evaluate_batch")).toHaveLength(0);
  });

  it("does not open or enqueue relevance review for an unread cursor row", async () => {
    const log = installQueue([
      pendingDiscoveryRow({
        id: "pending-0",
        title: "Role 0",
        location: "Northtown",
        rank: 2,
        readiness: "no_body",
        content_status: "not_yet_read",
        retained_content_id: null,
        current_content_id: null,
      }),
    ]);
    renderQueue();
    fireEvent.change(await screen.findByLabelText("Content readiness"), {
      target: { value: "all" },
    });
    await screen.findByText("Role 0");
    fireEvent.keyDown(await focusedRegion(), { key: "r" });

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByText("Refresh this posting first.")).toBeTruthy();
    expect(enqueued(writes(log), "discovery.relevance_review")).toHaveLength(0);
  });

  it("routes r through the same spend confirmation and enqueues one relevance review", async () => {
    const log = installQueue();
    renderQueue();
    await screen.findByText("Role 0");
    fireEvent.keyDown(await focusedRegion(), { key: "r" });

    const dialog = within(await screen.findByRole("dialog"));
    fireEvent.click(dialog.getByRole("button", { name: "Review selected relevance" }));

    await waitFor(() =>
      expect(enqueued(writes(log), "discovery.relevance_review")).not.toHaveLength(0),
    );
    expect(enqueued(writes(log), "discovery.relevance_review")[0]?.body).toMatchObject({
      task_type: "discovery.relevance_review",
      payload: { pending_discovery_ids: ["pending-0"] },
    });
  });

  it("does not open a paid confirmation for r when the relevance-review configuration cannot be described", async () => {
    // An OK response naming no model cannot be described, which holds paid
    // work even though the read itself succeeded.
    const log = installQueue(defaultRows, {
      provider: "anthropic",
      model_choice: { model: "", effort: null, service_tier: "standard" },
      batch_rows: 20,
    });
    renderQueue();
    await screen.findByText("Role 0");
    fireEvent.keyDown(await focusedRegion(), { key: "r" });

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(
      screen.getByText("Provider configuration could not be read, so paid review is held"),
    ).toBeTruthy();
    expect(enqueued(writes(log), "discovery.relevance_review")).toHaveLength(0);
  });

  it("does not fire a paid action when the key is pressed on a focused row control", async () => {
    const log = installQueue();
    renderQueue();
    await screen.findByText("Role 0");
    // Focus a row's own control (row 1's Dismiss button) rather than the container,
    // then press the paid evaluate key. It must not evaluate the cursor row (row 0).
    const dismissButtons = screen.getAllByRole("button", { name: "Dismiss from pending" });
    fireEvent.focus(dismissButtons[1]!);
    fireEvent.keyDown(dismissButtons[1]!, { key: "e" });

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(enqueued(writes(log), "discovery.evaluate_batch")).toHaveLength(0);
  });

  it("restores the last dismissal with the u shortcut", async () => {
    const log = installQueue();
    renderQueue();
    await screen.findByText("Role 0");
    fireEvent.keyDown(await focusedRegion(), { key: "x" });
    await screen.findByRole("button", { name: "Undo" });

    fireEvent.keyDown(await focusedRegion(), { key: "u" });
    await waitFor(() =>
      expect(
        writes(log).some(
          (call) =>
            call.path.endsWith("/pending-dispositions/preview") && call.body.action === "restore",
        ),
      ).toBe(true),
    );
  });

  it("dismisses a row its source no longer lists without offering undo", async () => {
    const log = installQueue([
      pendingDiscoveryRow({
        id: "pending-0",
        title: "Role 0",
        location: "Northtown",
        rank: 2,
        withdrawn_at: "2026-09-09T00:00:00Z",
      }),
    ]);
    renderQueue();
    await screen.findByText("Role 0");
    fireEvent.keyDown(await focusedRegion(), { key: "x" });

    expect(await screen.findByText("Dismissed Role 0 at Globex.")).toBeTruthy();
    expect(writes(log).some((call) => call.path.endsWith("/pending-dispositions/apply"))).toBe(
      true,
    );
    expect(screen.queryByRole("button", { name: "Undo" })).toBeNull();
  });

  it("keeps the undo offer when a restore fails for a reason other than the server's", async () => {
    installQueue();
    renderQueue();
    await screen.findByText("Role 0");
    fireEvent.keyDown(await focusedRegion(), { key: "x" });
    await screen.findByRole("button", { name: "Undo" });
    server.use(
      http.post("*/api/v1/workspaces/:workspaceId/pending-dispositions/preview", () =>
        HttpResponse.error(),
      ),
    );

    fireEvent.keyDown(await focusedRegion(), { key: "u" });

    expect(await screen.findByText("Not restored: the restore failed.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Undo" })).toBeTruthy();
  });

  it("says why the u shortcut restored nothing", async () => {
    installQueue();
    renderQueue();
    await screen.findByText("Role 0");
    fireEvent.keyDown(await focusedRegion(), { key: "x" });
    await screen.findByRole("button", { name: "Undo" });
    server.use(
      http.post("*/api/v1/workspaces/:workspaceId/pending-dispositions/preview", () =>
        HttpResponse.json(
          {
            detail:
              "Every selected discovery was withdrawn by its source, so none can be restored.",
          },
          { status: 409, headers: { "Content-Type": "application/problem+json" } },
        ),
      ),
    );

    fireEvent.keyDown(await focusedRegion(), { key: "u" });

    expect(
      await screen.findByText(
        "Not restored: Every selected discovery was withdrawn by its source, so none can be restored.",
      ),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Undo" })).toBeNull();
  });
});
