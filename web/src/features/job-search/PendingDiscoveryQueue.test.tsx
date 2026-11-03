import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type {
  PendingDiscovery,
  PendingDiscoveryFacets,
  RelevanceReview,
  RelevanceReviewConfiguration,
  RunCostHistory,
  Task,
} from "../../api/client";
import type { components } from "../../api/schema";
import { AppProviders } from "../../providers";
import { dispositionHandlers } from "../../test/msw/pendingDiscoveryHandlers";
import { server } from "../../test/msw/server";
import { evaluationPreflightHandler, TASK_TYPE_CATALOG } from "../../test/taskTypes";
import { jobDetail } from "../job-detail/jobDetailFixtures";
import { DEFAULT_QUEUE_FILTERS } from "./pendingDiscoveryFilterModel";
import type { PendingQueueFilters } from "./pendingDiscoveryFilterModel";
import { dispositionTarget, queueFacets } from "./pendingDiscoveryFixtures";
import { PendingDiscoveryQueue } from "./PendingDiscoveryQueue";

const WORKSPACE_ID = "workspace-1";
const emptyTaskList = [] satisfies Task[];
const emptyCostHistory = [] satisfies RunCostHistory[];
const reviewCostHistory = [
  {
    task_type: "discovery.relevance_review",
    currency: "USD",
    unit: "call",
    measured_runs: 25,
    unpriced_runs: 0,
    replayed_runs: 0,
    per_unit_low: "0.09",
    per_unit_median: "0.15",
    per_unit_high: "0.22",
  },
] satisfies RunCostHistory[];
const evaluationCostHistory = [
  {
    task_type: "discovery.evaluate_batch",
    currency: "USD",
    unit: "posting",
    measured_runs: 12,
    unpriced_runs: 0,
    replayed_runs: 0,
    per_unit_low: "0.20",
    per_unit_median: "0.30",
    per_unit_high: "0.40",
  },
] satisfies RunCostHistory[];
const emptyDismissedDiscoveries =
  [] satisfies components["schemas"]["DismissedDiscoveryResponse"][];
const relevanceReviewConfiguration = {
  model_choice: { effort: null, model: "claude-review", service_tier: "standard" },
  provider: "anthropic",
  batch_rows: 20,
} satisfies RelevanceReviewConfiguration;

function queuedTask(body: components["schemas"]["TaskCreate"]): Task {
  return {
    attempt_count: 0,
    cancellation_requested_at: null,
    completed_at: null,
    created_at: "2026-09-16T00:00:00Z",
    error_category: null,
    error_message: null,
    id: "task-1",
    job_posting_ids: [],
    pending_discovery_ids: (body.payload.pending_discovery_ids as string[] | undefined) ?? [],
    source_ids: [],
    progress: {},
    started_at: null,
    status: "queued",
    task_type: body.task_type,
    updated_at: "2026-09-16T00:00:00Z",
    workspace_id: body.workspace_id,
  };
}

function completedTask(overrides: Partial<Task>): Task {
  return {
    attempt_count: 1,
    cancellation_requested_at: null,
    completed_at: "2026-09-16T00:05:00Z",
    created_at: "2026-09-16T00:00:00Z",
    error_category: null,
    error_message: null,
    id: "task-eval",
    job_posting_ids: [],
    pending_discovery_ids: [],
    source_ids: [],
    progress: {},
    started_at: "2026-09-16T00:01:00Z",
    status: "completed",
    task_type: "discovery.evaluate",
    updated_at: "2026-09-16T00:05:00Z",
    workspace_id: WORKSPACE_ID,
    ...overrides,
  };
}

function installHandlers(
  submitted: components["schemas"]["TaskCreate"][],
  tasks: Task[] = [],
  costHistory: RunCostHistory[] = emptyCostHistory,
) {
  server.use(
    evaluationPreflightHandler(),
    http.get("*/api/v1/tasks/types", () => HttpResponse.json(TASK_TYPE_CATALOG)),
    http.get("*/api/v1/tasks", () => HttpResponse.json(tasks.length ? tasks : emptyTaskList)),
    http.get("*/api/v1/workspaces/:workspaceId/spend-history", () =>
      HttpResponse.json(costHistory),
    ),
    http.get("*/api/v1/workspaces/:workspaceId/relevance-review-configuration", () =>
      HttpResponse.json(relevanceReviewConfiguration),
    ),
    http.get("*/api/v1/workspaces/:workspaceId/pending-dispositions/dismissed", () =>
      HttpResponse.json(emptyDismissedDiscoveries),
    ),
    http.post("*/api/v1/tasks", async ({ request }) => {
      const body = (await request.json()) as components["schemas"]["TaskCreate"];
      submitted.push(body);
      return HttpResponse.json(queuedTask(body));
    }),
  );
}

function discovery(id: string, title: string, withdrawnAt: string | null = null): PendingDiscovery {
  return {
    also_in: [],
    arrangement_categories: ["remote"],
    arrangement_categories_source: "published",
    canonical_url: `https://jobs.example.test/${id}`,
    constraint_confidence: "investigate",
    content_note: "",
    content_status: "readable",
    current_robots_basis: null,
    discovery_url: `https://jobs.example.test/${id}`,
    employer: "Globex",
    first_seen_on: "2026-09-16",
    id,
    favorite: false,
    kept: false,
    keep_note: null,
    location: "Remote",
    observed_publisher_employer: "Globex",
    observed_publisher_title: title,
    rank: 1,
    ranked_because: [],
    readiness: "ready",
    reason: "Synthetic board read.",
    recommendation: "investigate",
    recommendation_because: "Needs evaluation.",
    retained_content_id: `content-${id}`,
    screening_findings: [],
    screening_standing: "unknown",
    staged_facts: {
      arrangements: { complete: true, provenance: "publisher", values: ["Remote"] },
      compensation: { complete: false, provenance: "", values: [] },
      employment_types: { complete: false, provenance: "", values: [] },
      freshness: "unknown",
      identity_trust: "publisher",
      locations: { complete: true, provenance: "publisher", values: ["Remote"] },
      schema_version: 5,
    },
    title,
    withdrawn_at: withdrawnAt,
    staging_provenance: "board_scan",
  };
}

