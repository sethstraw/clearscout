import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw/http";
import { MemoryRouter } from "react-router-dom";

import type { Health } from "../api/client/types";

import { AppProviders } from "../providers";
import { PaidAction } from "./PaidAction";
import { server } from "../test/msw/server";
import { evaluationPreflightHandler } from "../test/taskTypes";

const costs = {
  loaded: true,
  isError: false,
  isPaid: () => true,
  cost: () => "Paid",
  inline: () => "about $0.25 per attempt",
  summary: () => "May incur provider charges. About $0.25 for 1 attempt.",
  confirmation: () => "About $0.25 for 1 attempt (range $0.15 to $0.40, from 3 runs).",
};

it("shows current evaluation estimates before consent and cancellation queues nothing", async () => {
  let estimateRequests = 0;
  server.use(evaluationPreflightHandler(() => estimateRequests++));
  const onAct = vi.fn();
  render(
    <AppProviders>
      <PaidAction
        costs={costs}
        label="Evaluate"
        taskType="discovery.evaluate"
        evaluation={{ workspaceId: "workspace-jobs", selection: { job_posting_ids: ["job-1"] } }}
        confirmation={{
          title: "Evaluate job?",
          description: "Paid evaluation",
          confirmLabel: "Approve evaluation",
        }}
        onAct={onAct}
      />
    </AppProviders>,
  );
  screen.getByRole("button", { name: "Evaluate" }).focus();
  fireEvent.click(screen.getByRole("button", { name: "Evaluate" }));
  fireEvent.click(screen.getByRole("button", { name: "Preparing cost estimate" }));
  expect(await screen.findByRole("dialog", { name: "Evaluate job?" })).toBeTruthy();
  expect(screen.getByText(/Estimated total at configured rates/).textContent).toContain("$0.2000");
  expect(screen.getByText(/Modeled total range for one attempt/).textContent).toContain("$0.5000");
  expect(onAct).not.toHaveBeenCalled();
  expect(estimateRequests).toBe(1);
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(onAct).not.toHaveBeenCalled();
  await vi.waitFor(() =>
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Evaluate" })),
  );
});

it("explains a failed preflight without opening paid confirmation or running the task", async () => {
  server.use(
    http.post("*/api/v1/workspaces/:workspaceId/evaluation-preflight", () =>
      HttpResponse.json({ detail: "Retained input unavailable" }, { status: 422 }),
    ),
  );
  const onAct = vi.fn();
  render(
    <AppProviders>
      <PaidAction
        costs={costs}
        label="Evaluate"
        taskType="discovery.evaluate"
        evaluation={{ workspaceId: "workspace-jobs", selection: { job_posting_ids: ["job-1"] } }}
        confirmation={{
          title: "Evaluate job?",
          description: "Paid evaluation",
          confirmLabel: "Approve evaluation",
        }}
        onAct={onAct}
      />
    </AppProviders>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Evaluate" }));
  expect((await screen.findByRole("alert")).textContent).toContain("Retained input unavailable");
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(onAct).not.toHaveBeenCalled();
});

it("runs the task on a plain confirmation, with no amount to enter", async () => {
  const onAct = vi.fn();
  render(
    <AppProviders>
      <PaidAction
        label="Run paid task"
        taskType="search.guided_campaign"
        onAct={onAct}
        costs={costs}
        confirmation={{
          title: "Run search?",
          description: "About $0.25 for 1 attempt (range $0.15 to $0.40, from 3 runs).",
          confirmLabel: "Approve search",
        }}
      />
    </AppProviders>,
  );

  fireEvent.click(screen.getByRole("button", { name: "Run paid task" }));
  const approve = await screen.findByRole<HTMLButtonElement>("button", { name: "Approve search" });
  expect(approve.disabled).toBe(false);
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.getByText(/About \$0.25 for 1 attempt/)).toBeTruthy();
  fireEvent.click(approve);

  await vi.waitFor(() => expect(onAct).toHaveBeenCalledOnce());
});

it("continues a retry under the original authorization", async () => {
  const onContinue = vi.fn();
  render(
    <AppProviders>
      <PaidAction
        label="Retry task"
        taskType="search.guided_campaign"
        onContinue={onContinue}
        costs={{ ...costs, confirmation: () => "Past cost $0.25" }}
        confirmation={{
          title: "Retry search?",
          description: "Past cost $0.25",
          confirmLabel: "Continue task",
        }}
      />
    </AppProviders>,
  );

  fireEvent.click(screen.getByRole("button", { name: "Retry task" }));
  expect(await screen.findByText(/This retry may add charges\./)).toBeTruthy();
  expect(screen.queryByRole("textbox")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Continue task" }));

  await vi.waitFor(() => expect(onContinue).toHaveBeenCalledOnce());
});

function healthInFreeMode(freeMode: boolean) {
  let providerReads = 0;
  server.use(
    http.get("*/api/v1/health", () =>
      HttpResponse.json({
        status: "ok",
        version: "0.1.0",
        database_reachable: true,
        schema_current: true,
        current_revision: "0001",
        head_revision: "0001",
        database_exists: true,
        worker_available: true,
        free_mode: freeMode,
      } satisfies Health),
    ),
    http.get("*/api/v1/providers", () => {
      providerReads++;
      return HttpResponse.json([]);
    }),
  );
  return () => providerReads;
}

function renderSearchAction(onAct = vi.fn()) {
  render(
    <AppProviders>
      <MemoryRouter>
        <PaidAction
          label="Search the web"
          taskType="search.guided_campaign"
          onAct={onAct}
          costs={costs}
          confirmation={{
            title: "Run search?",
            description: "Paid search",
            confirmLabel: "Approve search",
          }}
        />
      </MemoryRouter>
    </AppProviders>,
  );
  return onAct;
}

it("puts the Paid mark and the estimate on the button and in its description", async () => {
  const providerReads = healthInFreeMode(false);
  renderSearchAction();
  const button = screen.getByRole<HTMLButtonElement>("button", { name: "Search the web" });
  expect(within(button).getByText("Paid")).toBeTruthy();
  expect(within(button).getByText(/about \$0\.25 per attempt/)).toBeTruthy();
  const description = (button.getAttribute("aria-describedby") ?? "")
    .split(" ")
    .map((id) => document.getElementById(id)?.textContent ?? "")
    .join(" ");
  expect(description).toBe("Paid about $0.25 per attempt");
  await vi.waitFor(() => expect(button.disabled).toBe(false));
  expect(screen.queryByText("Spends money")).toBeNull();
  expect(providerReads()).toBe(0);
});

it("disables paid work in free mode and links to the provider settings", async () => {
  const providerReads = healthInFreeMode(true);
  const onAct = renderSearchAction();
  expect(await screen.findByText("Add a provider key to use paid actions.")).toBeTruthy();
  const button = screen.getByRole<HTMLButtonElement>("button", { name: "Search the web" });
  expect(button.disabled).toBe(true);
  expect(within(button).getByText("Paid")).toBeTruthy();
  const link = screen.getByRole("link", { name: "Open provider settings" });
  expect(link.getAttribute("href")).toBe("/settings?tab=provider");
  fireEvent.click(button);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(onAct).not.toHaveBeenCalled();
  expect(providerReads()).toBe(0);
});
