import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw/http";
import { describe, expect, it, vi } from "vitest";

import type {
  Health,
  SearchVocabularyConfiguration,
  SpendSummary,
  Task,
  TaskBilling,
} from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { taskTypesHandler } from "../../test/taskTypes";
import { searchParametersState } from "../search-parameters/searchParameterFixtures";
import { spendSummary } from "../settings/settingsHandlers";
import { SearchVocabularyPanel } from "./SearchVocabularyPanel";

const API = "*/api/v1";
const WORKSPACE = `${API}/workspaces/:workspaceId`;

const vocabularyConfiguration = {
  provider: "openai",
  model_choice: { model: "gpt-5.6-terra", effort: "low", service_tier: "flex" },
} satisfies SearchVocabularyConfiguration;

const PARAMETERS = searchParametersState({
  id: "parameters-1",
  version: 1,
  configured: true,
  content_sha256: "e".repeat(64),
  effective_at: "2026-09-06T00:00:00Z",
  parameters: {
    opportunity_thesis: "",
    score_weight_set: "",
    working_day_shift_hours: 0,
    seniority: {
      minimum: "",
      maximum: "",
      title_matching: "guided",
      target_titles: [{ title: "Platform Lead", lane: "leadership" }],
    },
    locations: [
      {
        key: "place-1",
        metro: "Northtown",
        region: "North Region",
        country: "Canada",
        basis: "",
        eligibility: "authorized",
        resident: false,
      },
    ],
  },
});

const NO_BILLING = {
  task_id: "task-1",
  status: "no_model_calls",
  actual_cost_usd: null,
  call_count: 0,
  unpriced_call_count: 0,
  resent_on_standard_count: 0,
} satisfies TaskBilling;

function vocabularyTask(overrides: Partial<Task> = {}): Task {
  return {
    id: "task-1",
    workspace_id: "workspace-1",
    task_type: "search.vocabulary",
    status: "running",
    attempt_count: 1,
    job_posting_ids: [],
    pending_discovery_ids: [],
    source_ids: [],
    progress: {},
    cancellation_requested_at: null,
    error_category: null,
    error_message: null,
    created_at: "2026-09-07T00:00:00Z",
    started_at: "2026-09-07T00:00:01Z",
    updated_at: "2026-09-07T00:00:02Z",
    completed_at: null,
    ...overrides,
  };
}

/** Answer the task read with one progress payload, and accept the enqueue. */
function installHandlers(
  task: Task | null,
  existingTasks: Task[] = [],
  costs: {
    configuration?: SearchVocabularyConfiguration | Record<string, never>;
    spend?: SpendSummary;
  } = {},
) {
  const enqueued: unknown[] = [];
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
        free_mode: false,
      } satisfies Health),
    ),
    taskTypesHandler(),
    http.get(`${WORKSPACE}/search-vocabulary-configuration`, () =>
      HttpResponse.json(costs.configuration ?? vocabularyConfiguration),
    ),
    http.post(`${API}/tasks`, async ({ request }) => {
      enqueued.push(await request.json());
      return HttpResponse.json(task ?? vocabularyTask());
    }),
    http.get(`${WORKSPACE}/spend-history`, () => HttpResponse.json([])),
    http.get(`${WORKSPACE}/spend`, () => HttpResponse.json(costs.spend ?? spendSummary())),
    http.get(`${API}/tasks/:taskId/billing`, () => HttpResponse.json(NO_BILLING)),
    http.get(`${API}/tasks`, () => HttpResponse.json(existingTasks)),
    http.get(`${API}/job-search-parameters`, () => HttpResponse.json(PARAMETERS)),
    http.get(`${WORKSPACE}/search-title-proposals`, () =>
      HttpResponse.json({ capacities: [], items: [] }),
    ),
    http.get(`${WORKSPACE}/search-archetype-proposals`, () =>
      HttpResponse.json({ capacities: [], items: [] }),
    ),
    http.get(`${WORKSPACE}/search-cluster-proposals`, () =>
      HttpResponse.json({ capacities: [], items: [] }),
    ),
    http.get(`${API}/tasks/:taskId`, () =>
      task ? HttpResponse.json(task) : new HttpResponse(null, { status: 404 }),
    ),
  );
  return enqueued;
}

function panel(workspaceId = "workspace-1") {
  return render(
    <AppProviders>
      <SearchVocabularyPanel key={workspaceId} workspaceId={workspaceId} />
    </AppProviders>,
  );
}

async function waitUntilReady() {
  await waitFor(() =>
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "Propose new search terms" }),
    ).toHaveProperty("disabled", false),
  );
}