function review(pendingDiscoveryId: string): RelevanceReview {
  return {
    core_qualification_alignment: "established",
    core_work_alignment: "direct",
    decisive_basis: "core_match",
    employer: "Globex",
    fetched_at: "2026-09-16T00:00:00Z",
    hard_condition: "passes",
    id: `review-${pendingDiscoveryId}`,
    master_cv_evidence_ids: ["evidence-1"],
    master_cv_version_id: "cv-current",
    matched_exclusion: null,
    outcome: "judged",
    pending_discovery_id: pendingDiscoveryId,
    out_of_date_because: [],
    posting_citations: [],
    recommendation: "review",
    reviewed_at: "2026-09-16T00:00:00Z",
    search_parameter_set_id: "parameters-current",
    summary: "Current relevance review.",
    task_id: "task-review",
    title: "Role 1",
    unresolved_questions: [],
  };
}

function staleReview(
  pendingDiscoveryId: string,
  recommendation: NonNullable<RelevanceReview["recommendation"]>,
  outOfDateBecause: RelevanceReview["out_of_date_because"] = ["master_cv"],
): RelevanceReview {
  return { ...review(pendingDiscoveryId), out_of_date_because: outOfDateBecause, recommendation };
}

type QueueProps = {
  strictUncertainty?: boolean;
  discoveries: PendingDiscovery[];
  facets?: PendingDiscoveryFacets;
  filters?: PendingQueueFilters;
  onFiltersChange?: (next: PendingQueueFilters) => void;
  onSearchTextChange?: (value: string) => void;
  queueTotal?: number;
  relevanceReviews?: ReadonlyMap<string, RelevanceReview>;
  selectionScope?: string;
};

function queue({
  discoveries,
  facets = queueFacets(discoveries),
  filters = DEFAULT_QUEUE_FILTERS,
  onFiltersChange = () => undefined,
  onSearchTextChange = () => undefined,
  queueTotal = discoveries.length,
  relevanceReviews = new Map(),
  selectionScope = "snapshot-a",
  strictUncertainty = false,
}: QueueProps) {
  return (
    <MemoryRouter>
      <AppProviders>
        <PendingDiscoveryQueue
          countPending={false}
          discoveries={discoveries}
          employers={[]}
          facets={facets}
          filters={filters}
          focusPendingId={null}
          laneStatus={{
            ready: "Ready to evaluate: rows 1-1 of 1 matching.",
            attention: null,
          }}
          loaded
          matching={discoveries.length}
          onAdded={() => undefined}
          onFiltersChange={onFiltersChange}
          onFocusRestored={() => undefined}
          onPageSizeChange={() => undefined}
          onSearchTextChange={onSearchTextChange}
          pageSize="50"
          queueTotal={queueTotal}
          relevanceReviews={relevanceReviews}
          searchText={filters.search}
          selectionScope={selectionScope}
          settled
          strictUncertainty={strictUncertainty}
          onStrictUncertaintyChange={() => undefined}
          workspaceId={WORKSPACE_ID}
        />
      </AppProviders>
    </MemoryRouter>
  );
}

function selectedActions() {
  return screen.getByRole("region", { name: "Selected discovery actions" });
}

function selectAllCheckbox() {
  return screen.getByLabelText<HTMLInputElement>("Select eligible discoveries on this page");
}

