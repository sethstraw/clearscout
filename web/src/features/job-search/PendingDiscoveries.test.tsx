import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw/http";
import { delay } from "msw/utils/delay";
import { createElement } from "react";
import { Link, MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { PendingDiscoveryChanges, RelevanceReview } from "../../api/client";
import type { PendingDiscovery } from "../../api/client/types";
import type * as PendingRowTitleModule from "./PendingRowTitle";

type ScreeningFinding = PendingDiscovery["screening_findings"][number];
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { requestLog } from "../../test/requestLog";
import { evaluationPreflightHandler } from "../../test/taskTypes";
import {
  taskRecord,
  foundPostingCheck,
  pendingDiscoveryChanges,
  pendingDiscoveryPage,
  pendingDiscoveryRow,
  pendingDiscoveryRows,
  queueFacets,
  queueFilterFailures,
  publishedFact,
  QUEUE_SNAPSHOT_BEFORE,
  QUEUE_SNAPSHOT_SHA256,
  relevanceReview,
  stagedFacts,
} from "./pendingDiscoveryFixtures";
import {
  CURRENT_MASTER_CV_VERSIONS,
  dispositionHandlers,
  pendingQueueHandlers,
  type PendingQueueReads,
} from "../../test/msw/pendingDiscoveryHandlers";
import { PENDING_DISCOVERIES_LIMIT, PendingDiscoveries } from "./PendingDiscoveries";

const WORKSPACE_ROUTE = "*/api/v1/workspaces/:workspaceId";

// Every pending row renders one title, so this wrapper counts how often a row
// body re-executes; the real component still does the rendering.
const rowBodyRenders = vi.hoisted(() => new Map<string, number>());
vi.mock("./PendingRowTitle", async (importOriginal) => {
  const actual = await importOriginal<typeof PendingRowTitleModule>();
  return {
    PendingRowTitle: (props: Parameters<typeof actual.PendingRowTitle>[0]) => {
      rowBodyRenders.set(props.pendingId, (rowBodyRenders.get(props.pendingId) ?? 0) + 1);
      return createElement(actual.PendingRowTitle, props);
    },
  };
});

function installQueue(reads: PendingQueueReads = {}) {
  server.use(...pendingQueueHandlers(reads));
}

function openMoreFilters() {
  const summary = screen.getByText(/^More filters/);
  if (!summary.closest("details")?.open) fireEvent.click(summary);
}

/** Serve one queue of rows through the page route's legacy array shape. */
function installRows(rows: PendingDiscovery[], reads: PendingQueueReads = {}) {
  installQueue({ page: rows, ...reads });
}

const ADD_URL = "https://jobs.example.com/postings/1234";

const QUEUE_RESTART_NOTICE =
  "The queue order changed, so paging restarted from the beginning and your selection was cleared.";

const UNREADABLE_CHECK = foundPostingCheck({
  status: "unreadable",
  note: "The publisher returned HTTP 503.",
  title: "",
  location: "",
  staged_facts: undefined,
});

const UNSUPPORTED_CHECK = foundPostingCheck({
  ...UNREADABLE_CHECK,
  status: "unsupported",
  note: "No specialized publisher reader is available.",
  platform: "unknown",
});

/** Answer every read with an empty queue and accept the checked add. */
function installAddFlow(
  options: {
    outcome?: "created" | "requeued";
    addCheckStatus?: "readable" | "unreadable";
    constraintConfidence?: PendingDiscovery["constraint_confidence"];
    previewStatus?: "readable" | "unsupported";
  } = {},
) {
  const {
    outcome = "created",
    addCheckStatus = "readable",
    constraintConfidence = "investigate",
    previewStatus = "readable",
  } = options;
  const log = requestLog();
  let added = false;
  const staged = pendingDiscoveryRow({
    id: "pending-1",
    employer: "Example Health",
    title: "Product Manager",
    discovery_url: ADD_URL,
    canonical_url: ADD_URL,
    retained_content_id: "content-1",
    current_content_id: "content-1",
    reason: "Added from a supplied link.",
    first_seen_on: "2026-08-24",
    recommendation_because: "Needs evaluation.",
    constraint_confidence: constraintConfidence,
    staged_facts: stagedFacts({
      locations: publishedFact(["Remote"], "lever.location"),
      arrangements: publishedFact(["Remote"], "lever.arrangement"),
      employment_types: publishedFact(["Full-time"], "lever.employment_type"),
    }),
  });
  const readable = foundPostingCheck();
  server.use(
    http.post(`${WORKSPACE_ROUTE}/pending-discoveries/check`, async ({ request }) => {
      await log.record(request);
      return HttpResponse.json(previewStatus === "readable" ? readable : UNSUPPORTED_CHECK);
    }),
    http.post("*/api/v1/tasks", async ({ request }) => {
      await log.record(request);
      return HttpResponse.json({ id: "task-1", status: "queued" });
    }),
    http.get("*/api/v1/tasks/task-1", () =>
      HttpResponse.json({ id: "task-1", status: "completed" }),
    ),
    http.post(`${WORKSPACE_ROUTE}/pending-discoveries`, async ({ request }) => {
      await log.record(request);
      added = true;
      return HttpResponse.json({
        outcome,
        pending_discovery: staged,
        posting_check: addCheckStatus === "readable" ? readable : UNREADABLE_CHECK,
      });
    }),
    ...pendingQueueHandlers({ onRequest: log.reads, page: () => (added ? [staged] : []) }),
  );
  return log.requests;
}

describe("PendingDiscoveries", () => {
  it("keeps uncertainty group focus while counts load and ignores repeat activation", async () => {
    window.localStorage.setItem("clearscout.pendingStrictUncertainty.workspace-1", "true");
    let releaseInitial = () => {};
    let releaseGroup = () => {};
    let initialStarted = () => {};
    let groupStarted = () => {};
    const initialWait = new Promise<void>((resolve) => {
      releaseInitial = resolve;
    });
    const groupWait = new Promise<void>((resolve) => {
      releaseGroup = resolve;
    });
    const initialRequest = new Promise<void>((resolve) => {
      initialStarted = resolve;
    });
    const groupRequest = new Promise<void>((resolve) => {
      groupStarted = resolve;
    });
    const pursue = pendingDiscoveryRow({
      ...pendingDiscoveryRows(2)[0],
      constraint_confidence: "pursue",
    });
    const uncertain = pendingDiscoveryRows(2)[1];
    if (!uncertain) throw new Error("Expected an uncertain posting.");
    const reads: URL[] = [];
    installQueue();
    server.use(
      http.get(`${WORKSPACE_ROUTE}/pending-discoveries/page`, async ({ request }) => {
        const url = new URL(request.url);
        reads.push(url);
        const clarification = url.searchParams.get("constraint") === "needs_clarification";
        if (clarification) {
          groupStarted();
          await groupWait;
        } else {
          initialStarted();
          await initialWait;
        }
        const items = clarification ? [uncertain] : [pursue, uncertain];
        return HttpResponse.json(
          pendingDiscoveryPage({
            ready: { items, total: items.length },
            queue_total: 2,
            facets: queueFacets([pursue, uncertain], url.searchParams),
          }),
        );
      }),
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );
    try {
      await initialRequest;
      const loading = screen.getByRole<HTMLButtonElement>("button", {
        name: "Unconfirmed matches (loading)",
      });
      expect(loading.disabled).toBe(false);
      expect(loading.getAttribute("aria-disabled")).toBe("true");
      expect(loading.getAttribute("aria-busy")).toBe("true");
      fireEvent.click(loading);
      expect(reads).toHaveLength(1);
      await act(async () => {
        releaseInitial();
        await initialWait;
      });
      const clarification = await screen.findByRole<HTMLButtonElement>("button", {
        name: "Unconfirmed matches (1)",
      });
      clarification.focus();
      fireEvent.click(clarification);
      await groupRequest;
      expect(document.activeElement).toBe(clarification);
      expect(clarification.disabled).toBe(false);
      expect(clarification.getAttribute("aria-disabled")).toBe("true");
      expect(clarification.getAttribute("aria-busy")).toBe("true");
      fireEvent.click(clarification);
      fireEvent.click(screen.getByRole("button", { name: "Main queue (1)" }));
      expect(
        reads.filter((url) => url.searchParams.get("constraint") === "needs_clarification"),
      ).toHaveLength(1);
      expect(reads.at(-1)?.searchParams.get("constraint")).toBe("needs_clarification");
      await act(async () => {
        releaseGroup();
        await groupWait;
      });
      await screen.findByRole("heading", { name: "Unconfirmed matches" });
      await waitFor(() => expect(clarification.getAttribute("aria-disabled")).toBe("false"));
      expect(clarification.getAttribute("aria-busy")).toBe("false");
      expect(document.activeElement).toBe(clarification);
      expect(screen.queryByText("Role 0")).toBeNull();
      expect(screen.getByText("Role 1")).toBeTruthy();
    } finally {
      releaseInitial();
      releaseGroup();
    }
  });

  it("keeps clarification counts across content states and clears hidden selections when strict", async () => {
    const rows = pendingDiscoveryRows(12).map((row, index) => {
      const readiness = index < 9 ? "ready" : index < 10 ? "no_body" : "access_blocked";
      return pendingDiscoveryRow({
        ...row,
        constraint_confidence:
          index === 0
            ? "pursue"
            : index === 11
              ? "excluded"
              : (["investigate", "needs_verification", "uncertain_comp"] as const)[index % 3],
        favorite: index === 11,
        readiness,
        content_status:
          readiness === "ready"
            ? "readable"
            : readiness === "no_body"
              ? "not_yet_read"
              : "access_blocked",
        retained_content_id: readiness === "ready" ? row.retained_content_id : null,
      });
    });
    const reads: URL[] = [];
    installQueue({
      onPageRequest: (url) => {
        reads.push(url);
        const matching = rows.filter(
          (row) => queueFilterFailures(row, url.searchParams).size === 0,
        );
        const limit = Number(url.searchParams.get("limit") ?? 50);
        const lane = (ready: boolean) => {
          const items = matching.filter((row) => (row.readiness === "ready") === ready);
          const offset = Number(
            url.searchParams.get(ready ? "ready_offset" : "attention_offset") ?? 0,
          );
          return {
            items: items.slice(offset, offset + limit),
            total: items.length,
            offset,
            limit,
            has_more: offset + limit < items.length,
          };
        };
        return HttpResponse.json(
          pendingDiscoveryPage({
            queue_total: rows.length,
            ready: lane(true),
            attention: lane(false),
            facets: queueFacets(rows, url.searchParams),
          }),
        );
      },
    });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByText("Role 1");
    expect(reads[0]?.searchParams.has("readiness")).toBe(false);
    expect(screen.getByLabelText<HTMLSelectElement>("Content readiness").value).toBe("all");
    const groups = screen.getByRole("group", { name: "Uncertainty groups" });
    expect(within(groups).queryByRole("button", { name: "Unconfirmed matches (10)" })).toBeNull();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Role 1 at Globex" }));
    expect(screen.getByRole("button", { name: "Track selected (1)" })).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox", { name: "Separate uncertain postings" }));

    await waitFor(() => expect(screen.queryByText("Role 1")).toBeNull());
    expect(screen.getByText("Role 0")).toBeTruthy();
    expect(screen.queryByText("Role 11")).toBeNull();
    expect(screen.queryByRole("button", { name: /Track selected/ })).toBeNull();
    expect(reads.at(-1)?.searchParams.get("constraint")).toBe("pursue_only");
    fireEvent.click(screen.getByRole("button", { name: "Unconfirmed matches (10)" }));
    await screen.findByText("Role 1");
    expect(screen.getByRole("heading", { name: "Unconfirmed matches" })).toBeTruthy();
    expect(screen.getByText("10 of 12 in the queue match your filters; 2 hidden")).toBeTruthy();
    expect(screen.getByText("Role 9")).toBeTruthy();
    expect(screen.getByText("Role 10")).toBeTruthy();
    expect(screen.queryByText("Role 0")).toBeNull();
    expect(screen.queryByText("Role 11")).toBeNull();
    expect(reads.at(-1)?.searchParams.get("constraint")).toBe("needs_clarification");
    fireEvent.change(screen.getByLabelText("Content readiness"), { target: { value: "ready" } });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Unconfirmed matches (8)" })).toBeTruthy(),
    );
    expect(screen.queryByText("Role 9")).toBeNull();
    openMoreFilters();
    fireEvent.change(screen.getByLabelText("Constraint route"), { target: { value: "excluded" } });
    await screen.findByText("Role 11");
    expect(
      screen.getByRole<HTMLInputElement>("checkbox", { name: "Separate uncertain postings" })
        .checked,
    ).toBe(true);
    fireEvent.click(screen.getByRole("checkbox", { name: "Separate uncertain postings" }));
    await screen.findByText("Role 1");
    expect(reads.at(-1)?.searchParams.get("constraint")).toBe("not_excluded");
  });

  it("opens on Screened out from the address and shows the refusal patterns there", async () => {
    window.localStorage.setItem("clearscout.pendingStrictUncertainty.workspace-1", "true");
    const reads: URL[] = [];
    const refused = pendingDiscoveryRow({
      ...pendingDiscoveryRows(2)[1],
      constraint_confidence: "excluded",
    });
    installQueue({
      onRequest: (url) => reads.push(url),
      page: [pendingDiscoveryRow({ constraint_confidence: "pursue" }), refused],
      refusalPatternProposals: [
        {
          id: "proposal-1",
          condition: "work_authorization",
          normalized_reason: "work authorization required",
          reason_examples: [],
          refusal_count: 3,
          refused_postings: [],
          first_seen_at: "2026-08-20T12:00:00Z",
          last_seen_at: "2026-08-22T12:00:00Z",
          state: "proposed",
          proposed_at: "2026-08-22T12:00:00Z",
          decided_at: null,
        },
      ],
    });
    render(
      <MemoryRouter initialEntries={["/find?tab=review&constraint=excluded_only"]}>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByText("Role 1");
    expect(screen.queryByText("Role 0")).toBeNull();
    const pageReads = reads.filter((url) => url.pathname.endsWith("/pending-discoveries/page"));
    expect(pageReads[0]?.searchParams.get("constraint")).toBe("excluded_only");
    expect(screen.getByRole("button", { name: /^Screened out/ }).getAttribute("aria-pressed")).toBe(
      "true",
    );
    expect(await screen.findByText("work authorization required")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^Main queue/ }));
    await screen.findByText("Role 0");
    expect(screen.queryByText("Recurring refusal patterns")).toBeNull();
  });

  it("keeps the Screened out filter and the address in step both ways", async () => {
    installQueue({
      page: [
        pendingDiscoveryRow({ ...pendingDiscoveryRows(2)[0], constraint_confidence: "pursue" }),
        pendingDiscoveryRow({ ...pendingDiscoveryRows(2)[1], constraint_confidence: "excluded" }),
      ],
    });
    function Address() {
      return <output aria-label="Address">{useLocation().search}</output>;
    }
    render(
      <MemoryRouter initialEntries={["/find?tab=review"]}>
        <AppProviders>
          <Link to="/find?tab=review&constraint=excluded_only">Screened out link</Link>
          <Address />
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );
    const screenedOut = () => screen.getByRole("button", { name: /^Screened out/ });
    await screen.findByText("Role 0");
    expect(screenedOut().getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(screen.getByRole("link", { name: "Screened out link" }));
    await waitFor(() => expect(screenedOut().getAttribute("aria-pressed")).toBe("true"));
    const mainQueue = screen.getByRole("button", { name: /^Main queue/ });
    await waitFor(() => expect(mainQueue.getAttribute("aria-busy")).toBe("false"));

    fireEvent.click(mainQueue);
    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Address" }).textContent).toBe("?tab=review"),
    );
    await waitFor(() => expect(screenedOut().getAttribute("aria-busy")).toBe("false"));
    fireEvent.click(screenedOut());
    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Address" }).textContent).toBe(
        "?tab=review&constraint=excluded_only",
      ),
    );
  });

  it("saves strict uncertainty visibility per workspace and restores it on reopening", async () => {
    installRows(pendingDiscoveryRows(1));
    const view = (workspaceId: string) => (
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId={workspaceId} />
        </AppProviders>
      </MemoryRouter>
    );
    const first = render(view("workspace-1"));
    await screen.findByText("Role 0");
    fireEvent.click(screen.getByRole("checkbox", { name: "Separate uncertain postings" }));
    expect(window.localStorage.getItem("clearscout.pendingStrictUncertainty.workspace-1")).toBe(
      "true",
    );
    first.rerender(view("workspace-2"));
    expect(
      screen.getByRole<HTMLInputElement>("checkbox", { name: "Separate uncertain postings" })
        .checked,
    ).toBe(false);
    first.rerender(view("workspace-1"));
    expect(
      screen.getByRole<HTMLInputElement>("checkbox", { name: "Separate uncertain postings" })
        .checked,
    ).toBe(true);
    first.unmount();
    render(view("workspace-1"));
    expect(
      screen.getByRole<HTMLInputElement>("checkbox", { name: "Separate uncertain postings" })
        .checked,
    ).toBe(true);
    openMoreFilters();
    expect(screen.getByLabelText<HTMLSelectElement>("Constraint route").value).toBe("pursue_only");
  });

  it("applies strict uncertainty visibility when local storage is blocked", async () => {
    const getItem = window.localStorage.getItem.bind(window.localStorage);
    const setItem = window.localStorage.setItem.bind(window.localStorage);
    const get = vi.spyOn(window.localStorage, "getItem").mockImplementation((key) => {
      if (key.startsWith("clearscout.pendingStrictUncertainty."))
        throw new Error("Storage blocked");
      return getItem(key);
    });
    const set = vi.spyOn(window.localStorage, "setItem").mockImplementation((key, value) => {
      if (key.startsWith("clearscout.pendingStrictUncertainty."))
        throw new Error("Storage blocked");
      setItem(key, value);
    });
    try {
      installRows(pendingDiscoveryRows(1));
      render(
        <MemoryRouter>
          <AppProviders>
            <PendingDiscoveries workspaceId="workspace-1" />
          </AppProviders>
        </MemoryRouter>,
      );
      await screen.findByText("Role 0");
      const toggle = screen.getByRole<HTMLInputElement>("checkbox", {
        name: "Separate uncertain postings",
      });
      expect(toggle.checked).toBe(false);
      fireEvent.click(toggle);
      expect(toggle.checked).toBe(true);
      openMoreFilters();
      expect(screen.getByLabelText<HTMLSelectElement>("Constraint route").value).toBe(
        "pursue_only",
      );
    } finally {
      get.mockRestore();
      set.mockRestore();
    }
  });

  it("requests independent ready and attention pages for the active queue", async () => {
    const requests = installAddFlow();
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByText(/No openings are waiting/);
    const activeQueueRequest = requests
      .map(({ url }) => url.href)
      .find((url) => url.includes("pending-discoveries/page?"));
    expect(activeQueueRequest).toContain(`limit=${PENDING_DISCOVERIES_LIMIT}`);
    expect(activeQueueRequest).toContain("ready_offset=0");
    expect(activeQueueRequest).toContain("attention_offset=0");
    expect(activeQueueRequest).not.toContain("expected_snapshot_sha256");
  });

  it("forwards the first-page snapshot when requesting a later lane page", async () => {
    const rows = pendingDiscoveryRows(1);
    const log = requestLog();
    installQueue({
      onRequest: log.reads,
      onPageRequest: (url) => {
        const readyOffset = Number(url.searchParams.get("ready_offset") ?? 0);
        return HttpResponse.json(
          pendingDiscoveryPage({
            ready: { items: rows, total: 100, offset: readyOffset, has_more: readyOffset === 0 },
          }),
        );
      },
    });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(
        screen.getByRole<HTMLButtonElement>("button", { name: "Next ready page" }).disabled,
      ).toBe(false),
    );
    fireEvent.click(screen.getByRole("button", { name: "Next ready page" }));

    await waitFor(() => {
      const laterPage = log.requests
        .map(({ url }) => url)
        .find(
          (url) =>
            url.pathname.endsWith("/pending-discoveries/page") &&
            url.searchParams.get("ready_offset") === "50",
        );
      expect(laterPage?.searchParams.get("expected_snapshot_sha256")).toBe(QUEUE_SNAPSHOT_SHA256);
      expect(laterPage?.searchParams.get("snapshot_before")).toBe(QUEUE_SNAPSHOT_BEFORE);
    });
  });

  it("pages forward again after returning to the first page", async () => {
    const rows = pendingDiscoveryRows(1);
    installQueue({
      onPageRequest: (url) => {
        const readyOffset = Number(url.searchParams.get("ready_offset") ?? 0);
        return HttpResponse.json(
          pendingDiscoveryPage({
            ready: { items: rows, total: 100, offset: readyOffset, has_more: readyOffset === 0 },
          }),
        );
      },
    });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );
    const button = (name: string) => screen.getByRole<HTMLButtonElement>("button", { name });

    await waitFor(() => expect(button("Next ready page").disabled).toBe(false));
    fireEvent.click(button("Next ready page"));
    await waitFor(() => expect(button("Previous ready page").disabled).toBe(false));
    fireEvent.click(button("Previous ready page"));

    await waitFor(() => expect(button("Next ready page").disabled).toBe(false));
  });

  it("restarts from an uncached first page when the queue snapshot conflicts", async () => {
    const [row] = pendingDiscoveryRows(1);
    if (!row) throw new Error("The queue fixture did not create a row.");
    const oldSnapshot = "a".repeat(64);
    const newSnapshot = "b".repeat(64);
    const log = requestLog();
    let firstPageReads = 0;
    installQueue({
      onRequest: log.reads,
      onPageRequest: (url) => {
        const readyOffset = Number(url.searchParams.get("ready_offset") ?? 0);
        if (readyOffset > 0) {
          return HttpResponse.json(
            { detail: "The pending discovery queue changed." },
            { status: 409, headers: { "Content-Type": "application/problem+json" } },
          );
        }
        // Only a read with no pinned snapshot starts a fresh first page.
        if (!url.searchParams.has("snapshot_before")) firstPageReads += 1;
        const refreshed = firstPageReads > 1;
        return HttpResponse.json(
          pendingDiscoveryPage({
            snapshot_sha256: refreshed ? newSnapshot : oldSnapshot,
            snapshot_before: refreshed ? "2026-09-12T01:00:00Z" : QUEUE_SNAPSHOT_BEFORE,
            ready: {
              items: [{ ...row, title: refreshed ? "Refreshed queue role" : row.title }],
              total: refreshed ? 1 : 100,
              has_more: !refreshed,
            },
          }),
        );
      },
    });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(
        screen.getByRole<HTMLButtonElement>("button", { name: "Next ready page" }).disabled,
      ).toBe(false),
    );
    fireEvent.click(screen.getByRole("button", { name: "Next ready page" }));

    await waitFor(() => expect(firstPageReads).toBe(2));
    expect(await screen.findByText(QUEUE_RESTART_NOTICE)).toBeTruthy();
    expect(await screen.findByText("Refreshed queue role")).toBeTruthy();
    expect(firstPageReads).toBe(2);
    // The restarted page commits its rows before its pagination controls.
    await waitFor(() =>
      expect(
        screen.getByRole<HTMLButtonElement>("button", { name: "Previous ready page" }).disabled,
      ).toBe(true),
    );
    const pageRequests = log.requests.map(({ url }) => url);
    const laterRequests = pageRequests.filter(
      (url) => url.searchParams.get("ready_offset") === "50",
    );
    expect(laterRequests.length).toBeGreaterThan(0);
    expect(
      laterRequests.every(
        (url) => url.searchParams.get("expected_snapshot_sha256") === oldSnapshot,
      ),
    ).toBe(true);
    const refreshedRequest = pageRequests
      .filter((url) => url.searchParams.get("ready_offset") === "0")
      .at(-1);
    expect(refreshedRequest?.searchParams.has("expected_snapshot_sha256")).toBe(false);
  });

  it("shows the refreshed rows when a conflict advances the snapshot under the same before mark", async () => {
    const [row] = pendingDiscoveryRows(1);
    if (!row) throw new Error("The queue fixture did not create a row.");
    const oldSnapshot = "a".repeat(64);
    const newSnapshot = "b".repeat(64);
    let conflicted = false;
    vi.useFakeTimers();
    try {
      installQueue({
        onPageRequest: (url) => {
          const readyOffset = Number(url.searchParams.get("ready_offset") ?? 0);
          if (readyOffset > 0) {
            conflicted = true;
            return HttpResponse.json(
              { detail: "The pending discovery queue changed." },
              { status: 409, headers: { "Content-Type": "application/problem+json" } },
            );
          }
          // The queue is reordered within the same before mark, so only the hash moves.
          return HttpResponse.json(
            pendingDiscoveryPage({
              snapshot_sha256: conflicted ? newSnapshot : oldSnapshot,
              snapshot_before: QUEUE_SNAPSHOT_BEFORE,
              ready: {
                items: [
                  { ...row, title: conflicted ? "Reordered queue role" : "Original queue role" },
                ],
                total: conflicted ? 1 : 100,
                has_more: !conflicted,
              },
            }),
          );
        },
      });
      render(
        <MemoryRouter>
          <AppProviders>
            <PendingDiscoveries workspaceId="workspace-1" />
          </AppProviders>
        </MemoryRouter>,
      );

      await vi.waitFor(() => expect(screen.getByText("Original queue role")).toBeTruthy());
      await vi.waitFor(() =>
        expect(
          screen.getByRole<HTMLButtonElement>("button", { name: "Next ready page" }).disabled,
        ).toBe(false),
      );
      fireEvent.click(screen.getByRole("button", { name: "Next ready page" }));

      await vi.waitFor(() => expect(screen.getByText(QUEUE_RESTART_NOTICE)).toBeTruthy());
      // The restarted page pins the before mark again a tick later.
      await vi.advanceTimersByTimeAsync(0);
      await vi.waitFor(() => {
        expect(screen.queryByText("Original queue role")).toBeNull();
        expect(screen.getByText("Reordered queue role")).toBeTruthy();
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("says the restart cleared the selection when the queue snapshot conflicts", async () => {
    const rows = pendingDiscoveryRows(2);
    let conflicted = false;
    installQueue({
      onPageRequest: (url) => {
        const readyOffset = Number(url.searchParams.get("ready_offset") ?? 0);
        if (readyOffset > 0) {
          conflicted = true;
          return HttpResponse.json(
            { detail: "The pending discovery queue changed." },
            { status: 409, headers: { "Content-Type": "application/problem+json" } },
          );
        }
        return HttpResponse.json(
          pendingDiscoveryPage({
            snapshot_sha256: (conflicted ? "b" : "a").repeat(64),
            ready: { items: rows, total: 100, has_more: !conflicted },
          }),
        );
      },
    });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByLabelText("Select Role 0 at Globex"));
    expect(screen.getByText("1 selected")).toBeTruthy();
    await waitFor(() =>
      expect(
        screen.getByRole<HTMLButtonElement>("button", { name: "Next ready page" }).disabled,
      ).toBe(false),
    );
    fireEvent.click(screen.getByRole("button", { name: "Next ready page" }));

    expect(await screen.findByText(QUEUE_RESTART_NOTICE)).toBeTruthy();
    await waitFor(() => expect(screen.getByText("0 selected")).toBeTruthy());
  });

  it("keeps the loaded rows on screen while the pinned first page is read", async () => {
    let pinnedStarted = false;
    let releasePinned!: () => void;
    const pinnedRead = new Promise<void>((resolve) => {
      releasePinned = resolve;
    });
    installQueue();
    server.use(
      http.get(`${WORKSPACE_ROUTE}/pending-discoveries/page`, async ({ request }) => {
        if (new URL(request.url).searchParams.has("snapshot_before")) {
          pinnedStarted = true;
          await pinnedRead;
        }
        return HttpResponse.json(
          pendingDiscoveryPage({ ready: { items: pendingDiscoveryRows(1), total: 1 } }),
        );
      }),
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    try {
      expect(await screen.findByText("Role 0")).toBeTruthy();
      await waitFor(() => expect(pinnedStarted).toBe(true));
      expect(screen.getByText("Role 0")).toBeTruthy();
      expect(screen.queryByText("Reading discoveries")).toBeNull();
    } finally {
      releasePinned();
    }
    expect(await screen.findByText("Role 0")).toBeTruthy();
  });

  it("returns to the last available page when a mutation shrinks a lane below its offset", async () => {
    const [row] = pendingDiscoveryRows(1);
    if (!row) throw new Error("The queue fixture did not create a row.");
    const log = requestLog();
    let shrunk = false;
    installQueue({
      onRequest: log.reads,
      onPageRequest: (url) => {
        const readyOffset = Number(url.searchParams.get("ready_offset") ?? 0);
        if (readyOffset === 50) shrunk = true;
        return HttpResponse.json(
          pendingDiscoveryPage({
            ready:
              readyOffset === 0
                ? { items: [row], total: shrunk ? 50 : 100, has_more: !shrunk }
                : { items: [], total: 50, offset: 50 },
          }),
        );
      },
    });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByText(row.title);
    await waitFor(() =>
      expect(
        screen.getByRole<HTMLButtonElement>("button", { name: "Next ready page" }).disabled,
      ).toBe(false),
    );
    fireEvent.click(screen.getByRole("button", { name: "Next ready page" }));

    await waitFor(() =>
      expect(
        log.requests.some(
          ({ url }) =>
            url.pathname.endsWith("/pending-discoveries/page") &&
            url.searchParams.get("ready_offset") === "50",
        ),
      ).toBe(true),
    );
    await waitFor(() =>
      expect(
        screen.getByRole<HTMLButtonElement>("button", { name: "Previous ready page" }).disabled,
      ).toBe(true),
    );
    expect(screen.queryByText(/No openings are waiting/)).toBeNull();
    expect(screen.getByText(row.title)).toBeTruthy();
  });

  it("defers the server-filtered closed-history read until the user opens that disclosure", async () => {
    const requests = installAddFlow();
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const closedHistoryRead = () =>
      requests.some(({ url }) => url.searchParams.get("closed_only") === "true");
    await screen.findByText(/No openings are waiting/);
    expect(closedHistoryRead()).toBe(false);

    fireEvent.click(screen.getByText("Closed posting history"));
    await waitFor(() => expect(closedHistoryRead()).toBe(true));
  });

  it("leaves a favorite or kept closed posting out of the history the queue still lists", async () => {
    const closedAt = "2026-09-20T00:00:00Z";
    installRows([], {
      closedHistory: [
        pendingDiscoveryRow({ id: "closed-1", title: "Closed plain role", withdrawn_at: closedAt }),
        pendingDiscoveryRow({
          id: "closed-2",
          title: "Closed kept role",
          kept: true,
          withdrawn_at: closedAt,
        }),
        pendingDiscoveryRow({
          id: "closed-3",
          title: "Closed favorite role",
          favorite: true,
          withdrawn_at: closedAt,
        }),
      ],
    });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByText("Closed posting history"));

    expect(await screen.findByText("Closed plain role")).toBeTruthy();
    expect(screen.getByText("Closed posting history (1)")).toBeTruthy();
    expect(screen.queryByText("Closed kept role")).toBeNull();
    expect(screen.queryByText("Closed favorite role")).toBeNull();
  });

  it("credits the aggregator a closed posting names", async () => {
    installRows([], {
      closedHistory: [
        pendingDiscoveryRow({
          id: "closed-listed",
          title: "Closed listed role",
          aggregator: {
            credit_url: "https://listings.example.test/",
            name: "Example Listings",
            opens_discovery_url: false,
          },
          discovery_url: "https://listings.example.test/jobs/globex-example/data-analyst-4412",
          canonical_url: "https://careers.globex.example.test/jobs/4412",
          withdrawn_at: "2026-09-20T00:00:00Z",
        }),
      ],
    });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByText("Closed posting history"));

    const credit = await screen.findByRole("link", { name: "Job data from Example Listings" });
    expect(credit.getAttribute("href")).toBe("https://listings.example.test/");
  });

  it("keeps reversible dismissals reachable when the active queue is empty", async () => {
    installAddFlow();
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const empty = await screen.findByText(/No openings are waiting/);
    expect(empty.closest('[data-tour="find-review"]')).not.toBeNull();
    expect(screen.getByRole("link", { name: "Scan your boards" }).getAttribute("href")).toBe(
      "?tab=boards",
    );
    expect(screen.getByText("Pending-list dispositions")).toBeTruthy();
    expect(screen.getByText("Dismissed discoveries (0)")).toBeTruthy();
    expect(screen.queryByText("Dismiss the current filtered cohort")).toBeNull();
    fireEvent.click(screen.getByText("Queue maintenance"));
    expect(screen.getByRole("button", { name: "Reset pending queue" })).toBeTruthy();
  });

  it("checks complete publisher facts before staging and verifies again on add", async () => {
    const requests = installAddFlow();
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    fireEvent.change(await screen.findByLabelText("Posting link"), { target: { value: ADD_URL } });
    fireEvent.change(screen.getByLabelText("Title, if you know it"), {
      target: { value: "Product Manager" },
    });
    expect(screen.getByRole("button", { name: "Add posting" }).hasAttribute("disabled")).toBe(
      false,
    );
    fireEvent.click(screen.getByRole("button", { name: "Check posting" }));

    const details = (await screen.findByText("Publisher details found")).closest("div");
    if (!details) throw new Error("Expected the publisher details.");
    expect(within(details).getByText("Remote, Moncton")).toBeTruthy();
    expect(within(details).getByText("Full-time")).toBeTruthy();
    expect(within(details).queryByText("Employer")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Add posting" }));

    await waitFor(() => {
      const posted = requests.find(
        ({ method, url }) => method === "POST" && url.pathname.endsWith("/pending-discoveries"),
      );
      expect(posted).toBeDefined();
      expect(posted?.url.pathname).toContain("/workspaces/workspace-1/pending-discoveries");
      expect(posted?.body).toMatchObject({
        url: ADD_URL,
        title: "Product Manager",
        check_publisher_facts: true,
      });
    });
    expect(
      await screen.findByText(
        "Staged Product Manager at Example Health. Publisher verified title, location, work arrangement, employment type.",
      ),
    ).toBeTruthy();
    expect(
      await screen.findByText("Posting read queued. Check the pending queue for the result."),
    ).toBeTruthy();
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByLabelText("Select Product Manager at Example Health"),
      ),
    );
  });

  it("announces when an archived posting is returned to pending review", async () => {
    installAddFlow({ outcome: "requeued" });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    fireEvent.change(await screen.findByLabelText("Posting link"), {
      target: { value: "https://jobs.example.com/postings/1234" },
    });
    fireEvent.change(screen.getByLabelText("Title, if you know it"), {
      target: { value: "Product Manager" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Check posting" }));
    await screen.findByText("Publisher details found");
    fireEvent.click(screen.getByRole("button", { name: "Add posting" }));

    expect(
      await screen.findByText(
        "Returned Product Manager at Example Health. Publisher verified title, location, work arrangement, employment type.",
      ),
    ).toBeTruthy();
    expect(
      await screen.findByText("Posting read queued. Check the pending queue for the result."),
    ).toBeTruthy();
  });

  it("relaxes filters and focuses a newly added excluded posting", async () => {
    installAddFlow({ constraintConfidence: "excluded" });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    fireEvent.change(await screen.findByLabelText("Posting link"), {
      target: { value: "https://jobs.example.com/postings/1234" },
    });
    fireEvent.change(screen.getByLabelText("Title, if you know it"), {
      target: { value: "Product Manager" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Check posting" }));
    await screen.findByText("Publisher details found");
    fireEvent.click(screen.getByRole("button", { name: "Add posting" }));

    openMoreFilters();
    await waitFor(() => {
      expect(screen.getByLabelText<HTMLSelectElement>("Constraint route").value).toBe("all");
      expect(document.activeElement).toBe(
        screen.getByLabelText("Select Product Manager at Example Health"),
      );
    });
  });

  it("reveals an added posting explicitly and returns to the saved strict view", async () => {
    window.localStorage.setItem("clearscout.pendingStrictUncertainty.workspace-1", "true");
    installAddFlow({ constraintConfidence: "excluded" });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );
    fireEvent.change(await screen.findByLabelText("Posting link"), { target: { value: ADD_URL } });
    fireEvent.change(screen.getByLabelText("Title, if you know it"), {
      target: { value: "Product Manager" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Check posting" }));
    await screen.findByText("Publisher details found");
    fireEvent.click(screen.getByRole("button", { name: "Add posting" }));
    const checkbox = await screen.findByLabelText<HTMLInputElement>(
      "Select Product Manager at Example Health",
    );
    await waitFor(() => expect(document.activeElement).toBe(checkbox));
    expect(
      screen.getByText(
        "Showing all constraint routes to reveal the posting you added. Choose Main queue to return to strict view.",
      ),
    ).toBeTruthy();
    openMoreFilters();
    expect(screen.getByLabelText<HTMLSelectElement>("Constraint route").value).toBe("all");
    expect(
      screen.getByRole<HTMLInputElement>("checkbox", { name: "Separate uncertain postings" })
        .checked,
    ).toBe(true);
    expect(window.localStorage.getItem("clearscout.pendingStrictUncertainty.workspace-1")).toBe(
      "true",
    );
    const main = screen.getByRole("button", { name: "Main queue (0)" });
    expect(main.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(checkbox);
    expect(screen.getByRole("button", { name: "Track selected (1)" })).toBeTruthy();
    fireEvent.click(main);
    await waitFor(() =>
      expect(screen.queryByLabelText("Select Product Manager at Example Health")).toBeNull(),
    );
    expect(screen.getByLabelText<HTMLSelectElement>("Constraint route").value).toBe("pursue_only");
    expect(screen.queryByRole("button", { name: /Track selected/ })).toBeNull();
    expect(screen.queryByText(/Showing all constraint routes to reveal/)).toBeNull();
  });

  it("retains and reads a posting when the add-time recheck becomes unavailable", async () => {
    installAddFlow({ addCheckStatus: "unreadable" });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    fireEvent.change(await screen.findByLabelText("Posting link"), {
      target: { value: "https://jobs.example.com/postings/1234" },
    });
    fireEvent.change(screen.getByLabelText("Title, if you know it"), {
      target: { value: "Product Manager" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Check posting" }));
    await screen.findByText("Publisher details found");
    fireEvent.click(screen.getByRole("button", { name: "Add posting" }));

    expect(
      await screen.findByText("Staged Product Manager at Example Health with unverified hints."),
    ).toBeTruthy();
    expect(
      await screen.findByText("Posting read queued. Check the pending queue for the result."),
    ).toBeTruthy();
  });

  it("adds an unsupported publisher link and queues its exact free content read", async () => {
    const requests = installAddFlow({ previewStatus: "unsupported" });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const url = "https://publisher.example.test/openings/clinical-lead";
    fireEvent.change(await screen.findByLabelText("Posting link"), { target: { value: url } });
    fireEvent.change(screen.getByLabelText("Title, if you know it"), {
      target: { value: "Clinical Lead" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Check posting" }));
    expect(await screen.findByText("Publisher details could not be verified.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Add posting" }));

    await waitFor(() => {
      const add = requests.find(
        ({ method, url: target }) =>
          method === "POST" && target.pathname.endsWith("/pending-discoveries"),
      );
      expect(add?.body).toMatchObject({ url, check_publisher_facts: false });
      const task = requests.find(
        ({ method, url: target }) => method === "POST" && target.pathname.endsWith("/tasks"),
      );
      expect(task?.body).toMatchObject({
        task_type: "discovery.refresh_batch",
        payload: { pending_discovery_ids: ["pending-1"] },
      });
    });
  });

  it("uses one visible-row selection and prunes hidden targets when filters change", async () => {
    const item = (id: string, recommendation: PendingDiscovery["recommendation"], title: string) =>
      pendingDiscoveryRow({
        id,
        title,
        location: id === "unknown-1" ? "" : "Northtown",
        discovery_url: `https://jobs.example.test/${id}`,
        canonical_url: `https://jobs.example.test/${id}`,
        retained_content_id: `content-${id}`,
        current_content_id: `content-${id}`,
        rank: 1,
        recommendation,
        screening_standing: recommendation === "investigate" ? "unknown" : "refuse",
        arrangement_categories:
          id === "review-1" ? ["remote", "hybrid"] : id === "unlikely-1" ? ["hybrid"] : ["unknown"],
        arrangement_categories_source: id === "unknown-1" ? "inferred_from_location" : "published",
        staged_facts: stagedFacts({
          locations: {
            values: id === "unknown-1" ? [] : ["Northtown"],
            unresolved_values: id === "unknown-1" ? ["5 Locations"] : [],
            provenance: "synthetic.location",
            complete: id !== "unknown-1",
          },
          arrangements: {
            values: id === "review-1" ? ["Remote"] : id === "unlikely-1" ? ["Hybrid"] : [],
            unresolved_values: [],
            provenance: id === "unknown-1" ? "" : "synthetic.arrangement",
            complete: id !== "unknown-1",
          },
          employment_types: {
            values: id === "review-1" ? ["Full-time"] : [],
            unresolved_values: [],
            provenance: "",
            complete: false,
          },
        }),
      });
    const dispositions = requestLog();
    server.use(
      http.post(`${WORKSPACE_ROUTE}/pending-dispositions/apply`, async ({ request }) => {
        await dispositions.record(request);
        return HttpResponse.json([]);
      }),
    );
    installRows([
      item("review-1", "review", "Platform Lead"),
      item("unknown-1", "investigate", "Data Lead"),
      item("unlikely-1", "unlikely", "Warehouse Supervisor"),
    ]);
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    openMoreFilters();
    expect(await screen.findByRole("option", { name: "All (3)" })).toBeTruthy();
    const category = screen.getByLabelText("Show category");
    expect(screen.getByRole("option", { name: "Investigate (1)" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "Remote (1)" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "Hybrid (2)" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "Unknown or not stated (1)" })).toBeTruthy();
    const semanticRelevance = screen.getByLabelText<HTMLSelectElement>("Relevance verdict");
    expect(
      within(semanticRelevance).getByRole("option", { name: "Not yet reviewed (3)" }),
    ).toBeTruthy();
    const unresolvedRow = screen.getByLabelText("Select Data Lead at Globex").closest("tr");
    if (!unresolvedRow) throw new Error("Expected the unresolved discovery row.");
    expect(within(unresolvedRow).getByText("5 Locations unresolved")).toBeTruthy();
    expect(
      within(unresolvedRow).getByText(
        (_, node) => node?.className === "status status--tone-muted pending-verdict__badge",
      ).textContent,
    ).toBe("Investigate");
    expect(within(unresolvedRow).getByText("Not stated, from location")).toBeTruthy();
    expect(within(unresolvedRow).getByText("Employment not stated")).toBeTruthy();
    const reviewRow = screen.getByLabelText("Select Platform Lead at Globex").closest("tr");
    if (!reviewRow) throw new Error("Expected the multi-arrangement discovery row.");
    expect(within(reviewRow).getByText("Remote")).toBeTruthy();
    expect(within(reviewRow).getByText("Hybrid")).toBeTruthy();
    expect(within(reviewRow).queryByText(/from location/)).toBeNull();
    expect(within(unresolvedRow).getByText(/from location/)).toBeTruthy();
    // Only one row's Details panel is open at a time, matching the dismissal
    // and listing disclosures it shares state handling with.
    fireEvent.click(within(unresolvedRow).getByRole("button", { name: "Details" }));
    expect(screen.getAllByText("Synthetic board read.")[0]?.tagName).toBe("SUMMARY");
    expect(screen.getAllByText("First seen 2026-08-21")).toHaveLength(4);
    fireEvent.click(screen.getByText("Screening evidence and unknowns"));
    expect(screen.getByText(/unresolved: 5 Locations/)).toBeTruthy();
    const dismissButtons = screen.getAllByRole("button", { name: "Dismiss from pending" });
    expect(dismissButtons).toHaveLength(3);

    fireEvent.click(dismissButtons[0]!);
    const dismissalCategory = screen.getByLabelText<HTMLSelectElement>("Dismissal reason");
    expect(dismissalCategory.closest("tr")?.querySelector("td")?.colSpan).toBe(6);
    expect(document.activeElement).toBe(dismissalCategory);
    expect(Array.from(dismissalCategory.options).map((option) => option.text)).toContain(
      "Work arrangement outside scope",
    );
    fireEvent.change(dismissalCategory, { target: { value: "other" } });
    expect(screen.getByLabelText("Other reason")).toBeTruthy();
    fireEvent.keyDown(screen.getByLabelText("Other reason"), { key: "Escape" });
    expect(screen.queryByLabelText("Dismissal reason")).toBeNull();
    expect(document.activeElement).toBe(dismissButtons[0]);
    expect(dispositions.requests).toEqual([]);
    fireEvent.click(dismissButtons[0]!);
    expect(document.activeElement).toBe(screen.getByLabelText("Dismissal reason"));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(document.activeElement).toBe(dismissButtons[0]);
    expect(dispositions.requests).toEqual([]);

    const dataLead = screen.getByLabelText<HTMLInputElement>("Select Data Lead at Globex");
    const selectAll = screen.getByLabelText<HTMLInputElement>(
      "Select eligible discoveries on this page",
    );
    fireEvent.click(dataLead);
    expect(selectAll.indeterminate).toBe(true);
    expect(screen.getByRole("button", { name: "Dismiss selected (1)" })).toBeTruthy();

    fireEvent.change(category, { target: { value: "unlikely" } });
    await waitFor(() => expect(screen.queryByText("Platform Lead")).toBeNull());
    expect(screen.getByText("Warehouse Supervisor")).toBeTruthy();
    await waitFor(() => expect(screen.getByText("0 selected")).toBeTruthy());
  });

  it("shows a relevance review on the row itself", async () => {
    const review = relevanceReview("unknown-1", {
      id: "review-1",
      title: "Data Lead",
      summary: "Closely matches the record's data-platform ownership.",
      out_of_date_because: ["master_cv"],
    });
    installRows(
      [
        pendingDiscoveryRow({
          id: "unknown-1",
          title: "Data Lead",
          location: "Northtown",
          discovery_url: "https://jobs.example.test/unknown-1",
          canonical_url: "https://jobs.example.test/unknown-1",
          retained_content_id: "content-unknown-1",
          current_content_id: "content-unknown-1",
          rank: 1,
          arrangement_categories: ["unknown"],
          arrangement_categories_source: "missing",
          relevance_review: review,
        }),
      ],
      { masterCvVersions: CURRENT_MASTER_CV_VERSIONS },
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Details" }));
    const verdict = await screen.findByText(/Automated relevance review: High relevance/);
    expect(verdict.parentElement?.textContent).toContain(
      "Closely matches the record's data-platform ownership.",
    );
    expect(await screen.findByText("Review is out of date.")).toBeTruthy();
    const semanticRelevance = screen.getByLabelText<HTMLSelectElement>("Relevance verdict");
    expect(
      within(semanticRelevance).getByRole("option", { name: "High relevance (1, 1 out of date)" }),
    ).toBeTruthy();
    expect(
      within(semanticRelevance).getByRole("option", { name: "Not yet reviewed (0)" }),
    ).toBeTruthy();
  });

  it("separates failed-task detail from row actions and distinguishes running work", async () => {
    const item = (id: string, title: string) =>
      pendingDiscoveryRow({
        id,
        title,
        location: "Northtown",
        discovery_url: `https://jobs.example.test/${id}`,
        canonical_url: `https://jobs.example.test/${id}`,
        retained_content_id: `content-${id}`,
        current_content_id: `content-${id}`,
        reason: "Synthetic board read with a deliberately long explanation for the queue.",
        rank: 1,
        arrangement_categories: ["unknown"],
        arrangement_categories_source: "missing",
      });
    const task = (id: string, pendingId: string, status: "failed" | "running") =>
      taskRecord({
        id,
        status,
        progress: { pending_discovery_id: pendingId },
        completed_at: status === "failed" ? "2026-08-24T00:02:00Z" : null,
        error_category: status === "failed" ? "ProviderRateLimit" : null,
        error_message: status === "failed" ? "The provider asked the client to retry later." : null,
        pending_discovery_ids: [pendingId],
      });
    const batch = taskRecord({
      ...task("task-batch", "batch-now", "running"),
      task_type: "discovery.evaluate_batch",
      progress: { current: 1, total: 2, pending_discovery_id: "batch-now" },
      pending_discovery_ids: ["batch-now", "batch-later"],
    });
    installRows(
      [
        item("failed-1", "Failed role"),
        item("running-1", "Running role"),
        item("batch-later", "Later batch role"),
      ],
      {
        tasks: [
          task("task-failed", "failed-1", "failed"),
          task("task-running", "running-1", "running"),
          batch,
        ],
      },
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(
        screen
          .getAllByRole("status")
          .some((status) => status.textContent?.includes("Evaluation failed: provider throttle")),
      ).toBe(true),
    );
    const failure = screen
      .getAllByRole("status")
      .find((status) => status.textContent?.includes("Evaluation failed: provider throttle"));
    const alertRow = failure?.closest("tr");
    expect(alertRow?.classList).toContain("pending-row-alert");
    expect(alertRow?.querySelector("td")?.colSpan).toBe(6);
    expect(screen.getByRole("button", { name: "Retry evaluation" })).toBeTruthy();

    const running = screen.getAllByRole<HTMLButtonElement>("button", { name: "Running" });
    expect(running).toHaveLength(2);
    for (const button of running) {
      expect(button.disabled).toBe(true);
    }
    expect(screen.getByLabelText<HTMLInputElement>("Select Running role at Globex").disabled).toBe(
      true,
    );
    // A row a running batch names but has not reached is still in flight after a reload.
    expect(
      screen.getByLabelText<HTMLInputElement>("Select Later batch role at Globex").disabled,
    ).toBe(true);
  });

  it("prices a row's full evaluation before confirming it, like the batch button and e key", async () => {
    const log = requestLog();
    const row = pendingDiscoveryRow({ id: "pending-1", title: "Product Manager" });
    installRows([row]);
    server.use(
      evaluationPreflightHandler((request) => void log.record(request)),
      http.post("*/api/v1/tasks", async ({ request }) => {
        await log.record(request);
        return HttpResponse.json(taskRecord({ id: "task-1", status: "queued" }));
      }),
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const button = await screen.findByRole<HTMLButtonElement>("button", {
      name: "Full evaluation",
    });
    await waitFor(() => expect(button.disabled).toBe(false));
    fireEvent.click(button);
    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByText("Fully evaluate Product Manager at Globex?")).toBeTruthy();
    expect(dialog.getByText(/Estimated total at configured rates: \$0\.2000 USD\./)).toBeTruthy();
    const preflight = log.requests.find((request) =>
      request.url.pathname.endsWith("/evaluation-preflight"),
    );
    expect(preflight?.body).toEqual({ pending_discovery_ids: ["pending-1"] });
    expect(log.requests.some((request) => request.url.pathname.endsWith("/tasks"))).toBe(false);
    fireEvent.click(dialog.getByRole("button", { name: "Run full evaluation" }));
    await waitFor(() =>
      expect(log.requests.find((request) => request.url.pathname.endsWith("/tasks"))?.body).toEqual(
        {
          workspace_id: "workspace-1",
          task_type: "discovery.evaluate_batch",
          payload: { pending_discovery_ids: ["pending-1"] },
        },
      ),
    );
  });

  it("blocks re-evaluation when the durable identity already maps to an evaluated job", async () => {
    const rows = [
      pendingDiscoveryRow({
        id: "pending-fresh",
        title: "Fresh Role",
        discovery_url: "https://jobs.example.test/fresh",
        canonical_url: "https://jobs.example.test/fresh",
        retained_content_id: "content-fresh",
        current_content_id: "content-fresh",
        rank: 1,
      }),
      pendingDiscoveryRow({
        id: "pending-dup",
        title: "Duplicate Role",
        discovery_url: "https://jobs.example.test/dup",
        canonical_url: "https://jobs.example.test/dup",
        retained_content_id: "content-dup",
        current_content_id: "content-dup",
        rank: 1,
        evaluated_job_id: "job-abc",
        evaluated_evaluation_id: "eval-abc",
        evaluated_decision: "apply_now",
      }),
    ];
    installRows(rows);
    server.use(
      http.get("*/api/v1/jobs", () =>
        HttpResponse.json({
          items: [{ job_posting_id: "job-abc", title: "Tracked Role", employer: "Globex" }],
          total: 1,
        }),
      ),
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const dupCheckbox = await screen.findByLabelText<HTMLInputElement>(
      "Select Duplicate Role at Globex (already evaluated)",
    );
    expect(dupCheckbox.disabled).toBe(true);
    const dupRow = dupCheckbox.closest("tr");
    if (!dupRow) throw new Error("Expected the already-evaluated row.");
    expect(within(dupRow).getByText("Already evaluated")).toBeTruthy();
    const viewLink = within(dupRow).getByRole("link", { name: "View evaluated job" });
    expect(viewLink.getAttribute("href")).toBe("/jobs/job-abc?tab=evaluation");
    expect(within(dupRow).queryByRole("button", { name: /Full evaluation/ })).toBeNull();
    expect(within(dupRow).queryByRole("button", { name: /Retry evaluation/ })).toBeNull();

    fireEvent.click(within(dupRow).getByRole("button", { name: "Dismiss from pending" }));
    fireEvent.change(screen.getByLabelText("Dismissal reason"), {
      target: { value: "duplicate_posting" },
    });
    await screen.findByRole("option", { name: /Tracked Role at Globex/ });
    expect(screen.getByLabelText<HTMLSelectElement>("Duplicate of tracked job").value).toBe(
      "job-abc",
    );
    fireEvent.change(screen.getByLabelText("Find tracked job"), { target: { value: "missing" } });
    expect(screen.getByRole("option", { name: "Tracked Role at Globex (selected)" })).toBeTruthy();
    expect(screen.getByText("No other tracked job matches that search.")).toBeTruthy();

    const selectAll = screen.getByLabelText<HTMLInputElement>(
      "Select eligible discoveries on this page",
    );
    fireEvent.click(selectAll);
    expect(dupCheckbox.checked).toBe(false);
    expect(screen.getByLabelText<HTMLInputElement>("Select Fresh Role at Globex").checked).toBe(
      true,
    );
  });

  it("reports a completed duplicate race as a no-op and links the existing evaluation", async () => {
    const pendingId = "pending-race";
    const row = pendingDiscoveryRow({
      id: pendingId,
      title: "Raced Role",
      discovery_url: "https://jobs.example.test/race",
      canonical_url: "https://jobs.example.test/race",
      retained_content_id: "content-race",
      current_content_id: "content-race",
      rank: 1,
    });
    const task = taskRecord({
      id: "task-race",
      status: "completed",
      progress: {
        decision: "already_evaluated",
        pending_discovery_id: pendingId,
        job_posting_id: "job-existing",
        evaluation_id: "evaluation-existing",
        existing_decision: "apply_now",
      },
      completed_at: "2026-08-24T00:02:00Z",
      pending_discovery_ids: [pendingId],
    });
    const evaluations = requestLog();
    server.use(
      http.post("*/api/v1/tasks", async ({ request }) => {
        await evaluations.record(request);
        return HttpResponse.json(task);
      }),
    );
    installRows([row], { tasks: [task] });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(await screen.findByText(/No paid evaluation ran/)).toBeTruthy();
    expect(screen.getByText(/already has an evaluation with the Apply now decision/)).toBeTruthy();
    const link = screen.getByRole("link", { name: "View existing evaluation" });
    expect(link.getAttribute("href")).toBe("/jobs/job-existing?tab=evaluation");
    expect(screen.queryByRole("button", { name: "Full evaluation" })).toBeNull();

    const duplicateCheckbox = screen.getByLabelText<HTMLInputElement>(
      "Select Raced Role at Globex (already evaluated)",
    );
    expect(duplicateCheckbox.disabled).toBe(true);
    const selectAll = screen.getByLabelText<HTMLInputElement>(
      "Select eligible discoveries on this page",
    );
    expect(selectAll.disabled).toBe(true);
    fireEvent.click(selectAll);
    expect(duplicateCheckbox.checked).toBe(false);
    expect(screen.getByText("0 selected")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Full evaluation \(/ })).toBeNull();
    expect(evaluations.requests).toEqual([]);
  });

  /** Serve one ready lane, honoring the limit the SPA asks for. */
  const installQueuePage = (
    rows: PendingDiscovery[],
    pageRequests: string[] = [],
    failingLimit = 0,
  ) => {
    const requestedLimits: number[] = [];
    installQueue({
      onPageRequest: (url) => {
        const limit = Number(url.searchParams.get("limit"));
        requestedLimits.push(limit);
        pageRequests.push(url.href);
        if (limit === failingLimit) return new HttpResponse(null, { status: 500 });
        const items = rows.slice(0, limit);
        return HttpResponse.json(
          pendingDiscoveryPage({
            limit,
            snapshot_before: "2026-09-18T00:00:00Z",
            ready: { items, total: rows.length, has_more: items.length < rows.length },
          }),
        );
      },
    });
    return requestedLimits;
  };

  it("loads the whole lane and keeps that choice when the page size is All", async () => {
    const requestedLimits = installQueuePage(pendingDiscoveryRows(200));
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByText("Role 49");
    expect(screen.queryByText("Role 50")).toBeNull();
    fireEvent.change(screen.getByLabelText("Rows per page"), { target: { value: "all" } });

    expect(await screen.findByText("Role 199")).toBeTruthy();
    expect(requestedLimits.at(-1)).toBe(2000);
    expect(window.localStorage.getItem("clearscout.pendingPageSize")).toBe("all");
  });

  it("drops a row the finished re-screen held out from the queue", async () => {
    const rows = pendingDiscoveryRows(2);
    installQueuePage(rows);
    const task = {
      id: "task-rescreen",
      task_type: "discovery.rescreen_queue",
      created_at: "2026-09-18T00:00:00Z",
      progress: {},
    };
    server.use(
      http.post("*/api/v1/tasks", () => HttpResponse.json({ ...task, status: "queued" })),
      http.get("*/api/v1/tasks/task-rescreen", () => {
        // The worker finished and held out Role 0, so the server no longer lists it.
        rows.splice(0, 1);
        return HttpResponse.json({
          ...task,
          status: "completed",
          progress: { screened: 2, held_out: 1 },
        });
      }),
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByText("Role 0");
    fireEvent.click(screen.getByText("Queue maintenance"));
    fireEvent.click(screen.getByRole("button", { name: "Re-screen queue" }));
    const dialog = within(await screen.findByRole("dialog"));
    fireEvent.click(dialog.getByRole("button", { name: "Re-screen queue" }));

    expect(await screen.findByText("Re-screened 2 discoveries and held out 1.")).toBeTruthy();
    await waitFor(() => expect(screen.queryByText("Role 0")).toBeNull());
    expect(screen.getByText("Role 1")).toBeTruthy();
  });

  it("keeps Rows per page reachable when the All read fails", async () => {
    installQueuePage(pendingDiscoveryRows(200), [], 2000);
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByText("Role 49");
    fireEvent.change(screen.getByLabelText("Rows per page"), { target: { value: "all" } });

    await waitFor(() => expect(screen.queryByText("Role 0")).toBeNull());
    fireEvent.change(screen.getByLabelText("Rows per page"), { target: { value: "50" } });

    expect(await screen.findByText("Role 49")).toBeTruthy();
  });

  it("re-reads the first page when the person returns to a page size used earlier", async () => {
    let unpinnedFiftyReads = 0;
    installQueue({
      onPageRequest: (url) => {
        const limit = Number(url.searchParams.get("limit"));
        if (limit === 50 && !url.searchParams.has("snapshot_before")) unpinnedFiftyReads += 1;
        const title =
          limit === 50
            ? unpinnedFiftyReads > 1
              ? "Refreshed role"
              : "Original role"
            : "Wide role";
        return HttpResponse.json(
          pendingDiscoveryPage({
            limit,
            ready: { items: [pendingDiscoveryRow({ title })], total: 1 },
          }),
        );
      },
    });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(await screen.findByText("Original role")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Rows per page"), { target: { value: "100" } });
    expect(await screen.findByText("Wide role")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Rows per page"), { target: { value: "50" } });

    expect(await screen.findByText("Refreshed role")).toBeTruthy();
    expect(unpinnedFiftyReads).toBe(2);
  });

  it("abandons its in-flight queue read when the queue unmounts", async () => {
    let entered = () => {};
    const handlerEntered = new Promise<void>((resolve) => {
      entered = resolve;
    });
    let release = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    let captured: AbortSignal | undefined;
    installQueue();
    server.use(
      http.get(`${WORKSPACE_ROUTE}/pending-discoveries/page`, async ({ request }) => {
        captured = request.signal;
        entered();
        await held;
        return HttpResponse.json(pendingDiscoveryPage({}));
      }),
    );
    const view = render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );
    await handlerEntered;

    view.unmount();
    release();

    expect(captured?.aborted).toBe(true);
  });

  it("starts at the page size the person last chose", async () => {
    window.localStorage.setItem("clearscout.pendingPageSize", "100");
    const requestedLimits = installQueuePage(pendingDiscoveryRows(200));
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByText("Role 99");
    expect(screen.queryByText("Role 100")).toBeNull();
    expect(requestedLimits).toEqual([100]);
  });

  it("never pins a snapshot at page size All, so a changed queue cannot restart paging", async () => {
    const pageRequests: string[] = [];
    installQueuePage(pendingDiscoveryRows(200), pageRequests);
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByText("Role 49");
    fireEvent.change(screen.getByLabelText("Rows per page"), { target: { value: "all" } });

    await screen.findByText("Role 199");
    expect(pageRequests.some((url) => url.includes("expected_snapshot_sha256"))).toBe(false);
    expect(screen.queryByText(/paging restarted from the beginning/)).toBeNull();
  });

  it("refreshes the whole selection while assisted recovery covers the unreadable rows", async () => {
    const rows = pendingDiscoveryRows(2).map((row, index) =>
      index === 0
        ? pendingDiscoveryRow({
            ...row,
            content_status: "readable",
            retained_content_id: "content-1",
            content_note: "Verified publisher content.",
          })
        : pendingDiscoveryRow({
            ...row,
            content_status: "access_blocked",
            readiness: "access_blocked",
            content_note: "Publisher returned HTTP 403 after the allowed redirect.",
          }),
    );
    installRows(rows);
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByLabelText<HTMLInputElement>("Select eligible discoveries on this page");
    await screen.findByText("Role 1");
    fireEvent.click(screen.getByLabelText("Select eligible discoveries on this page"));

    expect(screen.getByRole("button", { name: "Refresh postings (2)" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Estimate assisted recovery (1)" })).toBeTruthy();
    const unreadRow = screen.getByLabelText("Select Role 1 at Globex").closest("tr");
    if (!unreadRow) throw new Error("Expected unread row.");
    expect(within(unreadRow).getByText("Needs recovery: publisher blocked access")).toBeTruthy();
    fireEvent.click(within(unreadRow).getByRole("button", { name: "Details" }));
    expect(
      screen.getByText("Publisher returned HTTP 403 after the allowed redirect."),
    ).toBeTruthy();
    const readRow = screen.getByLabelText("Select Role 0 at Globex").closest("tr");
    if (!readRow) throw new Error("Expected read row.");
    expect(within(readRow).queryByText(/^Needs /)).toBeNull();
    fireEvent.click(within(readRow).getByRole("button", { name: "Details" }));
    expect(screen.getByText("Verified publisher content.")).toBeTruthy();
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "Full evaluation (2)" }).disabled,
    ).toBe(true);
    expect(
      screen.getByText(/1 selected row needs\s+a refresh before either paid action/),
    ).toBeTruthy();
    expect(within(unreadRow).queryByRole("button", { name: "Full evaluation" })).toBeNull();
    expect(within(unreadRow).getByRole("button", { name: /Refresh postings/ })).toBeTruthy();
  });

  it("offers the free refresh beside a spends-money estimate for access_blocked", async () => {
    installRows([
      pendingDiscoveryRow({
        content_status: "access_blocked",
        readiness: "access_blocked",
        retained_content_id: null,
        content_note: "",
      }),
    ]);
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );
    fireEvent.change(await screen.findByLabelText("Content readiness"), {
      target: { value: "all" },
    });
    const refresh = await screen.findByRole("button", { name: /^Refresh postings for/ });
    const rowElement = refresh.closest("tr");
    if (!rowElement) throw new Error("Expected the row.");
    expect(
      within(rowElement).getByText("Re-reads facts, liveness, and posting text."),
    ).toBeTruthy();
    expect(within(rowElement).queryByText("Spends money")).toBeNull();
    fireEvent.click(within(rowElement).getByRole("button", { name: "Details" }));
    expect(
      screen.getByText(
        "The publisher blocked the read. Refresh postings retries; if it fails again, paste the posting text below.",
      ),
    ).toBeTruthy();
  });

  it("maps a classified content note to plain language for an access_blocked row", async () => {
    installRows([
      pendingDiscoveryRow({
        content_status: "access_blocked",
        readiness: "access_blocked",
        retained_content_id: null,
        content_note: "Rendered publisher content was classified as error_or_challenge.",
      }),
    ]);
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );
    fireEvent.change(await screen.findByLabelText("Content readiness"), {
      target: { value: "all" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Details" }));
    expect(
      screen.getByText(
        "The publisher showed a sign-in or verification page instead of the posting.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/error_or_challenge/)).toBeNull();
  });

  it("marks a row that shares posting text and opens its dismissal as a duplicate of the keeper", async () => {
    installRows([
      pendingDiscoveryRow({
        duplicate_suggestions: [
          {
            pending_discovery_id: "pending-board",
            employer: "Globex",
            title: "Data Steward",
            location: "Northtown",
            discovery_url: "https://careers.globex.example.test/jobs/4412",
            signal: "retained_body_digest",
            keeper_pending_discovery_id: "pending-board",
            keeper_basis: "employer_board_source",
            dismissed: false,
          },
        ],
      }),
    ]);
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(await screen.findByText("Possible duplicate")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Details" }));
    const table = screen.getByRole("table", { name: "Possible duplicates of this found job" });
    fireEvent.click(
      within(table).getByRole("button", {
        name: "Keep this one, dismiss the job you are reviewing",
      }),
    );

    expect(screen.getByLabelText<HTMLSelectElement>("Dismissal reason").value).toBe(
      "duplicate_pending",
    );
  });

  it("maps a classified content note to plain language for a render_required row", async () => {
    installRows([
      pendingDiscoveryRow({
        content_status: "render_required",
        readiness: "render_required",
        retained_content_id: null,
        content_note: "Rendered publisher content was classified as collection.",
      }),
    ]);
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );
    fireEvent.change(await screen.findByLabelText("Content readiness"), {
      target: { value: "all" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Details" }));
    expect(
      screen.getByText("The address shows a list of postings rather than one posting."),
    ).toBeTruthy();
    expect(screen.queryByText(/classified as collection/)).toBeNull();
  });

  it("maps a session_required classification to plain language and never shows the raw token", async () => {
    installRows([
      pendingDiscoveryRow({
        content_status: "session_required",
        readiness: "session_required",
        retained_content_id: null,
        content_note: "Rendered publisher content was classified as error_or_challenge.",
        current_content_id: "content-observed",
      }),
    ]);
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );
    fireEvent.change(await screen.findByLabelText("Content readiness"), {
      target: { value: "all" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Details" }));
    expect(
      screen.getByText(
        "The publisher showed a sign-in or verification page instead of the posting.",
      ),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "This publisher needs you to be signed in. Open the posting in your own browser, then attach its text below.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/error_or_challenge/)).toBeNull();
  });

  it("falls back to a generic sentence for an unmapped session_required classification", async () => {
    installRows([
      pendingDiscoveryRow({
        content_status: "session_required",
        readiness: "session_required",
        retained_content_id: null,
        content_note: "Rendered publisher content was classified as some_new_wall.",
        current_content_id: "content-observed",
      }),
    ]);
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );
    fireEvent.change(await screen.findByLabelText("Content readiness"), {
      target: { value: "all" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Details" }));
    expect(
      screen.getByText("The publisher did not show the posting to an automated read."),
    ).toBeTruthy();
    expect(screen.queryByText(/some_new_wall/)).toBeNull();
  });

  it.each(["policy_refused", "session_required", "fetch_failed"] as const)(
    "lets a %s row attach attested text bound to its current unusable observation",
    async (status) => {
      const row = pendingDiscoveryRow({
        content_status: status,
        readiness: status,
        retained_content_id: null,
        current_content_id: `content-${status}-read`,
        current_identity_status: "not_yet_verified",
      });
      const attachments = requestLog();
      server.use(
        http.post(
          `${WORKSPACE_ROUTE}/pending-discoveries/:pendingId/content`,
          async ({ request }) => {
            await attachments.record(request);
            return HttpResponse.json({
              pending_discovery_id: row.id,
              evidence_id: "evidence-user",
            });
          },
        ),
      );
      installRows([row]);
      render(
        <MemoryRouter>
          <AppProviders>
            <PendingDiscoveries workspaceId="workspace-1" />
          </AppProviders>
        </MemoryRouter>,
      );

      fireEvent.change(await screen.findByLabelText("Content readiness"), {
        target: { value: "all" },
      });
      fireEvent.click(
        await screen.findByRole("button", {
          name: `Attach posting text for ${row.title} at ${row.employer}`,
        }),
      );
      const postingBody = "Current publisher responsibilities and qualifications. ".repeat(10);
      fireEvent.change(
        screen.getByLabelText(`Current posting text for ${row.title} at ${row.employer}`),
        {
          target: { value: postingBody },
        },
      );
      const submit = screen.getByRole<HTMLButtonElement>("button", {
        name: "Use this posting text",
      });
      expect(submit.disabled).toBe(true);
      fireEvent.click(
        screen.getByLabelText(
          "I obtained this text from the current posting at the link in this row.",
        ),
      );
      fireEvent.click(submit);

      await waitFor(() =>
        expect(attachments.requests.at(-1)?.body).toEqual({
          content: postingBody.trim(),
          attested_current_exact_posting: true,
          expected_current_content_id: row.current_content_id,
          expected_url: row.canonical_url,
        }),
      );
      // One refresh is offered whatever the read reached, since it re-reads the posting too.
      expect(
        screen.getByRole("button", {
          name: `Refresh postings for ${row.title} at ${row.employer}`,
        }),
      ).toBeTruthy();
    },
  );

  it("confirms retained publisher identity with fallback labels and the exact attestation", async () => {
    const row = pendingDiscoveryRow({
      title: "",
      employer: "",
      readiness: "identity_ambiguous",
      retained_content_id: null,
      current_content_id: "content-observed",
      current_identity_status: "ambiguous",
      observed_publisher_title: "Clinical Data Lead",
      observed_publisher_employer: "Northwind Research",
    });
    const confirmations = requestLog();
    server.use(
      http.post(
        `${WORKSPACE_ROUTE}/pending-discoveries/:pendingId/confirm-identity`,
        async ({ request }) => {
          await confirmations.record(request);
          return HttpResponse.json({
            pending_discovery_id: row.id,
            evidence_id: "content-confirmed",
          });
        },
      ),
    );
    installRows([row]);
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    fireEvent.change(await screen.findByLabelText("Content readiness"), {
      target: { value: "all" },
    });
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Confirm same posting for posting at jobs.example.test, employer not stated",
      }),
    );

    await waitFor(() =>
      expect(confirmations.requests.at(-1)?.body).toEqual({
        attested_same_posting: true,
        expected_current_content_id: "content-observed",
        expected_url: row.canonical_url,
      }),
    );
    expect(
      screen.getByRole("button", {
        name: "Attach posting text for posting at jobs.example.test, employer not stated",
      }),
    ).toBeTruthy();
  });

  it("keeps an identity-confirmation error visible beside the remediation", async () => {
    const row = pendingDiscoveryRow({
      readiness: "identity_contradicted",
      retained_content_id: null,
      current_content_id: "content-observed",
      observed_publisher_title: "A different posting",
    });
    server.use(
      http.post(`${WORKSPACE_ROUTE}/pending-discoveries/:pendingId/confirm-identity`, () =>
        HttpResponse.json({ detail: "The retained evidence changed." }, { status: 409 }),
      ),
    );
    installRows([row]);
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    fireEvent.change(await screen.findByLabelText("Content readiness"), {
      target: { value: "all" },
    });
    fireEvent.click(
      await screen.findByRole("button", {
        name: `Confirm same posting for ${row.title} at Globex`,
      }),
    );

    expect(await screen.findByText("The retained evidence changed.")).toBeTruthy();
  });

  it("shows the captured board listing on demand from its artifact", async () => {
    server.use(
      http.get("*/api/v1/artifacts/listing-1/content", () =>
        HttpResponse.text("Own the data platform end to end."),
      ),
    );
    installRows([pendingDiscoveryRow({ listing_artifact_id: "listing-1" })]);
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Details" }));
    const toggle = await screen.findByRole("button", { name: "Show board listing" });
    // The full description is fetched only once the disclosure opens.
    expect(screen.queryByText("Own the data platform end to end.")).toBeNull();
    fireEvent.click(toggle);
    expect(await screen.findByText("Own the data platform end to end.")).toBeTruthy();
  });

  it("shows the stored publisher posting on demand from its artifact", async () => {
    server.use(
      http.get("*/api/v1/artifacts/stored-1/content", () =>
        HttpResponse.text("Lead the platform roadmap with the publisher's own words."),
      ),
    );
    installRows([
      pendingDiscoveryRow({
        listing_artifact_id: "listing-1",
        retained_content_artifact_id: "stored-1",
      }),
    ]);
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Details" }));
    const toggle = await screen.findByRole("button", { name: "Show stored posting" });
    expect(screen.getByRole("button", { name: "Show board listing" })).toBeTruthy();
    expect(screen.queryByText(/publisher's own words/)).toBeNull();
    fireEvent.click(toggle);
    expect(await screen.findByText(/publisher's own words/)).toBeTruthy();
    expect(screen.getByText("Stored posting as last read from the publisher.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Hide stored posting" })).toBeTruthy();
  });

  it("distinguishes no-attempt, unreadable, and rendered content-readiness routes", async () => {
    const routes = [
      { content_status: "readable", readiness: "ready" },
      { content_status: "render_required", readiness: "render_required" },
      { content_status: "session_required", readiness: "session_required" },
      { content_status: "not_yet_read", readiness: "no_body" },
      { content_status: "policy_refused", readiness: "policy_refused" },
    ] as const;
    const rows = routes.map((route, index) =>
      pendingDiscoveryRow({
        ...pendingDiscoveryRows(routes.length)[index],
        ...route,
        retained_content_id: route.readiness === "ready" ? `content-${index}` : null,
      }),
    );
    rows.push(
      pendingDiscoveryRow({
        id: "metadata-only-readable",
        title: "Metadata only role",
        content_status: "readable",
        readiness: "no_body",
        retained_content_id: null,
      }),
    );
    installRows(rows);
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(await screen.findByRole("option", { name: "Ready to evaluate (1)" })).toBeTruthy();
    const filter = screen.getByLabelText<HTMLSelectElement>("Content readiness");
    // Missing retained evidence and policy refusal remain separate remediation routes.
    expect(
      within(filter).getByRole("option", { name: "No retained posting body (2)" }),
    ).toBeTruthy();
    expect(
      within(filter).getByRole("option", {
        name: "Publisher policy refused automation (1)",
      }),
    ).toBeTruthy();
    expect(
      within(filter).getByRole("option", { name: "Rendered recovery required (1)" }),
    ).toBeTruthy();
    expect(
      within(filter).getByRole("option", { name: "Publisher session required (1)" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Publisher policy refused automation (1)" }),
    ).toBeTruthy();

    fireEvent.change(filter, { target: { value: "session_required" } });
    expect(await screen.findByText("Role 2")).toBeTruthy();
    await waitFor(() => expect(screen.queryByText("Role 0")).toBeNull());

    fireEvent.change(filter, { target: { value: "no_body" } });
    expect(await screen.findByText("Metadata only role")).toBeTruthy();
    await waitFor(() => expect(screen.queryByText("Role 2")).toBeNull());
    fireEvent.click(screen.getByLabelText("Select eligible discoveries on this page"));
    expect(screen.getByRole("button", { name: "Refresh postings (2)" })).toBeTruthy();

    fireEvent.change(filter, { target: { value: "policy_refused" } });
    fireEvent.click(await screen.findByRole("button", { name: "Details" }));
    expect(screen.getByText(/The publisher refused the read under its policy/)).toBeTruthy();
    expect(screen.getAllByRole("button", { name: /Refresh postings/ }).length).toBeGreaterThan(0);
  });

  it("shows a selection-independent attention summary that filters to a route", async () => {
    const routes = [
      { content_status: "readable", readiness: "ready" },
      { content_status: "not_yet_read", readiness: "no_body" },
      { content_status: "render_required", readiness: "render_required" },
    ] as const;
    installRows(
      routes.map((route, index) =>
        pendingDiscoveryRow({
          ...pendingDiscoveryRows(routes.length)[index],
          ...route,
          retained_content_id: route.readiness === "ready" ? `content-${index}` : null,
        }),
      ),
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const summary = await screen.findByRole("region", { name: "Postings needing attention" });
    // Two of the three rows still need recovery; the readable row does not.
    expect(
      within(summary).getByText("2 of 3 matching in any content state need attention"),
    ).toBeTruthy();
    fireEvent.click(within(summary).getByRole("button", { name: "No retained posting body (1)" }));
    expect(await screen.findByText("Role 1")).toBeTruthy();
    await waitFor(() => expect(screen.queryByText("Role 0")).toBeNull());
  });

  it("says why paid review is held when the configuration answers but cannot be described", async () => {
    installRows(pendingDiscoveryRows(1));
    // A healthy 200 naming a provider outside the generated enum, which the SPA
    // has no display name for; the body is off-contract on purpose.
    server.use(
      http.get(`${WORKSPACE_ROUTE}/relevance-review-configuration`, () =>
        HttpResponse.json({
          provider: "some-other-provider",
          model_choice: { model: "a-model", effort: null, service_tier: "standard" },
        }),
      ),
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByText("Role 0");
    fireEvent.click(screen.getByLabelText("Select Role 0 at Globex"));
    expect(await screen.findByText(/provider and model could not be read/)).toBeTruthy();
    expect(screen.queryByText(/configuration is unavailable/)).toBeNull();
  });

  it("reviews only selected discoveries under the entered task cap", async () => {
    const enqueued = requestLog();
    server.use(
      http.post("*/api/v1/tasks", async ({ request }) => {
        await enqueued.record(request);
        return HttpResponse.json({ id: "review-task", status: "queued" });
      }),
    );
    installRows(pendingDiscoveryRows(2));
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByText("Role 0");
    expect(screen.queryByRole("button", { name: /^Review relevance/ })).toBeNull();
    fireEvent.click(screen.getByLabelText("Select Role 0 at Globex"));
    fireEvent.click(screen.getByRole("button", { name: "Review relevance (1)" }));
    const dialog = within(await screen.findByRole("dialog"));
    fireEvent.click(dialog.getByRole("button", { name: "Review selected relevance" }));
    await waitFor(() => expect(enqueued.requests).toHaveLength(1));
    expect(enqueued.requests[0]?.body).toMatchObject({
      task_type: "discovery.relevance_review",
      payload: { pending_discovery_ids: ["pending-0"] },
    });
    await waitFor(() => expect(screen.getByText("0 selected")).toBeTruthy());
  });

  it("shows the configured relevance-review provider and model in the review confirmation", async () => {
    installRows(pendingDiscoveryRows(1), {
      relevanceReviewConfiguration: {
        provider: "openai",
        model_choice: { model: "gpt-5.6-terra", effort: "low", service_tier: "flex" },
        batch_rows: 20,
      },
    });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByText("Role 0");
    fireEvent.click(screen.getByLabelText("Select Role 0 at Globex"));
    fireEvent.click(screen.getByRole("button", { name: "Review relevance (1)" }));
    const dialog = within(await screen.findByRole("dialog"));
    expect(
      dialog.getByText(/Configured provider: OpenAI\. Relevance review: gpt-5\.6-terra\./),
    ).toBeTruthy();
  });

  it("refuses to charge for relevance review while any selected body is unread", async () => {
    installQueueWithReviews(
      [
        {
          ...queueRow("readable-fresh", "Role Fresh", 1),
          content_status: "readable",
          retained_content_id: "content-fresh",
        },
        {
          ...queueRow("readable-current", "Role Current", 2),
          content_status: "readable",
          retained_content_id: "content-current",
        },
        {
          ...queueRow("unread-1", "Role Unread", 3),
          content_status: "not_yet_read",
          readiness: "no_body",
          retained_content_id: null,
        },
      ],
      [queueReview("readable-current", "review")],
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByText("Role Fresh");
    await screen.findByText("Role Unread");
    fireEvent.click(screen.getByLabelText("Select eligible discoveries on this page"));
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "Review relevance (3)" }).disabled,
    ).toBe(true);
    expect(screen.queryByText(/will be reviewed now/)).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("enqueues one publisher-facts refresh batch for the whole selection", async () => {
    const enqueued = requestLog();
    server.use(
      http.post("*/api/v1/tasks", async ({ request }) => {
        await enqueued.record(request);
        return HttpResponse.json({ id: "task-refresh", status: "queued" });
      }),
    );
    installRows(pendingDiscoveryRows(2));
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByText("Role 0");
    expect(screen.queryByRole("button", { name: /^Refresh postings/ })).toBeNull();

    fireEvent.click(screen.getByLabelText("Select eligible discoveries on this page"));
    fireEvent.click(screen.getByRole("button", { name: "Refresh postings (2)" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Refresh 2" }),
    );

    await waitFor(() => expect(enqueued.requests).toHaveLength(1));
    expect(enqueued.requests[0]?.body).toMatchObject({
      task_type: "discovery.refresh_batch",
      payload: { pending_discovery_ids: ["pending-0", "pending-1"] },
    });
  });

  it("names the whole selection before any stored fact is rewritten", async () => {
    const enqueued = requestLog();
    server.use(
      http.post("*/api/v1/tasks", async ({ request }) => {
        await enqueued.record(request);
        return HttpResponse.json({ id: "task-refresh", status: "queued" });
      }),
    );
    installRows(pendingDiscoveryRows(3));
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByText("Role 0");
    fireEvent.click(screen.getByLabelText("Select eligible discoveries on this page"));
    fireEvent.click(screen.getByRole("button", { name: "Refresh postings (3)" }));

    const confirmation = await screen.findByRole("dialog");
    expect(confirmation.textContent).toContain("Refresh 3 selected postings?");
    expect(confirmation.textContent).toContain("keeps its stored facts and your corrected title");
    fireEvent.click(within(confirmation).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    // Canceling the confirmation enqueues no refresh-batch task.
    expect(enqueued.requests).toEqual([]);
  });

  it("names the window when the returned rows fill the requested limit", async () => {
    installRows(pendingDiscoveryRows(PENDING_DISCOVERIES_LIMIT));
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );
    expect(
      await screen.findByText(
        `${PENDING_DISCOVERIES_LIMIT} of ${PENDING_DISCOVERIES_LIMIT} in the queue match`,
      ),
    ).toBeTruthy();
  });

  it("asks for all content states, not-excluded rows and chosen employers from the first page", async () => {
    const pageReads: URL[] = [];
    installRows(
      [
        pendingDiscoveryRow({ ...pendingDiscoveryRows(1)[0], employer: "Northwind Health" }),
        pendingDiscoveryRow({ ...pendingDiscoveryRows(2)[1], employer: "Acme Analytics" }),
      ],
      {
        onRequest: (url) => {
          if (url.pathname.endsWith("/pending-discoveries/page")) pageReads.push(url);
        },
      },
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(await screen.findByText("Role 0")).toBeTruthy();
    expect(pageReads[0]?.searchParams.has("readiness")).toBe(false);
    expect(pageReads[0]?.searchParams.get("constraint")).toBe("not_excluded");

    fireEvent.click(screen.getByRole("checkbox", { name: "Acme Analytics (1)" }));
    await waitFor(() => expect(screen.queryByText("Role 0")).toBeNull());
    expect(screen.getByText("Role 1")).toBeTruthy();
    const narrowed = pageReads.at(-1);
    expect(narrowed?.searchParams.getAll("employer")).toEqual(["Acme Analytics"]);
    expect(narrowed?.searchParams.get("ready_offset")).toBe("0");
    expect(screen.getByText("1 of 2 in the queue match your filters; 1 hidden")).toBeTruthy();
  });

  it("says the count is updating only while a read under changed filters is pending", async () => {
    const heldRead = () => {
      const held = { started: false, release: () => {}, wait: Promise.resolve() };
      held.wait = new Promise<void>((resolve) => {
        held.release = resolve;
      });
      return held;
    };
    const pinned = heldRead();
    const nextPage = heldRead();
    const narrowed = heldRead();
    const [northwind, acme] = [
      pendingDiscoveryRow({ ...pendingDiscoveryRows(1)[0], employer: "Northwind Health" }),
      pendingDiscoveryRow({ ...pendingDiscoveryRows(2)[1], employer: "Acme Analytics" }),
    ];
    const employers = [
      { name: "Acme Analytics", count: 1 },
      { name: "Northwind Health", count: 59 },
    ];
    installQueue({
      page: (url) => {
        const offset = Number(url.searchParams.get("ready_offset") ?? 0);
        return url.searchParams.has("employer")
          ? pendingDiscoveryPage({ ready: { items: [acme], total: 1 }, queue_total: 60, employers })
          : pendingDiscoveryPage({
              ready: { items: [northwind, acme], total: 60, offset, has_more: offset === 0 },
              employers,
            });
      },
    });
    server.use(
      http.get(`${WORKSPACE_ROUTE}/pending-discoveries/page`, async ({ request }) => {
        const params = new URL(request.url).searchParams;
        const read = params.has("employer")
          ? params.has("snapshot_before")
            ? null
            : narrowed
          : params.get("ready_offset") === "50"
            ? nextPage
            : params.has("snapshot_before")
              ? pinned
              : null;
        if (read) {
          read.started = true;
          await read.wait;
        }
      }),
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );
    const updating = "Updating the count for your filters";

    try {
      expect(await screen.findByText("60 of 60 in the queue match")).toBeTruthy();
      await waitFor(() => expect(pinned.started).toBe(true));
      expect(screen.queryByText(updating)).toBeNull();
      expect(screen.getByText("60 of 60 in the queue match")).toBeTruthy();
      pinned.release();

      const next = screen.getByRole<HTMLButtonElement>("button", { name: "Next ready page" });
      await waitFor(() => expect(next.disabled).toBe(false));
      fireEvent.click(next);
      await waitFor(() => expect(nextPage.started).toBe(true));
      expect(screen.queryByText(updating)).toBeNull();
      expect(screen.getByText("60 of 60 in the queue match")).toBeTruthy();
      nextPage.release();
      expect(await screen.findByText(/rows 51-52 of 60 matching/)).toBeTruthy();

      fireEvent.click(screen.getByRole("checkbox", { name: "Acme Analytics (1)" }));
      await waitFor(() => expect(narrowed.started).toBe(true));
      expect(await screen.findByText(updating)).toBeTruthy();
      expect(screen.queryByText("60 of 60 in the queue match")).toBeNull();
      expect(screen.queryByText(/Ready to evaluate:/)).toBeNull();
    } finally {
      pinned.release();
      nextPage.release();
      narrowed.release();
    }
    expect(
      await screen.findByText("1 of 60 in the queue match your filters; 59 hidden"),
    ).toBeTruthy();
    expect(screen.queryByText(updating)).toBeNull();
  });

  it("sends the search only after typing settles for 250 ms", async () => {
    const searches: (string | null)[] = [];
    vi.useFakeTimers();
    try {
      installRows(
        [
          pendingDiscoveryRow({ ...pendingDiscoveryRows(1)[0], employer: "Northwind Health" }),
          pendingDiscoveryRow({ ...pendingDiscoveryRows(2)[1], employer: "Acme Analytics" }),
        ],
        {
          onRequest: (url) => {
            if (url.pathname.endsWith("/pending-discoveries/page")) {
              searches.push(url.searchParams.get("search"));
            }
          },
        },
      );
      render(
        <MemoryRouter>
          <AppProviders>
            <PendingDiscoveries workspaceId="workspace-1" />
          </AppProviders>
        </MemoryRouter>,
      );
      await vi.waitFor(() => expect(screen.getByText("Role 0")).toBeTruthy());

      const search = screen.getByLabelText("Search");
      // Each act lets React render and schedule the effects the elapsed time set off.
      const elapse = async (ms: number) => {
        await act(() => vi.advanceTimersByTimeAsync(ms));
        await act(() => vi.advanceTimersByTimeAsync(0));
      };
      fireEvent.change(search, { target: { value: "ac" } });
      await elapse(249);
      fireEvent.change(search, { target: { value: "acme" } });
      await elapse(250);
      await vi.waitFor(() => expect(screen.queryByText("Role 0")).toBeNull());
      expect(screen.getByText("Role 1")).toBeTruthy();
      expect(searches.filter(Boolean)).toEqual(["acme"]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the table and names the filters when the queue holds rows none of them match", async () => {
    installRows([
      pendingDiscoveryRow({
        ...pendingDiscoveryRows(1)[0],
        content_status: "not_yet_read",
        readiness: "no_body",
        retained_content_id: null,
      }),
    ]);
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByText("Role 0");
    fireEvent.change(screen.getByLabelText("Content readiness"), { target: { value: "ready" } });
    expect(await screen.findByText(/No discoveries match these filters\./)).toBeTruthy();
    expect(screen.queryByText(/No openings are waiting\./)).toBeNull();
  });

  it("names each lane's window in the attention callout", async () => {
    installRows([
      ...pendingDiscoveryRows(1),
      pendingDiscoveryRow({
        ...pendingDiscoveryRows(2)[1],
        content_status: "not_yet_read",
        readiness: "no_body",
        retained_content_id: null,
      }),
    ]);
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const summary = await screen.findByRole("region", { name: "Postings needing attention" });
    expect(within(summary).queryByText(/Ready to evaluate:/)).toBeNull();
    expect(within(summary).getByText("Needs attention: rows 1-1 of 1 matching.")).toBeTruthy();
    const count = (await screen.findByText("2 of 2 in the queue match")).closest('[role="status"]');
    expect(count?.textContent).toContain("Ready to evaluate: rows 1-1 of 1 matching.");

    fireEvent.click(within(summary).getByRole("button", { name: /No retained posting body/ }));
    expect(
      await within(summary).findByText("Needs attention: rows 1-1 of 1 matching."),
    ).toBeTruthy();
  });

  it("names the ready lane's window beside the count when nothing needs attention", async () => {
    installRows(pendingDiscoveryRows(3));
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const count = (await screen.findByText("3 of 3 in the queue match")).closest('[role="status"]');
    expect(screen.queryByRole("region", { name: "Postings needing attention" })).toBeNull();
    expect(count?.textContent).toContain("Ready to evaluate: rows 1-3 of 3 matching.");
  });

  it("omits the window banner when the returned rows are under the limit", async () => {
    installRows(pendingDiscoveryRows(40));
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByText("Role 0");
    expect(screen.queryByText(/older or newer rows may exist/)).toBeNull();
  });

  it("routes pending discoveries by deterministic constraint confidence", async () => {
    const routes = [
      "pursue",
      "needs_verification",
      "investigate",
      "uncertain_comp",
      "excluded",
    ] as const;
    const rows = routes.map((route, index) =>
      pendingDiscoveryRow({
        ...pendingDiscoveryRows(routes.length)[index],
        constraint_confidence: route,
      }),
    );
    installRows(rows);
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByText("Role 0");
    openMoreFilters();
    const filter = screen.getByLabelText<HTMLSelectElement>("Constraint route");
    expect(filter.value).toBe("not_excluded");
    expect(within(filter).getByRole("option", { name: "Not excluded (4)" })).toBeTruthy();
    expect(within(filter).getByRole("option", { name: "Pursue (1)" })).toBeTruthy();
    expect(within(filter).getByRole("option", { name: "Needs verification (1)" })).toBeTruthy();
    expect(within(filter).getByRole("option", { name: "Compensation uncertain (1)" })).toBeTruthy();
    expect(within(filter).getByRole("option", { name: "Screened out (1)" })).toBeTruthy();
    expect(within(filter).getByRole("option", { name: "Excluded (1)" })).toBeTruthy();
    expect(screen.queryByText("Role 4")).toBeNull();
    expect(screen.getByText("4 of 5 in the queue match your filters; 1 hidden")).toBeTruthy();

    fireEvent.change(filter, { target: { value: "excluded" } });
    expect(await screen.findByText("Role 4")).toBeTruthy();

    fireEvent.change(filter, { target: { value: "uncertain_comp" } });
    expect(await screen.findByText("Role 3")).toBeTruthy();
    await waitFor(() => expect(screen.queryByText("Role 4")).toBeNull());
    expect(screen.queryByText("Role 0")).toBeNull();
    const uncertainRow = screen.getByText("Role 3").closest("tr");
    if (!uncertainRow) throw new Error("Expected the uncertain-compensation row.");
    expect(within(uncertainRow).getByText("Compensation uncertain")).toBeTruthy();
  });

  it("explains an all-excluded loaded window and keeps the excluded route available", async () => {
    installRows([pendingDiscoveryRow({ constraint_confidence: "excluded" })]);
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(
        /No discoveries match these filters\. 1 matches on another constraint route/,
      ),
    ).toBeTruthy();
    openMoreFilters();
    const filter = screen.getByLabelText<HTMLSelectElement>("Constraint route");
    fireEvent.change(filter, { target: { value: "excluded" } });
    expect(await screen.findByText("Role 0")).toBeTruthy();
  });

  const queueRow = (id: string, title: string, rank: number) =>
    pendingDiscoveryRow({
      id,
      title,
      location: "Northtown",
      discovery_url: `https://jobs.example.test/${id}`,
      canonical_url: `https://jobs.example.test/${id}`,
      retained_content_id: `content-${id}`,
      current_content_id: `content-${id}`,
      rank,
      arrangement_categories: ["unknown"],
      arrangement_categories_source: "missing",
    });

  const queueReview = (
    pendingId: string,
    recommendation: RelevanceReview["recommendation"],
    outOfDateBecause: RelevanceReview["out_of_date_because"] = [],
  ) =>
    relevanceReview(pendingId, {
      recommendation,
      out_of_date_because: outOfDateBecause,
    });

  /** Serve the queue with each review embedded on the row it belongs to. */
  const installQueueWithReviews = (rows: PendingDiscovery[], reviews: RelevanceReview[]) => {
    const reviewsById = new Map(reviews.map((review) => [review.pending_discovery_id, review]));
    const embedded = rows.map((row) =>
      row.relevance_review ? row : { ...row, relevance_review: reviewsById.get(row.id) ?? null },
    );
    const resets = requestLog();
    server.use(
      http.post(`${WORKSPACE_ROUTE}/pending-discoveries/reset`, async ({ request }) => {
        await resets.record(request);
        return HttpResponse.json({ archived_count: 7, kept_count: 2, rescreened_count: 1 });
      }),
    );
    installRows(embedded, { masterCvVersions: CURRENT_MASTER_CV_VERSIONS });
    return resets.requests;
  };

  it("explains what reset preserves and reports the rebuilt queue counts", async () => {
    const resets = installQueueWithReviews([queueRow("reset-1", "Reset role", 1)], []);
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByText("Queue maintenance"));
    fireEvent.click(screen.getByRole("button", { name: "Reset pending queue" }));

    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByText(/keeps your disposition choices/)).toBeTruthy();
    expect(dialog.getByText(/running paid task/)).toBeTruthy();
    expect(dialog.getByText(/source-less rows are re-screened in place/)).toBeTruthy();
    expect(dialog.getByText(/Jobs and their evaluation history are unchanged/)).toBeTruthy();
    fireEvent.click(dialog.getByRole("button", { name: "Reset pending queue" }));

    expect(
      await screen.findByText("Reset archived 7, kept 2, and re-screened 1 discoveries."),
    ).toBeTruthy();
    expect(resets).toHaveLength(1);
  });

  it("lists favorites, then kept rows, then the rest, and filters to each", async () => {
    installQueueWithReviews(
      [
        queueRow("plain-1", "Plain role", 3),
        { ...queueRow("kept-1", "Kept role", 2), kept: true, keep_note: "Strong team" },
        { ...queueRow("fav-1", "Favorite role", 1), favorite: true },
      ],
      [],
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByLabelText(/Select Favorite role/);
    const order = () =>
      screen
        .getAllByRole("checkbox", { name: /^Select (?!eligible)/ })
        .map((box) => box.getAttribute("aria-label"));
    expect(order()).toEqual([
      expect.stringMatching(/Favorite role/),
      expect.stringMatching(/Kept role/),
      expect.stringMatching(/Plain role/),
    ]);
    expect(
      screen.getByRole("button", { name: /^Kept: Strong team\. Edit keep note for Kept role/ }),
    ).toBeTruthy();

    const markers = within(screen.getByRole("group", { name: "Your markers" }));
    const favorites = markers.getByRole("button", { name: "Favorites (1)" });
    const kept = markers.getByRole("button", { name: "Kept (1)" });
    expect(favorites.getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(kept);
    expect(kept.getAttribute("aria-pressed")).toBe("true");
    await waitFor(() => expect(order()).toEqual([expect.stringMatching(/Kept role/)]));

    fireEvent.click(favorites);
    await waitFor(() =>
      expect(order()).toEqual([
        expect.stringMatching(/Favorite role/),
        expect.stringMatching(/Kept role/),
      ]),
    );

    fireEvent.click(kept);
    await waitFor(() => expect(order()).toEqual([expect.stringMatching(/Favorite role/)]));
    fireEvent.click(favorites);
    await waitFor(() => expect(order()).toHaveLength(3));
  });

  it("saves a favorite and a keep note through the markers route", async () => {
    const puts = requestLog();
    let row: PendingDiscovery = { ...queueRow("plain-1", "Plain role", 1), kept: true };
    server.use(
      http.put(`${WORKSPACE_ROUTE}/pending-discoveries/:pendingId/markers`, async ({ request }) => {
        await puts.record(request);
        row = { ...row, ...(puts.requests.at(-1)?.body as Partial<PendingDiscovery>) };
        return HttpResponse.json(row);
      }),
    );
    installRows([], { page: () => [row], masterCvVersions: CURRENT_MASTER_CV_VERSIONS });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole("button", { name: /Favorite Plain role/ }));
    await waitFor(() => expect(puts.requests).toHaveLength(1));
    expect(puts.requests[0]?.body).toEqual({ favorite: true, kept: true, keep_note: null });
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /Favorite Plain role/ }).getAttribute("aria-pressed"),
      ).toBe("true"),
    );

    fireEvent.click(screen.getByRole("button", { name: /Edit keep note for Plain role/ }));
    const note = screen.getByLabelText(/Keep note for Plain role/);
    fireEvent.change(note, { target: { value: "Recruiter called" } });
    fireEvent.keyDown(note, { key: "Enter" });
    await waitFor(() => expect(puts.requests).toHaveLength(2));
    expect(puts.requests[1]?.url.pathname).toMatch(/pending-discoveries\/plain-1\/markers$/);
    expect(puts.requests[1]?.body).toEqual({
      favorite: true,
      kept: true,
      keep_note: "Recruiter called",
    });
  });

  it("returns focus to the queue when removing a marker drops the row from the view", async () => {
    let row: PendingDiscovery = {
      ...queueRow("closed-1", "Closed role", 1),
      kept: true,
      withdrawn_at: "2026-09-01T00:00:00Z",
    };
    server.use(
      http.put(`${WORKSPACE_ROUTE}/pending-discoveries/:pendingId/markers`, async ({ request }) => {
        row = { ...row, ...((await request.json()) as Partial<PendingDiscovery>) };
        return HttpResponse.json(row);
      }),
    );
    installRows([], {
      page: () =>
        row.kept || row.favorite
          ? [row, queueRow("open-1", "Open role", 2)]
          : [queueRow("open-1", "Open role", 2)],
      masterCvVersions: CURRENT_MASTER_CV_VERSIONS,
    });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const keep = await screen.findByRole("button", { name: /Keep Closed role/ });
    keep.focus();
    fireEvent.click(keep);
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Remove keep marker" }),
    );

    await waitFor(() => expect(screen.queryByText("Closed role")).toBeNull());
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole("group", { name: /keyboard triage/i })),
    );
  });

  // Confirms a row dismissal, holds the apply open, and drops focus to the body while
  // the Preview button is disabled; the returned call lets the apply finish.
  async function confirmDismissalWhileFocusDrops({ rowLeaves }: { rowLeaves: boolean }) {
    const dismissed = new Set<string>();
    let releaseApply = () => {};
    const applyHeld = new Promise<void>((resolve) => {
      releaseApply = resolve;
    });
    server.use(
      ...dispositionHandlers({
        onApply: async (request) => {
          const body = (await request.json()) as { pending_discovery_ids?: string[] };
          for (const id of body.pending_discovery_ids ?? []) dismissed.add(id);
          await applyHeld;
        },
      }),
    );
    installRows([], {
      page: () =>
        [queueRow("plain-1", "Plain role", 1), queueRow("plain-2", "Other role", 2)].filter(
          (candidate) => !rowLeaves || !dismissed.has(candidate.id),
        ),
      masterCvVersions: CURRENT_MASTER_CV_VERSIONS,
    });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const plainRow = (await screen.findByText("Plain role")).closest("tr");
    if (!plainRow) throw new Error("Expected the Plain role row.");
    fireEvent.click(within(plainRow).getByRole("button", { name: "Dismiss from pending" }));
    fireEvent.change(screen.getByLabelText("Dismissal reason"), {
      target: { value: "invalid_page" },
    });
    const preview = screen.getByRole<HTMLButtonElement>("button", { name: "Preview dismissal" });
    preview.focus();
    fireEvent.click(preview);
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Dismiss discoveries",
      }),
    );

    await waitFor(() => expect([...dismissed]).toEqual(["plain-1"]));
    await waitFor(() => expect(preview.disabled).toBe(true));
    act(() => (document.activeElement as HTMLElement | null)?.blur());
    expect(document.activeElement).toBe(document.body);
    return { plainRow, releaseApply };
  }

  it("returns focus to the queue when a row's confirmed dismissal removes it", async () => {
    const { releaseApply } = await confirmDismissalWhileFocusDrops({ rowLeaves: true });

    releaseApply();
    await waitFor(() => expect(screen.queryByText("Plain role")).toBeNull());
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole("group", { name: /keyboard triage/i })),
    );
  });

  it("returns focus to the row's Dismiss button when its confirmed dismissal keeps it", async () => {
    const { plainRow, releaseApply } = await confirmDismissalWhileFocusDrops({ rowLeaves: false });

    releaseApply();
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Preview dismissal" })).toBeNull(),
    );
    await waitFor(() =>
      expect(document.activeElement).toBe(
        within(plainRow).getByRole("button", { name: "Dismiss from pending" }),
      ),
    );
  });

  it("keeps a row's dismissal editor open while the queue re-renders with the same rows", async () => {
    vi.useFakeTimers();
    try {
      installRows(pendingDiscoveryRows(1));
      render(
        <MemoryRouter>
          <AppProviders>
            <PendingDiscoveries workspaceId="workspace-1" />
          </AppProviders>
        </MemoryRouter>,
      );
      const row = (await vi.waitFor(() => screen.getByText("Role 0"))).closest("tr");
      if (!row) throw new Error("Expected the Role 0 row.");
      fireEvent.click(within(row).getByRole("button", { name: "Dismiss from pending" }));
      expect(screen.getByLabelText("Dismissal reason")).toBeTruthy();

      // Opening the history re-renders the page around the queue without changing its rows.
      fireEvent.click(screen.getByText("Closed posting history"));
      await act(() => vi.advanceTimersByTimeAsync(10));

      expect(screen.queryByLabelText("Dismissal reason")).not.toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("offers no dismiss-all while the rows shown belong to the filter just left", async () => {
    installQueueWithReviews(
      [queueRow("stale-1", "Role Stale", 2), queueRow("current-1", "Role Current", 1)],
      [queueReview("stale-1", "review", ["master_cv"]), queueReview("current-1", "review")],
    );
    server.use(
      http.get(`${WORKSPACE_ROUTE}/pending-discoveries/page`, async ({ request }) => {
        if (new URL(request.url).searchParams.has("review_status")) await delay("infinite");
      }),
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(await screen.findByRole("button", { name: "Dismiss all shown (2)" })).toBeTruthy();
    openMoreFilters();
    fireEvent.change(screen.getByLabelText("Review status"), { target: { value: "out_of_date" } });

    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /Dismiss all shown/ })).toBeNull(),
    );
    expect(screen.getByText("Role Current")).toBeTruthy();
  });

  it("leaves focus where the person put it when a row they left later drops out", async () => {
    installQueueWithReviews(
      [queueRow("stale-1", "Role Stale", 2), queueRow("current-1", "Role Current", 1)],
      [queueReview("stale-1", "review", ["master_cv"]), queueReview("current-1", "review")],
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const currentRow = (await screen.findByText("Role Current")).closest("tr");
    if (!currentRow) throw new Error("Expected the Role Current row.");
    const dismiss = within(currentRow).getByRole("button", { name: "Dismiss from pending" });
    dismiss.focus();
    dismiss.blur();
    await act(async () => {});
    openMoreFilters();
    fireEvent.change(screen.getByLabelText("Review status"), { target: { value: "out_of_date" } });

    await waitFor(() => expect(screen.queryByText("Role Current")).toBeNull());
    expect(document.activeElement).toBe(document.body);
  });

  it("uses the exact review embedded with a pending page row", async () => {
    const review = queueReview("embedded", "review");
    installQueueWithReviews(
      [{ ...queueRow("embedded", "Embedded review", 1), relevance_review: review }],
      [],
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(await screen.findByText("High relevance")).toBeTruthy();
  });

  it("disables paid work for a posting with saved closure evidence", async () => {
    installQueueWithReviews(
      [{ ...queueRow("closed", "Closed role", 1), current_posting_status: "closed" }],
      [],
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const checkbox = await screen.findByLabelText("Select Closed role at Globex");
    expect(screen.queryByRole("button", { name: "Full evaluation" })).toBeNull();
    fireEvent.click(checkbox);
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "Review relevance (1)" }).disabled,
    ).toBe(true);
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "Full evaluation (1)" }).disabled,
    ).toBe(true);
    expect(
      screen.getByRole("button", { name: "Mark closed from saved evidence (1)" }),
    ).toBeTruthy();
  });

  it("offers relevance review for a row that already carries a current review", async () => {
    installQueueWithReviews(
      [queueRow("current", "Current review", 1)],
      [queueReview("current", "review")],
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByLabelText("Select Current review at Globex"));
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "Review relevance (1)" }).disabled,
    ).toBe(false);
  });

  it("selects every row of a lane that fits one action", async () => {
    installRows(pendingDiscoveryRows(51));
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByLabelText("Select eligible discoveries on this page"));
    expect(screen.getByLabelText<HTMLInputElement>("Select Role 50 at Globex").checked).toBe(true);
    expect(screen.getByLabelText<HTMLInputElement>("Select Role 0 at Globex").checked).toBe(true);
    expect(screen.getByText("51 selected")).toBeTruthy();
  });

  it("re-renders only the row whose selection changed", async () => {
    installRows(pendingDiscoveryRows(3));
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByLabelText("Select Role 1 at Globex");
    await waitFor(() => expect(screen.getByText("0 selected")).toBeTruthy());
    rowBodyRenders.clear();

    fireEvent.click(screen.getByLabelText("Select Role 1 at Globex"));

    await screen.findByText("1 selected");
    expect(rowBodyRenders.get("pending-1") ?? 0).toBeGreaterThan(0);
    expect(rowBodyRenders.get("pending-0") ?? 0).toBe(0);
    expect(rowBodyRenders.get("pending-2") ?? 0).toBe(0);
  });

  const rowIndexOf = (title: string) =>
    screen
      .getAllByRole("row")
      .map((node) => node.textContent ?? "")
      .findIndex((text) => text.includes(title));

  it("leads each row with a color-coded semantic verdict and demotes the initial screen", async () => {
    installQueueWithReviews(
      [queueRow("reviewed-1", "Role Alpha", 1), queueRow("unreviewed-1", "Role Beta", 2)],
      [queueReview("reviewed-1", "review")],
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(await screen.findByRole("columnheader", { name: "Relevance review" })).toBeTruthy();
    expect(screen.queryByRole("columnheader", { name: "Initial recommendation" })).toBeNull();

    const reviewedRow = screen.getByLabelText("Select Role Alpha at Globex").closest("tr");
    if (!reviewedRow) throw new Error("Expected the reviewed row.");
    const verdict = within(reviewedRow).getByText("High relevance");
    expect(verdict.className).toContain("status--tone-positive");
    const initial = within(reviewedRow).getByText("Initial: Investigate");
    expect(initial.tagName).toBe("P");
    expect(initial.className).toContain("field-note");

    const unreviewedRow = screen.getByLabelText("Select Role Beta at Globex").closest("tr");
    if (!unreviewedRow) throw new Error("Expected the unreviewed row.");
    // An unreviewed row badges its initial recommendation, not a placeholder,
    // so triage has an immediate verdict-shaped signal before any paid review runs.
    const initialBadge = within(unreviewedRow).getByText(
      (_, node) => node?.className === "status status--tone-muted pending-verdict__badge",
    );
    expect(initialBadge.textContent).toBe("Investigate");
  });

  it("shows verdict, reason, matched title, and a screening chip on the row without opening Details", async () => {
    const review = queueReview("flagged-1", "review");
    installQueueWithReviews(
      [
        {
          ...queueRow("flagged-1", "Role Gamma", 1),
          matched_target_title: "Director, Clinical Informatics",
          screening_findings: [
            {
              code: "location_unresolved",
              dimension: "location",
              standing: "unknown",
              subject: "",
            },
          ],
        },
      ],
      [review],
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const row = (await screen.findByLabelText("Select Role Gamma at Globex")).closest("tr");
    if (!row) throw new Error("Expected the flagged row.");
    expect(screen.queryByRole("button", { name: "Hide details" })).toBeNull();
    const scoped = within(row);
    expect(scoped.getByText("High relevance")).toBeTruthy();
    const reason = scoped.getByText("Typed screening result.");
    expect(reason.getAttribute("title")).toBe("Typed screening result.");
    expect(scoped.getByText("Matches: Director, Clinical Informatics")).toBeTruthy();
    expect(scoped.getByText("Location unresolved")).toBeTruthy();
    expect(scoped.getByText(review.summary)).toBeTruthy();
    expect(scoped.queryByText(/Review search preferences/)).toBeNull();

    fireEvent.click(scoped.getByRole("button", { name: "Details" }));
    expect(
      await screen.findByText("Refresh postings, or paste the current posting text below."),
    ).toBeTruthy();
    // The full relevance review card duplicates the row's short summary line.
    expect(screen.getByText(/Automated relevance review:/).parentElement?.textContent).toContain(
      review.summary,
    );
  });

  it("names each review-lane title finding on its chip and folds unknown pay into the route chip", async () => {
    const titleFinding = (code: ScreeningFinding["code"]): ScreeningFinding => ({
      code,
      dimension: "title",
      standing: "unknown",
      subject: "Director, Clinical Informatics",
    });
    installQueueWithReviews(
      [
        {
          ...queueRow("near-1", "Role Near", 1),
          constraint_confidence: "uncertain_comp",
          screening_findings: [
            titleFinding("title_near_match"),
            {
              code: "compensation_unknown",
              dimension: "compensation",
              standing: "unknown",
              subject: "",
            },
          ],
        },
        {
          ...queueRow("family-1", "Role Family", 2),
          screening_findings: [titleFinding("title_role_family_review")],
        },
        {
          ...queueRow("extra-1", "Role Extra", 3),
          screening_findings: [titleFinding("title_extra_role_words")],
        },
      ],
      [],
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const rowFor = async (title: string) => {
      const row = (await screen.findByLabelText(`Select ${title} at Globex`)).closest("tr");
      if (!row) throw new Error(`Expected ${title}.`);
      return within(row);
    };
    const near = await rowFor("Role Near");
    expect(near.getByText("Title near match")).toBeTruthy();
    expect(near.getByText("Compensation uncertain")).toBeTruthy();
    expect(near.queryByText("Comp unknown")).toBeNull();
    expect((await rowFor("Role Family")).getByText("Title family review")).toBeTruthy();
    expect((await rowFor("Role Extra")).getByText("Title extra words")).toBeTruthy();
    expect(screen.queryByText("Title mismatch")).toBeNull();
  });

  it("lists only genuinely additional places beside the stated location", async () => {
    const locations = (values: string[]) => ({
      values,
      unresolved_values: [],
      provenance: "workday.location",
      complete: true,
    });
    const withPlaces = (id: string, title: string, rank: number, values: string[]) => {
      const row = queueRow(id, title, rank);
      return { ...row, staged_facts: { ...row.staged_facts, locations: locations(values) } };
    };
    installQueueWithReviews(
      [
        withPlaces("one-1", "Role Country", 1, ["Northtown", "Canada"]),
        withPlaces("many-1", "Role Many", 2, ["Northtown", "Southtown", "Easttown"]),
        withPlaces("two-1", "Role Second", 3, ["Southtown"]),
      ],
      [],
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const rowFor = async (title: string) => {
      const row = (await screen.findByLabelText(`Select ${title} at Globex`)).closest("tr");
      if (!row) throw new Error(`Expected ${title}.`);
      return within(row);
    };
    expect((await rowFor("Role Country")).queryByText(/^Also:/)).toBeNull();
    expect((await rowFor("Role Many")).getByText("Also: Southtown, Easttown")).toBeTruthy();
    expect((await rowFor("Role Second")).getByText("Also: Southtown")).toBeTruthy();
  });

  it("counts and filters a stale verdict as its verdict, marked out of date", async () => {
    installQueueWithReviews(
      [queueRow("stale-1", "Role Alpha", 1), queueRow("fresh-1", "Role Beta", 1)],
      [queueReview("stale-1", "unlikely", ["master_cv"])],
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const staleRow = (await screen.findByLabelText("Select Role Alpha at Globex")).closest("tr");
    if (!staleRow) throw new Error("Expected the stale row.");
    expect(within(staleRow).getByText("Low relevance")).toBeTruthy();
    expect(within(staleRow).getByText("Out of date")).toBeTruthy();

    const relevance = screen.getByLabelText<HTMLSelectElement>("Relevance verdict");
    expect(within(relevance).getByRole("option", { name: "Not yet reviewed (1)" })).toBeTruthy();
    expect(
      within(relevance).getByRole("option", { name: "Low relevance (1, 1 out of date)" }),
    ).toBeTruthy();

    fireEvent.change(relevance, { target: { value: "unlikely" } });
    await waitFor(() => expect(screen.queryByText("Role Beta")).toBeNull());
    expect(screen.getByText("Role Alpha")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Select eligible discoveries on this page"));
    expect(screen.getByText("1 selected")).toBeTruthy();
  });

  it("moves a disputed row up the queue without passing a verdict the model raised", async () => {
    installQueueWithReviews(
      [
        queueRow("high-1", "Role High", 9),
        queueRow("closer-1", "Role Closer", 9),
        queueRow("disputed-1", "Role Disputed", 9),
        queueRow("unreviewed-1", "Role Unreviewed", 9),
      ],
      [
        queueReview("high-1", "review"),
        queueReview("closer-1", "investigate"),
        {
          ...queueReview("disputed-1", "unlikely"),
          disputed_at: "2026-09-17T00:00:00Z",
          dispute_note: "The advertised work is on my record.",
        },
      ],
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByText("Role Disputed");
    expect(rowIndexOf("Role High")).toBeLessThan(rowIndexOf("Role Disputed"));
    expect(rowIndexOf("Role Closer")).toBeLessThan(rowIndexOf("Role Disputed"));
    expect(rowIndexOf("Role Disputed")).toBeLessThan(rowIndexOf("Role Unreviewed"));

    const relevance = screen.getByLabelText<HTMLSelectElement>("Relevance verdict");
    expect(within(relevance).getByRole("option", { name: "Disputed by you (1)" })).toBeTruthy();
    fireEvent.change(relevance, { target: { value: "disputed" } });

    await waitFor(() => expect(screen.queryByText("Role High")).toBeNull());
    expect(screen.getByText("Role Disputed")).toBeTruthy();
    expect(screen.queryByText("Role Unreviewed")).toBeNull();
    const disputedRow = screen.getByText("Role Disputed").closest("tr");
    if (!disputedRow) throw new Error("Expected the disputed row.");
    expect(within(disputedRow).getByText("Disputed by you")).toBeTruthy();
    expect(within(disputedRow).getByText("Model verdict: Low relevance")).toBeTruthy();
  });

  it("defaults to a semantic-relevance sort and keeps a discovery-rank alternative", async () => {
    installQueueWithReviews(
      [queueRow("high-1", "Role High", 1), queueRow("low-1", "Role Low", 9)],
      [queueReview("high-1", "review"), queueReview("low-1", "unlikely")],
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByText("Role High");
    const sort = screen.getByLabelText<HTMLSelectElement>("Sort by");
    expect(sort.value).toBe("relevance");
    expect(rowIndexOf("Role High")).toBeLessThan(rowIndexOf("Role Low"));

    fireEvent.change(sort, { target: { value: "rank" } });
    expect(rowIndexOf("Role Low")).toBeLessThan(rowIndexOf("Role High"));
  });

  it("filters the queue by whether a review is out of date", async () => {
    installQueueWithReviews(
      [queueRow("stale-1", "Role Stale", 2), queueRow("current-1", "Role Current", 1)],
      [queueReview("stale-1", "review", ["master_cv"]), queueReview("current-1", "review")],
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByText("Role Stale");
    openMoreFilters();
    const staleness = screen.getByLabelText<HTMLSelectElement>("Review status");

    fireEvent.change(staleness, { target: { value: "out_of_date" } });
    await waitFor(() => expect(screen.queryByText("Role Current")).toBeNull());
    expect(screen.getByText("Role Stale")).toBeTruthy();

    fireEvent.change(staleness, { target: { value: "current" } });
    await waitFor(() => expect(screen.queryByText("Role Stale")).toBeNull());
    expect(screen.getByText("Role Current")).toBeTruthy();
  });

  it("shows no changes banner when neither follow-up nor new rows are reported", async () => {
    installQueue({ page: pendingDiscoveryPage(), changes: pendingDiscoveryChanges() });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByText(/No openings are waiting/);
    await waitFor(() => expect(screen.queryByText(/Publisher follow-up/)).toBeNull());
    expect(screen.queryByText(/New since this list loaded/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Refresh list" })).toBeNull();
  });

  it("shows a lane-split changes banner when follow-up and new rows are both reported", async () => {
    installQueue({
      page: pendingDiscoveryPage(),
      changes: pendingDiscoveryChanges({
        active_sources: 2,
        automatic_pending_count: 5,
        frontier_completed_count: 40,
        ready: 3,
        attention: 1,
      }),
    });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(
        "Reading 2 sources; 40 publisher links read; 5 known links queued. Final remaining total unknown. New since this list loaded: 3 ready, 1 need attention.",
      ),
    ).toBeTruthy();
    expect(screen.getByText(/Reading 2 sources; 40 publisher links read/).closest("header")).toBe(
      screen.getByRole("heading", { name: "Pending discoveries" }).closest("header"),
    );
    expect(screen.getByRole("button", { name: "Refresh list" })).toBeTruthy();
  });

  it("keeps the remaining total unknown while follow-up has no queued links", async () => {
    installQueue({
      page: pendingDiscoveryPage(),
      changes: pendingDiscoveryChanges({ active_sources: 1, automatic_pending_count: 0 }),
    });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(
        "Reading 1 source; 0 publisher links read; 0 known links queued. Final remaining total unknown.",
      ),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Refresh list" })).toBeTruthy();
  });

  it("does not describe inactive follow-up as reading when new rows are reported", async () => {
    installQueue({
      page: pendingDiscoveryPage(),
      changes: pendingDiscoveryChanges({
        frontier_completed_count: 589,
        automatic_pending_count: 1,
        ready: 2,
      }),
    });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(
      await screen.findByText("New since this list loaded: 2 ready, 0 need attention."),
    ).toBeTruthy();
    expect(screen.queryByText(/Reading \d+ sources?;/)).toBeNull();
    expect(screen.queryByText(/Final remaining total unknown/)).toBeNull();
    expect(screen.getByRole("button", { name: "Refresh list" })).toBeTruthy();
  });

  it("counts new rows under the filters the page is showing", async () => {
    const changesReads: URLSearchParams[] = [];
    installQueue({
      page: pendingDiscoveryPage(),
      changes: pendingDiscoveryChanges(),
      onRequest: (url) => {
        if (url.pathname.endsWith("/pending-discoveries/changes")) {
          changesReads.push(url.searchParams);
        }
      },
    });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    openMoreFilters();
    const staleness = await screen.findByLabelText<HTMLSelectElement>("Review status");
    fireEvent.change(staleness, { target: { value: "out_of_date" } });

    await waitFor(() => expect(changesReads.at(-1)?.get("review_status")).toBe("out_of_date"));
  });

  it("suppresses the changes banner while a paging notice is shown", async () => {
    const oldSnapshot = "a".repeat(64);
    const newSnapshot = "b".repeat(64);
    let laterPageReads = 0;
    installQueue({
      changes: pendingDiscoveryChanges({ active_sources: 1, automatic_pending_count: 1 }),
      onPageRequest: (url) => {
        const readyOffset = Number(url.searchParams.get("ready_offset") ?? 0);
        if (readyOffset > 0) {
          laterPageReads += 1;
          if (laterPageReads === 1) {
            return HttpResponse.json(
              { detail: "The pending discovery queue changed." },
              { status: 409, headers: { "Content-Type": "application/problem+json" } },
            );
          }
        }
        return HttpResponse.json(
          pendingDiscoveryPage({
            snapshot_sha256: laterPageReads > 0 ? newSnapshot : oldSnapshot,
            ready: { items: [], total: 100, has_more: true },
          }),
        );
      },
    });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(
        screen.getByRole<HTMLButtonElement>("button", { name: "Next ready page" }).disabled,
      ).toBe(false),
    );
    expect(await screen.findByRole("button", { name: "Refresh list" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Next ready page" }));

    expect(await screen.findByText(QUEUE_RESTART_NOTICE)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Refresh list" })).toBeNull();
  });

  it("resets paging and focuses the panel heading when Refresh list is clicked", async () => {
    installQueue({ page: pendingDiscoveryPage(), changes: pendingDiscoveryChanges({ ready: 2 }) });
    render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Refresh list" }));

    expect(
      await screen.findByText(
        "The list was refreshed from the beginning, so the order may have changed.",
      ),
    ).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Pending discoveries" })).toBe(
      document.activeElement,
    );
  });

  it("refetches the first page on Refresh and polls changes from its new snapshot", async () => {
    const firstSnapshot = "2026-09-12T00:00:00Z";
    const refreshedSnapshot = "2026-09-12T01:00:00Z";
    let serverSnapshot = firstSnapshot;
    let refreshedChangesReads = 0;
    const requestsAfterClick: URL[] = [];
    let clicked = false;
    vi.useFakeTimers();
    try {
      let changesSnapshotBefore = "";
      installQueue({
        onRequest: (url) => {
          if (clicked) requestsAfterClick.push(url);
          if (url.pathname.endsWith("/pending-discoveries/changes")) {
            changesSnapshotBefore = url.searchParams.get("snapshot_before") ?? "";
          }
        },
        onPageRequest: (url) => {
          const before = url.searchParams.get("snapshot_before") || serverSnapshot;
          return HttpResponse.json(
            pendingDiscoveryPage({
              snapshot_sha256: (before === firstSnapshot ? "a" : "b").repeat(64),
              snapshot_before: before,
            }),
          );
        },
        changes: () => {
          const fromRefreshed = changesSnapshotBefore === refreshedSnapshot;
          if (fromRefreshed) refreshedChangesReads += 1;
          const followUpStarted = fromRefreshed && refreshedChangesReads > 1;
          return pendingDiscoveryChanges({
            active_sources: followUpStarted ? 1 : 0,
            automatic_pending_count: followUpStarted ? 3 : 0,
            frontier_completed_count: followUpStarted ? 12 : 0,
            ready: fromRefreshed ? 0 : 2,
          });
        },
      });
      render(
        <MemoryRouter>
          <AppProviders>
            <PendingDiscoveries workspaceId="workspace-1" />
          </AppProviders>
        </MemoryRouter>,
      );

      await vi.waitFor(() =>
        expect(screen.getByRole("button", { name: "Refresh list" })).toBeTruthy(),
      );
      serverSnapshot = refreshedSnapshot;
      clicked = true;
      fireEvent.click(screen.getByRole("button", { name: "Refresh list" }));
      await vi.waitFor(() =>
        expect(
          requestsAfterClick.some(
            (url) =>
              url.pathname.endsWith("/pending-discoveries/page") &&
              !url.searchParams.has("snapshot_before"),
          ),
        ).toBe(true),
      );
      await vi.waitFor(() => expect(refreshedChangesReads).toBe(1));
      expect(screen.queryByRole("button", { name: "Refresh list" })).toBeNull();

      await vi.advanceTimersByTimeAsync(60_000);
      await vi.waitFor(() =>
        expect(
          screen.getByText(
            "Reading 1 source; 12 publisher links read; 3 known links queued. Final remaining total unknown.",
          ),
        ).toBeTruthy(),
      );
      expect(screen.getByRole("button", { name: "Refresh list" })).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  /** Answer each changes poll with the next reading, repeating the last one. */
  function installChangesSequence(responses: PendingDiscoveryChanges[]) {
    let index = 0;
    installQueue({
      page: pendingDiscoveryPage(),
      changes: () => {
        const response = responses[Math.min(index, responses.length - 1)];
        index += 1;
        if (!response) throw new Error("The changes sequence is empty.");
        return response;
      },
    });
  }

  it("does not announce follow-up that was already active on first load", async () => {
    installChangesSequence([
      pendingDiscoveryChanges({
        active_sources: 1,
        automatic_pending_count: 1,
        frontier_completed_count: 589,
      }),
    ]);
    const { container } = render(
      <MemoryRouter>
        <AppProviders>
          <PendingDiscoveries workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByText(
      "Reading 1 source; 589 publisher links read; 1 known link queued. Final remaining total unknown.",
    );
    expect(container.querySelector(".visually-hidden")?.textContent).toBe("");
  });

  it("announces exactly once when follow-up goes from active to finished", async () => {
    vi.useFakeTimers();
    try {
      installChangesSequence([
        pendingDiscoveryChanges({ active_sources: 1, automatic_pending_count: 4 }),
        pendingDiscoveryChanges(),
      ]);
      const { container } = render(
        <MemoryRouter>
          <AppProviders>
            <PendingDiscoveries workspaceId="workspace-1" />
          </AppProviders>
        </MemoryRouter>,
      );
      const liveRegion = () => container.querySelector(".visually-hidden");

      await vi.waitFor(() => expect(screen.queryByText(/Reading \d+ sources?;/)).toBeTruthy());
      expect(liveRegion()?.textContent).toBe("");

      await vi.advanceTimersByTimeAsync(15_000);
      await vi.waitFor(() =>
        expect(liveRegion()?.textContent).toBe("Publisher follow-up finished."),
      );

      await vi.advanceTimersByTimeAsync(60_000);
      expect(liveRegion()?.textContent).toBe("Publisher follow-up finished.");
    } finally {
      vi.useRealTimers();
    }
  });

  it("announces nothing new when the pending-link count changes while still active", async () => {
    vi.useFakeTimers();
    try {
      installChangesSequence([
        pendingDiscoveryChanges({
          active_sources: 1,
          automatic_pending_count: 4,
          frontier_completed_count: 10,
        }),
        pendingDiscoveryChanges({
          active_sources: 1,
          automatic_pending_count: 2,
          frontier_completed_count: 12,
        }),
      ]);
      const { container } = render(
        <MemoryRouter>
          <AppProviders>
            <PendingDiscoveries workspaceId="workspace-1" />
          </AppProviders>
        </MemoryRouter>,
      );
      const liveRegion = () => container.querySelector(".visually-hidden");

      await vi.waitFor(() => expect(screen.queryByText(/Reading \d+ sources?;/)).toBeTruthy());
      expect(liveRegion()?.textContent).toBe("");

      await vi.advanceTimersByTimeAsync(15_000);
      await vi.waitFor(() =>
        expect(
          screen.getByText(
            "Reading 1 source; 12 publisher links read; 2 known links queued. Final remaining total unknown.",
          ),
        ).toBeTruthy(),
      );
      expect(liveRegion()?.textContent).toBe("");
    } finally {
      vi.useRealTimers();
    }
  });

  it("announces once when new rows first appear while follow-up stays inactive", async () => {
    vi.useFakeTimers();
    try {
      installChangesSequence([
        pendingDiscoveryChanges(),
        pendingDiscoveryChanges({ ready: 1 }),
        pendingDiscoveryChanges({ ready: 1 }),
      ]);
      const { container } = render(
        <MemoryRouter>
          <AppProviders>
            <PendingDiscoveries workspaceId="workspace-1" />
          </AppProviders>
        </MemoryRouter>,
      );
      const liveRegion = () => container.querySelector(".visually-hidden");

      await vi.waitFor(() => expect(screen.queryByText(/No openings are waiting/)).toBeTruthy());
      expect(liveRegion()?.textContent).toBe("");

      await vi.advanceTimersByTimeAsync(60_000);
      await vi.waitFor(() =>
        expect(liveRegion()?.textContent).toBe("New pending discoveries are available."),
      );

      await vi.advanceTimersByTimeAsync(15_000);
      expect(liveRegion()?.textContent).toBe("New pending discoveries are available.");
    } finally {
      vi.useRealTimers();
    }
  });
});
