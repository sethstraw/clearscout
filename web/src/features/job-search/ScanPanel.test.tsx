import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import type { Source, Task } from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { taskTypesHandler } from "../../test/taskTypes";
import { searchParametersState } from "../search-parameters/searchParameterFixtures";
import { ScanPanel } from "./ScanPanel";
import { boardSource, sourceList } from "./sourceScanFixtures";

const NORTHWIND = boardSource();

/** Serve the saved search parameters with these target titles. */
function savedTitles(titles: string[]) {
  server.use(
    http.get("*/api/v1/job-search-parameters", () =>
      HttpResponse.json(
        searchParametersState({
          configured: true,
          parameters: {
            opportunity_thesis: "",
            score_weight_set: "",
            working_day_shift_hours: 0,
            locations: [],
            seniority: {
              minimum: "",
              maximum: "",
              title_matching: "guided",
              target_titles: titles.map((title) => ({ title, lane: null })),
            },
          },
        }),
      ),
    ),
  );
}

beforeEach(() => {
  server.use(
    http.get("/api/v1/workspaces/workspace-1/scan-schedule", () =>
      HttpResponse.json({ workspace_id: "workspace-1", state: null, unavailable_source_ids: [] }),
    ),
  );
  savedTitles(["Fleet Lead"]);
});
const INITECH = boardSource({
  id: "source-2",
  name: "Initech",
  employer: "Initech",
  url: "https://jobs.example.test/initech",
  terms: { condition: "Public board API.", url: "" },
});
const CONTOSO = boardSource({
  id: "source-3",
  name: "Contoso",
  employer: "Contoso",
  enabled: false,
  url: "https://jobs.example.test/contoso",
});

/** One enqueued board scan and whatever progress the panel should read from it. */
function scanTask(overrides: Partial<Task> = {}): Task {
  return {
    id: "task-1",
    workspace_id: "workspace-1",
    task_type: "source.scan",
    status: "completed",
    attempt_count: 1,
    job_posting_ids: [],
    pending_discovery_ids: [],
    source_ids: [],
    progress: {},
    cancellation_requested_at: null,
    error_category: null,
    error_message: null,
    created_at: "2026-08-21T12:00:00Z",
    started_at: "2026-08-21T12:00:01Z",
    updated_at: "2026-08-21T12:00:02Z",
    completed_at: "2026-08-21T12:00:03Z",
    ...overrides,
  };
}

function panel() {
  render(
    <AppProviders>
      <ScanPanel workspaceId="workspace-1" />
    </AppProviders>,
  );
}

