import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http, type HttpHandler } from "msw";
import { BrowserRouter, MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  GuidedCampaignPreview,
  RunCostHistory,
  SearchParameterOptions,
  SourceList,
  SourceScanHistory,
  SourceScanOutcomePage,
} from "../api/client/types";
import { AppProviders, useWorkspace } from "../providers";
import {
  pendingDiscoveryRow,
  pendingDiscoveryPage,
} from "../features/job-search/pendingDiscoveryFixtures";
import {
  boardSource,
  screenedOutTitlePage,
  sourceList,
  sourceScan,
  sourceScanHistory,
  sourceScanOutcomePage,
} from "../features/job-search/sourceScanFixtures";
import { spendSummary } from "../features/settings/settingsHandlers";
import { server } from "../test/msw/server";
import { evaluationPreflightHandler } from "../test/taskTypes";
import { pendingQueueHandlers, type PendingQueueReads } from "../test/msw/pendingDiscoveryHandlers";
import { JobSearchPage } from "./JobSearchPage";

const API = "*/api/v1";
const WORKSPACE = `${API}/workspaces/:workspaceId`;

const SEARCH_OPTIONS = {
  arrangements: ["preferred", "acceptable", "refused"],
  levels: ["junior", "mid", "senior"],
  bases: ["annual"],
  eligibility: ["authorized"],
  acceptance: ["preferred", "acceptable", "refused"],
  employment_types: [],
  lanes: [],
  caps: { titles: 8, archetypes: 6, capabilities: 5, domains: 5, cluster_terms: 10 },
} satisfies SearchParameterOptions;

const PROVIDER_CHOICE = {
  provider: "openai",
  model_choice: { model: "gpt-5.6-terra", effort: "low", service_tier: "flex" },
};

const EMPTY_CAMPAIGN_PREVIEW = {
  manifest_sha256: "a".repeat(64),
  cycle: 1,
  compiler_version: "gap-campaign-v1",
  bounds: {
    max_model_calls: 8,
    max_hosted_searches: 40,
    max_output_tokens: 16000,
    searches_per_attempt: 5,
  },
  branches: [],
  attempts: [],
  provider: "openai",
  model: "gpt-5.6-terra",
  supersedes_campaign_id: null,
  planned_search_fee_cents: 0,
  estimated_cost_cents: null,
  estimate_basis: "provider_model",
} satisfies GuidedCampaignPreview;

interface FindPageReads extends PendingQueueReads {
  outcomes?: SourceScanOutcomePage;
  onOutcomeRequest?: (url: URL) => void;
  scans?: SourceScanHistory;
  sources?: SourceList;
  costHistory?: RunCostHistory[];
  onEnqueue?: (body: Record<string, unknown>) => Promise<void> | void;
}

