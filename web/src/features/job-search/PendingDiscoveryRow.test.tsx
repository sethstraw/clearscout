import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { PendingDiscovery, RelevanceReview, Task } from "../../api/client";
import type { Health } from "../../api/client/types";
import type { TaskCosts } from "../../api/hooks";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { requestLog } from "../../test/requestLog";
import { taskTypesHandler } from "../../test/taskTypes";
import { PendingDiscoveryRow } from "./PendingDiscoveryRow";

const emptyFacts = { values: [], unresolved_values: [], provenance: "", complete: false };

function baseItem(): PendingDiscovery {
  return {
    id: "pending-1",
    employer: "Example Health",
    title: "Product Manager",
    location: "Remote",
    discovery_url: "https://jobs.example.com/postings/1234",
    canonical_url: "https://jobs.example.com/postings/1234",
    content_status: "readable",
    content_note: "",
    reason: "Added from a supplied link.",
    first_seen_on: "2026-08-24",
    rank: 0,
    favorite: false,
    kept: false,
    keep_note: null,
    observed_publisher_employer: "",
    observed_publisher_title: "",
    recommendation: "investigate",
    recommendation_because: "Needs evaluation.",
    constraint_confidence: "investigate",
    screening_standing: "unknown",
    screening_findings: [],
    readiness: "ready",
    arrangement_categories: ["remote"],
    arrangement_categories_source: "published",
    staged_facts: {
      schema_version: 5,
      identity_trust: "publisher",
      freshness: "unknown",
      locations: { ...emptyFacts, values: ["Remote"] },
      arrangements: { ...emptyFacts, values: ["Remote"] },
      employment_types: { ...emptyFacts, values: ["Full-time"] },
      compensation: emptyFacts,
    },
    current_robots_basis: null,
    staging_provenance: "board_scan",
  };
}

function evaluatedBatchTask(pendingDiscoveryId: string): Task {
  return {
    attempt_count: 1,
    cancellation_requested_at: null,
    completed_at: "2026-08-25T00:05:00Z",
    created_at: "2026-08-25T00:00:00Z",
    error_category: null,
    error_message: null,
    id: "task-batch",
    job_posting_ids: ["job-77"],
    pending_discovery_ids: [pendingDiscoveryId],
    source_ids: [],
    progress: {
      selected: 1,
      evaluated: 1,
      skipped: 0,
      skipped_rows: [],
      failed: 0,
      failures: [],
      results: [
        {
          job_posting_id: "job-77",
          evaluation_id: "evaluation-1",
          evaluation_version: 1,
          decision: "worth_applying",
        },
      ],
    },
    started_at: "2026-08-25T00:01:00Z",
    status: "completed",
    task_type: "discovery.evaluate_batch",
    updated_at: "2026-08-25T00:05:00Z",
    workspace_id: "workspace-1",
  };
}

function reviewedOn(reviewedAt: string): RelevanceReview {
  return {
    core_qualification_alignment: "established",
    core_work_alignment: "direct",
    decisive_basis: "core_match",
    employer: "Example Health",
    fetched_at: reviewedAt,
    hard_condition: "passes",
    id: "review-1",
    master_cv_evidence_ids: [],
    master_cv_version_id: "cv-current",
    matched_exclusion: null,
    outcome: "judged",
    pending_discovery_id: "pending-1",
    posting_citations: [],
    out_of_date_because: [],
    recommendation: "review",
    reviewed_at: reviewedAt,
    search_parameter_set_id: "parameters-current",
    summary: "Synthetic relevance summary.",
    task_id: "task-review",
    title: "Product Manager",
    unresolved_questions: [],
  };
}

type RowOptions = {
  detailsOpen?: boolean;
  evaluationTask?: Task;
  isSelected?: boolean;
  onEvaluate?: (item: PendingDiscovery) => void;
  relevanceReview?: RelevanceReview;
};

const ROW_COSTS = {
  loaded: true,
  isError: false,
  isPaid: () => true,
  cost: () => "Paid",
  inline: () => "about $0.40 per posting",
  summary: () => "About $0.40 for 1 posting.",
  confirmation: () => "About $0.40 for 1 posting (range $0.30 to $0.55, from 4 runs).",
} satisfies TaskCosts;

function healthWithFreeMode(freeMode: boolean) {
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
  );
}

function roleCell(): HTMLElement {
  const role = screen.getByText("Product Manager").closest("td");
  if (!role) throw new Error("Expected the role cell.");
  return role;
}

function renderRow(item: PendingDiscovery, options: RowOptions = {}) {
  return render(rowElement(item, options));
}

