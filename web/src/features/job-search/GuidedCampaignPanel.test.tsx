import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  GuidedCampaignConfiguration,
  GuidedCampaignPreview,
  GuidedCampaignReport,
  PendingDiscovery,
  RunCostHistory,
} from "../../api/client";
import type {
  Health,
  SourceScanOutcomePage,
  SpendSummary,
  Task,
  TaskBilling,
} from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { requestLog } from "../../test/requestLog";
import { TASK_TYPE_CATALOG } from "../../test/taskTypes";
import { GuidedCampaignPanel } from "./GuidedCampaignPanel";
import { pendingDiscoveryRow, taskRecord } from "./pendingDiscoveryFixtures";

const API = "*/api/v1";
const WORKSPACE = `${API}/workspaces/:workspaceId`;

const campaignConfiguration = {
  provider: "openai",
  model_choice: { model: "gpt-5.6-terra", effort: "low", service_tier: "flex" },
} satisfies GuidedCampaignConfiguration;

const emptyOutcomePage = {
  items: [],
  total: 0,
  offset: 0,
  limit: 100,
  sources: [],
  reasons: [],
  candidate_kinds: [],
  terminal_stages: [],
  capture_state: "complete",
  recorded_outcomes: 0,
  follow_up_outcomes: 0,
  reported_outcomes: 0,
  reported_omitted_outcomes: 0,
} satisfies SourceScanOutcomePage;

const emptySpend = {
  currency: "USD",
  total_usd: 0,
  call_count: 0,
  unpriced_calls: 0,
  by_stage: [],
  by_model: [],
  recent: [],
} satisfies SpendSummary;

const unbilledTask = {
  task_id: "task-1",
  status: "no_model_calls",
  actual_cost_usd: null,
  call_count: 0,
  unpriced_call_count: 0,
  resent_on_standard_count: 0,
} satisfies TaskBilling;

const searchParameters = {
  version: 1,
  parameters: {
    seniority: { target_titles: [{ title: "Platform Lead", lane: "leadership" }] },
    locations: [{ key: "place-1", metro: "Northtown", region: "North Region", country: "Canada" }],
  },
};

const defaultPreview: GuidedCampaignPreview = {
  manifest_sha256: "a".repeat(64),
  cycle: 1,
  compiler_version: "gap-campaign-v1",
  bounds: {
    max_model_calls: 8,
    max_hosted_searches: 40,
    max_output_tokens: 16000,
    searches_per_attempt: 5,
  },
  branches: [
    {
      position: 1,
      key: "b".repeat(64),
      label: "Platform Lead",
      kind: "title_topic",
      priority_family: "leadership",
      protected: true,
      markets: ["Northtown, Canada"],
      floor_query: "Platform Lead Northtown Canada",
      attempt_position: 1,
      prior_outcome: null,
      last_searched_at: null,
    },
  ],
  attempts: [{ position: 1, branch_positions: [1], search_cap: 5 }],
  provider: "openai",
  model: "gpt-5.6-terra",
  supersedes_campaign_id: null,
  planned_search_fee_cents: 40,
  estimated_cost_cents: null,
  estimate_basis: "provider_model",
};

const emptyPreview: GuidedCampaignPreview = { ...defaultPreview, branches: [], attempts: [] };

const [firstBranch] = defaultPreview.branches;
if (!firstBranch) throw new Error("The default preview must carry one branch.");

const defaultReport: GuidedCampaignReport = {
  campaign_id: "campaign-1",
  manifest_sha256: "a".repeat(64),
  cycle: 1,
  status: "completed",
  max_model_calls: 8,
  max_hosted_searches: 40,
  model_calls_used: 1,
  hosted_searches_used: 3,
  cost_cents: 125,
  known_posting_count: 2,
  registered_board_count: 1,
  findings: [
    {
      captured_url_id: "captured-1",
      url: "https://jobs.example/posting-1",
      staged_basis: "title_evidence",
      pending_discovery_id: "pending-1",
      employer: "Example Health",
      title: "Clinical Data Director",
      location: "Remote",
      branch_labels: ["Platform Lead"],
      model_rationale: "Matches the branch's floor query.",
    },
  ],
  boards_admitted: [],
  not_staged: [],
  branches: [
    {
      position: 1,
      label: "Platform Lead",
      priority_family: "leadership",
      protected: true,
      attempt_position: 1,
      outcome: "searched",
      matched_queries: ["Platform Lead Northtown Canada"],
    },
  ],
  attempts: [
    {
      position: 1,
      state: "processed",
      hosted_search_count: 3,
      page_action_count: 1,
      finish: "complete",
      captured_not_selected: 0,
      cost_cents: 125,
      error_category: null,
    },
  ],
  warnings: [],
  stranded_processing_attempts: 0,
};

afterEach(() => {
  window.localStorage.clear();
});

interface CampaignReads {
  /** The task the panel polls by id, when one is running. */
  task?: Task | null;
  tasks?: Task[];
  configuration?: GuidedCampaignConfiguration;
  preview?: GuidedCampaignPreview | (() => GuidedCampaignPreview);
  /** Answer the preview route directly, for a refusal or a held-open refresh. */
  onPreviewRequest?: () => Response | Promise<Response> | undefined;
  report?: GuidedCampaignReport | null;
  onReportRequest?: () => Response | undefined;
  onAbandon?: () => Response | undefined;
  history?: RunCostHistory[];
  pending?: PendingDiscovery[];
  spend?: SpendSummary;
  billing?: TaskBilling;
  outcomes?: SourceScanOutcomePage;
  freeMode?: boolean;
}

