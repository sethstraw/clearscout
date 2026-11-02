import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http, type HttpHandler } from "msw";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import type {
  Provider,
  ScoreWeightSet,
  SearchParameterOptions,
  SearchParametersSaved,
  Workspace,
} from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { searchParametersState } from "../search-parameters/searchParameterFixtures";
import { WelcomePage } from "./Onboarding";

const API = "*/api/v1";

const workspace = {
  id: "workspace-1",
  slug: "my-search",
  name: "My Search",
  root_path: "/Documents/TailorCV/workspaces/my-search",
  provider: "openai",
  created_at: "2026-08-05T12:00:00Z",
} satisfies Workspace;

const providers = [
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

const SEARCH_OPTIONS = {
  arrangements: [],
  levels: [],
  bases: [],
  eligibility: [],
  acceptance: [],
  employment_types: [],
  lanes: [],
  caps: { titles: 8, archetypes: 6, capabilities: 5, domains: 5, cluster_terms: 10 },
} satisfies SearchParameterOptions;

function welcomeReads(
  reads: { providers?: Provider[]; workspaces?: Workspace[]; parametersVersion?: number } = {},
): HttpHandler[] {
  return [
    http.get(`${API}/providers`, () => HttpResponse.json(reads.providers ?? providers)),
    http.get(`${API}/workspaces`, () => HttpResponse.json(reads.workspaces ?? [])),
    http.get(`${API}/workspaces/:workspaceId/master-cv/versions`, () => HttpResponse.json([])),
    http.get(`${API}/tasks`, () => HttpResponse.json([])),
    http.get(`${API}/workspaces/:workspaceId/score-weight-sets`, () =>
      HttpResponse.json([] satisfies ScoreWeightSet[]),
    ),
    http.get(`${API}/job-search-parameters/options`, () => HttpResponse.json(SEARCH_OPTIONS)),
    http.get(`${API}/job-search-parameters`, () =>
      HttpResponse.json(
        searchParametersState({
          version: reads.parametersVersion ?? 0,
          configured: (reads.parametersVersion ?? 0) > 0,
          effective_at: reads.parametersVersion ? "2026-08-01T12:00:00Z" : null,
        }),
      ),
    ),
  ];
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/welcome"]}>
      <AppProviders>
        <QueryClientProvider client={queryClient}>
          <Routes>
            <Route path="/welcome" element={<WelcomePage />} />
            <Route
              path="/find"
              element={
                <main>
                  <h1>Discovery destination</h1>
                  <Link to="/welcome">Return to setup</Link>
                </main>
              }
            />
          </Routes>
        </QueryClientProvider>
      </AppProviders>
    </MemoryRouter>,
  );
}

afterEach(() => window.sessionStorage.clear());

