import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./client";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("API client pagination", () => {
  it("reads every jobs page for complete-board consumers", async () => {
    const first = Array.from({ length: 250 }, (_, index) => ({ job_posting_id: `job-${index}` }));
    const fetch = vi.fn((url: string) =>
      Promise.resolve(
        new Response(
          JSON.stringify(
            url.includes("offset=0")
              ? { items: first, total: 251, offset: 0, limit: 250 }
              : { items: [{ job_posting_id: "job-250" }], total: 251, offset: 250, limit: 250 },
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    vi.stubGlobal("fetch", fetch);
    const jobs = await api.allJobs("workspace-1");
    expect(jobs).toHaveLength(251);
    expect(fetch.mock.calls.map((call) => call[0])).toEqual([
      "/api/v1/jobs?workspace_id=workspace-1&offset=0&limit=250&sort=recorded_score&descending=true",
      "/api/v1/jobs?workspace_id=workspace-1&offset=250&limit=250&sort=recorded_score&descending=true",
    ]);
  });
});

describe("API client idempotency", () => {
  it("shares one intent across a double application-event activation", async () => {
    const fetch = vi
      .fn<(input: string, init?: RequestInit) => Promise<Response>>()
      .mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ id: "event-1" }))));
    vi.stubGlobal("fetch", fetch);
    vi.spyOn(crypto, "randomUUID").mockReturnValue("00000000-0000-4000-8000-000000000001");

    await Promise.all([
      api.appendApplicationEvent("job-1", "workspace-1", {
        status: "applied",
        notes: "Sent",
        occurred_at: "first",
      }),
      api.appendApplicationEvent("job-1", "workspace-1", {
        status: "applied",
        notes: "Sent",
        occurred_at: "second",
      }),
    ]);

    expect(
      fetch.mock.calls.map((call) => new Headers(call[1]?.headers).get("Idempotency-Key")),
    ).toEqual(Array(2).fill("00000000-0000-4000-8000-000000000001"));
    expect(fetch.mock.calls.map((call) => call[1]?.body)).toEqual(
      Array(2).fill(JSON.stringify({ status: "applied", notes: "Sent", occurred_at: "first" })),
    );
  });

  it("separates task intents with different payloads", async () => {
    const fetch = vi
      .fn<(input: string, init?: RequestInit) => Promise<Response>>()
      .mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ id: "task-1" }))));
    vi.stubGlobal("fetch", fetch);
    vi.spyOn(crypto, "randomUUID")
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000002")
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000003");

    await Promise.all([
      api.enqueueTask({
        workspace_id: "workspace-1",
        task_type: "posting.refresh",
        payload: { job_posting_id: "job-1" },
      }),
      api.enqueueTask({
        workspace_id: "workspace-1",
        task_type: "posting.refresh",
        payload: { job_posting_id: "job-2" },
      }),
    ]);

    expect(
      fetch.mock.calls.map((call) => new Headers(call[1]?.headers).get("Idempotency-Key")),
    ).toEqual(["00000000-0000-4000-8000-000000000002", "00000000-0000-4000-8000-000000000003"]);
  });
});

describe("enqueuing a paid task", () => {
  it("sends the workspace, the type, and the payload, and nothing about money", async () => {
    const fetch = vi.fn(() => Promise.resolve(new Response(JSON.stringify({ id: "task-1" }))));
    vi.stubGlobal("fetch", fetch);
    await api.enqueueTask({
      workspace_id: "workspace-1",
      task_type: "search.guided_campaign",
      payload: {},
    });
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/tasks",
      expect.objectContaining({
        body: JSON.stringify({
          workspace_id: "workspace-1",
          task_type: "search.guided_campaign",
          payload: {},
        }),
      }),
    );
  });
});