/** Every read the three Find tabs make, on top of the shared pending-queue set. */
function findPageHandlers(reads: FindPageReads = {}): HttpHandler[] {
  return [
    evaluationPreflightHandler(),
    http.get(`${WORKSPACE}/source-scan-outcomes`, ({ request }) => {
      reads.onOutcomeRequest?.(new URL(request.url));
      return HttpResponse.json(reads.outcomes ?? sourceScanOutcomePage({ items: [] }));
    }),
    http.get(`${WORKSPACE}/source-scans`, () =>
      HttpResponse.json(reads.scans ?? sourceScanHistory([])),
    ),
    http.get(`${WORKSPACE}/sources`, () => HttpResponse.json(reads.sources ?? { items: [] })),
    http.get(`${WORKSPACE}/scan-schedule`, ({ params }) =>
      HttpResponse.json({
        workspace_id: params.workspaceId,
        state: null,
        unavailable_source_ids: [],
      }),
    ),
    http.get(`${WORKSPACE}/screened-out-titles`, () => HttpResponse.json(screenedOutTitlePage([]))),
    http.get(`${WORKSPACE}/refusal-pattern-proposals`, () => HttpResponse.json([])),
    http.get(`${WORKSPACE}/guided-campaign-configuration`, () =>
      HttpResponse.json(PROVIDER_CHOICE),
    ),
    http.get(`${WORKSPACE}/search-vocabulary-configuration`, () =>
      HttpResponse.json(PROVIDER_CHOICE),
    ),
    http.get(`${WORKSPACE}/pending-dispositions/duplicate-candidates`, () => HttpResponse.json([])),
    http.get(`${WORKSPACE}/spend-history`, () => HttpResponse.json(reads.costHistory ?? [])),
    http.get(`${WORKSPACE}/spend`, () => HttpResponse.json(spendSummary())),
    http.get(`${API}/guided-campaign/preview`, () => HttpResponse.json(EMPTY_CAMPAIGN_PREVIEW)),
    http.get(`${API}/guided-campaigns/latest`, () => HttpResponse.json(null)),
    ...["title", "archetype", "cluster"].map((kind) =>
      http.get(`${WORKSPACE}/search-${kind}-proposals`, () =>
        HttpResponse.json({ capacities: [], items: [] }),
      ),
    ),
    http.get(`${API}/job-search-parameters/options`, () => HttpResponse.json(SEARCH_OPTIONS)),
    http.post(`${API}/tasks`, async ({ request }) => {
      await reads.onEnqueue?.((await request.json()) as Record<string, unknown>);
      return HttpResponse.json({ id: crypto.randomUUID() });
    }),
    ...pendingQueueHandlers(reads),
  ];
}

function useFindPage(reads: FindPageReads = {}) {
  server.use(...findPageHandlers(reads));
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
  window.history.replaceState({}, "", "/");
});