it("keeps retained-body recovery guidance beside a readable classification explanation", () => {
  server.use(
    taskTypesHandler(),
    http.get("*/api/v1/workspaces/:workspaceId/spend-history", () => HttpResponse.json([])),
  );
  renderRow(
    {
      ...baseItem(),
      content_status: "render_required",
      readiness: "render_required",
      content_note:
        "Rendered publisher content was classified as challenge. The retained posting body is unavailable; attach published text or refresh the posting.",
    },
    { detailsOpen: true },
  );
  expect(
    screen.getByText(
      /The retained posting body is unavailable; attach published text or refresh the posting\./,
    ),
  ).toBeTruthy();
  expect(screen.queryByText(/classified as challenge/)).toBeNull();
});

function rowElement(
  item: PendingDiscovery,
  {
    detailsOpen = false,
    evaluationTask,
    isSelected = false,
    onEvaluate = () => undefined,
    relevanceReview,
  }: RowOptions = {},
) {
  return (
    <AppProviders>
      <MemoryRouter>
        <table>
          <tbody>
            <PendingDiscoveryRow
              costs={ROW_COSTS}
              detailsOpen={detailsOpen}
              dismissalOpen={false}
              dismissalPending={false}
              enqueuePending={false}
              evaluationTask={evaluationTask}
              isInFlight={false}
              isSelected={isSelected}
              item={item}
              keyboardFocused={false}
              openListing={null}
              onDetailsToggle={() => undefined}
              onDismissalClose={() => undefined}
              onDismissalPendingChange={() => undefined}
              onDismissalToggle={() => undefined}
              onEvaluate={onEvaluate}
              onListingToggle={() => undefined}
              onSelect={() => undefined}
              registerCheckbox={() => undefined}
              registerDismissButton={() => undefined}
              relevanceReview={relevanceReview}
              workspaceId="workspace-1"
            />
          </tbody>
        </table>
      </MemoryRouter>
    </AppProviders>
  );
}

