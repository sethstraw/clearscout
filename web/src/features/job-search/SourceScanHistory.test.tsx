import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import type { SourceList, SourceScanHistory as ScanHistory, Task } from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { SourceScanHistory } from "./SourceScanHistory";
import {
  sourceScan,
  sourceScanDetailFailures,
  sourceScanHistory,
  sourceScanSkips,
} from "./sourceScanFixtures";

const WORKSPACE = "*/api/v1/workspaces/:workspaceId";

const CONTINUED = {
  id: "continue-1",
  workspace_id: "workspace-1",
  task_type: "source.continue",
  status: "queued",
  job_posting_ids: [],
  pending_discovery_ids: [],
  source_ids: [],
  progress: {},
  attempt_count: 0,
  cancellation_requested_at: null,
  error_category: null,
  error_message: null,
  created_at: "2026-08-24T00:00:00Z",
  started_at: null,
  updated_at: "2026-08-24T00:00:00Z",
  completed_at: null,
} satisfies Task;

// The history also reads the source list for its filter; these handlers describe scans only.
function historyHandlers(history: ScanHistory, sources: SourceList = { items: [] }) {
  return [
    http.get(`${WORKSPACE}/sources`, () => HttpResponse.json(sources)),
    http.post(`${WORKSPACE}/source-scans/:scanId/continue`, () => HttpResponse.json(CONTINUED)),
    http.get(`${WORKSPACE}/source-scans`, () => HttpResponse.json(history)),
  ];
}

function useScans(items: ScanHistory["items"]) {
  server.use(...historyHandlers(sourceScanHistory(items)));
}