/** Answer the campaign reads, and accept the enqueue, through MSW. */
function installCampaign(reads: CampaignReads = {}) {
  const log = requestLog();
  const report = reads.report === undefined ? defaultReport : reads.report;
  server.use(
    http.get(`${API}/health`, () =>
      HttpResponse.json({
        status: "ok",
        version: "0.1.0",
        database_reachable: true,
        schema_current: true,
        current_revision: "0001",
        head_revision: "0001",
        database_exists: true,
        worker_available: true,
        free_mode: reads.freeMode ?? false,
      } satisfies Health),
    ),
    http.post(`${API}/guided-campaigns/:campaignId/attempts/:position/abandon`, ({ params }) => {
      const refused = reads.onAbandon?.();
      if (refused) return refused;
      const position = Number(params.position);
      if (!report) return HttpResponse.json(null);
      return HttpResponse.json({
        ...report,
        attempts: report.attempts.map((attempt) =>
          attempt.position === position ? { ...attempt, state: "abandoned" } : attempt,
        ),
      });
    }),
    http.post(`${API}/tasks`, async ({ request }) => {
      await log.record(request);
      return HttpResponse.json({ id: "task-1" });
    }),
    http.get(`${API}/tasks/types`, () => HttpResponse.json(TASK_TYPE_CATALOG)),
    http.get(`${API}/tasks/:taskId/billing`, () =>
      HttpResponse.json(reads.billing ?? unbilledTask),
    ),
    http.get(`${API}/tasks/:taskId`, () => HttpResponse.json(reads.task ?? {})),
    http.get(`${API}/tasks`, ({ request }) => {
      log.reads(new URL(request.url));
      return HttpResponse.json(reads.tasks ?? []);
    }),
    http.get(`${API}/guided-campaign/preview`, async ({ request }) => {
      log.reads(new URL(request.url));
      const answered = await reads.onPreviewRequest?.();
      if (answered) return answered;
      return HttpResponse.json(
        typeof reads.preview === "function" ? reads.preview() : (reads.preview ?? defaultPreview),
      );
    }),
    http.get(`${API}/guided-campaigns/latest`, () => {
      const refused = reads.onReportRequest?.();
      return refused ?? HttpResponse.json(report);
    }),
    http.get(`${API}/job-search-parameters`, () => HttpResponse.json(searchParameters)),
    http.get(`${WORKSPACE}/source-scan-outcomes`, ({ request }) => {
      log.reads(new URL(request.url));
      return HttpResponse.json(reads.outcomes ?? emptyOutcomePage);
    }),
    http.get(`${WORKSPACE}/guided-campaign-configuration`, () =>
      HttpResponse.json(reads.configuration ?? campaignConfiguration),
    ),
    http.get(`${WORKSPACE}/sources`, () => HttpResponse.json({ items: [] })),
    http.get(`${WORKSPACE}/spend-history`, () => HttpResponse.json(reads.history ?? [])),
    http.get(`${WORKSPACE}/spend`, () => HttpResponse.json(reads.spend ?? emptySpend)),
    http.get(`${WORKSPACE}/pending-discoveries`, () => HttpResponse.json(reads.pending ?? [])),
    http.get(`${WORKSPACE}/search-title-proposals`, () =>
      HttpResponse.json({ capacities: [], items: [] }),
    ),
    http.get(`${WORKSPACE}/search-archetype-proposals`, () =>
      HttpResponse.json({ capacities: [], items: [] }),
    ),
  );
  return log;
}

function panel(workspaceId = "workspace-1") {
  return render(
    <MemoryRouter>
      <AppProviders>
        <GuidedCampaignPanel key={workspaceId} workspaceId={workspaceId} />
      </AppProviders>
    </MemoryRouter>,
  );
}

async function waitUntilWebReady() {
  await waitFor(() =>
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "Search the web for new jobs" }),
    ).toHaveProperty("disabled", false),
  );
}