describe("PendingDiscoveryQueue selection", () => {
  it("routes readable screened out rows for attention without submitting work or dismissing them", async () => {
    const submitted: components["schemas"]["TaskCreate"][] = [];
    const dismissals: string[] = [];
    const changes: PendingQueueFilters[] = [];
    installHandlers(submitted);
    server.use(
      http.post("*/api/v1/workspaces/:workspaceId/pending-dispositions/*", ({ request }) => {
        dismissals.push(request.url);
        return HttpResponse.json({});
      }),
    );
    const refused = {
      ...discovery("pending-1", "Refused role"),
      constraint_confidence: "excluded",
      readiness: "screening_refused",
      screening_standing: "refuse",
    } satisfies PendingDiscovery;
    const filters = { ...DEFAULT_QUEUE_FILTERS, constraint: "excluded_only" } as const;
    render(
      queue({ discoveries: [refused], filters, onFiltersChange: (next) => changes.push(next) }),
    );
    const attention = await screen.findByRole("region", { name: "Postings needing attention" });
    expect(
      within(attention).getByText("1 of 1 matching in any content state need attention"),
    ).toBeTruthy();
    fireEvent.click(
      within(attention).getByRole("button", { name: "Screened out: review your rules (1)" }),
    );
    expect(changes).toEqual([{ ...filters, readiness: "screening_refused" }]);
    expect(screen.getByRole("button", { name: "Track job" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Dismiss from pending" })).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Refused role at Globex" }));
    expect(
      screen.getByText(
        "1 selected row conflicts with your search rules; review your rules before either paid action.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/a refresh before either paid action/)).toBeNull();
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "Full evaluation (1)" }).disabled,
    ).toBe(true);
    expect(submitted).toEqual([]);
    expect(dismissals).toEqual([]);
  });

  it("tracks selected identities across pages for free and retries only failed rows", async () => {
    const submitted: components["schemas"]["TaskCreate"][] = [];
    const tracked: string[] = [];
    const preflights: Request[] = [];
    const providerRequests: Request[] = [];
    let failSecond = true;
    let finishFirst!: () => void;
    const firstFinished = new Promise<void>((resolve) => {
      finishFirst = resolve;
    });
    installHandlers(submitted);
    server.use(
      evaluationPreflightHandler((request) => preflights.push(request)),
      http.all("*/api/v1/providers/*", ({ request }) => {
        providerRequests.push(request);
        return HttpResponse.json({ detail: "No provider key configured." }, { status: 403 });
      }),
      http.post(
        "*/api/v1/workspaces/:workspaceId/pending-discoveries/:pendingId/track",
        async ({ params }) => {
          const id = String(params.pendingId);
          tracked.push(id);
          if (id === "pending-2" && failSecond) {
            return HttpResponse.json(
              { detail: "The saved posting changed. Try again." },
              { status: 409 },
            );
          }
          if (id === "pending-1") await firstFinished;
          return HttpResponse.json(jobDetail({ job_posting_id: `job-${id}` }));
        },
      ),
    );
    const first = discovery("pending-1", "Role 1");
    const second = discovery("pending-2", "Role 2");
    const view = render(queue({ discoveries: [first], queueTotal: 2 }));
    fireEvent.click(await screen.findByLabelText("Select Role 1 at Globex"));
    view.rerender(queue({ discoveries: [second], queueTotal: 2 }));
    fireEvent.click(screen.getByLabelText("Select Role 2 at Globex"));
    fireEvent.click(within(selectedActions()).getByRole("button", { name: "Track selected (2)" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).toContain("including 1 selected on other pages");
    expect(within(dialog).getByText("Role 1 at Globex")).toBeTruthy();
    expect(within(dialog).getByText("Role 2 at Globex")).toBeTruthy();
    expect(dialog.textContent).toContain("No provider key or evaluation is needed.");
    fireEvent.click(within(dialog).getByRole("button", { name: "Track 2 jobs" }));

    await waitFor(() => expect(tracked).toHaveLength(1));
    expect(
      within(selectedActions())
        .getByRole<HTMLButtonElement>("button", {
          name: "Tracking selected jobs",
        })
        .getAttribute("aria-disabled"),
    ).toBe("true");
    await act(async () => {
      finishFirst();
      await firstFinished;
    });
    const outcomes = await screen.findByRole("region", { name: "Tracking outcomes" });
    await waitFor(() =>
      expect(within(outcomes).getByRole("alert").textContent).toContain(
        "The saved posting changed. Try again.",
      ),
    );
    expect(within(outcomes).getByRole("status").textContent).toContain("Tracked Role 1 at Globex.");
    expect(within(selectedActions()).getByText("1 selected")).toBeTruthy();
    expect(
      screen.getByRole<HTMLInputElement>("checkbox", { name: "Select Role 2 at Globex" }).checked,
    ).toBe(true);

    view.rerender(queue({ discoveries: [discovery("pending-3", "Role 3")], queueTotal: 2 }));
    expect(within(outcomes).getByRole("alert").textContent).toContain("Role 2 at Globex");
    failSecond = false;
    fireEvent.click(within(selectedActions()).getByRole("button", { name: "Track selected (1)" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Track 1 job",
      }),
    );
    await waitFor(() => expect(within(outcomes).queryByRole("alert")).toBeNull());
    expect(tracked.filter((id) => id === "pending-1")).toHaveLength(1);
    expect(tracked.filter((id) => id === "pending-2")).toHaveLength(2);
    expect(within(selectedActions()).getByText("0 selected")).toBeTruthy();
    expect(submitted).toEqual([]);
    expect(preflights).toEqual([]);
    expect(providerRequests).toEqual([]);
    view.rerender(queue({ discoveries: [], queueTotal: 0 }));
    expect(
      within(screen.getByRole("region", { name: "Tracking outcomes" })).getAllByRole("status"),
    ).toHaveLength(2);
  });

  it("does not track a selection hidden by a changed filter scope during confirmation", async () => {
    const submitted: components["schemas"]["TaskCreate"][] = [];
    const tracked: string[] = [];
    installHandlers(submitted);
    server.use(
      http.post(
        "*/api/v1/workspaces/:workspaceId/pending-discoveries/:pendingId/track",
        ({ params }) => {
          tracked.push(String(params.pendingId));
          return HttpResponse.json(jobDetail());
        },
      ),
    );
    const view = render(queue({ discoveries: [discovery("pending-1", "Role 1")] }));
    fireEvent.click(await screen.findByLabelText("Select Role 1 at Globex"));
    fireEvent.click(within(selectedActions()).getByRole("button", { name: "Track selected (1)" }));
    await screen.findByRole("dialog");
    view.rerender(
      queue({ discoveries: [discovery("pending-2", "Role 2")], selectionScope: "snapshot-b" }),
    );
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Track 1 job" }),
    );
    await screen.findByText("Selection changed. Select the jobs again before tracking.");
    expect(tracked).toEqual([]);
    expect(within(selectedActions()).getByText("0 selected")).toBeTruthy();
    expect(within(selectedActions()).queryByRole("button", { name: /Track selected/ })).toBeNull();
    expect(submitted).toEqual([]);
  });

  it("selects only eligible discoveries on the current page", async () => {
    const submitted: components["schemas"]["TaskCreate"][] = [];
    installHandlers(submitted);
    render(queue({ discoveries: [discovery("pending-1", "Role 1")], queueTotal: 8 }));
    fireEvent.click(
      await screen.findByRole("checkbox", { name: "Select eligible discoveries on this page" }),
    );
    expect(within(selectedActions()).getByText("1 selected")).toBeTruthy();
    fireEvent.click(within(selectedActions()).getByRole("button", { name: "Track selected (1)" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Role 1 at Globex")).toBeTruthy();
    expect(within(dialog).getByRole("button", { name: "Track 1 job" })).toBeTruthy();
  });

  it("submits selections retained across pages in one full-evaluation task", async () => {
    const submitted: components["schemas"]["TaskCreate"][] = [];
    installHandlers(submitted);
    const first = discovery("pending-1", "Role 1");
    const second = discovery("pending-2", "Role 2");
    const view = render(queue({ discoveries: [first] }));

    fireEvent.click(await screen.findByLabelText("Select Role 1 at Globex"));
    view.rerender(queue({ discoveries: [second] }));
    fireEvent.click(await screen.findByLabelText("Select Role 2 at Globex"));

    const actions = selectedActions();
    expect(within(actions).getByText("2 selected")).toBeTruthy();
    const evaluate = within(actions).getByRole<HTMLButtonElement>("button", {
      name: "Full evaluation (2)",
    });
    await waitFor(() => expect(evaluate.disabled).toBe(false));
    fireEvent.click(evaluate);
    const confirmation = within(await screen.findByRole("dialog"));
    fireEvent.click(confirmation.getByRole("button", { name: "Run 2 full evaluations" }));

    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(submitted[0]?.payload.pending_discovery_ids).toEqual(
      expect.arrayContaining(["pending-1", "pending-2"]),
    );
  });

  it("offers one relevance action and one refresh for the whole selection", async () => {
    const submitted: components["schemas"]["TaskCreate"][] = [];
    installHandlers(submitted);
    const first = discovery("pending-1", "Role 1");
    render(
      queue({ discoveries: [first], relevanceReviews: new Map([[first.id, review(first.id)]]) }),
    );

    fireEvent.click(await screen.findByLabelText("Select Role 1 at Globex"));

    const actions = selectedActions();
    const relevance = within(actions).getByRole<HTMLButtonElement>("button", {
      name: "Review relevance (1)",
    });
    await waitFor(() => expect(relevance.disabled).toBe(false));
    expect(within(actions).getAllByRole("button", { name: /relevance/i })).toHaveLength(1);
    expect(within(actions).getByRole("button", { name: "Refresh posting" })).toBeTruthy();
    expect(within(actions).getAllByRole("button", { name: /^Refresh/ })).toHaveLength(1);
  });

  it("requests a new review of a row that already carries a current review", async () => {
    const submitted: components["schemas"]["TaskCreate"][] = [];
    installHandlers(submitted);
    const first = discovery("pending-1", "Role 1");
    render(
      queue({ discoveries: [first], relevanceReviews: new Map([[first.id, review(first.id)]]) }),
    );

    fireEvent.click(await screen.findByLabelText("Select Role 1 at Globex"));
    const relevance = within(selectedActions()).getByRole<HTMLButtonElement>("button", {
      name: "Review relevance (1)",
    });
    await waitFor(() => expect(relevance.disabled).toBe(false));
    fireEvent.click(relevance);

    const confirmation = within(await screen.findByRole("dialog"));
    expect((await screen.findByRole("dialog")).textContent).toContain(
      "Reviews all 1 against your CV and search parameters, including rows already reviewed.",
    );
    fireEvent.click(confirmation.getByRole("button", { name: "Review selected relevance" }));

    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(submitted[0]).toEqual({
      workspace_id: WORKSPACE_ID,
      task_type: "discovery.relevance_review",
      payload: { pending_discovery_ids: ["pending-1"] },
    });
  });

  it("prices a relevance review by the rows selected, not by the run", async () => {
    const submitted: components["schemas"]["TaskCreate"][] = [];
    installHandlers(submitted, [], reviewCostHistory);
    const first = discovery("pending-0", "Role 0");
    const rows = [
      first,
      ...Array.from({ length: 3 }, (_, index) =>
        discovery(`pending-${index + 1}`, `Role ${index + 1}`),
      ),
    ];
    const one = render(queue({ discoveries: [first] }));

    fireEvent.click(await screen.findByLabelText("Select Role 0 at Globex"));
    fireEvent.click(
      within(selectedActions()).getByRole("button", { name: "Review relevance (1)" }),
    );
    expect((await screen.findByRole("dialog")).textContent).toContain(
      "About $0.15 for 1 call (range $0.09 to $0.22, from 25 runs).",
    );
    one.unmount();

    render(queue({ discoveries: rows }));
    fireEvent.click(await screen.findByLabelText("Select eligible discoveries on this page"));
    fireEvent.click(
      within(selectedActions()).getByRole("button", { name: "Review relevance (4)" }),
    );
    expect((await screen.findByRole("dialog")).textContent).toContain(
      "About $0.15 for 1 call (range $0.09 to $0.22, from 25 runs).",
    );
  });

  it("floors the review call count by the batch size the server reports", async () => {
    const submitted: components["schemas"]["TaskCreate"][] = [];
    installHandlers(submitted, [], reviewCostHistory);
    server.use(
      http.get("*/api/v1/workspaces/:workspaceId/relevance-review-configuration", () =>
        HttpResponse.json({ ...relevanceReviewConfiguration, batch_rows: 5 }),
      ),
    );
    const rows = Array.from({ length: 6 }, (_, index) =>
      discovery(`pending-${index}`, `Role ${index}`),
    );
    render(queue({ discoveries: rows }));

    fireEvent.click(await screen.findByLabelText("Select eligible discoveries on this page"));
    fireEvent.click(
      within(selectedActions()).getByRole("button", { name: "Review relevance (6)" }),
    );
    expect((await screen.findByRole("dialog")).textContent).toContain("for 2 calls");
  });

  it("clears selection when the queue snapshot changes", async () => {
    const submitted: components["schemas"]["TaskCreate"][] = [];
    installHandlers(submitted);
    const first = discovery("pending-1", "Role 1");
    const second = discovery("pending-2", "Role 2");
    const view = render(queue({ discoveries: [first], selectionScope: "snapshot-a" }));

    fireEvent.click(await screen.findByLabelText("Select Role 1 at Globex"));
    view.rerender(queue({ discoveries: [second], selectionScope: "snapshot-b" }));

    await waitFor(() => expect(within(selectedActions()).getByText("0 selected")).toBeTruthy());
  });

  it("selects every filtered row when the lane fits one action", async () => {
    const submitted: components["schemas"]["TaskCreate"][] = [];
    installHandlers(submitted, [], evaluationCostHistory);
    const lane = Array.from({ length: 15 }, (_, index) =>
      discovery(`pending-${index}`, `Role ${index}`),
    );
    render(queue({ discoveries: lane }));

    expect(await screen.findByText("Select up to 250.")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Select eligible discoveries on this page"));

    const actions = selectedActions();
    expect(within(actions).getByText("15 selected")).toBeTruthy();
    const evaluate = within(actions).getByRole<HTMLButtonElement>("button", {
      name: "Full evaluation (15)",
    });
    await waitFor(() => expect(evaluate.disabled).toBe(false));
    // The button prices the batch task it enqueues, from that task's measured history.
    expect(within(evaluate).getByText("Past runs: about $4.50 total")).toBeTruthy();
    fireEvent.click(evaluate);
    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).toContain("One task evaluates all 15 selected discoveries");
    expect(dialog.textContent).toContain(
      "These are the 15 rows you selected, including any selected on other pages.",
    );
    expect(dialog.textContent).toContain("Estimated total at configured rates: $0.2000 USD.");
  });

  it("holds the selection at the bound while it grows across pages", async () => {
    const submitted: components["schemas"]["TaskCreate"][] = [];
    installHandlers(submitted);
    const held = Array.from({ length: 250 }, (_, index) =>
      discovery(`pending-${index}`, `Role ${index}`),
    );
    const view = render(queue({ discoveries: held }));
    await screen.findByText("Select up to 250.");
    fireEvent.click(selectAllCheckbox());
    expect(within(selectedActions()).getByText("250 selected")).toBeTruthy();

    view.rerender(queue({ discoveries: [discovery("pending-250", "Role 250")] }));
    fireEvent.click(screen.getByLabelText("Select Role 250 at Globex"));

    expect(within(selectedActions()).getByText("250 selected")).toBeTruthy();
    expect(screen.getByText("Select no more than 250 postings.")).toBeTruthy();
  });

  it("refuses select-all when the held selection and this page would pass the bound", async () => {
    const submitted: components["schemas"]["TaskCreate"][] = [];
    installHandlers(submitted);
    const pageOf = (page: number, size: number) =>
      Array.from({ length: size }, (_, index) =>
        discovery(`pending-${page}-${index}`, `Role ${page}-${index}`),
      );
    const view = render(queue({ discoveries: pageOf(0, 230) }));
    await screen.findByText("Select up to 250.");
    fireEvent.click(selectAllCheckbox());
    expect(within(selectedActions()).getByText("230 selected")).toBeTruthy();

    view.rerender(queue({ discoveries: pageOf(1, 40) }));

    const selectAll = selectAllCheckbox();
    expect(selectAll.disabled).toBe(true);
    expect(
      screen.getByText(
        "Select at most 250; 230 already selected and 40 more of the rows loaded on this page match the filters. " +
          "Act on those 230 or clear them first.",
      ),
    ).toBeTruthy();
    expect(within(selectedActions()).getByText("230 selected")).toBeTruthy();
  });

  it("removes a selected row when it becomes withdrawn", async () => {
    const submitted: components["schemas"]["TaskCreate"][] = [];
    installHandlers(submitted);
    const active = discovery("pending-1", "Role 1");
    const withdrawn = discovery("pending-1", "Role 1", "2026-09-16T00:00:00Z");
    const view = render(queue({ discoveries: [active] }));

    fireEvent.click(await screen.findByLabelText("Select Role 1 at Globex"));
    view.rerender(queue({ discoveries: [withdrawn] }));

    await waitFor(() => expect(within(selectedActions()).getByText("0 selected")).toBeTruthy());
  });

  async function submitEvaluation(selectedCount: number) {
    const actions = selectedActions();
    const evaluate = within(actions).getByRole<HTMLButtonElement>("button", {
      name: `Full evaluation (${selectedCount})`,
    });
    await waitFor(() => expect(evaluate.disabled).toBe(false));
    fireEvent.click(evaluate);
    const confirmation = within(await screen.findByRole("dialog"));
    fireEvent.click(
      confirmation.getByRole("button", {
        name: `Run ${selectedCount} full ${selectedCount === 1 ? "evaluation" : "evaluations"}`,
      }),
    );
  }

  it("drops an off-page row a completed normal single evaluation already evaluated", async () => {
    const submitted: components["schemas"]["TaskCreate"][] = [];
    const first = discovery("pending-1", "Role 1");
    const second = discovery("pending-2", "Role 2");
    installHandlers(submitted, [
      completedTask({
        pending_discovery_ids: ["pending-1"],
        progress: {
          job_posting_id: "job-99",
          evaluation_id: "evaluation-1",
          evaluation_version: 1,
          decision: "worth_applying",
        },
      }),
    ]);
    const view = render(queue({ discoveries: [first] }));

    fireEvent.click(await screen.findByLabelText("Select Role 1 at Globex"));
    view.rerender(queue({ discoveries: [second] }));
    fireEvent.click(await screen.findByLabelText("Select Role 2 at Globex"));

    await waitFor(() => expect(within(selectedActions()).getByText("1 selected")).toBeTruthy());
    await submitEvaluation(1);

    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(submitted[0]?.payload.pending_discovery_ids).toEqual(["pending-2"]);
  });

  it("drops an off-page row a completed evaluate-batch task already evaluated", async () => {
    const submitted: components["schemas"]["TaskCreate"][] = [];
    const first = discovery("pending-1", "Role 1");
    const second = discovery("pending-2", "Role 2");
    installHandlers(submitted, [
      completedTask({
        task_type: "discovery.evaluate_batch",
        pending_discovery_ids: ["pending-1"],
        progress: {
          selected: 1,
          evaluated: 1,
          skipped: 0,
          skipped_rows: [],
          failed: 0,
          failures: [],
          results: [
            {
              job_posting_id: "job-99",
              evaluation_id: "evaluation-1",
              evaluation_version: 1,
              decision: "worth_applying",
            },
          ],
        },
      }),
    ]);
    const view = render(queue({ discoveries: [first] }));

    fireEvent.click(await screen.findByLabelText("Select Role 1 at Globex"));
    view.rerender(queue({ discoveries: [second] }));
    fireEvent.click(await screen.findByLabelText("Select Role 2 at Globex"));

    await waitFor(() => expect(within(selectedActions()).getByText("1 selected")).toBeTruthy());
    await submitEvaluation(1);

    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(submitted[0]?.payload.pending_discovery_ids).toEqual(["pending-2"]);
  });

  it("keeps an off-page row an evaluate-batch task skipped as unreadable", async () => {
    const submitted: components["schemas"]["TaskCreate"][] = [];
    const first = discovery("pending-1", "Role 1");
    const second = discovery("pending-2", "Role 2");
    installHandlers(submitted, [
      completedTask({
        task_type: "discovery.evaluate_batch",
        pending_discovery_ids: ["pending-1"],
        progress: {
          selected: 1,
          evaluated: 0,
          skipped: 1,
          skipped_rows: [
            { pending_discovery_id: "pending-1", skipped: "unreadable", detail: "no content" },
          ],
          failed: 0,
          failures: [],
          results: [],
        },
      }),
    ]);
    const view = render(queue({ discoveries: [first] }));

    fireEvent.click(await screen.findByLabelText("Select Role 1 at Globex"));
    view.rerender(queue({ discoveries: [second] }));
    fireEvent.click(await screen.findByLabelText("Select Role 2 at Globex"));

    await waitFor(() => expect(within(selectedActions()).getByText("2 selected")).toBeTruthy());
    await submitEvaluation(2);

    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(submitted[0]?.payload.pending_discovery_ids).toEqual(
      expect.arrayContaining(["pending-1", "pending-2"]),
    );
  });

  it("collapses the selection panel to one line until a row is selected", async () => {
    installHandlers([]);
    render(queue({ discoveries: [discovery("pending-1", "Role 1")] }));

    await screen.findByLabelText("Select Role 1 at Globex");
    const actions = selectedActions();
    expect(actions.textContent).toBe("0 selected. Select up to 250.");
    expect(within(actions).queryByRole("button")).toBeNull();

    fireEvent.click(screen.getByLabelText("Select Role 1 at Globex"));

    expect(within(actions).getByText("1 selected")).toBeTruthy();
    expect(within(actions).getByRole("button", { name: "Full evaluation (1)" })).toBeTruthy();
    expect(within(actions).getByRole("button", { name: "Refresh posting" })).toBeTruthy();
  });

  it("keeps an off-page selection until the row is visible again and confirmed withdrawn", async () => {
    const submitted: components["schemas"]["TaskCreate"][] = [];
    installHandlers(submitted);
    const active = discovery("pending-1", "Role 1");
    const second = discovery("pending-2", "Role 2");
    const withdrawn = discovery("pending-1", "Role 1", "2026-09-16T00:10:00Z");
    const view = render(queue({ discoveries: [active] }));

    fireEvent.click(await screen.findByLabelText("Select Role 1 at Globex"));
    view.rerender(queue({ discoveries: [second] }));
    expect(within(selectedActions()).getByText("1 selected")).toBeTruthy();

    view.rerender(queue({ discoveries: [withdrawn] }));
    await waitFor(() => expect(within(selectedActions()).getByText("0 selected")).toBeTruthy());
  });
});

describe("PendingDiscoveryQueue re-screen", () => {
  it("queues one free re-screen task and reports what it did to the queue", async () => {
    const submitted: components["schemas"]["TaskCreate"][] = [];
    installHandlers(submitted);
    server.use(
      http.get("*/api/v1/tasks/task-1", () =>
        HttpResponse.json(
          completedTask({
            id: "task-1",
            task_type: "discovery.rescreen_queue",
            progress: { screened: 4, held_out: 1, vanished: 1, skipped_for_paid_task: 0 },
          }),
        ),
      ),
    );
    render(queue({ discoveries: [discovery("pending-1", "Role 1")] }));

    fireEvent.click(await screen.findByText("Queue maintenance"));
    fireEvent.click(screen.getByRole("button", { name: "Re-screen queue" }));
    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByText(/fetches nothing.*costs nothing/)).toBeTruthy();
    fireEvent.click(dialog.getByRole("button", { name: "Re-screen queue" }));

    expect(
      await screen.findByText("Re-screened 4 discoveries and held out 1. 1 left the queue first."),
    ).toBeTruthy();
    expect(submitted).toEqual([
      { workspace_id: WORKSPACE_ID, task_type: "discovery.rescreen_queue", payload: {} },
    ]);
  });

  it("shows the running count and offers no second re-screen while one runs", async () => {
    const running = completedTask({
      id: "task-rescreen",
      task_type: "discovery.rescreen_queue",
      status: "running",
      completed_at: null,
      progress: { current: 3, total: 40, held_out: 1 },
    });
    installHandlers([], [running]);
    server.use(http.get("*/api/v1/tasks/task-rescreen", () => HttpResponse.json(running)));
    render(queue({ discoveries: [discovery("pending-1", "Role 1")] }));

    expect(
      await screen.findByText("Re-screening 3 of 40 discoveries; 1 held out so far."),
    ).toBeTruthy();
    fireEvent.click(screen.getByText("Queue maintenance"));
    const button = screen.getByRole<HTMLButtonElement>("button", { name: "Re-screening queue" });
    expect(button.disabled).toBe(true);
  });

  it("prints the server's own sentence when no worker can run the re-screen", async () => {
    const sentence =
      "The worker is not running. Start it with 'clearscout worker' and queue the work again.";
    installHandlers([]);
    server.use(
      http.post("*/api/v1/tasks", () => HttpResponse.json({ detail: sentence }, { status: 503 })),
    );
    render(queue({ discoveries: [discovery("pending-1", "Role 1")] }));

    fireEvent.click(await screen.findByText("Queue maintenance"));
    fireEvent.click(screen.getByRole("button", { name: "Re-screen queue" }));
    const dialog = within(await screen.findByRole("dialog"));
    fireEvent.click(dialog.getByRole("button", { name: "Re-screen queue" }));

    fireEvent.click(await screen.findByRole("button", { name: "Dismiss notification" }));

    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Dismiss notification" })).toBeNull(),
    );
    expect(screen.getByRole("alert").textContent).toBe(sentence);
  });
});

describe("PendingDiscoveryQueue header actions", () => {
  it("keeps Add a posting in view and puts re-screen and reset in Queue maintenance", async () => {
    installHandlers([]);
    const resets: Request[] = [];
    server.use(
      http.post("*/api/v1/workspaces/:workspaceId/pending-discoveries/reset", ({ request }) => {
        resets.push(request);
        return HttpResponse.json({ archived_count: 0, kept_count: 0, rescreened_count: 0 });
      }),
    );
    render(queue({ discoveries: [discovery("pending-1", "Role 1")] }));

    const menu = (await screen.findByText("Queue maintenance")).closest("details");
    if (!menu) throw new Error("Expected the maintenance menu.");
    expect(screen.getByText("Add a posting").closest("details")).not.toBe(menu);
    expect(menu.open).toBe(false);

    fireEvent.click(screen.getByText("Queue maintenance"));
    expect(menu.open).toBe(true);
    expect(within(menu).getByRole("button", { name: "Re-screen queue" })).toBeTruthy();
    fireEvent.click(within(menu).getByRole("button", { name: "Reset pending queue" }));

    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByText("Reset the pending queue?")).toBeTruthy();
    fireEvent.click(dialog.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(resets).toHaveLength(0);
  });
});

describe("PendingDiscoveryQueue out-of-date verdicts", () => {
  it("shows the server's count of out-of-date verdicts beside each verdict", async () => {
    installHandlers([]);
    const reviewed = discovery("pending-1", "Role 1");
    const unreviewed = discovery("pending-2", "Role 2");
    render(
      queue({
        discoveries: [reviewed, unreviewed],
        facets: {
          ...queueFacets([reviewed, unreviewed]),
          relevance: [
            { value: "unlikely", count: 1, out_of_date: 1 },
            { value: "not_reviewed", count: 1, out_of_date: 0 },
          ],
        },
        relevanceReviews: new Map([["pending-1", staleReview("pending-1", "unlikely")]]),
      }),
    );

    const verdicts = await screen.findByLabelText("Relevance verdict");
    expect(within(verdicts).getByText("Low relevance (1, 1 out of date)")).toBeTruthy();
    expect(within(verdicts).getByText("Not yet reviewed (1)")).toBeTruthy();
  });

  it("badges an out-of-date verdict carried on the discovery itself", async () => {
    installHandlers([]);
    const carried = {
      ...discovery("pending-1", "Role 1"),
      relevance_review: staleReview("pending-1", "unlikely"),
    } satisfies PendingDiscovery;
    render(queue({ discoveries: [carried] }));

    const row = within(await screen.findByRole("row", { name: /Role 1/ }));
    expect(row.getByText("Low relevance")).toBeTruthy();
    expect(row.getByText("Out of date")).toBeTruthy();
  });

  it("lists the server's review-status counts for each input that changed", async () => {
    installHandlers([]);
    const rows = [discovery("pending-1", "Role 1"), discovery("pending-2", "Role 2")];
    render(
      queue({
        discoveries: rows,
        facets: {
          ...queueFacets(rows),
          review_status: [
            { value: "out_of_date", count: 2 },
            { value: "out_of_date_master_cv", count: 1 },
            { value: "out_of_date_search_parameters", count: 1 },
            { value: "out_of_date_review_instructions", count: 1 },
            { value: "out_of_date_keep_note", count: 0 },
            { value: "current", count: 1 },
          ],
          review_status_total: 4,
        },
      }),
    );

    const status = await screen.findByLabelText("Review status");
    const optionNames = within(status)
      .getAllByRole("option")
      .map((option) => option.textContent);
    expect(optionNames).toEqual([
      "All review states (4)",
      "Review out of date (2)",
      "Out of date: master CV (1)",
      "Out of date: search parameters (1)",
      "Out of date: review instructions (1)",
      "Out of date: your keep note (0)",
      "Review current (1)",
    ]);
  });
});

describe("PendingDiscoveryQueue origins", () => {
  it("says on the row which origin staged it, and says so when nothing recorded one", async () => {
    installHandlers([]);
    const paid = {
      ...discovery("pending-1", "Role 1"),
      staging_provenance: "guided_campaign",
    } satisfies PendingDiscovery;
    const personAdded = {
      ...discovery("pending-2", "Role 2"),
      reason: "You added this posting from a link you found.",
      staging_provenance: "person_added",
    } satisfies PendingDiscovery;
    const unknown = {
      ...discovery("pending-3", "Role 3"),
      reason: "Staged before the queue recorded origins.",
      staging_provenance: "unrecorded",
    } satisfies PendingDiscovery;
    render(queue({ discoveries: [paid, personAdded, unknown] }));

    const campaignRow = within(await screen.findByRole("row", { name: /Role 1/ }));
    expect(campaignRow.getByText("Web search")).toBeTruthy();
    const personRow = within(screen.getByRole("row", { name: /Role 2/ }));
    expect(personRow.getByText("Added by you")).toBeTruthy();
    const unknownRow = within(screen.getByRole("row", { name: /Role 3/ }));
    expect(unknownRow.getByText("Origin not recorded")).toBeTruthy();
  });
});

describe("PendingDiscoveryQueue favorite and kept rows", () => {
  it("lists open rows before no-longer-listed ones inside each marker group", async () => {
    installHandlers([]);
    const closedAt = "2026-09-20T00:00:00Z";
    const row = (id: string, rank: number, marker: object, withdrawnAt: string | null) => ({
      ...discovery(id, id, withdrawnAt),
      ...marker,
      rank,
    });
    render(
      queue({
        discoveries: [
          row("Plain closed", 9, {}, closedAt),
          row("Plain open", 1, {}, null),
          row("Kept closed", 9, { kept: true }, closedAt),
          row("Kept open", 1, { kept: true }, null),
          row("Favorite closed", 9, { favorite: true }, closedAt),
          row("Favorite open", 1, { favorite: true }, null),
        ],
      }),
    );

    await screen.findByLabelText("Select Plain open at Globex");
    const order = screen
      .getAllByRole("checkbox", { name: /^Select (?!eligible)/ })
      .map((box) => box.getAttribute("aria-label"));
    expect(order).toEqual([
      "Select Favorite open at Globex",
      "Select Favorite closed at Globex",
      "Select Kept open at Globex",
      "Select Kept closed at Globex",
      "Select Plain open at Globex",
      "Select Plain closed at Globex",
    ]);
    const markers = within(screen.getByRole("group", { name: "Your markers" }));
    expect(markers.getByRole("button", { name: "Favorites (2)" }).title).toBe(
      "Favorite: your best matches.",
    );
    expect(markers.getByRole("button", { name: "Kept (2)" }).title).toBe(
      "Keep: rows the program misjudged.",
    );
  });

  it("lists a withdrawn kept row without counting it as needing attention", async () => {
    installHandlers([]);
    const withdrawnKept = {
      ...discovery("pending-1", "Kept role", "2026-09-20T00:00:00Z"),
      kept: true,
      readiness: "withdrawn",
    } satisfies PendingDiscovery;
    const unread = { ...discovery("pending-2", "Unread role"), readiness: "no_body" } as const;
    render(queue({ discoveries: [withdrawnKept, unread] }));

    const attention = await screen.findByRole("region", { name: "Postings needing attention" });
    expect(
      within(attention).getByText("1 of 2 matching in any content state need attention"),
    ).toBeTruthy();
    expect(screen.getByLabelText("Select Kept role at Globex")).toBeTruthy();
  });

  it("routes an attention entry to its readiness keeping every other filter and the search", async () => {
    installHandlers([]);
    const changes: PendingQueueFilters[] = [];
    const searches: string[] = [];
    const filters = {
      ...DEFAULT_QUEUE_FILTERS,
      constraint: "investigate",
      search: "globex",
    } satisfies PendingQueueFilters;
    const unread = { ...discovery("pending-1", "Unread role"), readiness: "no_body" } as const;
    render(
      queue({
        discoveries: [unread],
        filters,
        onFiltersChange: (next) => changes.push(next),
        onSearchTextChange: (value) => searches.push(value),
      }),
    );

    const attention = await screen.findByRole("region", { name: "Postings needing attention" });
    fireEvent.click(within(attention).getByRole("button", { name: /No retained posting body/ }));

    expect(changes).toEqual([{ ...filters, readiness: "no_body" }]);
    expect(searches).toEqual([]);
  });

  it("counts a favorite row under each attention route it would list, not as needing attention", async () => {
    installHandlers([]);
    const favorite = { ...discovery("pending-1", "Favorite role"), favorite: true };
    const unread = { ...discovery("pending-2", "Unread role"), readiness: "no_body" } as const;
    render(queue({ discoveries: [favorite, unread] }));

    const attention = await screen.findByRole("region", { name: "Postings needing attention" });
    expect(
      within(attention).getByText("1 of 2 matching in any content state need attention"),
    ).toBeTruthy();
    expect(
      within(attention)
        .getAllByRole("button")
        .map((button) => button.textContent),
    ).toEqual(["No retained posting body (2)"]);
  });

  it("warns before a keyboard dismissal that undo will not bring the markers back", async () => {
    installHandlers([]);
    server.use(...dispositionHandlers());
    render(
      queue({
        discoveries: [
          { ...discovery("pending-1", "Role 1"), favorite: true, kept: true, keep_note: "Team" },
        ],
      }),
    );

    const region = await screen.findByRole("group", { name: /keyboard triage/i });
    fireEvent.focus(region);
    fireEvent.keyDown(region, { key: "x" });

    const dialog = (await screen.findByRole("dialog")).textContent;
    expect(dialog).toContain("the favorite, the keep marker, and the keep note");
    expect(dialog).toContain("Restoring or undoing the dismissal does not bring them back.");
    expect(dialog).not.toContain("This changes only the reversible pending-list disposition.");
  });

  it("keeps the skipped-row note after a bulk dismissal clears the selection", async () => {
    installHandlers([]);
    server.use(
      ...dispositionHandlers({
        skippedMarkedIds: ["pending-2"],
        targetsFor: () => [dispositionTarget("pending-1")],
      }),
    );
    const plain = discovery("pending-1", "Role 1");
    const kept = { ...discovery("pending-2", "Role 2"), kept: true };
    const view = render(queue({ discoveries: [plain, kept] }));

    fireEvent.click(await screen.findByLabelText("Select Role 1 at Globex"));
    fireEvent.click(screen.getByLabelText("Select Role 2 at Globex"));
    fireEvent.click(
      within(selectedActions()).getByRole("button", { name: "Dismiss selected (2)" }),
    );
    fireEvent.change(screen.getByLabelText("Dismissal reason"), {
      target: { value: "invalid_page" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Preview dismissal" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Dismiss discoveries",
      }),
    );
    expect(await screen.findByText("1 favorite or kept row skipped.")).toBeTruthy();

    // The refetch after a dismissal is a new snapshot, which clears the selection.
    view.rerender(queue({ discoveries: [kept], selectionScope: "snapshot-b" }));

    expect(screen.queryByRole("button", { name: /Dismiss selected/ })).toBeNull();
    expect(screen.getByText("1 favorite or kept row skipped.")).toBeTruthy();
  });
});

describe("PendingDiscoveryQueue columns", () => {
  it("folds origin and readiness into the role and spans the empty row across every column", async () => {
    installHandlers([]);
    render(queue({ discoveries: [], queueTotal: 1 }));

    const table = await screen.findByRole("table", { name: "Pending job discoveries" });
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((header) => header.textContent),
    ).toEqual(["", "Employer", "Role", "Place", "Relevance review", "Action"]);
    expect(
      within(table)
        .getByText(/No discoveries match/)
        .closest("td")?.colSpan,
    ).toBe(6);
  });
});