describe("SourceScanHistory", () => {
  it("distinguishes an empty read from a read whose listings did not match", async () => {
    useScans([
      sourceScan({
        id: "scan-empty",
        source_name: "Empty board",
        employer: "Acme Empty",
        source_url: "https://jobs.example.test/empty",
        status: "completed_empty",
        raw_count: 3,
        read_count: 0,
        skip_reasons: sourceScanSkips({ missing_required_posting_identity: 3 }),
        result_count: 0,
        new_count: 0,
        notes: "The board API answered with zero openings.",
        scan_completed_at: "2026-08-24T00:00:00Z",
        duration_ms: 0,
      }),
      sourceScan({
        id: "scan-filtered",
        source_name: "Filtered board",
        employer: "Acme Filtered",
        source_url: "https://jobs.example.test/filtered",
        priority: 2,
        raw_count: 12,
        read_count: 12,
        result_count: 0,
        new_count: 0,
        frontier_pending_count: 2,
        frontier_completed_count: 4,
        frontier_failed_count: 1,
        follow_up_state: "waiting",
        detail_attempted_count: 7,
        detail_read_count: 5,
        detail_failed_count: 2,
        detail_interrupted_count: 0,
        detail_capped_count: 4,
        detail_failure_reasons: sourceScanDetailFailures({ timeout: 1, http_status: 1 }),
        notes:
          "detail_failure_examples: https://example.test/1|https://example.test/2\nNo opening matched the current search.",
        scan_completed_at: "2026-08-24T00:00:01.234Z",
        duration_ms: 1234,
      }),
    ]);

    render(
      <AppProviders>
        <SourceScanHistory workspaceId="workspace-1" />
      </AppProviders>,
    );

    expect(await screen.findByText(/3 listed \/ 0 read \/ 0 kept/)).toBeTruthy();
    expect(screen.getByText(/missing required posting identity: 3/)).toBeTruthy();
    expect(screen.getByText(/12 listed \/ 12 read \/ 0 kept/)).toBeTruthy();
    expect(screen.getByText(/2 deferred links need your read, 1 exhausted\./)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Read deferred publisher links (2)" })).toBeTruthy();
    expect(
      screen.getByText(
        "Recorded detail attempts: 7 started, 5 read, 2 failed. Interrupted: 0. Not attempted at read deadlines: 4.",
      ),
    ).toBeTruthy();
    const alerts = screen.getAllByRole("alert");
    expect(alerts.length).toBeGreaterThan(0);
    const firstAlert = alerts[0];
    if (!firstAlert) throw new Error("expected at least one alert");
    expect(firstAlert.textContent).toContain("1 timeout");
    expect(firstAlert.textContent).toContain("1 http status");
    expect(firstAlert.textContent).toContain("https://example.test/1");
    expect(screen.getByText("1.2s")).toBeTruthy();
  });

  it("confirms a continue-scan request landed with a queued affordance", async () => {
    useScans([
      sourceScan({
        id: "scan-pending",
        source_name: "Pending board",
        employer: "Acme Pending",
        source_url: "https://jobs.example.test/pending",
        raw_count: 4,
        read_count: 4,
        frontier_pending_count: 2,
        follow_up_state: "waiting",
      }),
    ]);

    render(
      <AppProviders>
        <SourceScanHistory workspaceId="workspace-1" />
      </AppProviders>,
    );

    const button = await screen.findByRole("button", {
      name: "Read deferred publisher links (2)",
    });
    expect(screen.queryByText(/Queued/)).toBeNull();
    fireEvent.click(button);
    expect(await screen.findByText(/Queued/)).toBeTruthy();
  });

  it.each([null, 0])(
    "hides empty detail accounting with interruption count %s",
    async (interrupted) => {
      useScans([sourceScan({ detail_interrupted_count: interrupted })]);
      render(
        <AppProviders>
          <SourceScanHistory workspaceId="workspace-1" />
        </AppProviders>,
      );

      await screen.findByText(/2 listed \/ 2 read/);
      expect(screen.queryByText(/Recorded detail attempts:/)).toBeNull();
      expect(screen.queryByText(/Workday detail:/)).toBeNull();
    },
  );

  it.each([
    { interrupted: null, label: "Not recorded" },
    { interrupted: 0, label: "0" },
    { interrupted: 1, label: "1" },
  ])(
    "distinguishes interruption count $label from the remaining frontier",
    async ({ interrupted, label }) => {
      useScans([
        sourceScan({
          detail_attempted_count: 7 + (interrupted ?? 0),
          detail_read_count: 5,
          detail_failed_count: 2,
          detail_interrupted_count: interrupted,
          detail_capped_count: 9,
          frontier_pending_count: 2,
          follow_up_state: "waiting",
        }),
      ]);
      render(
        <AppProviders>
          <SourceScanHistory workspaceId="workspace-1" />
        </AppProviders>,
      );

      const summary = await screen.findByText(
        `Recorded detail attempts: ${7 + (interrupted ?? 0)} started, 5 read, 2 failed. Interrupted: ${label}. Not attempted at read deadlines: 9.`,
      );
      const description = summary.getAttribute("aria-description");
      expect(description).toContain("closed publisher records");
      expect(description).toContain("may count a row more than once");
      expect(description).toContain("remaining deferred links");
      expect(
        screen.getByText(
          "These counts describe recorded read attempts, not the remaining links. A row can be deferred more than once.",
        ),
      ).toBeTruthy();
      expect(
        screen.getByRole("button", { name: "Read deferred publisher links (2)" }),
      ).toBeTruthy();
      expect(screen.queryByText(/Workday detail:/)).toBeNull();
      expect(screen.queryByText(/at the cap/)).toBeNull();
    },
  );

  it("shows rows not attempted at read deadlines without any started requests", async () => {
    useScans([sourceScan({ detail_capped_count: 4, detail_interrupted_count: 0 })]);
    render(
      <AppProviders>
        <SourceScanHistory workspaceId="workspace-1" />
      </AppProviders>,
    );

    expect(
      await screen.findByText(
        "Recorded detail attempts: 0 started, 0 read, 0 failed. Interrupted: 0. Not attempted at read deadlines: 4.",
      ),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Read deferred publisher links/ })).toBeNull();
  });

  it("names why an incomplete Workday read stopped short", async () => {
    useScans([
      sourceScan({
        status: "incomplete",
        skip_reasons: sourceScanSkips({ incomplete_workday_listing: 1, protected_posting: 2 }),
      }),
    ]);
    render(
      <AppProviders>
        <SourceScanHistory workspaceId="workspace-1" />
      </AppProviders>,
    );

    expect(await screen.findByText("Workday listing changed during the read: 1")).toBeTruthy();
    expect(screen.getByText("protected posting: 2")).toBeTruthy();
  });

  it("renders an omitted row as a not-scanned note and formats long durations as m/s", async () => {
    useScans([
      sourceScan({
        id: "scan-omitted",
        source_name: "Skipped board",
        employer: "Acme Skipped",
        source_url: "https://jobs.example.test/skipped",
        priority: 5,
        status: "omitted",
        raw_count: 0,
        read_count: 0,
        result_count: 0,
        new_count: 0,
        notes: "priority below cutoff",
        scan_completed_at: "2026-08-24T00:03:05Z",
        duration_ms: 185000,
      }),
    ]);

    render(
      <AppProviders>
        <SourceScanHistory workspaceId="workspace-1" />
      </AppProviders>,
    );

    expect(await screen.findByText(/Not scanned\. Reason: priority below cutoff\./)).toBeTruthy();
    expect(screen.queryByText(/listed \/ .* read/)).toBeNull();
    expect(screen.queryByText(/Recorded detail attempts:/)).toBeNull();
    // 185000 ms -> 3m 5s.
    expect(screen.getByText("3m 5s")).toBeTruthy();
  });

  it("surfaces an unsupported representation as an incomplete read", async () => {
    useScans([
      sourceScan({
        id: "scan-unsupported",
        source_name: "Publisher page",
        employer: "Acme Publisher",
        source_url: "https://jobs.example.test/publisher",
        priority: 5,
        scanned_on: "2026-08-25",
        status: "incomplete",
        read_outcome: "no_supported_representation",
        raw_count: 0,
        read_count: 0,
        result_count: 0,
        new_count: 0,
        notes: "The publisher page exposed no supported representation.",
        complete: false,
        scan_started_at: "2026-08-25T00:00:00Z",
        scan_completed_at: "2026-08-25T00:00:00Z",
        duration_ms: 0,
      }),
    ]);

    render(
      <AppProviders>
        <SourceScanHistory workspaceId="workspace-1" />
      </AppProviders>,
    );

    expect(await screen.findByText("Incomplete")).toBeTruthy();
    expect(screen.getByText(/0 listed \/ 0 read \/ 0 kept \/ 0 new/)).toBeTruthy();
    expect(screen.queryByText("Failed")).toBeNull();
  });

  it("does not present a policy-refused board as an empty source", async () => {
    useScans([
      sourceScan({
        id: "scan-policy",
        source_name: "Policy-bound board",
        employer: "Acme Health",
        source_url: "https://jobs.example.test/health",
        scanned_on: "2026-09-10",
        status: "failed",
        read_outcome: "policy_refused",
        raw_count: 0,
        read_count: 0,
        result_count: 0,
        new_count: 0,
        notes: "robots.txt disallows the API read",
        complete: false,
        scan_started_at: "2026-09-10T00:00:00Z",
        scan_completed_at: "2026-09-10T00:00:00Z",
        duration_ms: 0,
      }),
    ]);

    render(
      <AppProviders>
        <SourceScanHistory workspaceId="workspace-1" />
      </AppProviders>,
    );

    expect(await screen.findByText("Automatic coverage unavailable:")).toBeTruthy();
    expect(screen.getByText(/Zero openings were not inferred/)).toBeTruthy();
    expect(screen.queryByText("No listings")).toBeNull();
  });

  const FIXTURE_ITEMS = () => [
    sourceScan({
      id: "scan-a-completed",
      source_name: "Alpha board",
      employer: "Acme Alpha",
      source_url: "https://jobs.example.test/alpha",
      scan_started_at: "2026-08-24T21:24:51Z",
      scan_completed_at: "2026-08-24T21:24:52Z",
    }),
    sourceScan({
      id: "scan-b-completed-empty",
      source_name: "Beta board",
      employer: "Acme Beta",
      source_url: "https://jobs.example.test/beta",
      priority: 2,
      status: "completed_empty",
      raw_count: 0,
      read_count: 0,
      result_count: 0,
      new_count: 0,
      scan_started_at: "2026-08-24T00:00:00Z",
      scan_completed_at: "2026-08-24T00:00:00Z",
      duration_ms: 0,
    }),
    sourceScan({
      id: "scan-c-failed",
      source_name: "Gamma board",
      employer: "Acme Gamma",
      source_url: "https://jobs.example.test/gamma",
      priority: 3,
      status: "failed",
      raw_count: 0,
      read_count: 0,
      result_count: 0,
      new_count: 0,
      notes: "board did not answer",
      scan_started_at: "2026-08-24T05:10:00Z",
      scan_completed_at: "2026-08-24T05:10:01Z",
    }),
  ];

  const FIXTURE_SOURCES = [
    { id: "source-gamma", name: "Gamma board" },
    { id: "source-alpha", name: "Alpha board" },
    { id: "source-beta", name: "Beta board" },
    { id: "source-delta", name: "Delta board" },
  ];

  // Answers like the server: scan history honors source_id and status, and sources lists all.
  function useFixtureScans() {
    const requested: URL[] = [];
    server.use(
      http.get(`${WORKSPACE}/sources`, () => HttpResponse.json({ items: FIXTURE_SOURCES })),
      http.get(`${WORKSPACE}/source-scans`, ({ request }) => {
        const url = new URL(request.url);
        requested.push(url);
        const sourceName = FIXTURE_SOURCES.find(
          (source) => source.id === url.searchParams.get("source_id"),
        )?.name;
        const status = url.searchParams.get("status");
        const items = FIXTURE_ITEMS().filter(
          (item) =>
            (!url.searchParams.get("source_id") || item.source_name === sourceName) &&
            (!status || item.status === status),
        );
        return HttpResponse.json(sourceScanHistory(items));
      }),
    );
    return requested;
  }

  it("renders Started at HH:MM UTC when scan_started_at is not midnight", async () => {
    useFixtureScans();
    render(
      <AppProviders>
        <SourceScanHistory workspaceId="workspace-1" />
      </AppProviders>,
    );
    expect(await screen.findByText("Started at 21:24 UTC")).toBeTruthy();
  });

  it("shows no start time for a scan whose start-timestamp equals scanned_on midnight", async () => {
    useFixtureScans();
    render(
      <AppProviders>
        <SourceScanHistory workspaceId="workspace-1" />
      </AppProviders>,
    );
    await screen.findByText("Acme Alpha - P1 - employer board");
    expect(screen.queryByText("Started at 00:00 UTC")).toBeNull();
  });

  it("asks the server for one source's history and lists every source, scanned or not", async () => {
    const requested = useFixtureScans();
    render(
      <AppProviders>
        <SourceScanHistory workspaceId="workspace-1" />
      </AppProviders>,
    );
    await screen.findByText("Acme Alpha - P1 - employer board");
    const sourceSelect = screen.getByRole("combobox", { name: /source/i });
    await waitFor(() =>
      expect(
        within(sourceSelect)
          .getAllByRole("option")
          .map((option) => option.textContent),
      ).toEqual(["All sources", "Alpha board", "Beta board", "Delta board", "Gamma board"]),
    );
    fireEvent.change(sourceSelect, { target: { value: "source-beta" } });
    expect(await screen.findByText(/Acme Beta -/)).toBeTruthy();
    await waitFor(() => expect(screen.queryByText(/Acme Alpha -/)).toBeNull());
    expect(screen.queryByText(/Acme Gamma -/)).toBeNull();
    expect(requested.some((url) => url.searchParams.get("source_id") === "source-beta")).toBe(true);
  });

  it("filters by status on the server and says when nothing matches", async () => {
    const requested = useFixtureScans();
    render(
      <AppProviders>
        <SourceScanHistory workspaceId="workspace-1" />
      </AppProviders>,
    );
    await screen.findByText("Acme Alpha - P1 - employer board");
    fireEvent.change(screen.getByRole("combobox", { name: /status/i }), {
      target: { value: "failed" },
    });
    expect(await screen.findByText(/Acme Gamma -/)).toBeTruthy();
    expect(requested.some((url) => url.searchParams.get("status") === "failed")).toBe(true);
    fireEvent.change(screen.getByRole("combobox", { name: /source/i }), {
      target: { value: "source-delta" },
    });
    expect(
      await screen.findByText("No latest or latest complete read matches these filters."),
    ).toBeTruthy();
    expect(screen.getByRole("combobox", { name: /status/i })).toBeTruthy();
  });

  function followUpScan(overrides: Partial<ScanHistory["items"][number]> = {}) {
    return sourceScan({
      id: "scan-follow-up",
      source_name: "Follow-up board",
      employer: "Acme Follow-up",
      source_url: "https://jobs.example.test/follow-up",
      raw_count: 4,
      read_count: 4,
      ...overrides,
    });
  }

  it("shows automatic follow-up reading with no continue button while active", async () => {
    useScans([
      followUpScan({
        frontier_pending_count: 1,
        frontier_completed_count: 12,
        automatic_pending_count: 1,
        follow_up_state: "active",
      }),
    ]);
    render(
      <AppProviders>
        <SourceScanHistory workspaceId="workspace-1" />
      </AppProviders>,
    );

    const message = await screen.findByText(
      "Reading publisher links automatically (12 read so far, 1 queued).",
    );
    expect(message.closest("tr")?.className).toBe("row-note");
    expect(screen.queryByRole("button", { name: /Read deferred publisher links/ })).toBeNull();
  });

  it("names each kept row's kind of read and whether it is the latest or latest complete read", async () => {
    useScans([
      sourceScan({
        id: "scan-board-latest",
        source_name: "Globex board",
        kept_as: "latest",
        complete: false,
      }),
      sourceScan({
        id: "scan-board-complete",
        source_name: "Globex board",
        kept_as: "latest_complete",
      }),
      sourceScan({
        id: "scan-guided",
        source_name: "Globex board",
        query_label: "guided-search",
        kind: "guided-search",
        kept_as: "latest",
        follow_up_state: "superseded",
        complete: false,
      }),
      sourceScan({
        id: "scan-other",
        source_name: "Globex board",
        query_label: "custom-label",
        kind: "other",
        kept_as: "latest_and_complete",
      }),
    ]);
    render(
      <AppProviders>
        <SourceScanHistory workspaceId="workspace-1" />
      </AppProviders>,
    );

    await screen.findAllByText("Globex board");
    const rows = screen
      .getAllByRole("row")
      .filter((row) => within(row).queryByText("Globex board"))
      .map((row) => within(row).getByRole("rowheader").textContent);
    expect(rows).toEqual([
      expect.stringContaining("Latest board scan"),
      expect.stringContaining("Latest complete board scan"),
      expect.stringContaining("Latest guided search read"),
      expect.stringContaining("Latest read of another kind, complete"),
    ]);
  });

  it("marks a superseded follow-up with no continue button", async () => {
    useScans([followUpScan({ frontier_pending_count: 1, follow_up_state: "superseded" })]);
    render(
      <AppProviders>
        <SourceScanHistory workspaceId="workspace-1" />
      </AppProviders>,
    );

    const message = await screen.findByText("A newer scan of this source replaced this follow-up.");
    expect(message.closest("tr")?.className).toBe("row-note");
    expect(screen.queryByRole("button", { name: /Read deferred publisher links/ })).toBeNull();
  });

  it("folds a single deferred link and its action into the source row", async () => {
    useScans([followUpScan({ frontier_pending_count: 1, follow_up_state: "waiting" })]);
    render(
      <AppProviders>
        <SourceScanHistory workspaceId="workspace-1" />
      </AppProviders>,
    );

    const message = await screen.findByText("1 deferred link needs your read.");
    const row = message.closest("tr");
    expect(row).toBeTruthy();
    // No separate full-width alert row: the count and action share the source's own row.
    expect(row?.className).toBe("");
    expect(
      within(row!).getByRole("button", { name: "Read deferred publisher links (1)" }),
    ).toBeTruthy();
  });

  it("shows only the failed-count note for a complete follow-up", async () => {
    useScans([followUpScan({ frontier_failed_count: 2, follow_up_state: "complete" })]);
    render(
      <AppProviders>
        <SourceScanHistory workspaceId="workspace-1" />
      </AppProviders>,
    );

    expect(await screen.findByText("Exhausted publisher links:")).toBeTruthy();
    expect(screen.getByText(/2 failed after repeated attempts\./)).toBeTruthy();
    expect(screen.queryByText(/Reading publisher links automatically/)).toBeNull();
    expect(screen.queryByText(/need your read/)).toBeNull();
    expect(screen.queryByRole("button", { name: /Read deferred publisher links/ })).toBeNull();
  });

  it("labels the exhausted-links row separately from an active follow-up row", async () => {
    useScans([
      followUpScan({
        frontier_pending_count: 3,
        automatic_pending_count: 3,
        frontier_failed_count: 2,
        follow_up_state: "active",
      }),
    ]);
    render(
      <AppProviders>
        <SourceScanHistory workspaceId="workspace-1" />
      </AppProviders>,
    );

    await screen.findByText("Reading publisher links automatically (0 read so far, 3 queued).");
    expect(screen.getAllByText("Deferred publisher links:")).toHaveLength(1);
    expect(screen.getByText("Exhausted publisher links:")).toBeTruthy();
    expect(screen.getByText(/2 failed after repeated attempts\./)).toBeTruthy();
  });

  it("shows no deferred-links row for a scan with no follow-up and no failures", async () => {
    useScans([followUpScan()]);
    render(
      <AppProviders>
        <SourceScanHistory workspaceId="workspace-1" />
      </AppProviders>,
    );

    await screen.findByText("Acme Follow-up - P1 - employer board");
    expect(screen.queryByText(/Deferred publisher links/)).toBeNull();
  });
});