describe("GuidedCampaignPanel", () => {
  it("settles a missing-CV preview with setup links and no untouched bound errors", async () => {
    const log = installCampaign({
      report: null,
      onPreviewRequest: () =>
        HttpResponse.json(
          { detail: "No master CV has been imported into this workspace." },
          { status: 404 },
        ),
    });
    panel();

    await screen.findByText(/No master CV has been imported/);
    expect(screen.queryByRole("button", { name: "Updating preview" })).toBeNull();
    expect(screen.getByRole("button", { name: "Search the web for new jobs" })).toHaveProperty(
      "disabled",
      true,
    );
    expect(screen.getByRole("link", { name: "Import a master CV" }).getAttribute("href")).toBe(
      "/cv",
    );
    expect(
      screen
        .getByRole("link", { name: "Configure provider access in Settings" })
        .getAttribute("href"),
    ).toBe("/settings?tab=provider");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByLabelText("Model calls").getAttribute("aria-invalid")).toBeNull();
    expect(screen.getByLabelText("Hosted searches").getAttribute("aria-invalid")).toBeNull();
    expect(log.requests.filter(({ method }) => method === "POST")).toHaveLength(0);
  });

  it("validates only an edited bound after an initial preview failure", async () => {
    installCampaign({
      report: null,
      onPreviewRequest: () => new HttpResponse(null, { status: 503 }),
    });
    panel();
    await screen.findByText(/The campaign preview could not be read:/);

    fireEvent.change(screen.getByLabelText("Model calls"), { target: { value: "0" } });

    expect(screen.getByRole("alert").textContent).toBe("Enter a whole number of 1 or more.");
    expect(screen.getByLabelText("Model calls").getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByLabelText("Hosted searches").getAttribute("aria-invalid")).toBeNull();
  });

  it("retries an initial preview failure and restores the paid confirmation when it succeeds", async () => {
    let unavailable = true;
    const log = installCampaign({
      report: null,
      onPreviewRequest: () => (unavailable ? new HttpResponse(null, { status: 503 }) : undefined),
    });
    panel();
    await screen.findByText(/The campaign preview could not be read:/);
    expect(screen.queryByRole("button", { name: "Updating preview" })).toBeNull();

    unavailable = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitUntilWebReady();

    expect(screen.getByLabelText("Model calls")).toHaveProperty("value", "8");
    expect(screen.getByLabelText("Hosted searches")).toHaveProperty("value", "40");
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Search the web for new jobs" }));
    expect(await screen.findByRole("dialog")).toBeTruthy();
    expect(log.requests.filter(({ method }) => method === "POST")).toHaveLength(0);
  });

  it("preserves an edited bound when retry loads the first successful preview", async () => {
    let unavailable = true;
    installCampaign({
      report: null,
      onPreviewRequest: () => (unavailable ? new HttpResponse(null, { status: 503 }) : undefined),
    });
    panel();
    await screen.findByText(/The campaign preview could not be read:/);
    fireEvent.change(screen.getByLabelText("Model calls"), { target: { value: "0" } });

    unavailable = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText("Review the campaign preview");

    expect(screen.getByLabelText("Model calls")).toHaveProperty("value", "0");
    expect(screen.getByLabelText("Hosted searches")).toHaveProperty("value", "40");
    expect(screen.getByRole("alert").textContent).toBe("Enter a whole number of 1 or more.");
    expect(screen.getByRole("button", { name: "Search the web for new jobs" })).toHaveProperty(
      "disabled",
      true,
    );
  });

  it("shows the compiled campaign preview before spending anything", async () => {
    installCampaign();
    panel();
    await waitUntilWebReady();

    expect(screen.getByLabelText("Guided campaign plan").textContent).toContain(
      "Plan ready: 1 of 1 branch in 1 attempt.",
    );
    fireEvent.click(screen.getByText("Review the campaign preview"));
    const planFacts = screen.getByLabelText("Campaign plan facts").textContent ?? "";
    expect(planFacts).toContain("Plan compiled by gap-campaign-v1");
    expect(planFacts).toContain("capped at 5 searches");
    expect(planFacts.replaceAll(",", "")).toContain("16000 output tokens");
    const branches = screen.getByRole("table", { name: "Campaign coverage branches" });
    expect(within(branches).getByText("Platform Lead")).toBeTruthy();
    expect(within(branches).getByText("Northtown, Canada")).toBeTruthy();
    const attempts = screen.getByRole("table", { name: "Campaign attempt grouping" });
    expect(within(attempts).getByText("5")).toBeTruthy();
  });

  it("counts several branches with the plural the word takes", async () => {
    installCampaign({
      preview: {
        ...defaultPreview,
        branches: [
          firstBranch,
          { ...firstBranch, position: 2, key: "c".repeat(64), label: "Data Lead" },
        ],
        attempts: [{ position: 1, branch_positions: [1, 2], search_cap: 5 }],
      },
    });
    panel();
    await waitUntilWebReady();

    expect(screen.getByLabelText("Guided campaign plan").textContent).toContain(
      "Plan ready: 2 of 2 branches in 1 attempt.",
    );
  });

  it("says every branch has been searched when the preview compiles empty, with a next action", async () => {
    installCampaign({ preview: emptyPreview });
    panel();

    expect(await screen.findByText(/Every coverage branch has already been searched/)).toBeTruthy();
    const note = screen.getByLabelText("Guided campaign plan");
    expect(note.textContent).toMatch(/Accept a suggested term|edit search preferences/);
    expect(screen.getByRole("button", { name: "Search the web for new jobs" })).toHaveProperty(
      "disabled",
      true,
    );
  });

  it("keeps the bound inputs editable and shows the last good preview when a refresh fails", async () => {
    let failNext = false;
    installCampaign({
      onPreviewRequest: () => (failNext ? new HttpResponse(null, { status: 500 }) : undefined),
    });
    panel();
    await waitUntilWebReady();

    failNext = true;
    vi.useFakeTimers();
    try {
      fireEvent.change(screen.getByLabelText("Model calls"), { target: { value: "3" } });
      await vi.advanceTimersByTimeAsync(400);
    } finally {
      vi.useRealTimers();
    }

    await waitFor(() => {
      expect(screen.getByText(/The campaign preview could not be refreshed:/)).toBeTruthy();
      expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    });
    expect(screen.getByLabelText("Model calls")).toHaveProperty("disabled", false);
    expect(screen.getByLabelText("Hosted searches")).toHaveProperty("disabled", false);
    expect(screen.queryByRole("button", { name: "Updating preview" })).toBeNull();
    expect(screen.getByRole("button", { name: "Search the web for new jobs" })).toHaveProperty(
      "disabled",
      true,
    );
    // The last good preview (branches, attempts) stays visible under the error.
    expect(screen.getByText("Review the campaign preview")).toBeTruthy();
  });

  it("previews a bound above the default instead of capping it", async () => {
    const log = installCampaign();
    panel();
    await waitUntilWebReady();

    const bound = screen.getByLabelText("Model calls");
    vi.useFakeTimers();
    try {
      fireEvent.change(bound, { target: { value: "9" } });
      await vi.advanceTimersByTimeAsync(400);
    } finally {
      vi.useRealTimers();
    }

    await waitFor(() =>
      expect(log.requests.some(({ url }) => url.searchParams.get("max_model_calls") === "9")).toBe(
        true,
      ),
    );
    expect(bound.getAttribute("aria-invalid")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("blocks the paid action on a cleared bound and describes the input with the error", async () => {
    installCampaign();
    panel();
    await waitUntilWebReady();

    const bound = screen.getByLabelText("Hosted searches");
    const description = () =>
      document.getElementById(bound.getAttribute("aria-describedby") ?? "")?.textContent;
    expect(description()).toMatch(/1 or more\. Default \d+\./);
    fireEvent.change(bound, { target: { value: "" } });

    expect(description()).toBe("Enter a whole number of 1 or more.");
    expect(screen.getByRole("button", { name: "Search the web for new jobs" })).toHaveProperty(
      "disabled",
      true,
    );
  });

  it("rejects a bound below one with an inline message instead of a request", async () => {
    const log = installCampaign();
    panel();
    await waitUntilWebReady();
    const before = log.requests.length;

    const bound = screen.getByLabelText("Model calls");
    vi.useFakeTimers();
    try {
      fireEvent.change(bound, { target: { value: "0" } });
      await vi.advanceTimersByTimeAsync(450);
    } finally {
      vi.useRealTimers();
    }

    expect(screen.getByRole("alert").textContent).toMatch(/1 or more/);
    expect(bound.getAttribute("aria-invalid")).toBe("true");
    expect(
      log.requests.slice(before).some(({ url }) => url.searchParams.get("max_model_calls") === "0"),
    ).toBe(false);
  });

  it("disables the paid action and labels it while a bound change is refetching", async () => {
    const previewResolver: { current: (() => void) | null } = { current: null };
    let secondCall = false;
    installCampaign({
      onPreviewRequest: () =>
        secondCall
          ? new Promise<Response>((resolve) => {
              previewResolver.current = () =>
                resolve(
                  HttpResponse.json({
                    ...defaultPreview,
                    bounds: { ...defaultPreview.bounds, max_model_calls: 3 },
                  }),
                );
            })
          : undefined,
    });
    panel();
    await waitUntilWebReady();

    secondCall = true;
    // The bound is debounced before it refetches, so drive that window with fake
    // timers rather than waiting it out; the refetch itself resolves on a promise.
    vi.useFakeTimers();
    try {
      fireEvent.change(screen.getByLabelText("Model calls"), { target: { value: "3" } });
      await vi.advanceTimersByTimeAsync(400);
    } finally {
      vi.useRealTimers();
    }

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Updating preview" })).toBeTruthy(),
    );
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "Updating preview" }),
    ).toHaveProperty("disabled", true);

    await waitFor(() => expect(previewResolver.current).not.toBeNull());
    previewResolver.current?.();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Search the web for new jobs" })).toHaveProperty(
        "disabled",
        false,
      ),
    );
  });

  it("states the approved execution scope in the confirmation", async () => {
    installCampaign();
    panel();
    await waitUntilWebReady();

    fireEvent.click(screen.getByRole("button", { name: "Search the web for new jobs" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).toContain("Runs up to 8 model calls and 5 hosted searches");
    expect(dialog.textContent).toContain("1 branch");
    expect(dialog.textContent).toContain("1 attempt");
    expect(dialog.textContent).toContain("openai gpt-5.6-terra");
    expect(dialog.textContent).toContain("Flex tier");
    expect(dialog.textContent).toContain("nothing stops the task once you confirm");
    expect(dialog.textContent).not.toContain("provider-hosted web_search");
  });

  it("pluralizes branches in the confirmation", async () => {
    installCampaign({
      preview: {
        ...defaultPreview,
        branches: [
          firstBranch,
          {
            ...firstBranch,
            position: 2,
            key: "c".repeat(64),
            label: "Data Lead",
          },
        ],
        attempts: [{ position: 1, branch_positions: [1, 2], search_cap: 5 }],
      },
    });
    panel();
    await waitUntilWebReady();

    fireEvent.click(screen.getByRole("button", { name: "Search the web for new jobs" }));
    expect((await screen.findByRole("dialog")).textContent).toContain("2 branches");
  });

  it("counts only the branches the plan assigned and the searches it capped", async () => {
    installCampaign({
      preview: {
        ...defaultPreview,
        branches: [
          firstBranch,
          {
            ...firstBranch,
            position: 2,
            key: "c".repeat(64),
            label: "Data Lead",
            attempt_position: null,
          },
        ],
      },
    });
    panel();
    await waitUntilWebReady();

    fireEvent.click(screen.getByRole("button", { name: "Search the web for new jobs" }));
    expect((await screen.findByRole("dialog")).textContent).toContain(
      "Runs up to 8 model calls and 5 hosted searches over 1 of 2 branches in 1 attempt",
    );
  });

  it("prices the confirmation by the attempts the plan will run", async () => {
    installCampaign({
      history: [
        {
          task_type: "search.guided_campaign",
          currency: "USD",
          unit: "attempt",
          measured_runs: 4,
          unpriced_runs: 0,
          replayed_runs: 0,
          per_unit_low: "0.04",
          per_unit_median: "0.08",
          per_unit_high: "0.12",
        },
      ],
      preview: {
        ...defaultPreview,
        attempts: [
          { position: 1, branch_positions: [1], search_cap: 5 },
          { position: 2, branch_positions: [1], search_cap: 5 },
        ],
      },
    });
    panel();
    await waitUntilWebReady();

    fireEvent.click(screen.getByRole("button", { name: "Search the web for new jobs" }));
    expect((await screen.findByRole("dialog")).textContent).toContain(
      "About $0.16 for 2 attempts (range $0.08 to $0.24, from 4 runs).",
    );
  });

  it("binds a web search enqueue to the previewed manifest and bounds", async () => {
    const log = installCampaign();
    panel();
    await waitUntilWebReady();

    fireEvent.click(screen.getByRole("button", { name: "Search the web for new jobs" }));
    fireEvent.click(screen.getByRole("button", { name: "Search the web" }));

    const enqueues = () => log.requests.filter(({ method }) => method === "POST");
    await waitFor(() => expect(enqueues()).toHaveLength(1));
    expect((enqueues()[0]?.body as { payload: unknown })?.payload).toEqual({
      campaign_manifest_sha256: defaultPreview.manifest_sha256,
      campaign_cycle: defaultPreview.cycle,
      max_model_calls: defaultPreview.bounds.max_model_calls,
      max_hosted_searches: defaultPreview.bounds.max_hosted_searches,
    });
  });

  it("warns that starting closes an unfinished campaign only when the preview names one", async () => {
    installCampaign();
    panel();
    await waitUntilWebReady();
    expect(screen.queryByLabelText("Unfinished campaign notice")).toBeNull();

    cleanup();
    installCampaign({ preview: { ...defaultPreview, supersedes_campaign_id: "campaign-0" } });
    panel();
    await waitUntilWebReady();
    expect(screen.getByLabelText("Unfinished campaign notice").textContent).toContain(
      "closes it as incomplete",
    );
  });

  it("names the sample an estimate was measured over", async () => {
    installCampaign({
      preview: { ...defaultPreview, estimated_cost_cents: 150, estimate_basis: "provider_model" },
    });
    panel();
    await waitUntilWebReady();

    expect(screen.getByLabelText("Campaign cost estimate").textContent).toContain(
      "Estimated $1.50 (1 attempt at the median of completed campaigns on openai gpt-5.6-terra).",
    );
    expect(screen.getByLabelText("Campaign cost estimate").textContent).toContain(
      "Hosted-search fees: about $0.40.",
    );

    cleanup();
    installCampaign({
      preview: { ...defaultPreview, estimated_cost_cents: 150, estimate_basis: "workspace" },
    });
    panel();
    await waitUntilWebReady();
    expect(screen.getByLabelText("Campaign cost estimate").textContent).toContain(
      "Estimated $1.50 (1 attempt at the median of completed campaigns).",
    );
  });

  it("states a missing estimate and an unpriced search fee", async () => {
    installCampaign({
      preview: {
        ...defaultPreview,
        estimated_cost_cents: null,
        planned_search_fee_cents: null,
        estimate_basis: null,
      },
    });
    panel();
    await waitUntilWebReady();

    const outlook = screen.getByLabelText("Campaign cost estimate").textContent;
    expect(outlook).toContain("there is no estimate yet");
    expect(outlook).toContain("The hosted-search fee for this model is unpriced.");
    expect(outlook).toContain("Cost never stops the campaign.");
  });

  it("says a branch with no floor query is searched with the model's own queries", async () => {
    installCampaign({
      preview: {
        ...defaultPreview,
        branches: [
          firstBranch,
          {
            ...firstBranch,
            position: 2,
            key: "c".repeat(64),
            label: "records custody",
            kind: "focus_bundle",
            floor_query: "",
          },
        ],
      },
    });
    panel();
    await waitUntilWebReady();

    const queries = within(screen.getByRole("table", { name: "Campaign coverage branches" }))
      .getAllByRole("row")
      .slice(1)
      .map((row) => within(row).getAllByRole("cell")[4]?.textContent);
    expect(queries).toEqual([
      "Platform Lead Northtown Canada",
      "No fixed query; the model writes its own searches for this branch",
    ]);
  });

  it("labels a branch whose searches could not be tied to it in plain words", async () => {
    installCampaign({
      report: {
        ...defaultReport,
        branches: [
          { ...defaultReport.branches[0]!, outcome: "unattributable", matched_queries: [] },
        ],
      },
    });
    panel();

    fireEvent.click(await screen.findByText("Branch coverage (1)"));
    const branchCoverage = screen.getByRole("table", { name: "Campaign branch coverage" });
    expect(within(branchCoverage).getByText("searched, not tied to this branch")).toBeTruthy();
    expect(within(branchCoverage).queryByText("unattributable")).toBeNull();
  });

  it("shows when each branch was last searched, or a dash when never", async () => {
    const searchedAt = "2026-09-01T12:00:00Z";
    installCampaign({
      preview: {
        ...defaultPreview,
        branches: [
          firstBranch,
          {
            ...firstBranch,
            position: 2,
            key: "c".repeat(64),
            label: "Data Lead",
            last_searched_at: searchedAt,
          },
        ],
      },
    });
    panel();
    await waitUntilWebReady();

    const rows = within(screen.getByRole("table", { name: "Campaign coverage branches" }))
      .getAllByRole("row")
      .slice(1)
      .map((row) => within(row).getAllByRole("cell").at(-1)?.textContent);
    expect(rows).toEqual(["-", new Date(searchedAt).toLocaleDateString()]);
  });

  it("shows URLs that were screened out or held out with their reason", async () => {
    installCampaign({
      report: {
        ...defaultReport,
        not_staged: [
          {
            url: "https://jobs.example/posting-2",
            outcome: "screened_out",
            detail: "outside the configured seniority band",
          },
          { url: "https://jobs.example/posting-3", outcome: "held_out", detail: null },
        ],
      },
    });
    panel();

    fireEvent.click(await screen.findByText("URLs not staged (2)"));
    const table = screen.getByRole("table", { name: "Campaign URLs not staged" });
    expect(within(table).getByText("screened out")).toBeTruthy();
    expect(within(table).getByText("outside the configured seniority band")).toBeTruthy();
    expect(within(table).getByText("held out")).toBeTruthy();
  });

  it("renders the latest campaign report regardless of the latest task's status, with one Pending link", async () => {
    installCampaign({
      task: taskRecord({ status: "failed", error_message: "temporary refusal" }),
      tasks: [taskRecord({ task_type: "search.guided_campaign" })],
      pending: [pendingDiscoveryRow({ id: "pending-1" })],
    });
    panel();

    expect(await screen.findByText("Findings (1)")).toBeTruthy();
    fireEvent.click(screen.getByText("Findings (1)"));
    const findings = screen.getByRole("table", { name: "Campaign findings" });
    expect(within(findings).getByText("Clinical Data Director at Example Health")).toBeTruthy();
    expect(within(findings).getByText("Matches the branch's floor query.")).toBeTruthy();
    const reviewLinks = screen.getAllByRole("link", { name: "Open Review" });
    expect(reviewLinks).toHaveLength(1);
    expect(reviewLinks[0]!.getAttribute("href")).toBe("/find?tab=review");
    const reportDetails = screen.getByText("Report details").closest("details");
    expect(reportDetails?.open).toBe(false);
    expect(reportDetails?.contains(findings)).toBe(true);
    expect(reportDetails?.contains(reviewLinks[0]!)).toBe(false);

    fireEvent.click(screen.getByText("Branch coverage (1)"));
    const branchCoverage = screen.getByRole("table", { name: "Campaign branch coverage" });
    expect(within(branchCoverage).getByText("searched")).toBeTruthy();

    fireEvent.click(screen.getByText("Attempts (1)"));
    const attempts = screen.getByRole("table", { name: "Campaign attempts" });
    expect(within(attempts).getByText("$1.25")).toBeTruthy();
  });

  it("says branch labels on findings and boards come from the whole search call", async () => {
    const note =
      "Branches name every query in the search call that returned the URL, not the one query that found it.";
    const report = {
      ...defaultReport,
      findings: [
        { ...defaultReport.findings[0]!, branch_labels: ["Platform Lead", "Data Director"] },
      ],
      boards_admitted: [
        {
          source_id: "source-1",
          employer: "Example Health",
          url: "https://jobs.example/careers",
          branch_labels: ["Platform Lead", "Data Director"],
          admitted_postings: [],
        },
      ],
    } satisfies GuidedCampaignReport;
    installCampaign({ report });
    panel();

    fireEvent.click(await screen.findByText("Findings (1)"));
    const findings = screen.getByRole("table", { name: "Campaign findings" });
    expect(within(findings).getByText("Platform Lead, Data Director")).toBeTruthy();
    expect(within(findings.closest("details")!).getByText(note)).toBeTruthy();

    fireEvent.click(screen.getByText("Boards admitted (1)"));
    const boards = screen.getByRole("table", { name: "Campaign boards admitted" });
    expect(within(boards).getByText("Platform Lead, Data Director")).toBeTruthy();
    expect(within(boards.closest("details")!).getByText(note)).toBeTruthy();
  });

  it("names a network board admitted without a source by its address", async () => {
    const report = {
      ...defaultReport,
      boards_admitted: [
        {
          source_id: null,
          employer: "",
          url: "https://example-ventures.getro.com/jobs",
          branch_labels: [],
          admitted_postings: [],
        },
      ],
    } satisfies GuidedCampaignReport;
    installCampaign({ report });
    panel();

    fireEvent.click(await screen.findByText("Boards admitted (1)"));
    const boards = screen.getByRole("table", { name: "Campaign boards admitted" });
    expect(within(boards).getByText("https://example-ventures.getro.com/jobs")).toBeTruthy();
  });

  it("flags a model-selected finding as unverified rather than matching it to verified evidence", async () => {
    installCampaign({
      report: {
        ...defaultReport,
        findings: [{ ...defaultReport.findings[0]!, staged_basis: "model_selected" }],
      },
    });
    panel();

    fireEvent.click(await screen.findByText("Findings (1)"));
    const findings = screen.getByRole("table", { name: "Campaign findings" });
    expect(within(findings).getByText("Model selected (unverified)")).toBeTruthy();
  });

  it("shows an overlap and status summary for the latest report", async () => {
    installCampaign();
    panel();

    const summary = await screen.findByRole("status", { name: "Campaign report status" });
    expect(summary.textContent).toContain("Status: completed");
    expect(summary.textContent).toContain("1 new finding");
    expect(summary.textContent).toContain("2 postings already known");
    expect(summary.textContent).toContain("1 board already registered");
  });

  it("shows bound consumption and recorded cost from the report", async () => {
    installCampaign();
    panel();

    await screen.findByRole("status", { name: "Campaign report status" });
    expect(
      screen.getByText("Used 1 of 8 model calls and 3 of 40 hosted searches. Recorded cost $1.25."),
    ).toBeTruthy();
  });

  it("lists campaign warnings as plain sentences naming the next step", async () => {
    installCampaign({
      report: { ...defaultReport, warnings: ["uncertain_attempt", "search_overrun"] },
    });
    panel();

    const list = await screen.findByRole("list");
    expect(within(list).getByText(/An attempt's outcome could not be confirmed/)).toBeTruthy();
    expect(within(list).getByText(/the campaign then closes as incomplete/)).toBeTruthy();
    expect(within(list).getByText(/used more hosted searches than it planned/)).toBeTruthy();
  });

  it("abandons an uncertain attempt through the shared confirmation provider, with danger styling", async () => {
    const uncertainReport: GuidedCampaignReport = {
      ...defaultReport,
      status: "paused_uncertain",
      attempts: [
        {
          position: 1,
          state: "uncertain",
          hosted_search_count: 2,
          page_action_count: 0,
          finish: null,
          captured_not_selected: null,
          cost_cents: null,
          error_category: "rate_limited",
        },
      ],
    };
    const abandons = requestLog();
    installCampaign({
      report: uncertainReport,
      onAbandon: () => {
        abandons.requests.push({ method: "POST", url: new URL("http://abandon/"), body: null });
        return undefined;
      },
    });
    panel();

    // The uncertain attempt and its abandon action surface immediately, outside a collapsed section.
    expect(await screen.findByText(/could not be confirmed \(rate limited\)/)).toBeTruthy();
    const abandonButton = screen.getByRole("button", { name: "Abandon attempt 1" });
    expect(abandonButton.className).toContain("button--danger");
    expect(screen.getByText("Report details").closest("details")?.open).toBe(true);
    fireEvent.click(abandonButton);
    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).toContain("closes the campaign as incomplete");
    fireEvent.click(within(dialog).getByRole("button", { name: "Abandon attempt" }));

    await waitFor(() => expect(abandons.requests).toHaveLength(1));
    expect(await screen.findByText("abandoned")).toBeTruthy();
  });

  it("queues an unpaid retry for captured attempts an earlier campaign could not process", async () => {
    const log = installCampaign({
      report: { ...defaultReport, stranded_processing_attempts: 2 },
    });
    panel();

    expect(
      await screen.findByText(/2 captured attempts from an earlier campaign were not processed/),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry processing" }));

    await waitFor(() =>
      expect(
        log.requests.filter(({ method }) => method === "POST").map(({ body }) => body),
      ).toContainEqual(
        expect.objectContaining({ task_type: "search.guided_campaign_retry", payload: {} }),
      ),
    );
  });

  it("states why the server refused to compile a campaign preview", async () => {
    installCampaign({
      onPreviewRequest: () =>
        HttpResponse.json(
          {
            detail:
              "A guided web campaign needs background web search, which Anthropic does not offer.",
          },
          { status: 422 },
        ),
    });
    panel();

    expect(await screen.findByText(/which Anthropic does not offer/)).toBeTruthy();
  });

  it("will not queue a second processing retry while one is unfinished", async () => {
    installCampaign({
      tasks: [
        taskRecord({
          id: "retry-1",
          task_type: "search.guided_campaign_retry",
          status: "queued",
          created_at: "2026-09-15T12:00:00Z",
        }),
      ],
      report: { ...defaultReport, stranded_processing_attempts: 1 },
    });
    panel();

    await waitFor(() =>
      expect(
        screen.getByRole<HTMLButtonElement>("button", { name: "Retry queued" }),
      ).toHaveProperty("disabled", true),
    );
  });

  it("shows an abandon failure next to the button that caused it", async () => {
    const uncertainReport: GuidedCampaignReport = {
      ...defaultReport,
      status: "paused_uncertain",
      attempts: [
        {
          position: 1,
          state: "uncertain",
          hosted_search_count: 2,
          page_action_count: 0,
          finish: null,
          captured_not_selected: 0,
          cost_cents: null,
          error_category: null,
        },
      ],
    };
    installCampaign({
      report: uncertainReport,
      onAbandon: () => new HttpResponse(null, { status: 409 }),
    });
    panel();

    const abandonButton = await screen.findByRole("button", { name: "Abandon attempt 1" });
    fireEvent.click(abandonButton);
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Abandon attempt" }));

    const error = await screen.findByText(/TailorCV request failed \(409\)/);
    expect(
      error.compareDocumentPosition(abandonButton) & Node.DOCUMENT_POSITION_PRECEDING,
    ).toBeTruthy();
  });

  it("reports when the latest campaign report could not be read, with a retry", async () => {
    installCampaign({ onReportRequest: () => new HttpResponse(null, { status: 500 }) });
    panel();

    expect(
      await screen.findByText(
        "The latest campaign report could not be read.",
        {},
        { timeout: 3000 },
      ),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });

  it.each(["running", "completed", "failed"] as const)(
    "shows the durable screened-candidate ledger for a %s web task",
    async (status) => {
      const log = installCampaign({
        task: taskRecord({ status, updated_at: "2026-09-11T12:00:00Z" }),
        tasks: [taskRecord({ task_type: "search.guided_campaign", status })],
        outcomes: {
          ...emptyOutcomePage,
          items: Array.from({ length: 100 }, (_, index) => ({
            id: `outcome-${index + 501}`,
            task_id: "task-1",
            source_scan_id: null,
            source_name: "Web discovery",
            candidate_kind: "direct_posting",
            url: `https://jobs.example/posting-${index + 501}`,
            employer: "Example Health",
            title: index === 0 ? "Clinical Data Director" : `Clinical Data Role ${index + 1}`,
            location: "Remote",
            terminal_stage: "proposed",
            reason: "stage_limit_reached",
            detail: "The campaign stopped before reading this candidate.",
            screening_findings: [],
            created_at: "2026-09-11T12:00:00Z",
          })),
          total: 650,
          recorded_outcomes: 650,
          reported_outcomes: 650,
          sources: ["Web discovery"],
          reasons: ["stage_limit_reached"],
          candidate_kinds: ["direct_posting"],
          terminal_stages: ["proposed"],
        },
      });
      panel();

      expect(await screen.findByRole("heading", { name: "Screened candidates" })).toBeTruthy();
      const outcomeRequest = log.requests
        .map(({ url }) => url)
        .find((url) => url.pathname.endsWith("/source-scan-outcomes"));
      expect(outcomeRequest?.searchParams.get("task_id")).toBe("task-1");
    },
  );

  it("keeps the funnel counts and leaves the candidate rows to the searchable ledger", async () => {
    const funnelTask = taskRecord({
      task_type: "search.guided_campaign",
      status: "completed",
      progress: {
        funnel: {
          counts: { proposed: 2, staged: 1, rejected: 1 },
          total_outcomes: 2,
          omitted_outcomes: 0,
          omission_reason: "",
          reason_counts: { staged: 1, already_known: 1 },
          outcomes: [
            {
              kind: "direct_posting",
              employer: "Example Health",
              title: "Data Platform Director",
              location: "Remote",
              url: "https://jobs.example/posting-1",
              source: "Web discovery",
              stages: ["proposed", "staged"],
              reason: "staged",
              detail: "",
              screening_findings: [],
            },
            {
              kind: "direct_posting",
              employer: "Northwind Research",
              title: "Platform Lead",
              location: "Remote",
              url: "https://jobs.example/posting-2",
              source: "Web discovery",
              stages: ["proposed", "rejected"],
              reason: "already_known",
              detail: "",
              screening_findings: [],
            },
          ],
        },
      },
    });
    installCampaign({
      task: funnelTask,
      tasks: [funnelTask],
      outcomes: {
        ...emptyOutcomePage,
        items: [
          {
            id: "outcome-1",
            task_id: "task-1",
            source_scan_id: null,
            source_name: "Web discovery",
            candidate_kind: "direct_posting",
            url: "https://jobs.example/posting-2",
            employer: "Northwind Research",
            title: "Platform Lead",
            location: "Remote",
            terminal_stage: "rejected",
            reason: "already_known",
            detail: "The workspace already holds this posting.",
            screening_findings: [],
            created_at: "2026-09-11T12:00:00Z",
          },
        ],
        total: 1,
        recorded_outcomes: 1,
        reported_outcomes: 2,
        sources: ["Web discovery"],
        reasons: ["already_known"],
        candidate_kinds: ["direct_posting"],
        terminal_stages: ["rejected"],
      },
    });
    panel();

    expect(await screen.findByRole("heading", { name: "Screened candidates" })).toBeTruthy();
    expect(
      screen.getByText(
        /Discovery funnel: 2 candidates the campaign read - 1 staged, 1 already known/,
      ),
    ).toBeTruthy();
    expect(screen.queryByRole("table", { name: "Discovery funnel outcomes" })).toBeNull();
    expect(
      screen.getByText(
        "Every candidate this run read is listed under Screened candidates below, where you can search, filter, and sort it.",
      ),
    ).toBeTruthy();
    const ledger = screen.getByRole("region", { name: "Screened candidates" });
    expect(within(ledger).getByRole("link", { name: "Platform Lead" })).toBeTruthy();
    expect(within(ledger).getByText("The workspace already holds this posting.")).toBeTruthy();
  });

  it("keeps the route to the specialist IC setting a campaign screened a posting on", async () => {
    installCampaign({
      task: taskRecord({ status: "completed", updated_at: "2026-09-11T12:00:00Z" }),
      tasks: [taskRecord({ task_type: "search.guided_campaign", status: "completed" })],
      outcomes: {
        ...emptyOutcomePage,
        items: [
          {
            id: "outcome-1",
            task_id: "task-1",
            source_scan_id: null,
            source_name: "Web discovery",
            candidate_kind: "direct_posting",
            url: "https://jobs.example/posting-1",
            employer: "Example Health",
            title: "Data Systems Architect",
            location: "Remote",
            terminal_stage: "rejected",
            reason: "insufficient_title_evidence",
            detail: "",
            screening_findings: [
              {
                dimension: "title",
                code: "title_specialist_ic_review",
                subject: "Data Systems Architect",
              },
            ],
            created_at: "2026-09-11T12:00:00Z",
          },
        ],
        total: 1,
        recorded_outcomes: 1,
        reported_outcomes: 1,
        sources: ["Web discovery"],
        reasons: ["insufficient_title_evidence"],
        candidate_kinds: ["direct_posting"],
        terminal_stages: ["rejected"],
      },
    });
    panel();

    const ledger = await screen.findByRole("region", { name: "Screened candidates" });
    expect(
      within(ledger)
        .getByRole("link", { name: "Edit specialist IC review settings" })
        .getAttribute("href"),
    ).toBe("/settings?tab=search");
  });

  it("points spending totals at Settings > Costs instead of repeating them", async () => {
    installCampaign({
      spend: {
        currency: "USD",
        total_usd: 1.25,
        call_count: 6,
        unpriced_calls: 0,
        by_stage: [
          { name: "guided_campaign", calls: 4, unpriced_calls: 0, amount_usd: 0.75 },
          { name: "matching", calls: 2, unpriced_calls: 0, amount_usd: 0.5 },
        ],
        by_model: [],
        recent: [],
      },
    });
    panel();
    await waitUntilWebReady();

    expect(
      screen.getByRole("link", { name: "See spending in Settings" }).getAttribute("href"),
    ).toBe("/settings?tab=costs");
    expect(screen.queryByText(/\$0\.7500/)).toBeNull();
  });

  it("holds the web search in free mode and says how to turn it on", async () => {
    installCampaign({ freeMode: true });
    panel();

    const link = await screen.findByRole("link", { name: "Open provider settings" });
    expect(link.getAttribute("href")).toBe("/settings?tab=provider");
    expect(screen.getByRole("button", { name: "Search the web for new jobs" })).toHaveProperty(
      "disabled",
      true,
    );
    expect(screen.getByText("Add a provider key to use paid actions.")).toBeTruthy();
  });

  it("keeps the search button and estimate up front and folds bounds and preview", async () => {
    installCampaign();
    panel();
    await waitUntilWebReady();

    const start = screen.getByRole("region", { name: "Start a new web search" });
    expect(within(start).getByLabelText("Campaign cost estimate")).toBeTruthy();
    const advanced = within(start).getByText("Advanced").closest("details");
    expect(advanced?.open).toBe(false);
    expect(advanced?.contains(screen.getByLabelText("Model calls"))).toBe(true);
    expect(advanced?.contains(screen.getByText("Review the campaign preview"))).toBe(true);
    expect(
      within(start).getByRole("link", { name: "Scan your saved boards" }).getAttribute("href"),
    ).toBe("/find?tab=boards");
  });

  it("shows recorded billing for a cancelled task", async () => {
    const billings = requestLog();
    installCampaign({
      task: taskRecord({ status: "cancelled" }),
      tasks: [taskRecord({ task_type: "search.guided_campaign", status: "cancelled" })],
      billing: {
        task_id: "task-1",
        status: "priced",
        actual_cost_usd: "0.500000",
        call_count: 2,
        unpriced_call_count: 0,
        resent_on_standard_count: 0,
      },
    });
    server.use(
      http.get(`${API}/tasks/:taskId/billing`, ({ request }) => {
        billings.reads(new URL(request.url));
        return HttpResponse.json({
          task_id: "task-1",
          status: "priced",
          actual_cost_usd: "0.500000",
          call_count: 2,
          unpriced_call_count: 0,
          resent_on_standard_count: 0,
        } satisfies TaskBilling);
      }),
    );
    panel();

    expect(
      await screen.findByText(/Recorded cost for this task: \$0\.5000 across 2 model calls/),
    ).toBeTruthy();
    expect(
      billings.requests.some(({ url }) => url.pathname.endsWith("/tasks/task-1/billing")),
    ).toBe(true);
  });

  it("finds the latest guided run past the default 50-row window", async () => {
    const older = Array.from({ length: 80 }, (_, index) =>
      taskRecord({
        id: `evaluate-${index}`,
        status: "completed",
        created_at: `2026-08-20T00:${String(index).padStart(2, "0")}:00Z`,
      }),
    );
    const campaign = taskRecord({
      task_type: "search.guided_campaign",
      status: "completed",
      created_at: "2026-08-19T00:00:00Z",
    });
    const log = installCampaign({
      task: taskRecord({ status: "completed" }),
      tasks: [...older, campaign],
    });
    panel();

    expect(await screen.findByRole("status", { name: "Campaign report status" })).toBeTruthy();
    const listUrls = log.requests
      .map(({ url }) => url)
      .filter((url) => url.pathname.endsWith("/tasks"));
    expect(listUrls.some((url) => url.searchParams.get("limit") === "250")).toBe(true);
  });
});