describe("PendingDiscoveryRow", () => {
  it("offers recovery in place of a paid evaluation when only part of the posting was read", () => {
    server.use(taskTypesHandler());
    renderRow({ ...baseItem(), readiness: "partial_text", current_content_id: "content-1" });
    expect(
      screen.getByText(
        "Only part of this posting was read; the text appears cut off. Attach the full posting text or find the employer's page before a full evaluation.",
      ),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Full evaluation" })).toBeNull();
    expect(screen.getByRole("button", { name: /^Attach posting text for/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^Estimate assisted recovery for/ })).toBeTruthy();
  });

  it("keeps a readable screened out posting available for review and explicit dismissal", () => {
    const item = {
      ...baseItem(),
      readiness: "screening_refused",
      constraint_confidence: "excluded",
      screening_standing: "refuse",
      screening_findings: [
        {
          dimension: "location",
          code: "location_outside",
          standing: "refuse",
          subject: "Outside your selected locations",
        },
      ],
    } satisfies PendingDiscovery;
    renderRow(item, { detailsOpen: true });
    expect(
      within(roleCell()).getByText("Screened out: conflicts with your search rules"),
    ).toBeTruthy();
    expect(screen.getByText(/The posting text is readable/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Track job" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Hide details" }).ariaExpanded).toBe("true");
    expect(screen.getByRole("button", { name: "Dismiss from pending" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Full evaluation" })).toBeNull();
    expect(screen.queryByText(/No posting text saved/)).toBeNull();
  });

  it("hands Full evaluation to the queue's priced path and shows the estimate on the button", () => {
    server.use(taskTypesHandler());
    healthWithFreeMode(false);
    const onEvaluate = vi.fn();
    const item = baseItem();
    renderRow(item, { onEvaluate });
    const button = screen.getByRole<HTMLButtonElement>("button", { name: "Full evaluation" });
    expect(within(button).getByText("Paid")).toBeTruthy();
    expect(within(button).getByText("about $0.40 per posting")).toBeTruthy();
    fireEvent.click(button);
    expect(onEvaluate).toHaveBeenCalledWith(item);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("disables Full evaluation in free mode and links to the provider settings", async () => {
    server.use(taskTypesHandler());
    healthWithFreeMode(true);
    const onEvaluate = vi.fn();
    renderRow(baseItem(), { onEvaluate });
    expect(await screen.findByText("Add a provider key to use paid actions.")).toBeTruthy();
    const button = screen.getByRole<HTMLButtonElement>("button", { name: "Full evaluation" });
    expect(button.disabled).toBe(true);
    expect(screen.getByRole("link", { name: "Open provider settings" }).getAttribute("href")).toBe(
      "/settings?tab=provider",
    );
    fireEvent.click(button);
    expect(onEvaluate).not.toHaveBeenCalled();
  });

  it("retains an unsaved closing date while Details is hidden and reopened", () => {
    const item = baseItem();
    const view = renderRow(item);
    view.rerender(rowElement(item, { detailsOpen: true }));
    fireEvent.click(screen.getByText("Edit closing date (free)"));
    fireEvent.change(screen.getByLabelText("Your closing date", { selector: "input" }), {
      target: { value: "2026-10-21" },
    });
    view.rerender(rowElement(item));
    expect(screen.queryByRole("group", { name: "Closing dates for Product Manager" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Save date" })).toBeNull();
    expect(screen.getAllByRole("row")).toHaveLength(1);
    view.rerender(rowElement(item, { detailsOpen: true }));
    expect(
      screen.getByLabelText<HTMLInputElement>("Your closing date", { selector: "input" }).value,
    ).toBe("2026-10-21");
  });

  it("retains an in-flight closing date across Details toggles and exposes a retry after failure", async () => {
    const writes = requestLog();
    let releaseFirst!: (response: Response) => void;
    const firstReply = new Promise<Response>((resolve) => {
      releaseFirst = resolve;
    });
    let acknowledgeFirst!: () => void;
    const firstReceived = new Promise<void>((resolve) => {
      acknowledgeFirst = resolve;
    });
    server.use(
      http.put(
        "*/api/v1/workspaces/:workspaceId/pending-discoveries/:pendingId/closing-date",
        async ({ request }) => {
          await writes.record(request);
          if (writes.requests.length === 1) {
            acknowledgeFirst();
            return firstReply;
          }
          return HttpResponse.json(baseItem());
        },
      ),
    );
    const item = baseItem();
    const view = renderRow(item, { detailsOpen: true });
    fireEvent.click(screen.getByText("Edit closing date (free)"));
    fireEvent.change(screen.getByLabelText("Your closing date", { selector: "input" }), {
      target: { value: "2026-10-21" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save date" }));
    await act(async () => {
      await firstReceived;
    });
    view.rerender(rowElement(item));
    expect(screen.queryByRole("button", { name: "Save date" })).toBeNull();
    view.rerender(rowElement(item, { detailsOpen: true }));
    const input = screen.getByLabelText<HTMLInputElement>("Your closing date", {
      selector: "input",
    });
    expect(input.value).toBe("2026-10-21");
    expect(input.disabled).toBe(true);
    const save = screen.getByRole("button", { name: "Save date" });
    expect(save.getAttribute("aria-busy")).toBe("true");
    fireEvent.click(save);
    act(() => {
      releaseFirst(
        HttpResponse.json({ detail: "Synthetic closing-date save failed." }, { status: 503 }),
      );
    });
    const dates = screen.getByRole("group", { name: "Closing dates for Product Manager" });
    const error = await within(dates).findByRole("alert");
    expect(error.textContent).toContain("Synthetic closing-date save failed.");
    expect(input.getAttribute("aria-describedby")).toBe(error.id);
    expect(input.disabled).toBe(false);
    expect(input.value).toBe("2026-10-21");
    expect(writes.requests).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Save date" }));
    await screen.findByText("Your closing date was saved.");
    expect(writes.requests).toHaveLength(2);
    expect(writes.requests.map((request) => request.body)).toEqual([
      { closing_date_override: "2026-10-21" },
      { closing_date_override: "2026-10-21" },
    ]);
  });

  it("keeps absent closing dates out of the collapsed row and exposes editing in Details", () => {
    const item = baseItem();
    const view = renderRow(item);
    expect(screen.queryByText("Publisher closing date")).toBeNull();
    expect(screen.queryByText("Not stated")).toBeNull();
    expect(screen.queryByText("Not set; using publisher information")).toBeNull();
    expect(screen.queryByText("Edit closing date (free)")).toBeNull();
    view.rerender(rowElement(item, { detailsOpen: true }));
    const dates = screen.getByRole("group", { name: "Closing dates for Product Manager" });
    expect(within(dates).getByText("Not stated")).toBeTruthy();
    expect(within(dates).getByText("Not set; using publisher information")).toBeTruthy();
    expect(within(dates).getByText("Edit closing date (free)")).toBeTruthy();
  });

  it("shows a personal closing date and its passed warning without the collapsed-row editor", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    try {
      vi.setSystemTime(new Date("2026-10-20T12:00:00Z"));
      renderRow({ ...baseItem(), closing_date_override: "2026-10-19" });
      const dates = screen.getByRole("group", { name: "Closing date summary for Product Manager" });
      expect(within(dates).getByText(/2026-10-19/)).toBeTruthy();
      expect(
        within(dates).getByText("Your closing date has passed. It is a tracking reminder."),
      ).toBeTruthy();
      expect(screen.queryByText("Edit closing date (free)")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("edits and resets a personal closing date without paid or disposition requests", async () => {
    const writes = requestLog();
    server.use(
      http.put(
        "*/api/v1/workspaces/:workspaceId/pending-discoveries/:pendingId/closing-date",
        async ({ request }) => {
          await writes.record(request);
          return HttpResponse.json(baseItem());
        },
      ),
      http.post("*/api/v1/tasks", async ({ request }) => {
        await writes.record(request);
        return HttpResponse.json({}, { status: 500 });
      }),
    );
    renderRow(baseItem(), { detailsOpen: true });
    fireEvent.click(screen.getByText("Edit closing date (free)"));
    fireEvent.change(screen.getByLabelText("Your closing date", { selector: "input" }), {
      target: { value: "2026-10-21" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save date" }));
    await screen.findByText("Your closing date was saved.");
    fireEvent.click(screen.getByText("Edit closing date (free)"));
    fireEvent.click(screen.getByRole("button", { name: "Use publisher date" }));
    await screen.findByText("Using the publisher closing date.");
    expect(writes.requests).toHaveLength(2);
    expect(writes.requests.map((request) => request.url.pathname)).toEqual([
      "/api/v1/workspaces/workspace-1/pending-discoveries/pending-1/closing-date",
      "/api/v1/workspaces/workspace-1/pending-discoveries/pending-1/closing-date",
    ]);
  });

  it("tracks a pending job without enqueuing a paid evaluation", async () => {
    const posts = requestLog();
    server.use(
      http.post(
        "*/api/v1/workspaces/:workspaceId/pending-discoveries/:pendingId/track",
        async ({ request }) => {
          await posts.record(request);
          return HttpResponse.json({ job_posting_id: "job-1" });
        },
      ),
    );
    renderRow(baseItem());

    fireEvent.click(screen.getByRole("button", { name: "Track job" }));

    await waitFor(() => expect(posts.requests).toHaveLength(1));
    expect(posts.requests[0]?.url.pathname).toBe(
      "/api/v1/workspaces/workspace-1/pending-discoveries/pending-1/track",
    );
  });

  it("notes a single posting read when robots.txt disallowed crawling", () => {
    renderRow({ ...baseItem(), current_robots_basis: "single_posting_read" });

    expect(screen.getByText("Single page read")).toBeTruthy();
  });

  it("advises confirming the stated focus, not editing target titles, for a specialist row", () => {
    renderRow(
      {
        ...baseItem(),
        screening_findings: [
          {
            code: "title_specialist_ic_review",
            dimension: "title",
            standing: "unknown",
            subject: "analyst, clinical data integration (posting)",
          },
        ],
      },
      { detailsOpen: true },
    );

    expect(screen.getByText("Specialist IC review")).toBeTruthy();
    expect(
      screen.getByText(
        "Open the posting to confirm its stated focus describes this role, or run a relevance review.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/edit target titles/)).toBeNull();
  });

  it("shows no note when the read was allowed", () => {
    renderRow({ ...baseItem(), current_robots_basis: "allowed" });

    expect(screen.queryByText("Single page read")).toBeNull();
  });

  it("shows no note when the basis is not stated", () => {
    renderRow({ ...baseItem(), current_robots_basis: null });

    expect(screen.queryByText("Single page read")).toBeNull();
  });

  it("names the board a posting was read from when the board belongs to another employer", () => {
    renderRow({ ...baseItem(), employer: "Initech", board_employer: "Globex" });

    const employer = screen.getByRole("rowheader");
    expect(employer.textContent).toContain("Initech");
    expect(screen.getByText("On Globex's board")).toBeTruthy();
  });

  it("says the employer is not stated instead of naming the ATS tenant or host", () => {
    const url = "https://jobs.lever.co/northwind/abc-123";
    renderRow({ ...baseItem(), employer: "", discovery_url: url, canonical_url: url });

    const employer = screen.getByRole("rowheader");
    expect(within(employer).getByText("Employer not stated")).toBeTruthy();
    const note = within(employer).getByText("Listed on jobs.lever.co");
    expect(note.querySelectorAll("wbr")).toHaveLength(2);
    expect(within(employer).getByText("Employer not stated").tagName).toBe("P");
    expect(
      screen.getByRole("checkbox", {
        name: "Select Product Manager, employer not stated, listed on jobs.lever.co",
      }),
    ).toBeTruthy();
    expect(screen.queryByText(/northwind/)).toBeNull();
  });

  it("shows the source host without its www prefix and keeps the full host in a title", () => {
    const url = "https://www.careers-example.test/jobs/1234";
    renderRow({ ...baseItem(), discovery_url: url, canonical_url: url });

    const badge = screen.getByTitle("Listed on careers-example.test");
    expect(badge.textContent).toBe("careers-example.test");
    // A line break may fall only after a dot, so the host never splits mid-word.
    expect(badge.querySelectorAll("wbr")).toHaveLength(1);
    expect(within(screen.getByRole("rowheader")).queryByText(/Listed on/)).toBeNull();
  });

  it("opens a Himalayas listing at its applicationLink and credits Himalayas with a link", () => {
    const applicationLink = "https://himalayas.app/companies/initech/jobs/product-manager";
    renderRow({
      ...baseItem(),
      discovery_url: applicationLink,
      canonical_url: "https://jobs.example.test/initech/4412",
    });

    expect(screen.getByRole("link", { name: "Product Manager" }).getAttribute("href")).toBe(
      applicationLink,
    );
    const credit = screen.getByRole("link", { name: "Job data from Himalayas" });
    expect(credit.getAttribute("href")).toBe(applicationLink);
    // jsdom draws no ::before, so this checks the span sibling that carries the " | " separator.
    expect(credit.closest(".pending-origin > span + span")).not.toBeNull();
    expect(screen.queryByText(/jobs\.example\.test/)).toBeNull();
  });

  it("credits Arbeitnow with a link to arbeitnow.com in place of the host label", () => {
    const listing = "https://www.arbeitnow.com/jobs/companies/initech/product-manager-1234";
    renderRow({ ...baseItem(), discovery_url: listing, canonical_url: listing });

    expect(screen.getByRole("link", { name: "Product Manager" }).getAttribute("href")).toBe(
      listing,
    );
    const credit = screen.getByRole("link", { name: "Job data from Arbeitnow" });
    expect(credit.getAttribute("href")).toBe("https://www.arbeitnow.com/");
    expect(credit.closest(".pending-origin > span + span")).not.toBeNull();
    expect(screen.queryByTitle("Listed on arbeitnow.com")).toBeNull();
  });

  it("keeps the Arbeitnow credit when the row opens an employer address", () => {
    const employerPage = "https://jobs.example.test/initech/77";
    renderRow({
      ...baseItem(),
      discovery_url: "https://www.arbeitnow.com/jobs/companies/initech/pm-1",
      canonical_url: employerPage,
    });

    expect(screen.getByRole("link", { name: "Product Manager" }).getAttribute("href")).toBe(
      employerPage,
    );
    expect(screen.getByRole("link", { name: "Job data from Arbeitnow" }).getAttribute("href")).toBe(
      "https://www.arbeitnow.com/",
    );
  });

  it("names no board when the posting names the board's own employer", () => {
    renderRow({ ...baseItem(), board_employer: null });

    expect(screen.queryByText(/board$/)).toBeNull();
  });

  it("marks a row whose paid review recorded nothing, and offers it no retry", () => {
    renderRow({
      ...baseItem(),
      relevance_review_rejection: {
        pending_discovery_id: "pending-1",
        failure_code: "leading_excerpt_only",
        detail: "The result cited only the leading excerpt of a longer posting.",
        rejected_at: "2026-09-20T00:00:00Z",
        task_id: "task-review",
      },
    });

    expect(screen.getByText("Paid review returned nothing usable")).toBeTruthy();
    expect(
      screen.getByText(
        "The last paid review recorded no verdict here, because the answer read only the " +
          "opening of the posting. Reviewing it again spends again.",
      ),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
  });

  it("keeps the stored verdict when a later paid review recorded nothing for the row", () => {
    renderRow(
      {
        ...baseItem(),
        relevance_review_rejection: {
          pending_discovery_id: "pending-1",
          failure_code: "leading_excerpt_only",
          detail: "The result cited only the leading excerpt of a longer posting.",
          rejected_at: "2026-09-20T00:00:00Z",
          task_id: "task-review-2",
        },
      },
      { relevanceReview: reviewedOn("2026-09-17T11:00:00Z") },
    );

    expect(screen.getByText("High relevance")).toBeTruthy();
    expect(
      screen.getByText(
        "The newest paid review recorded nothing, because the answer read only the opening of " +
          "the posting. The verdict shown is from an earlier run. Reviewing it again spends again.",
      ),
    ).toBeTruthy();
  });

  it("marks the verdict out of date when the server names a changed input", () => {
    renderRow(baseItem(), {
      relevanceReview: {
        ...reviewedOn("2026-09-17T11:00:00Z"),
        out_of_date_because: ["master_cv"],
      },
    });

    expect(screen.getByText("Out of date")).toBeTruthy();
  });

  it("leaves a current verdict unmarked", () => {
    renderRow(baseItem(), { relevanceReview: reviewedOn("2026-09-17T11:00:00Z") });

    expect(screen.getByText("High relevance")).toBeTruthy();
    expect(screen.queryByText("Out of date")).toBeNull();
  });

  it("says when the posting was read, reviewed, and evaluated without opening Details", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-09-18T13:00:00Z"));
      renderRow(
        {
          ...baseItem(),
          current_content_observed_at: "2026-09-18T11:00:00Z",
          evaluated_at: "2026-09-16T11:00:00Z",
        },
        { relevanceReview: reviewedOn("2026-09-17T11:00:00Z") },
      );

      expect(
        screen.getByText("Read 2h ago, reviewed 2026-09-17, evaluated 2026-09-16"),
      ).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it("omits the reads a posting has not had yet", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-09-18T13:00:00Z"));
      renderRow({ ...baseItem(), current_content_observed_at: "2026-09-18T11:00:00Z" });

      expect(screen.getByText("Read 2h ago")).toBeTruthy();
      expect(screen.queryByText(/reviewed/)).toBeNull();
      expect(screen.queryByText(/evaluated/)).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    ["match", "status--tone-positive"],
    ["refuse", "status--tone-danger"],
    ["unknown", "status--tone-muted"],
  ] as const)(
    "tones the work arrangement by its %s screening and adds no arrangement chip",
    (standing, tone) => {
      renderRow({
        ...baseItem(),
        location: "Northtown",
        screening_findings: [
          { dimension: "arrangement", standing, code: "arrangement_unknown", subject: "" },
        ],
      });

      expect(screen.getByText("Remote").className).toContain(tone);
      expect(screen.queryByText(/^Arrangement/)).toBeNull();
    },
  );

  it("tells an unreviewed first screen apart from a reviewed verdict of the same weight", () => {
    renderRow({ ...baseItem(), recommendation: "investigate", constraint_confidence: "pursue" });
    expect(screen.getByText("Investigate").className).toContain("status--tone-muted");
  });

  it("colors each screening chip by its standing and clamps the row to two with a count", () => {
    const finding = (
      dimension: PendingDiscovery["screening_findings"][number]["dimension"],
      standing: "refuse" | "unknown",
    ): PendingDiscovery["screening_findings"][number] => ({
      dimension,
      standing,
      code: "compensation_unknown",
      subject: "",
    });
    renderRow({
      ...baseItem(),
      constraint_confidence: "pursue",
      screening_findings: [
        finding("employer", "refuse"),
        finding("seniority", "unknown"),
        finding("location", "unknown"),
        finding("employment_type", "refuse"),
        finding("compensation", "unknown"),
      ],
    });

    expect(screen.getByText("Employer mismatch").className).toContain("status--tone-danger");
    expect(screen.getByText("Seniority unclear").className).toContain("status--tone-muted");
    expect(screen.getByText("+3").getAttribute("title")).toBe(
      "Location unresolved, Employment mismatch, Comp unknown",
    );
    expect(screen.queryByText("Location unresolved")).toBeNull();
  });

  it("shows a readiness chip beside the role only when the posting is not ready", () => {
    renderRow({ ...baseItem(), content_status: "access_blocked", readiness: "access_blocked" });

    const role = screen.getByText("Product Manager").closest("td");
    if (!role) throw new Error("Expected the role cell.");
    expect(within(role).getByText("Needs recovery: publisher blocked access")).toBeTruthy();
    expect(role.className).toContain("content-readiness--access_blocked");
  });

  it("adds no readiness chip to a ready posting", () => {
    renderRow(baseItem());

    const role = screen.getByText("Product Manager").closest("td");
    if (!role) throw new Error("Expected the role cell.");
    expect(within(role).queryByText(/^Needs/)).toBeNull();
    expect(within(role).queryByText(/^Ready/)).toBeNull();
  });

  it("names where the row came from on one line under the role", () => {
    renderRow(baseItem());

    const role = screen.getByText("Product Manager").closest("td");
    if (!role) throw new Error("Expected the role cell.");
    expect(within(role).getByText("Board scan")).toBeTruthy();
    expect(within(role).getByText("First seen 2026-08-24")).toBeTruthy();
    expect(within(role).getByText("Rank 0")).toBeTruthy();
    expect(screen.getAllByRole("cell").length + screen.getAllByRole("rowheader").length).toBe(6);
  });

  it("names the countries the place states or suggests", () => {
    renderRow({
      ...baseItem(),
      location: "Berlin or Boston",
      countries: ["Germany", "United States"],
    });

    expect(screen.getByText("Germany, United States")).toBeTruthy();
  });

  it("shows a kept row its board no longer lists as gone, with no paid evaluation", () => {
    renderRow({ ...baseItem(), kept: true, withdrawn_at: "2026-09-20T00:00:00Z" });

    const role = screen.getByText("Product Manager").closest("td");
    if (!role) throw new Error("Expected the role cell.");
    expect(within(role).getByText("No longer listed")).toBeTruthy();
    expect(screen.getByText("No longer listed; evaluation disabled")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Full evaluation" })).toBeNull();
    expect(screen.getByRole("button", { name: /Keep Product Manager/ }).ariaPressed).toBe("true");
  });

  it("keeps focus and the typed text in the keep note when a refetch brings a new note", () => {
    const item = { ...baseItem(), kept: true, keep_note: "First note" };
    const view = renderRow(item);
    fireEvent.click(screen.getByRole("button", { name: /Edit keep note for Product Manager/ }));
    const note = screen.getByLabelText<HTMLInputElement>(/Keep note for Product Manager/);
    fireEvent.change(note, { target: { value: "Still typing" } });

    view.rerender(rowElement({ ...item, keep_note: "Saved elsewhere" }));

    const after = screen.getByLabelText<HTMLInputElement>(/Keep note for Product Manager/);
    expect(document.activeElement).toBe(after);
    expect(after.value).toBe("Still typing");
  });

  it("shows a new server note in the keep note while the person is not editing it", () => {
    const item = { ...baseItem(), kept: true, keep_note: "First note" };
    const view = renderRow(item);

    view.rerender(rowElement({ ...item, keep_note: "Saved elsewhere" }));

    expect(
      screen.getByRole("button", {
        name: /^Kept: Saved elsewhere\. Edit keep note for Product Manager/,
      }),
    ).toBeTruthy();
  });

  it("shows the keep note under the role and edits it in place", async () => {
    const item = { ...baseItem(), kept: true, keep_note: "Strong team" };
    const puts = requestLog();
    server.use(
      http.put(
        "*/api/v1/workspaces/:workspaceId/pending-discoveries/:pendingId/markers",
        async ({ request }) => {
          await puts.record(request);
          return HttpResponse.json({ ...item, ...(puts.requests.at(-1)?.body as object) });
        },
      ),
    );
    renderRow(item);

    const role = roleCell();
    const shown = within(role).getByRole("button", {
      name: /^Kept: Strong team\. Edit keep note for Product Manager/,
    });
    expect(screen.queryByLabelText(/Keep note for Product Manager/)).toBeNull();

    fireEvent.click(shown);
    const note = within(role).getByLabelText<HTMLInputElement>(/Keep note for Product Manager/);
    expect(document.activeElement).toBe(note);
    expect(note.maxLength).toBe(300);
    fireEvent.change(note, { target: { value: "Recruiter called" } });
    fireEvent.keyDown(note, { key: "Enter" });

    await waitFor(() => expect(puts.requests).toHaveLength(1));
    expect(puts.requests[0]?.body).toEqual({
      favorite: false,
      kept: true,
      keep_note: "Recruiter called",
    });
    expect(
      within(role).getByRole("button", {
        name: /^Kept: Recruiter called\. Edit keep note for Product Manager/,
      }),
    ).toBeTruthy();
  });

  it("puts the marker toggles on the role's title line and keeps them out of Action", () => {
    renderRow({ ...baseItem(), favorite: true });

    const role = roleCell();
    expect(
      within(role).getByRole("button", { name: /^Favorite Product Manager/ }).ariaPressed,
    ).toBe("true");
    expect(within(role).getByRole("button", { name: /^Keep Product Manager/ })).toBeTruthy();
    const action = screen.getByRole("button", { name: "Dismiss from pending" }).closest("td");
    if (!action) throw new Error("Expected the action cell.");
    expect(
      within(action)
        .getAllByRole("button")
        .map((button) => button.textContent),
    ).toEqual(["Track job", expect.stringMatching(/^Full evaluation/), "Details", "Dismiss"]);
  });

  it.each([
    ["compensation_below_target", "Pay: USD 90,000 - 120,000 per year, top under target"],
    ["compensation_target_met", "Pay: USD 90,000 - 120,000 per year, top reaches target"],
    ["compensation_stated", "Pay: USD 90,000 - 120,000 per year"],
    ["compensation_match", "Pay: USD 90,000 - 120,000 per year, top reaches minimum"],
    ["compensation_below_minimum", "Pay: USD 90,000 - 120,000 per year, top under minimum"],
    ["compensation_unknown", "Pay: USD 90,000 - 120,000 per year"],
  ] as const)("shows stated pay and its standing against the band: %s", (code, shown) => {
    renderRow({
      ...baseItem(),
      screening_findings: [
        {
          dimension: "compensation",
          standing: "match",
          code,
          subject: "USD 90,000 - 120,000 per year",
        },
      ],
    });

    expect(screen.getByText(shown)).toBeTruthy();
  });

  it("shows no pay when the posting states none", () => {
    renderRow(baseItem());

    expect(screen.queryByText(/^Pay:/)).toBeNull();
  });

  it.each([
    [
      ["Published annual USD range"],
      "Pay range found in posting text; comparison with your pay preference is unconfirmed.",
    ],
    [[], "No pay was confirmed in the saved evidence."],
  ])(
    "explains unconfirmed compensation without requiring a paid evaluation: %s",
    (values, explanation) => {
      const item = {
        ...baseItem(),
        constraint_confidence: "uncertain_comp",
        screening_findings: [
          {
            dimension: "compensation",
            standing: "unknown",
            code: "compensation_unknown",
            subject: values.join("; "),
          },
        ],
        staged_facts: {
          ...baseItem().staged_facts,
          compensation: { ...emptyFacts, values },
        },
      } satisfies PendingDiscovery;
      renderRow(item, { detailsOpen: true });
      expect(screen.getByTitle(explanation).textContent).toBe("Compensation uncertain");
      expect(screen.getByText(explanation)).toBeTruthy();
      expect(
        screen.queryByText("Refresh postings, or paste the current posting text below."),
      ).toBeNull();
      expect(
        screen.getByText(
          "Compare any stated pay with your preference yourself. You can track this job for free while pay remains unconfirmed.",
        ),
      ).toBeTruthy();
      expect(screen.getByRole("button", { name: "Track job" })).toBeTruthy();
    },
  );

  it("names the employment type in words, whatever form the board stored", () => {
    renderRow({
      ...baseItem(),
      staged_facts: {
        ...baseItem().staged_facts,
        employment_types: { ...emptyFacts, values: ["FULL_TIME"], complete: true },
      },
    });

    expect(screen.getByText("Full time")).toBeTruthy();
    expect(screen.queryByText(/FULL_TIME/)).toBeNull();
  });

  it("leaves out a country the place already names", () => {
    renderRow({ ...baseItem(), location: "Berlin, Germany", countries: ["Germany"] });

    expect(screen.getByText("Berlin, Germany")).toBeTruthy();
    expect(screen.queryByTitle(/Countries this place names/)).toBeNull();
  });

  it("keeps only the countries the place does not already name", () => {
    renderRow({
      ...baseItem(),
      location: "Berlin, Germany or Boston",
      countries: ["Germany", "United States"],
    });

    expect(screen.getByTitle(/Countries this place names/).textContent).toBe("United States");
  });

  it.each([
    ["a short form in the place", "Remote - US", [], ["United States"], null],
    ["a short form with stops", "Remote (U.S.A.)", [], ["United States"], null],
    [
      "a longer name in the also-places",
      "Remote",
      ["Austin, United States of America"],
      ["United States"],
      null,
    ],
    ["the UK short form", "London, UK", [], ["United Kingdom", "Germany"], "Germany"],
    ["no country at all", "Business Park, Columbus", [], ["United States"], "United States"],
  ])(
    "repeats no country the row's places already name: %s",
    (_, location, also, countries, shown) => {
      renderRow({
        ...baseItem(),
        location,
        countries,
        staged_facts: {
          ...baseItem().staged_facts,
          locations: { ...emptyFacts, values: [location, ...also] },
        },
      });

      expect(screen.queryByTitle(/Countries this place names/)?.textContent ?? null).toBe(shown);
    },
  );

  it("folds an inferred arrangement source into its badge", () => {
    renderRow({ ...baseItem(), arrangement_categories_source: "inferred_from_location" });

    expect(screen.getByText("Remote, from location")).toBeTruthy();
    expect(screen.queryByText("(from location)")).toBeNull();
  });

  it("locks selection for a row a completed batch evaluation already evaluated", () => {
    const item = baseItem();
    renderRow(item, { evaluationTask: evaluatedBatchTask(item.id), isSelected: true });

    const checkbox = screen.getByRole<HTMLInputElement>("checkbox", {
      name: /^Select Product Manager at Example Health/,
    });
    expect(checkbox.getAttribute("aria-label")).toContain("(already evaluated)");
    expect(checkbox.disabled).toBe(true);
    expect(checkbox.checked).toBe(false);
  });
});
