import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";

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