describe("ScanPanel", () => {
  it("keeps scan scope and progress visible while the fresh-details option stays under Advanced", async () => {
    const active = scanTask({
      status: "running",
      source_ids: [NORTHWIND.id],
      progress: { phase: "reading_content", current: 2, total: 7 },
    });
    installOneBoard(active, [active]);
    panel();

    const summary = await screen.findByText("Advanced");
    const disclosure = summary.closest("details")!;
    expect(disclosure.open).toBe(false);
    const progress = await screen.findByText("Reading posting content: 2/7");
    expect(progress.closest("details")).toBeNull();
    expect(screen.getByRole("button", { name: "Scan now" })).toBeTruthy();
    expect(screen.getByText("All enabled sources").closest("details")).toBeNull();
    expect(
      screen.getByText(/sends titles that match yours to Review/).closest("details"),
    ).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "What a scan can read" }));
    expect(screen.getByText(/does not promise permission or full coverage/)).toBeTruthy();
    fireEvent.click(summary);
    expect(disclosure.open).toBe(true);
    const fresh = within(disclosure).getByRole("checkbox", {
      name: "Read fresh posting details for this scan",
    });
    fireEvent.click(fresh);
    fireEvent.click(summary);
    expect(screen.getByText("Fresh posting details requested for this scan.")).toBeTruthy();
    fireEvent.click(screen.getByText("Advanced"));
    fireEvent.click(within(disclosure).getByRole("button", { name: "About fresh details" }));
    expect(screen.getByText(/failed fresh read leaves retained content/)).toBeTruthy();
  });

  it("explains a disabled bulk scan and points at the Add a board form", async () => {
    installHandlers(scanTask(), []);
    panel();

    const button = await screen.findByRole<HTMLButtonElement>("button", { name: "Scan now" });
    await screen.findByText("No boards yet.", { exact: false });
    expect(button.disabled).toBe(true);
    const reason = document.getElementById(button.getAttribute("aria-describedby") ?? "");
    expect(reason?.textContent).toBe("Add a board below to scan.");
    expect(screen.getByRole("link", { name: "Add a board" }).getAttribute("href")).toBe(
      "#source-add-heading",
    );
    expect(screen.queryByText(/Settings before scanning/)).toBeNull();
  });

  it("says to turn a board on when every saved board is off", async () => {
    installHandlers(scanTask(), [CONTOSO]);
    panel();

    await screen.findByText("Sources (0 enabled of 1): scan all, selected, or one");
    const button = screen.getByRole<HTMLButtonElement>("button", { name: "Scan now" });
    expect(button.disabled).toBe(true);
    expect(
      document.getElementById(button.getAttribute("aria-describedby") ?? "")?.textContent,
    ).toBe("Turn on sweeping for a board below to scan.");
  });

  it("links to Settings > Search only while no target title is saved", async () => {
    savedTitles([]);
    installOneBoard(scanTask());
    const view = render(
      <AppProviders>
        <ScanPanel workspaceId="workspace-1" />
      </AppProviders>,
    );

    const link = await screen.findByRole("link", {
      name: "Add target titles in Settings > Search",
    });
    expect(link.getAttribute("href")).toBe("/settings?tab=search");
    expect(screen.getByRole("link", { name: "add one posting link" }).getAttribute("href")).toBe(
      "?tab=review",
    );
    view.unmount();

    savedTitles(["Fleet Lead"]);
    panel();
    await screen.findByText("Sources (1 enabled of 1): scan all, selected, or one");
    expect(screen.queryByText(/Scans need at least one target title/)).toBeNull();
  });

  it("keeps failed scan recovery available outside optional controls", async () => {
    const failed = scanTask({ status: "failed", error_message: "Publisher read failed." });
    installOneBoard(failed, [failed]);
    panel();

    const error = await screen.findByText("Scan failed: Publisher read failed.");
    expect(error.closest("details")).toBeNull();
    expect(screen.getByRole("link", { name: "View scan tasks" }).getAttribute("href")).toBe(
      "/tasks",
    );
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Scan now" }).disabled).toBe(
      false,
    );
    expect(screen.getByText("Advanced").closest("details")!.open).toBe(false);
  });

  it.each(["all", "selected", "one"])(
    "requests fresh details explicitly for the %s source scope",
    async (scope) => {
      const submitted = installHandlers(scanTask());
      panel();
      await screen.findByText("Sources (2 enabled of 2): scan all, selected, or one");
      fireEvent.click(screen.getByText("Advanced"));
      const fresh = screen.getByRole("checkbox", {
        name: "Read fresh posting details for this scan",
      });
      expect(fresh.getAttribute("aria-describedby")).toBeTruthy();
      fireEvent.click(fresh);
      if (scope === "selected") {
        fireEvent.click(screen.getByRole("checkbox", { name: "Select Northwind for scan" }));
      }
      fireEvent.click(
        screen.getByRole("button", {
          name:
            scope === "all"
              ? "Scan now"
              : scope === "selected"
                ? "Scan selected sources"
                : "Scan Northwind",
        }),
      );
      const dialog = screen.getByRole("dialog");
      expect(within(dialog).getByText(/Fresh details requested/)).toBeTruthy();
      expect(within(dialog).getByText(/Dismissed and retired postings stay unread/)).toBeTruthy();
      fireEvent.click(
        within(dialog).getByRole("button", {
          name:
            scope === "all"
              ? "Scan boards"
              : scope === "selected"
                ? "Scan selected boards"
                : "Scan board",
        }),
      );
      await screen.findByText("Showing the scan you just requested.");
      expect(submitted).toEqual([
        {
          workspace_id: "workspace-1",
          task_type: "source.scan",
          payload: { source_ids: scope === "all" ? [] : [NORTHWIND.id], fresh_details: true },
        },
      ]);
    },
  );

  it("names the queued scan and keeps another manual scan available", async () => {
    const queued = scanTask({ status: "queued", source_ids: [NORTHWIND.id] });
    installOneBoard(queued, [queued]);
    panel();
    expect(await screen.findByText("Queued board scan: Northwind.")).not.toBeNull();
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Scan now" }).disabled).toBe(
      false,
    );
  });
  it("carries no free or paid label", async () => {
    installOneBoard(scanTask());
    panel();

    const control = screen.getByRole("button", { name: "Scan now" });
    await screen.findByText("Sources (1 enabled of 1): scan all, selected, or one");
    expect(control.className).not.toContain("button--paid");
    expect(control.closest(".paid-action")).toBeNull();
    expect(screen.queryByText("Paid")).toBeNull();
    expect(screen.queryByText(/provider charge/i)).toBeNull();
  });

  it("says which openings the boards stopped listing", async () => {
    installOneBoard(
      scanTask({
        progress: {
          scanned: [{ source: "Acme", open: 4, matched: 1, new: 0 }],
          new_discoveries: 0,
          skipped: [],
          withdrawn: [{ source: "Acme", employer: "Acme", title: "Data Platform Lead" }],
        },
      }),
    );

    panel();
    await screen.findByText("Sources (1 enabled of 1): scan all, selected, or one");
    fireEvent.click(screen.getByRole("button", { name: "Scan now" }));
    fireEvent.click(screen.getByRole("button", { name: "Scan boards" }));

    const summary = await screen.findByText(/no longer listed/);
    const disclosure = summary.closest("details");
    if (!disclosure) throw new Error("Expected the withdrawn summary inside a disclosure.");
    expect(within(disclosure).getByText("Data Platform Lead")).toBeTruthy();
    expect(within(disclosure).getByText("Acme")).toBeTruthy();
  });

  it("says nothing about withdrawals when the boards still list everything", async () => {
    installOneBoard(
      scanTask({
        progress: {
          scanned: [{ source: "Acme", open: 4, matched: 1, new: 1 }],
          new_discoveries: 1,
          skipped: [],
          withdrawn: [],
        },
      }),
    );

    panel();
    await screen.findByText("Sources (1 enabled of 1): scan all, selected, or one");
    fireEvent.click(screen.getByRole("button", { name: "Scan now" }));
    fireEvent.click(screen.getByRole("button", { name: "Scan boards" }));

    expect(await screen.findByText(/1 new posting/)).toBeTruthy();
    expect(screen.queryByText(/no longer listed/)).toBeNull();
    expect(screen.getByRole("heading", { name: "New candidate records" })).toBeTruthy();
    expect(screen.getByText(/ready, recovery, or screened-out view/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Go to Review" }).getAttribute("href")).toBe(
      "?tab=review",
    );
    expect(
      screen.getByRole("link", { name: "Open this scan in History" }).getAttribute("href"),
    ).toBe("?tab=boards&scan=task-1#screened-candidates");
    expect(screen.getByText("Scan audit").closest("details")!.open).toBe(false);
  });

  it("says how many rows the run's own content reads found closed", async () => {
    installOneBoard(
      scanTask({
        progress: {
          scanned: [{ source: "Acme", open: 4, matched: 2, new: 2 }],
          new_discoveries: 2,
          skipped: [],
          withdrawn: [],
          withdrawn_on_read: 1,
        },
      }),
    );

    panel();
    await screen.findByText("Sources (1 enabled of 1): scan all, selected, or one");
    fireEvent.click(screen.getByRole("button", { name: "Scan now" }));
    fireEvent.click(screen.getByRole("button", { name: "Scan boards" }));

    expect(await screen.findByText(/found 1 posting closed by the publisher/)).toBeTruthy();
    expect(screen.queryByText(/no longer listed/)).toBeNull();
  });

  it("reconciles returned source outcomes and failed reads in the immediate result", async () => {
    installOneBoard(
      scanTask({
        progress: {
          scanned: [{ source: "Northwind", open: 4, matched: 0, new: 0 }],
          failed: [{ source: "Southgate", reason: "publisher refused the request" }],
          new_discoveries: 0,
        },
      }),
    );

    panel();
    await screen.findByText("Sources (1 enabled of 1): scan all, selected, or one");
    fireEvent.click(screen.getByRole("button", { name: "Scan now" }));
    fireEvent.click(screen.getByRole("button", { name: "Scan boards" }));

    expect(
      await screen.findByText(
        /Attempted 2 sources in this run: 1 source was read and 1 could not be read/,
      ),
    ).toBeTruthy();
  });

  it("labels phase-specific progress without changing units", async () => {
    installOneBoard(
      scanTask({
        status: "running",
        progress: { phase: "reading_content", current: 24, total: 53 },
      }),
    );
    panel();

    await screen.findByText("Sources (1 enabled of 1): scan all, selected, or one");
    fireEvent.click(screen.getByRole("button", { name: "Scan now" }));
    fireEvent.click(screen.getByRole("button", { name: "Scan boards" }));
    expect((await screen.findByText(/Reading posting content: 24\/53/)).getAttribute("role")).toBe(
      "status",
    );
    expect(screen.queryByText(/Scanning sources/)).toBeNull();
  });

  it("renders scan sources and withdrawn openings as tables, not bulleted lists", async () => {
    installOneBoard(
      scanTask({
        progress: {
          scanned: [{ source: "Northwind", open: 4, matched: 1, new: 0 }],
          new_discoveries: 0,
          skipped: [],
          withdrawn: [{ source: "Northwind", employer: "Northwind Freight", title: "Fleet Lead" }],
        },
      }),
    );
    panel();

    await screen.findByText("Sources (1 enabled of 1): scan all, selected, or one");
    fireEvent.click(screen.getByText("Sources (1 enabled of 1): scan all, selected, or one"));
    const sourcesTable = screen.getByRole("table", { name: "Scan sources" });
    expect(within(sourcesTable).getByRole("columnheader", { name: "Source" })).toBeTruthy();
    expect(
      within(sourcesTable).getByRole("columnheader", { name: "Reading condition" }),
    ).toBeTruthy();
    expect(within(sourcesTable).getByText("Northwind")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Scan now" }));
    fireEvent.click(screen.getByRole("button", { name: "Scan boards" }));

    const withdrawnTable = await screen.findByRole("table", { name: "Openings no longer listed" });
    expect(within(withdrawnTable).getByRole("columnheader", { name: "Role" })).toBeTruthy();
    expect(within(withdrawnTable).getByRole("columnheader", { name: "Employer" })).toBeTruthy();
    expect(within(withdrawnTable).getByText("Fleet Lead")).toBeTruthy();
    expect(within(withdrawnTable).getByText("Northwind Freight")).toBeTruthy();
  });

  it("separates a board that never listed an opening from one whose openings the screen dropped", async () => {
    installHandlers(scanTask(), [
      boardSource({
        id: "source-4",
        name: "Fabrikam",
        employer: "Fabrikam",
        url: "https://jobs.example.test/vendor",
        last_status: "failed",
        last_scanned_on: "2026-09-01",
        last_read_outcome: "policy_refused",
        scan_count: 8,
        staged_count: 0,
      }),
      boardSource({
        id: "source-5",
        name: "Litware",
        employer: "Litware",
        url: "https://jobs.example.test/quiet",
        last_status: "incomplete",
        last_scanned_on: "2026-09-01",
        last_read_outcome: "crawl_continuation",
        scan_count: 12,
        staged_count: 0,
        read_scan_count: 12,
        first_read_on: "2026-07-01",
      }),
      boardSource({
        id: "source-9",
        name: "Coho",
        employer: "Coho",
        url: "https://jobs.example.test/broken",
        last_status: "failed",
        last_scanned_on: "2026-09-01",
        last_read_outcome: "fetch_failed",
        scan_count: 3,
        failed_scan_count: 3,
      }),
      boardSource({
        id: "source-11",
        name: "Woodgrove",
        employer: "Woodgrove",
        url: "https://jobs.example.test/partial",
        last_status: "incomplete",
        last_scanned_on: "2026-09-01",
        last_read_outcome: "crawl_continuation",
        scan_count: 4,
        incomplete_scan_count: 3,
      }),
      boardSource({
        id: "source-12",
        name: "Lucerne",
        employer: "Lucerne",
        url: "https://jobs.example.test/older",
        last_status: "completed",
        last_scanned_on: "2026-09-01",
        last_read_outcome: "ats_complete",
        scan_count: 2,
        staged_count: 5,
        read_scan_count: 2,
        first_read_on: "2026-07-01",
      }),
      boardSource({
        id: "source-13",
        name: "Trey",
        employer: "Trey",
        url: "https://jobs.example.test/down",
        last_status: "failed",
        last_scanned_on: "2026-09-01",
        last_read_outcome: "fetch_failed",
        scan_count: 3,
        read_scan_count: 1,
        first_read_on: "2026-08-10",
        last_listed_on: "2026-08-10",
      }),
      boardSource({
        id: "source-10",
        name: "Wingtip",
        employer: "Wingtip",
        url: "https://jobs.example.test/flaky",
        last_status: "failed",
        last_scanned_on: "2026-09-01",
        last_read_outcome: "fetch_failed",
        scan_count: 5,
        read_scan_count: 2,
        first_read_on: "2026-08-01",
      }),
      boardSource({
        id: "source-7",
        name: "Contoso",
        employer: "Contoso",
        url: "https://jobs.example.test/lapsed",
        last_status: "completed_empty",
        last_scanned_on: "2026-09-01",
        last_read_outcome: "ats_complete",
        scan_count: 6,
        staged_count: 0,
        first_read_on: "2026-07-01",
        last_listed_on: "2026-08-10",
      }),
      boardSource({
        id: "source-8",
        name: "Adatum",
        employer: "Adatum",
        url: "https://jobs.example.test/new",
      }),
      boardSource({
        id: "source-14",
        name: "Northwind",
        employer: "Northwind",
        url: "https://jobs.example.test/skipped",
        last_status: "omitted",
        last_scanned_on: "2026-09-01",
        scan_count: 2,
      }),
      boardSource({
        id: "source-6",
        name: "Proseware",
        employer: "Proseware",
        url: "https://jobs.example.test/busy",
        last_status: "completed",
        last_scanned_on: "2026-09-01",
        last_read_outcome: "ats_complete",
        scan_count: 12,
        staged_count: 31,
        first_read_on: "2026-07-01",
        last_listed_on: "2026-09-01",
      }),
    ]);
    panel();

    await screen.findByText("Sources (11 enabled of 11): scan all, selected, or one");
    fireEvent.click(screen.getByText("Sources (11 enabled of 11): scan all, selected, or one"));
    const table = screen.getByRole("table", { name: "Scan sources" });
    expect(within(table).getByRole("columnheader", { name: "What it has produced" })).toBeTruthy();

    const rowFor = (name: string) => {
      const row = within(table).getByText(name).closest("tr");
      if (!row) throw new Error(`Expected a scan-source row for ${name}.`);
      return row;
    };
    expect(
      within(rowFor("Fabrikam")).getByText("Refused by policy, so no sweep can read it"),
    ).toBeTruthy();
    expect(
      within(rowFor("Litware")).getByText("No listings in 12 scans since 7/1/2026"),
    ).toBeTruthy();
    expect(within(rowFor("Contoso")).getByText("Listed, none kept")).toBeTruthy();
    // The lapse is its own block line, not a run-on of the yield note.
    expect(within(rowFor("Contoso")).getByText("No listings since 8/10/2026").tagName).toBe("P");
    expect(within(rowFor("Proseware")).getByText("31 kept across scans")).toBeTruthy();
    expect(within(rowFor("Proseware")).queryByText(/No listings since/)).toBeNull();
    expect(within(rowFor("Adatum")).getByText("Never read")).toBeTruthy();
    expect(within(rowFor("Northwind")).getByText("Never read")).toBeTruthy();
    expect(
      within(rowFor("Coho")).getByText("Every read failed in 3 scans; see Board scan history"),
    ).toBeTruthy();
    expect(
      within(rowFor("Woodgrove")).getByText(
        "Never read in full in 3 scans; see Board scan history",
      ),
    ).toBeTruthy();
    expect(within(rowFor("Lucerne")).getByText("5 kept across scans")).toBeTruthy();
    expect(within(rowFor("Trey")).getByText("Listed, none kept")).toBeTruthy();
    expect(within(rowFor("Trey")).queryByText(/No listings since/)).toBeNull();
    expect(
      within(rowFor("Wingtip")).getByText("No listings in 2 scans since 8/1/2026"),
    ).toBeTruthy();
  });

  it("previews the exact enabled scope and reconnects to durable scan history", async () => {
    installOneBoard(scanTask({ progress: { scanned: [], new_discoveries: 0 } }), [scanTask()]);
    panel();

    expect(
      await screen.findByText("Sources (1 enabled of 1): scan all, selected, or one"),
    ).toBeTruthy();
    fireEvent.click(screen.getByText("Sources (1 enabled of 1): scan all, selected, or one"));
    expect(screen.getByText("Northwind")).toBeTruthy();
    expect(screen.getByText("Public board API")).toBeTruthy();
    expect(await screen.findByText(/Attempted 0 sources/)).toBeTruthy();
    expect(
      screen.getByText(/current scan scope has 1 enabled source; this completed run attempted 0/),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Scan now" }));
    expect(screen.getByRole("dialog").textContent).toMatch(/mark linked openings no longer listed/);
    expect(screen.getByRole("table", { name: "Sources this scan will read" })).toBeTruthy();
  });
});

/** Answer the enqueue and the task read through MSW, recording what was submitted. */
function installHandlers(
  task: Task,
  sources: Source[] = [NORTHWIND, INITECH],
  existing: Task[] = [],
) {
  const submitted: unknown[] = [];
  server.use(
    taskTypesHandler(),
    http.get("*/api/v1/workspaces/workspace-1/sources", () =>
      HttpResponse.json(sourceList(sources)),
    ),
    http.get("*/api/v1/tasks", () => HttpResponse.json(existing)),
    http.post("*/api/v1/tasks", async ({ request }) => {
      submitted.push(await request.json());
      return HttpResponse.json(scanTask({ status: "queued" }));
    }),
    http.get("*/api/v1/tasks/task-1", () => HttpResponse.json(task)),
  );
  return submitted;
}

/** The first panel tests measure one enabled board rather than the pair. */
function installOneBoard(task: Task, existing: Task[] = []) {
  return installHandlers(task, [NORTHWIND], existing);
}

describe("ScanPanel board selection", () => {
  it.each([
    [
      "already_in_progress",
      "Initech already has a read in progress; this run did not read it again.",
    ],
    [
      "resumed",
      "Retained work for Initech was queued to resume; this run did not read the board again.",
    ],
  ])("reports a %s single-source outcome without claiming a read", async (key, message) => {
    const task = scanTask({
      source_ids: ["source-2"],
      progress: {
        scanned: [],
        failed: [],
        new_discoveries: 0,
        [key]: [{ source_id: "source-2", source: "Initech" }],
      },
    });
    installHandlers(task, [NORTHWIND, INITECH], [task]);
    panel();
    expect(await screen.findByText(message)).toBeTruthy();
    expect(screen.queryByText(/^Read Initech:/)).toBeNull();
  });

  it("names selected boards and distinguishes reads, busy boards, and resumed work", async () => {
    const task = scanTask({
      source_ids: ["source-1", "source-2", "source-3"],
      progress: {
        scanned: [{ source: "Northwind", open: 2, matched: 0, new: 0 }],
        failed: [],
        new_discoveries: 0,
        already_in_progress: [{ source_id: "source-2", source: "Initech" }],
        resumed: [{ source_id: "source-3", source: "Contoso" }],
      },
    });
    installHandlers(task, [NORTHWIND, INITECH, CONTOSO], [task]);
    panel();
    expect(
      await screen.findByText(/Attempted 1 source in this run: 1 source was read/),
    ).toBeTruthy();
    expect(
      screen.getByText("Already in progress: Initech. This run did not read these boards again."),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Retained work queued to resume: Contoso. These boards were not read again by this run.",
      ),
    ).toBeTruthy();
    expect(screen.getByText("This run selected Northwind, Initech, Contoso.")).toBeTruthy();
    expect(screen.queryByText(/This run scanned/)).toBeNull();
  });

  it("scans the one board a row names, and measures the run against it", async () => {
    const submitted = installHandlers(
      scanTask({
        source_ids: ["source-2"],
        progress: {
          scanned: [{ source: "Initech", open: 2, matched: 0, new: 0 }],
          new_discoveries: 0,
        },
      }),
    );
    panel();

    await screen.findByText("Sources (2 enabled of 2): scan all, selected, or one");
    const row = screen.getByRole("button", { name: "Scan Initech" });
    expect(row.textContent).toBe("Scan");
    fireEvent.click(row);
    expect(screen.getByRole("dialog").textContent).toMatch(
      "Reads Initech alone, under its reading condition: Public board API. A complete board read",
    );
    fireEvent.click(await screen.findByRole("button", { name: "Scan board" }));

    expect(
      await screen.findByText("Read Initech: 0 new postings recorded and routed."),
    ).toBeTruthy();
    expect(screen.queryByText(/current scan scope has/)).toBeNull();
    expect(screen.queryByText(/This run scanned/)).toBeNull();
    expect(submitted).toEqual([
      {
        workspace_id: "workspace-1",
        task_type: "source.scan",
        payload: { source_ids: ["source-2"] },
      },
    ]);
  });

  it("says plainly when the one board it read could not be read", async () => {
    installHandlers(
      scanTask({
        source_ids: ["source-2"],
        progress: {
          scanned: [],
          failed: [{ source: "Initech", reason: "publisher refused the request" }],
          new_discoveries: 0,
        },
      }),
    );
    panel();

    await screen.findByText("Sources (2 enabled of 2): scan all, selected, or one");
    fireEvent.click(screen.getByRole("button", { name: "Scan Initech" }));
    fireEvent.click(await screen.findByRole("button", { name: "Scan board" }));

    expect(await screen.findByText("Could not read Initech; no postings recorded.")).toBeTruthy();
  });

  it("counts a one-board run against that board before the first progress event", async () => {
    installHandlers(scanTask({ status: "running", source_ids: ["source-2"] }));
    panel();

    await screen.findByText("Sources (2 enabled of 2): scan all, selected, or one");
    fireEvent.click(screen.getByRole("button", { name: "Scan Initech" }));
    fireEvent.click(await screen.findByRole("button", { name: "Scan board" }));

    expect((await screen.findByText("Scanning sources: 0/1")).getAttribute("role")).toBe("status");
  });

  it("names the board being read while the sweep reports one", async () => {
    installHandlers(
      scanTask({ status: "running", progress: { source: "Northwind", current: 1, total: 2 } }),
    );
    panel();

    await screen.findByText("Sources (2 enabled of 2): scan all, selected, or one");
    fireEvent.click(screen.getByRole("button", { name: "Scan now" }));
    fireEvent.click(await screen.findByRole("button", { name: "Scan boards" }));

    expect((await screen.findByText("Scanning Northwind: 1/2")).getAttribute("role")).toBe(
      "status",
    );
  });

  it("filters the scan sources by name and still scans the row that remains", async () => {
    const submitted = installHandlers(
      scanTask({
        source_ids: ["source-2"],
        progress: { scanned: [{ source: "Initech", open: 2, matched: 0, new: 0 }] },
      }),
    );
    panel();

    await screen.findByText("Sources (2 enabled of 2): scan all, selected, or one");
    const search = screen.getByRole("searchbox", { name: "Search sources" });
    fireEvent.change(search, { target: { value: "init" } });

    const table = screen.getByRole("table", { name: "Scan sources" });
    expect(within(table).getByText("Initech")).toBeTruthy();
    expect(within(table).queryByText("Northwind")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Scan Initech" }));
    fireEvent.click(await screen.findByRole("button", { name: "Scan board" }));
    await screen.findByText("Read Initech: 0 new postings recorded and routed.");
    expect(submitted).toEqual([
      {
        workspace_id: "workspace-1",
        task_type: "source.scan",
        payload: { source_ids: ["source-2"] },
      },
    ]);
  });

  it("says so in a sentence when no source matches the filter", async () => {
    installHandlers(scanTask());
    panel();

    await screen.findByText("Sources (2 enabled of 2): scan all, selected, or one");
    fireEvent.change(screen.getByRole("searchbox", { name: "Search sources" }), {
      target: { value: "zzz" },
    });

    expect(screen.getByText("No source matches that name.")).toBeTruthy();
    expect(screen.queryByRole("table", { name: "Scan sources" })).toBeNull();
  });

  it("lists a source excluded from bulk scans and scans it on its own", async () => {
    const submitted = installHandlers(
      scanTask({
        source_ids: ["source-3"],
        progress: { scanned: [{ source: "Contoso", open: 1, matched: 0, new: 0 }] },
      }),
      [NORTHWIND, CONTOSO],
    );
    panel();

    await screen.findByText("Sources (1 enabled of 2): scan all, selected, or one");
    const table = screen.getByRole("table", { name: "Scan sources" });
    const row = within(table).getByText("Contoso").closest("tr");
    if (!row) throw new Error("Expected a table row for the source left out of the sweep.");
    expect(within(row).getByText("No")).toBeTruthy();

    fireEvent.click(within(row).getByRole("button", { name: "Scan Contoso" }));
    fireEvent.click(await screen.findByRole("button", { name: "Scan board" }));

    await screen.findByText("Read Contoso: 0 new postings recorded and routed.");
    expect(submitted).toEqual([
      {
        workspace_id: "workspace-1",
        task_type: "source.scan",
        payload: { source_ids: ["source-3"] },
      },
    ]);
  });

  it("names no source when the whole scope is scanned", async () => {
    const submitted = installHandlers(
      scanTask({
        progress: {
          scanned: [
            { source: "Initech", open: 2, matched: 0, new: 0 },
            { source: "Northwind", open: 1, matched: 0, new: 0 },
          ],
          new_discoveries: 0,
        },
      }),
    );
    panel();

    await screen.findByText("Sources (2 enabled of 2): scan all, selected, or one");
    fireEvent.click(screen.getByRole("button", { name: "Scan now" }));
    fireEvent.click(await screen.findByRole("button", { name: "Scan boards" }));

    expect(await screen.findByText(/Attempted 2 sources/)).toBeTruthy();
    expect(screen.queryByText(/This run scanned/)).toBeNull();
    expect(submitted).toEqual([
      { workspace_id: "workspace-1", task_type: "source.scan", payload: { source_ids: [] } },
    ]);
  });
});

describe("ScanPanel source subsets", () => {
  it("confirms and scans an exact subset including a disabled source", async () => {
    const submitted = installHandlers(scanTask({ source_ids: ["source-1", "source-3"] }), [
      NORTHWIND,
      INITECH,
      CONTOSO,
    ]);
    panel();

    fireEvent.click(await screen.findByRole("checkbox", { name: "Select Northwind for scan" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Contoso for scan" }));
    expect(screen.getByText("2 sources selected.")).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "Included in bulk scans" })).toBeTruthy();
    const row = screen.getByText("Contoso").closest("tr");
    if (!row) throw new Error("Expected the disabled source row.");
    expect(within(row).getByText("No")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Scan selected sources" }));

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/Reads exactly 2 selected sources/)).toBeTruthy();
    expect(within(dialog).getByText(/Boards already being read are skipped/)).toBeTruthy();
    const scope = within(dialog).getByRole("table", { name: "Sources this scan will read" });
    expect(within(scope).getByText("Northwind")).toBeTruthy();
    expect(within(scope).getByText("Contoso")).toBeTruthy();
    expect(within(scope).queryByText("Initech")).toBeNull();
    expect(within(scope).getAllByText("Public board API")).toHaveLength(2);
    fireEvent.click(within(dialog).getByRole("button", { name: "Scan selected boards" }));

    await screen.findByText("This run selected Northwind, Contoso.");
    expect(submitted).toEqual([
      {
        workspace_id: "workspace-1",
        task_type: "source.scan",
        payload: { source_ids: ["source-1", "source-3"] },
      },
    ]);
    expect(within(row).getByText("No")).toBeTruthy();
  });

  it("clears selection visibly when the source filter changes", async () => {
    const submitted = installHandlers(scanTask(), [NORTHWIND, INITECH, CONTOSO]);
    panel();

    fireEvent.click(await screen.findByRole("checkbox", { name: "Select Northwind for scan" }));
    fireEvent.change(screen.getByRole("searchbox", { name: "Search sources" }), {
      target: { value: "initech" },
    });

    expect(screen.getByText("Selection cleared because the source filter changed.")).toBeTruthy();
    expect(screen.getByText("0 sources selected.")).toBeTruthy();
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "Scan selected sources" }).disabled,
    ).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Select displayed sources" }));
    fireEvent.click(screen.getByRole("button", { name: "Scan selected sources" }));
    const scope = within(screen.getByRole("dialog")).getByRole("table", {
      name: "Sources this scan will read",
    });
    expect(within(scope).getByText("Initech")).toBeTruthy();
    expect(within(scope).queryByText("Northwind")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Scan selected boards" }));
    await screen.findByText(/Attempted 0 sources/);
    expect(submitted[0]).toEqual({
      workspace_id: "workspace-1",
      task_type: "source.scan",
      payload: { source_ids: ["source-2"] },
    });
  });

  it("clears selection and cancels a selected scan without submitting work", async () => {
    const submitted = installHandlers(scanTask());
    panel();

    await screen.findByRole("checkbox", { name: "Select Northwind for scan" });
    fireEvent.click(screen.getByRole("button", { name: "Select displayed sources" }));
    fireEvent.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(
      screen.getByRole<HTMLInputElement>("checkbox", { name: "Select Northwind for scan" }).checked,
    ).toBe(false);
    expect(screen.getByText("0 sources selected.")).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Initech for scan" }));
    fireEvent.click(screen.getByRole("button", { name: "Scan selected sources" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    expect(submitted).toEqual([]);
    expect(screen.getByText("1 source selected.")).toBeTruthy();
  });

  it("keeps the all-enabled action independent of selected and filtered sources", async () => {
    const submitted = installHandlers(scanTask(), [NORTHWIND, INITECH, CONTOSO]);
    panel();

    await screen.findByRole("checkbox", { name: "Select Northwind for scan" });
    fireEvent.change(screen.getByRole("searchbox", { name: "Search sources" }), {
      target: { value: "contoso" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Contoso for scan" }));
    fireEvent.click(screen.getByRole("button", { name: "Scan now" }));
    const dialog = screen.getByRole("dialog");
    const scope = within(dialog).getByRole("table", { name: "Sources this scan will read" });
    expect(within(scope).getByText("Northwind")).toBeTruthy();
    expect(within(scope).getByText("Initech")).toBeTruthy();
    expect(within(scope).queryByText("Contoso")).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "Scan boards" }));
    await screen.findByText(/Attempted 0 sources/);
    expect(submitted[0]).toEqual({
      workspace_id: "workspace-1",
      task_type: "source.scan",
      payload: { source_ids: [] },
    });
  });

  it("selects and submits every displayed source when there are more than 250", async () => {
    const sources = Array.from({ length: 251 }, (_, index) =>
      boardSource({
        id: `source-${index + 1}`,
        name: `Northwind board ${index + 1}`,
        enabled: index % 2 === 0,
      }),
    );
    const submitted = installHandlers(
      scanTask({ source_ids: sources.map((source) => source.id) }),
      sources,
    );
    let resolveTaskResponse!: () => void;
    const taskResponse = new Promise<void>((resolve) => {
      resolveTaskResponse = resolve;
    });
    const onResponse = ({ request }: { request: Request }) => {
      if (request.method === "GET" && new URL(request.url).pathname === "/api/v1/tasks/task-1") {
        resolveTaskResponse();
      }
    };
    panel();

    await screen.findByLabelText("Select Northwind board 251 for scan");
    fireEvent.click(screen.getByText("Select displayed sources", { selector: "button" }));
    expect(screen.getByText("251 sources selected.")).toBeTruthy();
    fireEvent.click(screen.getByText("Scan selected sources", { selector: "button" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/Reads exactly 251 selected sources/)).toBeTruthy();
    const scope = within(dialog).getByRole("table", { name: "Sources this scan will read" });
    expect(within(scope).getAllByRole("row", { hidden: true })).toHaveLength(252);
    server.events.on("response:mocked", onResponse);
    try {
      fireEvent.click(within(dialog).getByRole("button", { name: "Scan selected boards" }));
      await taskResponse;
      await act(async () => {});
    } finally {
      server.events.removeListener("response:mocked", onResponse);
    }
    expect(await screen.findByText(/This run selected Northwind board 1,/)).toBeTruthy();
    expect(submitted[0]).toEqual({
      workspace_id: "workspace-1",
      task_type: "source.scan",
      payload: { source_ids: sources.map((source) => source.id) },
    });
  });

  it("allows another subset while preserving the named progress of an active scan", async () => {
    const active = scanTask({
      status: "running",
      source_ids: ["source-1"],
      progress: { source: "Northwind", current: 1, total: 1 },
    });
    const submitted = installHandlers(active, [NORTHWIND, INITECH], [active]);
    server.use(
      http.post("*/api/v1/tasks", async ({ request }) => {
        submitted.push(await request.json());
        return HttpResponse.json(
          scanTask({ id: "task-2", status: "queued", source_ids: ["source-2"] }),
        );
      }),
      http.get("*/api/v1/tasks/task-2", () =>
        HttpResponse.json(
          scanTask({
            id: "task-2",
            source_ids: ["source-2"],
            progress: { scanned: [{ source: "Initech" }] },
          }),
        ),
      ),
    );
    panel();

    fireEvent.click(await screen.findByRole("checkbox", { name: "Select Initech for scan" }));
    expect(screen.getByText("Scanning Northwind: 1/1")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Scan selected sources" }));
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Scan selected boards" }),
    );

    await screen.findByText("Read Initech: 0 new postings recorded and routed.");
    expect(
      screen.getByText("Other active scan: Northwind - running. Scanning Northwind: 1/1."),
    ).toBeTruthy();
    expect(screen.getByText("Showing the scan you just requested.")).toBeTruthy();
    expect(screen.getByRole("link", { name: "View scan tasks" }).getAttribute("href")).toBe(
      "/tasks",
    );
    expect(submitted[0]).toEqual({
      workspace_id: "workspace-1",
      task_type: "source.scan",
      payload: { source_ids: ["source-2"] },
    });
  });

  it("disables launchers only while a scan request is being submitted", async () => {
    const task = scanTask({ source_ids: ["source-2"] });
    installHandlers(task);
    let finishSubmission: () => void = () => {
      throw new Error("Submission did not start.");
    };
    let markStarted: () => void = () => {};
    const started = new Promise<void>((resolve) => {
      markStarted = resolve;
    });
    server.use(
      http.post("*/api/v1/tasks", async () => {
        const finished = new Promise<void>((resolve) => {
          finishSubmission = resolve;
        });
        markStarted();
        await finished;
        return HttpResponse.json(scanTask({ status: "queued", source_ids: ["source-2"] }));
      }),
    );
    panel();

    fireEvent.click(await screen.findByRole("checkbox", { name: "Select Initech for scan" }));
    fireEvent.click(screen.getByRole("button", { name: "Scan selected sources" }));
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Scan selected boards" }),
    );
    await started;
    try {
      expect(
        (await screen.findByRole<HTMLButtonElement>("button", { name: "Submitting scan" }))
          .disabled,
      ).toBe(true);
      expect(
        screen.getByRole<HTMLButtonElement>("button", { name: "Scan selected sources" }).disabled,
      ).toBe(true);
      expect(
        screen.getByRole<HTMLButtonElement>("button", { name: "Scan Northwind" }).disabled,
      ).toBe(true);
    } finally {
      finishSubmission();
    }
    await screen.findByText(/No board read was recorded for Initech/);
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Scan now" }).disabled).toBe(
      false,
    );
  });
});