describe("SearchVocabularyPanel", () => {
  it("restores the vocabulary action label after reload", async () => {
    installHandlers(vocabularyTask(), [vocabularyTask()]);
    panel();
    expect(await screen.findByRole("button", { name: "Search running" })).toHaveProperty(
      "disabled",
      true,
    );
  });

  it("reports completed vocabulary without claiming posting discovery", async () => {
    installHandlers(vocabularyTask({ status: "completed" }), [vocabularyTask()]);
    panel();
    const result = await screen.findByLabelText("Search vocabulary result");
    expect(result.textContent).toContain("No postings or new employer boards were discovered");
  });

  it("enqueues one vocabulary search and will not start a second while it runs", async () => {
    const enqueued = installHandlers(vocabularyTask());
    panel();

    await waitUntilReady();
    const button = screen.getByRole("button", { name: "Propose new search terms" });
    fireEvent.click(button);
    expect(enqueued).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Propose search terms" }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Search running" }).hasAttribute("disabled")).toBe(
        true,
      ),
    );
    expect(enqueued).toEqual([
      { workspace_id: "workspace-1", task_type: "search.vocabulary", payload: {} },
    ]);
  });

  it("keeps the paid action disabled until the queued task's state loads", async () => {
    const enqueued = installHandlers(vocabularyTask());
    const release: { current?: () => void } = {};
    let holdTaskRead = false;
    server.use(
      http.get(`${API}/tasks/task-1`, () =>
        holdTaskRead
          ? new Promise<Response>((resolve) => {
              release.current = () => resolve(HttpResponse.json(vocabularyTask()));
            })
          : HttpResponse.json(vocabularyTask()),
      ),
    );
    panel();
    await waitUntilReady();

    holdTaskRead = true;
    fireEvent.click(screen.getByRole("button", { name: "Propose new search terms" }));
    fireEvent.click(screen.getByRole("button", { name: "Propose search terms" }));
    await waitFor(() => expect(release.current).toBeDefined());

    expect(enqueued).toHaveLength(1);
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "Propose new search terms" }),
    ).toHaveProperty("disabled", true);
    release.current?.();
    expect(await screen.findByRole("button", { name: "Search running" })).toHaveProperty(
      "disabled",
      true,
    );
  });

  it("says why the paid action is disabled when recent search status cannot be read", async () => {
    installHandlers(null);
    server.use(http.get(`${API}/tasks`, () => new HttpResponse(null, { status: 500 })));
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      panel();
      // The app retries a failed read once, one second later.
      await vi.advanceTimersByTimeAsync(1000);
      expect(
        await screen.findByText(
          "Recent search vocabulary status could not be read. Reload before starting another paid search.",
        ),
      ).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
    expect(screen.getByRole("button", { name: "Propose new search terms" })).toHaveProperty(
      "disabled",
      true,
    );
  });

  it("fails closed when the configured provider tier is unavailable", async () => {
    installHandlers(null, [], { configuration: {} });
    panel();

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("The provider, model, and effort could not be read");
    expect(screen.getByRole("button", { name: "Propose new search terms" })).toHaveProperty(
      "disabled",
      true,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("points spending totals at Settings > Costs instead of repeating them", async () => {
    installHandlers(null, [], {
      spend: spendSummary({
        total_usd: 1.25,
        call_count: 6,
        by_stage: [
          { name: "search_vocabulary", calls: 4, unpriced_calls: 0, amount_usd: 0.75 },
          { name: "matching", calls: 2, unpriced_calls: 0, amount_usd: 0.5 },
        ],
      }),
    });
    panel();
    await waitUntilReady();

    expect(
      screen.getByRole("link", { name: "See spending in Settings" }).getAttribute("href"),
    ).toBe("/settings?tab=costs");
    expect(screen.queryByText(/\$0\.7500/)).toBeNull();
  });

  it("confirms in two sentences what runs and what it costs, naming the model below", async () => {
    installHandlers(null);
    panel();
    await waitUntilReady();
    fireEvent.click(screen.getByRole("button", { name: "Propose new search terms" }));

    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).toContain(
      "Runs one call that reads your CV and proposes search terms, and it finds no jobs.",
    );
    expect(dialog.textContent).not.toContain("scan registered sources for free");
    expect(dialog.textContent).toContain("Configured provider:");
  });

  it("surfaces a search that fails after launch instead of leaving the panel blank", async () => {
    const enqueued = installHandlers(
      vocabularyTask({ status: "failed", error_message: "the provider refused the key" }),
    );
    panel();
    await waitUntilReady();
    fireEvent.click(screen.getByRole("button", { name: "Propose new search terms" }));
    fireEvent.click(screen.getByRole("button", { name: "Propose search terms" }));

    expect(await screen.findByText(/the provider refused the key/)).toBeTruthy();
    expect(enqueued).toContainEqual({
      workspace_id: "workspace-1",
      task_type: "search.vocabulary",
      payload: {},
    });
  });
});