describe("PendingDiscoveryQueue empty view", () => {
  const defaultView = new URLSearchParams({ readiness: "ready", constraint: "not_excluded" });

  it("points at another content state when that is where the search matched", async () => {
    installHandlers([]);
    const unread = { ...discovery("pending-1", "Unread role"), readiness: "no_body" } as const;
    render(
      queue({
        discoveries: [],
        facets: queueFacets([unread], defaultView),
        queueTotal: 3,
        filters: { ...DEFAULT_QUEUE_FILTERS, readiness: "ready" },
      }),
    );

    const empty = (await screen.findByText(/No discoveries match/)).textContent;
    expect(empty).toBe(
      "No discoveries match these filters. 1 matches in another content state; choose Content readiness > All content states.",
    );
  });

  it("points at another constraint route when that is where the search matched", async () => {
    installHandlers([]);
    const refused = {
      ...discovery("pending-1", "Refused role"),
      constraint_confidence: "excluded",
    } satisfies PendingDiscovery;
    render(queue({ discoveries: [], facets: queueFacets([refused], defaultView), queueTotal: 3 }));

    const empty = (await screen.findByText(/No discoveries match/)).textContent;
    expect(empty).toBe(
      "No discoveries match these filters. 1 matches on another constraint route; choose Screened out, or More filters > Constraint route to inspect unconfirmed matches or all confidence routes.",
    );
  });

  it("names the available clarification group when the strict queue is empty", async () => {
    installHandlers([]);
    const uncertain = {
      ...discovery("pending-1", "Uncertain role"),
      constraint_confidence: "investigate",
    } satisfies PendingDiscovery;
    render(
      queue({
        discoveries: [],
        facets: queueFacets([uncertain], defaultView),
        queueTotal: 1,
        strictUncertainty: true,
      }),
    );
    expect((await screen.findByText(/No discoveries match/)).textContent).toContain(
      "open Unconfirmed matches",
    );
    expect(screen.queryByText(/choose Constraint route > All confidence routes/)).toBeNull();
  });
});
