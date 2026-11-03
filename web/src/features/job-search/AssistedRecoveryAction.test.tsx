import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw/http";
import { describe, expect, it } from "vitest";

import type { components } from "../../api/schema";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { TASK_TYPE_CATALOG } from "../../test/taskTypes";
import { AssistedRecoveryAction } from "./AssistedRecoveryAction";

const manifest = {
  pending_discovery_ids: ["pending-1"],
  snapshot_sha256: "a".repeat(64),
  provider: "openai",
  model: "gpt-5.4-mini",
  system_prompt_sha256: "b".repeat(64),
  user_prompt_sha256: "c".repeat(64),
  schema_sha256: "d".repeat(64),
  settings_sha256: "e".repeat(64),
  price_sha256: "f".repeat(64),
  estimate_cents: 37,
  issued_at: "2026-08-28T18:00:00Z",
  expires_at: "2026-08-28T18:15:00Z",
} satisfies components["schemas"]["PostingRecoveryApprovalManifest"];

function installHandlers(submitted: components["schemas"]["TaskCreate"][]) {
  server.use(
    http.get("*/api/v1/tasks/types", () => HttpResponse.json(TASK_TYPE_CATALOG)),
    http.get("*/api/v1/workspaces/:workspaceId/spend-history", () => HttpResponse.json([])),
    http.post("*/api/v1/workspaces/:workspaceId/posting-recovery-preflight", () =>
      HttpResponse.json(manifest),
    ),
    http.post("*/api/v1/tasks", async ({ request }) => {
      submitted.push((await request.json()) as components["schemas"]["TaskCreate"]);
      return HttpResponse.json({ id: "assisted-task", status: "queued", progress: {} });
    }),
    http.get("*/api/v1/tasks/assisted-task", () =>
      HttpResponse.json({ id: "assisted-task", status: "completed", progress: {} }),
    ),
  );
}

describe("AssistedRecoveryAction", () => {
  it("enqueues the approved manifest after one confirmation, with nothing to enter", async () => {
    const submitted: components["schemas"]["TaskCreate"][] = [];
    installHandlers(submitted);
    render(
      <AppProviders>
        <AssistedRecoveryAction pendingDiscoveryIds={["pending-1"]} workspaceId="workspace-1" />
      </AppProviders>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Estimate assisted recovery (1)" }));
    fireEvent.click(await screen.findByRole("button", { name: "Run assisted recovery" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/About \$0.37 for 1 posting/)).not.toBeNull();
    expect(within(dialog).getByText(/expires in 15 minutes/)).not.toBeNull();
    expect(within(dialog).queryByRole("textbox")).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "Run assisted recovery" }));

    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(submitted[0]).toEqual({
      workspace_id: "workspace-1",
      task_type: "posting.recover_batch",
      payload: { approval: manifest },
    });
  });

  it("marks only the control that enqueues the paid task as spending money", async () => {
    installHandlers([]);
    render(
      <AppProviders>
        <AssistedRecoveryAction pendingDiscoveryIds={["pending-1"]} workspaceId="workspace-1" />
      </AppProviders>,
    );

    expect(screen.getByRole("button", { name: "Estimate assisted recovery (1)" })).not.toBeNull();
    expect(screen.queryByText("Paid")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Estimate assisted recovery (1)" }));

    const run = await screen.findByRole("button", { name: "Run assisted recovery" });
    expect(within(run).getByText("Paid")).not.toBeNull();
  });
});
