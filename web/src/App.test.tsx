import { act, render, screen } from "@testing-library/react";
import { HttpResponse, delay, http, type HttpHandler } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Health, Provider, Task, Workspace } from "./api/client/types";
import { App, RouteFallback } from "./App";
import { AppProviders } from "./providers";
import { server } from "./test/msw/server";
import { TASK_TYPE_CATALOG } from "./test/taskTypes";

const API = "*/api/v1";
const WORKSPACE = `${API}/workspaces/:workspaceId`;

const READY_WORKSPACE = {
  id: "workspace-ready",
  slug: "ready",
  name: "Ready",
  root_path: null,
  provider: "openai",
  created_at: "2026-08-05T12:00:00Z",
} satisfies Workspace;

const UNCONFIGURED_PROVIDERS = [
  {
    provider: "anthropic",
    key_present: false,
    key_source: null,
    stored_in_keyring: false,
    environment_variable: "ANTHROPIC_API_KEY",
    tiers: [],
  },
  {
    provider: "openai",
    key_present: false,
    key_source: null,
    stored_in_keyring: false,
    environment_variable: "OPENAI_API_KEY",
    tiers: [],
  },
] satisfies Provider[];

function health(workerAvailable: boolean): Health {
  return {
    status: "ok",
    version: "0.1.0",
    database_exists: true,
    database_reachable: true,
    schema_current: true,
    current_revision: "0001",
    head_revision: "0001",
    worker_available: workerAvailable,
  };
}

interface ShellReads {
  workerAvailable?: boolean;
  workspaces?: Workspace[];
  tasks?: Task[];
}

/** What the application shell reads before any route's own queries run. */
function shellHandlers(reads: ShellReads = {}): HttpHandler[] {
  return [
    http.get(`${API}/health`, () => HttpResponse.json(health(reads.workerAvailable ?? true))),
    http.get(`${API}/providers`, () => HttpResponse.json(UNCONFIGURED_PROVIDERS)),
    http.get(`${API}/workspaces`, () => HttpResponse.json(reads.workspaces ?? [])),
    http.get(`${API}/tasks/types`, () => HttpResponse.json(TASK_TYPE_CATALOG)),
    http.get(`${API}/tasks`, () => HttpResponse.json(reads.tasks ?? [])),
    http.get(`${API}/job-search-parameters`, () => HttpResponse.json({ configured: false })),
    http.get(`${WORKSPACE}/master-cv/versions`, () => HttpResponse.json([])),
  ];
}

const queuedRefresh = {
  id: "task-1",
  workspace_id: "workspace-ready",
  task_type: "posting.refresh",
  status: "queued",
  attempt_count: 0,
  job_posting_ids: [],
  pending_discovery_ids: [],
  source_ids: [],
  progress: {},
  cancellation_requested_at: null,
  error_category: null,
  error_message: null,
  created_at: "2026-08-06T12:00:00Z",
  started_at: null,
  updated_at: "2026-08-06T12:00:00Z",
  completed_at: null,
} satisfies Task;

function renderApp(route: string) {
  render(
    <MemoryRouter initialEntries={[route]}>
      <AppProviders>
        <App />
      </AppProviders>
    </MemoryRouter>,
  );
}