describe("WelcomePage", () => {
  it.each([false, true])(
    "keeps onboarding recovery available after another tab saves (failed read: %s)",
    async (failRead) => {
      window.localStorage.setItem("tailorcv.workspace", workspace.id);
      let version = 0;
      let readFailed = false;
      const attempts: {
        expected_version: number;
        parameters: SearchParametersSaved["parameters"];
      }[] = [];
      server.use(
        http.get(`${API}/job-search-parameters`, () => {
          if (version === 1 && failRead && !readFailed) {
            readFailed = true;
            return HttpResponse.json({ detail: "Read unavailable" }, { status: 503 });
          }
          return HttpResponse.json(
            searchParametersState({ workspace_id: workspace.id, version, configured: version > 0 }),
          );
        }),
        http.put(`${API}/job-search-parameters`, async ({ request }) => {
          const body = (await request.json()) as (typeof attempts)[number];
          attempts.push(body);
          if (version === 0) {
            version = 1;
            return HttpResponse.json(
              { detail: "Search parameters changed in another tab." },
              { status: 409 },
            );
          }
          version = 2;
          return HttpResponse.json({
            id: "parameters-2",
            workspace_id: workspace.id,
            version,
            content_sha256: "synthetic-digest",
            effective_at: "2026-10-08T12:00:00Z",
            supersedes_id: "parameters-1",
            unchanged: false,
            parameters: body.parameters,
          } satisfies SearchParametersSaved);
        }),
        ...welcomeReads({ workspaces: [workspace] }),
      );
      renderPage();
      const titles = await screen.findByLabelText("Target titles");
      fireEvent.change(titles, { target: { value: "Implementation Consultant" } });
      fireEvent.keyDown(titles, { key: "Enter" });
      for (let step = 0; step < 4; step += 1) {
        fireEvent.click(screen.getByRole("button", { name: "No restriction in this section" }));
        fireEvent.click(screen.getByRole("button", { name: "Next" }));
      }
      fireEvent.click(screen.getByRole("button", { name: "Confirm and save preferences" }));
      fireEvent.click(await screen.findByRole("button", { name: "Read latest saved preferences" }));
      if (failRead) {
        fireEvent.click(
          await screen.findByRole("button", { name: "Retry reading latest preferences" }),
        );
      }
      const preserved = await screen.findByRole("region", { name: "Preserved draft" });
      expect(within(preserved).getByText(/Implementation Consultant/)).toBeTruthy();
      expect(screen.queryByText(/My Search: search preferences saved/)).toBeNull();
      expect(attempts).toHaveLength(1);
      fireEvent.click(
        screen.getByRole("button", { name: "Use preserved draft against version 1" }),
      );
      expect(attempts).toHaveLength(1);
      fireEvent.click(screen.getByRole("button", { name: "Confirm and save preferences" }));
      await waitFor(() => expect(attempts).toHaveLength(2));
      expect(attempts[1]?.expected_version).toBe(1);
      expect(attempts[1]?.parameters.seniority?.target_titles).toEqual([
        { title: "Implementation Consultant", lane: null },
      ]);
    },
  );
  it.each([
    { label: "Workspace name", value: "", message: "Enter a workspace name." },
    {
      label: "Short name",
      value: "Invalid Name",
      message: "Use lowercase letters, numbers, and hyphens.",
    },
  ])("associates the $label error and permits correction", async ({ label, value, message }) => {
    let created = 0;
    server.use(
      ...welcomeReads(),
      http.post(`${API}/workspaces`, () => {
        created += 1;
        return HttpResponse.json(workspace);
      }),
    );
    renderPage();
    await screen.findByRole("heading", { name: "Create the workspace" });
    const input = screen.getByLabelText<HTMLInputElement>(label);
    fireEvent.change(input, { target: { value } });
    fireEvent.click(screen.getByRole("button", { name: "Create workspace" }));
    await waitFor(() => expect(input.getAttribute("aria-invalid")).toBe("true"));
    const description = input.getAttribute("aria-describedby");
    expect(description).toBeTruthy();
    expect(document.getElementById(description ?? "")?.textContent).toBe(message);
    expect(created).toBe(0);
    fireEvent.change(input, { target: { value: "corrected" } });
    fireEvent.click(screen.getByRole("button", { name: "Create workspace" }));
    await waitFor(() => expect(created).toBe(1));
  });

  it.each([{ choices: providers }, { choices: [] }])(
    "creates an empty workspace without a provider key (%#)",
    async ({ choices }) => {
      let created: Record<string, unknown> | undefined;
      let workspaces: Workspace[] = [];
      server.use(
        http.get(`${API}/workspaces`, () => HttpResponse.json(workspaces)),
        ...welcomeReads({ providers: choices }),
        http.post(`${API}/workspaces`, async ({ request }) => {
          created = (await request.json()) as Record<string, unknown>;
          workspaces = [workspace];
          return HttpResponse.json(workspace);
        }),
      );
      renderPage();
      expect(await screen.findByRole("heading", { name: "Create the workspace" })).toBeTruthy();
      expect(screen.queryByLabelText(/API key/)).toBeNull();
      expect(screen.getByLabelText<HTMLInputElement>("Workspace name").value).toBe("My Job Search");
      expect(screen.getByLabelText<HTMLInputElement>("Short name").value).toBe("my-job-search");
      fireEvent.click(screen.getByRole("button", { name: "Create workspace" }));
      expect(await screen.findByRole("heading", { name: "Basic search preferences" })).toBeTruthy();
      expect(created).toEqual({ name: "My Job Search", slug: "my-job-search", provider: "openai" });
    },
  );

  it("stores an optional paid provider choice even without its key", async () => {
    let created: Record<string, unknown> | undefined;
    server.use(
      ...welcomeReads(),
      http.post(`${API}/workspaces`, async ({ request }) => {
        created = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ ...workspace, provider: "anthropic" } satisfies Workspace);
      }),
    );
    renderPage();
    await screen.findByRole("heading", { name: "Create the workspace" });
    fireEvent.click(screen.getByText("Provider for paid features"));
    await screen.findByRole("option", { name: "Anthropic" });
    fireEvent.change(screen.getByLabelText("Provider"), { target: { value: "anthropic" } });
    fireEvent.click(screen.getByRole("button", { name: "Create workspace" }));
    await waitFor(() => expect(created?.provider).toBe("anthropic"));
  });

  it("opens the existing search setup without a CV or provider key", async () => {
    window.localStorage.setItem("tailorcv.workspace", workspace.id);
    server.use(...welcomeReads({ workspaces: [workspace] }));
    renderPage();
    expect(await screen.findByRole("heading", { name: "Basic search preferences" })).toBeTruthy();
    expect(await screen.findByLabelText("Target titles")).toBeTruthy();
    expect(screen.getByRole("listitem", { current: "step" }).textContent).toContain("Search setup");
    expect(screen.queryByRole("heading", { name: "Import the evidence source" })).toBeNull();
  });

  it("keeps an unsaved search draft when skipping to discovery and returning", async () => {
    window.localStorage.setItem("tailorcv.workspace", workspace.id);
    server.use(...welcomeReads({ workspaces: [workspace] }));
    renderPage();
    const titles = await screen.findByLabelText("Target titles");
    fireEvent.change(titles, {
      target: { value: "Implementation Consultant" },
    });
    fireEvent.keyDown(titles, { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    expect(await screen.findByRole("heading", { name: "Discovery destination" })).toBeTruthy();
    fireEvent.click(screen.getByRole("link", { name: "Return to setup" }));
    fireEvent.click(await screen.findByRole("button", { name: "Resume basic walkthrough" }));
    expect(await screen.findByText("Implementation Consultant")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Next" }).hasAttribute("disabled")).toBe(true);
    expect(
      screen.getByRole("link", { name: "Review search setup in Settings" }).getAttribute("href"),
    ).toBe("/settings?tab=search");
  });

  it("explains the optional paid requirements and links their existing routes", async () => {
    window.localStorage.setItem("tailorcv.workspace", workspace.id);
    server.use(...welcomeReads({ workspaces: [workspace] }));
    renderPage();
    await screen.findByRole("heading", { name: "Basic search preferences" });
    expect(screen.getByText(/need a provider key and master CV/)).toBeTruthy();
    expect(screen.getByText(/Manual search setup, posting-link intake/)).toBeTruthy();
    expect(
      screen
        .getByRole("link", { name: "Configure provider access in Settings" })
        .getAttribute("href"),
    ).toBe("/settings?tab=provider");
    expect(screen.getByRole("link", { name: "Import a master CV" }).getAttribute("href")).toBe(
      "/cv",
    );
  });

  it("offers free discovery after search parameters are saved without a CV or key", async () => {
    window.localStorage.setItem("tailorcv.workspace", workspace.id);
    server.use(...welcomeReads({ workspaces: [workspace], parametersVersion: 1 }));
    renderPage();
    expect(
      await screen.findByRole("heading", { name: "My Search: search preferences saved" }),
    ).toBeTruthy();
    expect(screen.getByRole("listitem", { current: "step" }).textContent).toContain(
      "Posting links",
    );
    expect(screen.getByRole("link", { name: "Find jobs" }).getAttribute("href")).toBe(
      "/find?tab=pending",
    );
  });
});
