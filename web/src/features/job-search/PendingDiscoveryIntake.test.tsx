import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { FoundPostingAddResponse, FoundPostingCreate } from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { PendingDiscoveryIntake } from "./PendingDiscoveryIntake";
import { foundPostingCheck, pendingDiscoveryRow, taskRecord } from "./pendingDiscoveryFixtures";

const INTAKE_ROUTE = "*/api/v1/workspaces/workspace-1/pending-discoveries";

function renderIntake() {
  const onAdded = vi.fn();
  render(
    <AppProviders>
      <PendingDiscoveryIntake onAdded={onAdded} workspaceId="workspace-1" />
    </AppProviders>,
  );
  fireEvent.click(screen.getByText("Add a posting"));
  return onAdded;
}

function submitLinks(links: string) {
  fireEvent.change(screen.getByLabelText("Posting link"), { target: { value: links } });
  fireEvent.submit(screen.getByLabelText("Posting link").closest("form")!);
}

function intakeResult(url: string) {
  const list = within(screen.getByRole("list", { name: "Posting link results" }));
  const item = list.getAllByRole("listitem").find((entry) => within(entry).queryByText(url));
  if (!item) throw new Error(`Expected the intake result for ${url}.`);
  return within(item);
}

describe("PendingDiscoveryIntake", () => {
  it("keeps each mixed-list result and queues only newly added postings without checking them", async () => {
    const posted: FoundPostingCreate[] = [];
    const queued: unknown[] = [];
    const first = "https://jobs.example.test/first";
    const last = "https://jobs.example.test/last";
    const duplicate = "https://jobs.example.test/already-staged";
    const unsupported = "https://unsupported.example.test/posting";
    const failed = "https://jobs.example.test/failing";
    server.use(
      http.post(INTAKE_ROUTE, async ({ request }) => {
        const body = (await request.json()) as FoundPostingCreate;
        posted.push(body);
        if (body.url === duplicate) {
          return HttpResponse.json({ detail: "This posting is already staged." }, { status: 409 });
        }
        if (body.url === unsupported) {
          return HttpResponse.json(
            { detail: "This host is not allowed by policy." },
            { status: 422 },
          );
        }
        if (body.url === failed) {
          return HttpResponse.json({ detail: "Staging is unavailable." }, { status: 503 });
        }
        return HttpResponse.json({
          outcome: "created",
          pending_discovery: pendingDiscoveryRow({
            id: body.url === first ? "p1" : "p2",
            title: "",
          }),
          posting_check: null,
        } satisfies FoundPostingAddResponse);
      }),
      http.post("*/api/v1/tasks", async ({ request }) => {
        queued.push(await request.json());
        return HttpResponse.json(taskRecord({ id: "read-1", status: "queued" }));
      }),
      http.get("*/api/v1/tasks/read-1", () =>
        HttpResponse.json(taskRecord({ id: "read-1", status: "queued" })),
      ),
    );
    const onAdded = renderIntake();
    submitLinks([first, "not a URL", duplicate, unsupported, failed, first, last].join("\n"));

    await screen.findByText(
      "Processed 7 links: 2 added, 2 duplicates, 2 invalid or unsupported, 1 failed.",
    );
    expect(intakeResult(first).getByText(/Free posting read queued/)).toBeTruthy();
    expect(intakeResult(last).getByText(/Free posting read queued/)).toBeTruthy();
    expect(intakeResult(duplicate).getByText(/already staged/)).toBeTruthy();
    expect(intakeResult(unsupported).getByText(/not allowed by policy/)).toBeTruthy();
    expect(intakeResult(failed).getByText(/Staging is unavailable/)).toBeTruthy();
    expect(
      intakeResult("not a URL").getByText(/Use a complete http or https posting link/),
    ).toBeTruthy();
    expect(posted.map((body) => body.url)).toEqual([first, duplicate, unsupported, failed, last]);
    expect(posted.every((body) => body.check_publisher_facts === false && body.title === "")).toBe(
      true,
    );
    expect(queued).toEqual([
      {
        workspace_id: "workspace-1",
        task_type: "discovery.refresh_batch",
        payload: { pending_discovery_ids: ["p1", "p2"] },
      },
    ]);
    expect(onAdded.mock.calls).toEqual([["p1"], ["p2"]]);
    expect(screen.getByLabelText<HTMLTextAreaElement>("Posting link").value).toBe(
      ["not a URL", unsupported, failed].join("\n"),
    );
  });

  it("accepts another submission while an earlier read remains queued and preserves its result", async () => {
    const posted: string[] = [];
    let tasks = 0;
    server.use(
      http.post(INTAKE_ROUTE, async ({ request }) => {
        const body = (await request.json()) as FoundPostingCreate;
        posted.push(body.url);
        return HttpResponse.json({
          outcome: "created",
          pending_discovery: pendingDiscoveryRow({ id: `p${posted.length}`, title: "" }),
        } satisfies FoundPostingAddResponse);
      }),
      http.post("*/api/v1/tasks", () =>
        HttpResponse.json(taskRecord({ id: `read-${++tasks}`, status: "queued" })),
      ),
      http.get("*/api/v1/tasks/:taskId", ({ params }) =>
        HttpResponse.json(taskRecord({ id: String(params.taskId), status: "queued" })),
      ),
    );
    renderIntake();
    const first = "https://jobs.example.test/first";
    const second = "https://jobs.example.test/second";
    submitLinks(first);
    await screen.findByText(
      "Processed 1 link: 1 added, 0 duplicates, 0 invalid or unsupported, 0 failed.",
    );

    fireEvent.change(screen.getByLabelText("Posting link"), { target: { value: second } });
    expect(screen.getByRole("button", { name: "Add posting" }).hasAttribute("disabled")).toBe(
      false,
    );
    fireEvent.click(screen.getByRole("button", { name: "Add posting" }));
    await waitFor(() => expect(tasks).toBe(2));
    expect(intakeResult(first).getByText(/Free posting read queued/)).toBeTruthy();
    expect(intakeResult(second).getByText(/Free posting read queued/)).toBeTruthy();

    submitLinks(first);
    await screen.findByText(
      "Processed 1 link: 0 added, 1 duplicate, 0 invalid or unsupported, 0 failed.",
    );
    expect(posted).toEqual([first, second]);
    expect(tasks).toBe(2);
  });

  it("keeps a saved posting distinct from a failed read enqueue", async () => {
    server.use(
      http.post(INTAKE_ROUTE, () =>
        HttpResponse.json({
          outcome: "created",
          pending_discovery: pendingDiscoveryRow({ id: "saved", title: "" }),
        } satisfies FoundPostingAddResponse),
      ),
      http.post("*/api/v1/tasks", () =>
        HttpResponse.json({ detail: "Read queue unavailable." }, { status: 503 }),
      ),
    );
    const onAdded = renderIntake();
    const url = "https://jobs.example.test/saved";
    submitLinks(url);

    expect(
      await screen.findByText(/Saved, but its read could not be queued: Read queue unavailable/),
    ).toBeTruthy();
    expect(intakeResult(url).getByText("Added")).toBeTruthy();
    expect(onAdded.mock.calls).toEqual([["saved"]]);
    expect(screen.getByLabelText<HTMLTextAreaElement>("Posting link").value).toBe("");
    expect(screen.queryByText(/Free posting read queued/)).toBeNull();
  });

  it("leaves a retired duplicate untouched without a publisher check or read", async () => {
    const requests: string[] = [];
    server.use(
      http.post(INTAKE_ROUTE, async ({ request }) => {
        const body = (await request.json()) as FoundPostingCreate;
        requests.push(body.url);
        expect(body.check_publisher_facts).toBe(false);
        return HttpResponse.json(
          { detail: "This posting was dismissed; restore it explicitly." },
          { status: 409 },
        );
      }),
    );
    const onAdded = renderIntake();
    submitLinks("https://jobs.example.test/retired");

    expect(await screen.findByText(/dismissed; restore it explicitly/)).toBeTruthy();
    expect(requests).toEqual(["https://jobs.example.test/retired"]);
    expect(onAdded).not.toHaveBeenCalled();
    expect(screen.getByLabelText<HTMLTextAreaElement>("Posting link").value).toBe("");
  });

  it("keeps the optional title and publisher check for a single link", async () => {
    const posted: FoundPostingCreate[] = [];
    server.use(
      http.post(`${INTAKE_ROUTE}/check`, () => HttpResponse.json(foundPostingCheck())),
      http.post(INTAKE_ROUTE, async ({ request }) => {
        posted.push((await request.json()) as FoundPostingCreate);
        return HttpResponse.json({
          outcome: "requeued",
          pending_discovery: pendingDiscoveryRow({ id: "returned", title: "Product Manager" }),
          posting_check: foundPostingCheck(),
        } satisfies FoundPostingAddResponse);
      }),
      http.post("*/api/v1/tasks", () => HttpResponse.json(taskRecord({ status: "queued" }))),
      http.get("*/api/v1/tasks/task-1", () => HttpResponse.json(taskRecord({ status: "queued" }))),
    );
    renderIntake();
    const url = "https://jobs.example.test/single";
    fireEvent.change(screen.getByLabelText("Posting link"), { target: { value: url } });
    fireEvent.change(screen.getByLabelText("Title, if you know it"), {
      target: { value: "Known title" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Check posting" }));
    await screen.findByText("Publisher details found");
    fireEvent.click(screen.getByRole("button", { name: "Add posting" }));

    await screen.findByText(/Returned Product Manager at Globex/);
    expect(posted).toEqual([
      { url, title: "Known title", employer: "", location: "", check_publisher_facts: true },
    ]);
    expect(screen.getByLabelText<HTMLInputElement>("Title, if you know it").value).toBe("");
  });

  it("queues every link in contract-sized batches without truncating the list", async () => {
    const queued: string[][] = [];
    let finish: () => void = () => undefined;
    const finished = new Promise<void>((resolve) => {
      finish = resolve;
    });
    server.use(
      http.post(INTAKE_ROUTE, async ({ request }) => {
        const body = (await request.json()) as FoundPostingCreate;
        return HttpResponse.json({
          outcome: "created",
          pending_discovery: pendingDiscoveryRow({
            id: new URL(body.url).pathname.slice(1),
            title: "",
          }),
        } satisfies FoundPostingAddResponse);
      }),
      http.post("*/api/v1/tasks", async ({ request }) => {
        const body = (await request.json()) as { payload: { pending_discovery_ids: string[] } };
        queued.push(body.payload.pending_discovery_ids);
        if (queued.length === 2) finish();
        return HttpResponse.json(taskRecord({ id: `read-${queued.length}`, status: "queued" }));
      }),
      http.get("*/api/v1/tasks/:taskId", ({ params }) =>
        HttpResponse.json(taskRecord({ id: String(params.taskId), status: "queued" })),
      ),
    );
    const onAdded = renderIntake();
    await act(async () => {
      submitLinks(
        Array.from({ length: 251 }, (_, index) => `https://jobs.example.test/p${index}`).join("\n"),
      );
      await finished;
    });

    expect(queued.map((batch) => batch.length)).toEqual([250, 1]);
    expect(new Set(queued.flat()).size).toBe(251);
    expect(onAdded).toHaveBeenCalledTimes(251);
    expect(
      await screen.findByText(
        "Processed 251 links: 251 added, 0 duplicates, 0 invalid or unsupported, 0 failed.",
      ),
    ).toBeTruthy();
  });
});