function stubMatchMedia() {
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({ matches: false })),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("App onboarding route", () => {
  it("sends an empty installation to workspace-first free setup", async () => {
    stubMatchMedia();
    server.use(...shellHandlers());

    renderApp("/");

    expect(await screen.findByRole("heading", { name: "Create the workspace" })).toBeTruthy();
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
  });

  it("warns when queued work has no background worker", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-ready");
    stubMatchMedia();
    server.use(
      ...shellHandlers({
        workerAvailable: false,
        workspaces: [READY_WORKSPACE],
        tasks: [queuedRefresh],
      }),
    );

    renderApp("/not-a-route");

    const status = await screen.findByRole("status", {
      name: "Background worker is not running",
    });
    expect(status.textContent).toContain("tailorcv worker");
  });

  it("keeps worker availability out of the way when no task is open", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-ready");
    stubMatchMedia();
    server.use(...shellHandlers({ workerAvailable: false, workspaces: [READY_WORKSPACE] }));

    renderApp("/not-a-route");

    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeTruthy();
    expect(screen.queryByRole("status", { name: "Background worker is not running" })).toBeNull();
  });

  it("opens Find while an active scan and its page queries are still pending", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-ready");
    stubMatchMedia();
    const scan = {
      ...queuedRefresh,
      id: "scan-1",
      task_type: "source.scan",
      status: "running",
      attempt_count: 1,
      progress: { current: 3, total: 10 },
    } satisfies Task;
    // Everything the Find page reads for itself stays open, so the page shows its
    // own loading state rather than the router's.
    server.use(
      http.get(`${WORKSPACE}/*`, () => delay("infinite")),
      http.get(`${API}/job-search-parameters/options`, () => delay("infinite")),
      ...shellHandlers({ workspaces: [READY_WORKSPACE], tasks: [scan] }),
    );

    renderApp("/find");

    expect(await screen.findByRole("heading", { name: "Find jobs" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Pending discoveries" })).toBeTruthy();
    expect(screen.getByText("Reading discoveries")).toBeTruthy();
    expect(screen.queryByText("Opening page")).toBeNull();
  });

  it.each(["/workspace", "/job-search"])(
    "does not preserve the prerelease route %s",
    async (route) => {
      window.localStorage.setItem("tailorcv.workspace", "workspace-ready");
      stubMatchMedia();
      server.use(...shellHandlers({ workspaces: [READY_WORKSPACE] }));

      renderApp(route);

      expect(await screen.findByRole("heading", { name: "Page not found" })).toBeTruthy();
    },
  );

  it("carries a short label beside the full label on every primary nav link", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-ready");
    stubMatchMedia();
    server.use(...shellHandlers({ workspaces: [READY_WORKSPACE] }));

    renderApp("/not-a-route");

    await screen.findByRole("heading", { name: "Page not found" });

    const expected: [string, string][] = [
      ["Command Center", "Command"],
      ["Master CV", "CV"],
      ["Find jobs", "Find"],
      ["Tracked postings", "Postings"],
      ["Applications", "Applications"],
      ["Tailored documents", "Tailored"],
      ["Notes, tags, saved views", "Notes"],
      ["Prompts", "Prompts"],
      ["Settings", "Settings"],
    ];
    for (const [label, short] of expected) {
      const link = screen.getByRole("link", { name: label });
      const labels = Array.from(link.querySelectorAll("span")).map((span) => span.textContent);
      expect(labels).toEqual([label, short]);
      // The narrow bar shows the short label, so it has to sit inside the accessible name.
      expect(label.toLowerCase()).toContain(short.toLowerCase());
    }
  });

  it("addresses the admin at the path the server redirects to", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-ready");
    stubMatchMedia();
    server.use(...shellHandlers({ workspaces: [READY_WORKSPACE] }));

    renderApp("/not-a-route");

    await screen.findByRole("heading", { name: "Page not found" });

    const browser = screen.getByRole("link", { name: "Data browser" });

    // The bare path costs a redirect, and jsdom cannot tell an anchor from a
    // NavLink here, so the trailing slash is the part a test can hold.
    expect(browser.getAttribute("href")).toBe("/admin/");
  });
});

describe("RouteFallback", () => {
  it("offers a reload only once the page has been opening a while", () => {
    vi.useFakeTimers();
    try {
      render(<RouteFallback />);
      expect(screen.getByText("Opening page")).toBeTruthy();
      expect(screen.queryByRole("button", { name: "Reload this page" })).toBeNull();
      act(() => {
        vi.advanceTimersByTime(12000);
      });
      expect(screen.getByRole("button", { name: "Reload this page" })).toBeTruthy();
      expect(
        screen.getByText("This page's code is taking longer than usual to load."),
      ).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });
});