describe("JobSearchPage", () => {
  it("starts pending-queue paging fresh after the active workspace changes", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-one");
    const pageRequests: URL[] = [];
    useFindPage({
      onPageRequest: (url) => {
        pageRequests.push(url);
        const firstWorkspace = url.pathname.includes("/workspace-one/");
        const offset = Number(url.searchParams.get("ready_offset") ?? 0);
        return HttpResponse.json(
          pendingDiscoveryPage({
            snapshot_sha256: (firstWorkspace ? "a" : "b").repeat(64),
            snapshot_before: "2026-09-12T00:00:00Z",
            ready: {
              total: firstWorkspace ? 100 : 0,
              offset,
              has_more: firstWorkspace && offset === 0,
            },
          }),
        );
      },
    });

    function WorkspaceSwitchingSearch() {
      const { setWorkspaceId } = useWorkspace();
      return (
        <>
          <button onClick={() => setWorkspaceId("workspace-two")} type="button">
            Switch workspace
          </button>
          <JobSearchPage />
        </>
      );
    }

    render(
      <AppProviders>
        <MemoryRouter>
          <WorkspaceSwitchingSearch />
        </MemoryRouter>
      </AppProviders>,
    );

    await waitFor(() =>
      expect(
        screen.getByRole<HTMLButtonElement>("button", { name: "Next ready page" }).disabled,
      ).toBe(false),
    );
    fireEvent.click(screen.getByRole("button", { name: "Next ready page" }));
    await waitFor(() =>
      expect(
        pageRequests.some(
          (url) =>
            url.pathname.includes("/workspace-one/") &&
            url.searchParams.get("ready_offset") === "50",
        ),
      ).toBe(true),
    );
    fireEvent.click(screen.getByRole("button", { name: "Switch workspace" }));

    await waitFor(() => {
      const workspaceTwoPage = pageRequests.find((url) => url.pathname.includes("/workspace-two/"));
      expect(workspaceTwoPage?.searchParams.get("ready_offset")).toBe("0");
      expect(workspaceTwoPage?.searchParams.has("expected_snapshot_sha256")).toBe(false);
    });
    expect(screen.queryByText(/queue order changed/)).toBeNull();
  });

  it("persists source changes and clears the URL filter across router remounts", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-search");
    const requested: URL[] = [];
    useFindPage({
      outcomes: sourceScanOutcomePage({ items: [], total: 0, sources: ["Acme & Partners"] }),
      onOutcomeRequest: (url) => requested.push(url),
    });
    window.history.replaceState(
      {},
      "",
      "/find?tab=boards&scan=task-1&source=Missing%20%26%20Board&keep=unchanged",
    );
    const mount = () =>
      render(
        <AppProviders>
          <BrowserRouter>
            <JobSearchPage />
          </BrowserRouter>
        </AppProviders>,
      );
    let view = mount();
    const filters = await screen.findByRole("region", { name: "Screened candidate filters" });
    const source = within(filters).getByRole("combobox", { name: "Source" });
    expect(source).toHaveProperty("value", "Missing & Board");
    fireEvent.change(source, { target: { value: "Acme & Partners" } });
    await waitFor(() =>
      expect(new URLSearchParams(window.location.search).get("source")).toBe("Acme & Partners"),
    );
    view.unmount();
    view = mount();
    expect(await screen.findByRole("combobox", { name: /^Source$/ })).toHaveProperty(
      "value",
      "Acme & Partners",
    );
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    await waitFor(() =>
      expect(new URLSearchParams(window.location.search).has("source")).toBe(false),
    );
    expect(new URLSearchParams(window.location.search).get("tab")).toBe("boards");
    expect(new URLSearchParams(window.location.search).get("scan")).toBe("task-1");
    expect(new URLSearchParams(window.location.search).get("keep")).toBe("unchanged");
    view.unmount();
    mount();
    expect(await screen.findByRole("combobox", { name: /^Source$/ })).toHaveProperty("value", "");
    await waitFor(() => expect(requested.at(-1)?.searchParams.has("source")).toBe(false));
  });

  it("keeps recurring search actions separate from setup", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-search");
    useFindPage({
      sources: sourceList([
        boardSource({
          name: "Example Cooperative",
          terms: { condition: "Published synthetic fixture.", url: "" },
        }),
      ]),
    });

    render(
      <AppProviders>
        <MemoryRouter>
          <JobSearchPage />
        </MemoryRouter>
      </AppProviders>,
    );

    expect(await screen.findByText("Find jobs")).toBeTruthy();
    expect(screen.queryByText("Search setup")).toBeNull();
    expect(
      screen.getByRole("link", { name: "Review search preferences" }).getAttribute("href"),
    ).toBe("/settings?tab=search");
    expect(screen.getByRole("tab", { name: "Review", selected: true })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Pending discoveries" })).toBeTruthy();
    expect(screen.getByRole("navigation", { name: "Your application path" })).toBeTruthy();
    for (const name of ["My background", "My search", "My shortlist", "My application"]) {
      expect(screen.getByRole("link", { name })).toBeTruthy();
    }
    fireEvent.click(screen.getByRole("tab", { name: "Boards" }));
    await waitFor(() =>
      expect(screen.getByRole<HTMLButtonElement>("button", { name: "Scan now" }).disabled).toBe(
        false,
      ),
    );
    expect(screen.getByRole("heading", { name: "Scan your boards" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "What a scan can read" })).toBeTruthy();
  });

  it("marks the tour targets and opens the help for the selected tab", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-search");
    useFindPage({ sources: sourceList([boardSource()]) });

    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/find?tab=boards"]}>
          <JobSearchPage />
        </MemoryRouter>
      </AppProviders>,
    );

    const scan = await screen.findByRole("button", { name: "Scan now" });
    expect(scan.closest('[data-tour="find-scan"]')).not.toBeNull();
    expect(
      screen
        .getByRole("tablist", { name: "Find jobs sections" })
        .closest('[data-tour="find-tabs"]'),
    ).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Help: Boards" }));
    expect(screen.getByRole("heading", { name: "Boards" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Take the tour" })).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Web search" }));
    expect(await screen.findByRole("button", { name: "Help: Web search" })).toBeTruthy();
  });

  it("renders the Find new jobs panels without a duplicate-key warning", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-search");
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    useFindPage();
    try {
      render(
        <AppProviders>
          <MemoryRouter initialEntries={["/find?tab=web"]}>
            <JobSearchPage />
          </MemoryRouter>
        </AppProviders>,
      );

      expect(await screen.findByRole("heading", { name: "Search the web" })).toBeTruthy();
      expect(consoleError.mock.calls.map(String).join(" ")).not.toContain("same key");
    } finally {
      consoleError.mockRestore();
    }
  });

  it("keeps board sources up front and scan history under a closed History", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-search");
    useFindPage({
      scans: sourceScanHistory([
        sourceScan({
          id: "scan-1",
          source_name: "Example careers",
          employer: "Example",
          source_url: "https://boards.example.test/jobs",
          scanned_on: "2026-08-13",
          query_label: "title-filter",
          raw_count: 10,
          read_count: 9,
          result_count: 8,
          new_count: 2,
          scan_started_at: "2026-08-13T00:00:00Z",
          scan_completed_at: "2026-08-13T00:00:00Z",
          duration_ms: 0,
        }),
      ]),
    });

    render(
      <AppProviders>
        <MemoryRouter>
          <JobSearchPage />
        </MemoryRouter>
      </AppProviders>,
    );

    expect(screen.queryByRole("tab", { name: "Screened out" })).toBeNull();
    fireEvent.click(await screen.findByRole("tab", { name: "Boards" }));
    const boards = await screen.findByRole("heading", { name: "Your boards" });
    expect(boards.closest("details")).toBeNull();
    const history = (await screen.findByText("Board scan history")).closest("details")!;
    expect(history.open).toBe(false);
    expect(within(history).getByText("History")).toBeTruthy();
    expect(screen.getByText("Example careers")).toBeTruthy();
    expect(screen.getByText(/10 listed \/ 9 read \/ 8 kept \/ 2 new/)).toBeTruthy();
  });

  it("shows the screened-out summary on the Boards tab and scrolls a deep-linked ledger into view", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-search");
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    useFindPage({
      outcomes: sourceScanOutcomePage({ items: [], total: 0, sources: [] }),
      scans: {
        ...sourceScanHistory([
          sourceScan({
            id: "acme-latest",
            task_id: "task-1",
            source_name: "Acme Robotics",
            screened_out_count: 3,
          }),
        ]),
        limit: 250,
      },
    });
    window.history.replaceState({}, "", "/find?tab=boards&scan=task-1#screened-candidates");

    render(
      <AppProviders>
        <BrowserRouter>
          <JobSearchPage />
        </BrowserRouter>
      </AppProviders>,
    );

    expect(
      await screen.findByText("Screened out in latest scans (3 listings from 1 source)"),
    ).toBeTruthy();
    const ledger = await screen.findByRole("heading", { name: "Screened candidates" });
    expect(ledger.closest("details")!.open).toBe(true);
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("tab", { name: "Review" }));
    expect(screen.queryByText(/Screened out in latest scans/)).toBeNull();
  });

  it("states evaluation cost and evaluates the whole selection as one batch", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-evaluate");
    const pending = Array.from({ length: 11 }, (_, index) =>
      pendingDiscoveryRow({
        id: `pending-${index + 1}`,
        employer: `Employer ${index + 1}`,
        title: `Role ${index + 1}`,
        location: "Canada",
        discovery_url: `https://example.test/jobs/${index + 1}`,
        canonical_url: `https://example.test/jobs/${index + 1}`,
        retained_content_id: `content-${index + 1}`,
        current_content_id: `content-${index + 1}`,
        reason: "guided search verified this posting",
        first_seen_on: "2026-08-10",
        rank: index === 0 ? 2 : 0,
        ranked_because: index === 0 ? ["matches data-platform work"] : [],
        matched_target_title: index === 0 ? "Data Platform Lead" : null,
        recommendation: index === 0 ? "review" : "investigate",
        recommendation_because:
          index === 0
            ? "The staged title and location still align with the current settings."
            : "The staged location does not resolve to a current saved place.",
        arrangement_categories: ["unknown"],
        arrangement_categories_source: "missing",
        also_in: index === 0 ? ["Montreal", "Vancouver"] : [],
      }),
    );
    const enqueued: Record<string, unknown>[] = [];
    useFindPage({
      page: pending,
      costHistory: [
        {
          task_type: "discovery.evaluate_batch",
          currency: "USD",
          unit: "posting",
          measured_runs: 3,
          unpriced_runs: 0,
          replayed_runs: 0,
          per_unit_low: "0.08",
          per_unit_median: "0.12",
          per_unit_high: "0.20",
        },
      ],
      onEnqueue: (body) => {
        enqueued.push(body);
      },
    });

    render(
      <AppProviders>
        <MemoryRouter>
          <JobSearchPage />
        </MemoryRouter>
      </AppProviders>,
    );

    await screen.findByRole("checkbox", { name: "Select Role 1 at Employer 1" });
    expect(screen.queryByRole("button", { name: /^Full evaluation \(\d+\)$/ })).toBeNull();
    expect(screen.getAllByText("First seen 2026-08-10")).toHaveLength(11);
    expect(screen.getByText("Rank 2")).toBeTruthy();
    expect(screen.queryByText("Also advertised in Montreal, Vancouver.")).toBeNull();
    const detailsButtons = screen.getAllByRole("button", { name: "Details" });
    expect(detailsButtons).toHaveLength(11);
    fireEvent.click(detailsButtons[0]!);
    const details = document.getElementById(detailsButtons[0]!.getAttribute("aria-controls") ?? "");
    expect(details).not.toBeNull();
    const panel = within(details!);
    expect(panel.getByText("Also advertised in Montreal, Vancouver.")).toBeTruthy();
    expect(panel.getByText("Ranked because: matches data-platform work.")).toBeTruthy();
    expect(panel.getByText("Matched target title: Data Platform Lead.")).toBeTruthy();
    expect(
      screen.getAllByText("The staged title and location still align with the current settings.")
        .length,
    ).toBeGreaterThan(0);
    expect(panel.getAllByRole("link", { name: "Review job posting" })).toHaveLength(1);
    for (let index = 1; index <= 11; index += 1) {
      const checkbox =
        index === 1
          ? await screen.findByRole("checkbox", {
              name: "Select Role 1 at Employer 1",
            })
          : screen.getByRole("checkbox", {
              name: `Select Role ${index} at Employer ${index}`,
            });
      fireEvent.click(checkbox);
    }
    // The whole selection is enabled and runs as one batch.
    const runButton = screen.getByRole<HTMLButtonElement>("button", {
      name: "Full evaluation (11)",
    });
    expect(runButton.disabled).toBe(false);
    expect(runButton.className).toContain("button--paid");
    const described = runButton.getAttribute("aria-describedby") ?? "";
    expect(document.getElementById(described)?.textContent).toContain(
      "Past runs: about $1.32 total",
    );
    fireEvent.click(runButton);
    expect(
      within(await screen.findByRole("dialog")).getByText(
        /Estimated total at configured rates: \$0.2000 USD/,
      ),
    ).toBeTruthy();
    expect(enqueued).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Run 11 full evaluations" }));

    await waitFor(() => {
      expect(enqueued).toHaveLength(1);
      expect(enqueued[0]).toMatchObject({ task_type: "discovery.evaluate_batch" });
      const payload = enqueued[0]?.payload as { pending_discovery_ids: string[] };
      expect(payload.pending_discovery_ids).toHaveLength(11);
    });
  });
});
