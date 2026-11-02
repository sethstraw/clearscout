import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import type { DispositionSelection, PendingDiscovery, PendingMarkers } from "../../api/client";
import { AppProviders } from "../../providers";
import { jobSummary } from "../job-ledger/jobFixtures";
import { server } from "../../test/msw/server";
import { requestLog } from "../../test/requestLog";
import {
  DiscoveryDispositionManager,
  DismissalEditor,
  DismissSelection,
} from "./DiscoveryDispositions";
import {
  dismissedDiscovery,
  dispositionTarget,
  duplicateCandidate,
  pendingDiscoveryRow,
  pendingDisposition,
  publishedFact,
  stagedFacts,
} from "./pendingDiscoveryFixtures";
import {
  dispositionHandlers,
  type DispositionReads,
} from "../../test/msw/pendingDiscoveryHandlers";

const discovery: PendingDiscovery = pendingDiscoveryRow({
  also_in: ["South District"],
  arrangement_categories: ["unknown"],
  arrangement_categories_source: "missing",
  canonical_url: "https://jobs.example.test/opening/1",
  content_status: "not_yet_read",
  readiness: "no_body",
  retained_content_id: null,
  current_content_id: null,
  discovery_url: "https://jobs.example.test/opening/1",
  employer: "Northwind Research",
  id: "pending-1",
  location: "Northtown",
  rank: 1,
  recommendation: "unlikely",
  recommendation_because: "The title is excluded.",
  constraint_confidence: "excluded",
  screening_findings: [
    { code: "title_excluded", dimension: "title", standing: "refuse", subject: "Excluded title" },
  ],
  screening_standing: "refuse",
  staged_facts: stagedFacts({ locations: publishedFact(["Northtown"], "board API") }),
  title: "Warehouse Supervisor",
});

function installDispositions(reads: DispositionReads = {}) {
  server.use(...dispositionHandlers(reads));
}

const RESTORE_LABEL = "Operations Coordinator at Contoso Health";

/** One dismissed row, its restore preview and apply, and the markers PUT, each logged. */
function installRestoreAndKeep({
  failMarkers = false,
  skippedWithdrawnIds,
}: { failMarkers?: boolean; skippedWithdrawnIds?: string[] } = {}) {
  const previews = requestLog();
  const applies = requestLog();
  const markerPuts = requestLog();
  installDispositions({
    dismissed: [
      dismissedDiscovery({
        pending_discovery_id: "dismissed-1",
        employer: "Contoso Health",
        title: "Operations Coordinator",
      }),
    ],
    onApply: applies.record,
    onPreview: previews.record,
    skippedWithdrawnIds,
    targetsFor: () => [
      dispositionTarget("dismissed-1", {
        employer: "Contoso Health",
        title: "Operations Coordinator",
      }),
    ],
  });
  server.use(
    http.put(
      "*/api/v1/workspaces/:workspaceId/pending-discoveries/:pendingId/markers",
      async ({ request }) => {
        await markerPuts.record(request);
        if (failMarkers) {
          return HttpResponse.json(
            { detail: "The markers could not be saved." },
            { status: 409, headers: { "Content-Type": "application/problem+json" } },
          );
        }
        const body = markerPuts.requests.at(-1)?.body as PendingMarkers;
        return HttpResponse.json({
          ...pendingDiscoveryRow({ id: "dismissed-1" }),
          ...body,
        } satisfies PendingDiscovery);
      },
    ),
  );
  return { applies, markerPuts, previews };
}

function renderManager() {
  render(
    <AppProviders>
      <DiscoveryDispositionManager
        discoveries={[discovery]}
        visibleDiscoveries={[discovery]}
        workspaceId="workspace-1"
      />
    </AppProviders>,
  );
}

// A browser moves focus to the body when a focused control becomes disabled. jsdom
// does not, and will not blur a disabled control, so focus passes through a removed sink.
async function pressDisablingControl(control: HTMLButtonElement) {
  fireEvent.click(control);
  await waitFor(() => expect(control.disabled).toBe(true));
  const sink = document.body.appendChild(document.createElement("button"));
  sink.focus();
  sink.remove();
  expect(document.activeElement).toBe(document.body);
}

/** A request handler hold that the test releases, to observe the in-flight state. */
function held() {
  let release = () => undefined as void;
  const released = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { release: () => release(), wait: () => released };
}

/** The preview echo every cohort test shares: one primary target per selected id. */
function cohortTargets(cohort: PendingDiscovery[]) {
  return (selection: DispositionSelection) =>
    selection.pending_discovery_ids.map((id, index) =>
      dispositionTarget(id, {
        employer: cohort[index]?.employer ?? "Adatum Logistics",
        title: cohort[index]?.title ?? "Warehouse Supervisor",
      }),
    );
}

describe("DismissSelection confirmation", () => {
  it("names a posting with no title or employer by its page instead of a bare 'at'", async () => {
    installDispositions({
      targetsFor: () => [
        dispositionTarget("pending-untitled", {
          employer: "",
          title: "",
          location: "",
          discovery_url: "https://careers.example.test/search?tag=data",
          canonical_url: "https://careers.example.test/search?tag=data",
        }),
      ],
    });
    render(
      <AppProviders>
        <DismissSelection
          label="Dismiss selected (1)"
          pendingDiscoveryIds={["pending-untitled"]}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Dismiss selected (1)" }));
    fireEvent.change(screen.getByLabelText("Dismissal reason"), {
      target: { value: "invalid_page" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Preview dismissal" }));

    const targets = await screen.findByRole("list", { name: "Exact affected jobs" });
    expect(targets.textContent).toBe("posting at careers.example.test");
  });

  it("says a dismissal of a row its source no longer lists stays until it is listed again", async () => {
    installDispositions({
      targetsFor: () => [
        dispositionTarget("pending-gone", { withdrawn_at: "2026-09-09T00:00:00Z" }),
      ],
    });
    render(
      <AppProviders>
        <DismissSelection
          label="Dismiss selected (1)"
          pendingDiscoveryIds={["pending-gone"]}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Dismiss selected (1)" }));
    fireEvent.change(screen.getByLabelText("Dismissal reason"), {
      target: { value: "closed_at_source" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Preview dismissal" }));

    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByText(/stays dismissed until its source lists it again/)).toBeTruthy();
    expect(dialog.queryByText(/reversible/)).toBeNull();
  });

  it("says how many favorite or kept rows the dismissal left in the queue", async () => {
    const applies = requestLog();
    installDispositions({
      skippedMarkedIds: ["pending-kept"],
      targetsFor: () => [dispositionTarget("pending-plain")],
      onApply: applies.record,
    });
    render(
      <AppProviders>
        <DismissSelection
          label="Dismiss selected (2)"
          pendingDiscoveryIds={["pending-plain", "pending-kept"]}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Dismiss selected (2)" }));
    fireEvent.change(screen.getByLabelText("Dismissal reason"), {
      target: { value: "invalid_page" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Preview dismissal" }));
    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByText(/1 favorite or kept discovery is left out/)).toBeTruthy();
    fireEvent.click(dialog.getByRole("button", { name: "Dismiss discoveries" }));

    expect((await screen.findByRole("status")).textContent).toBe("1 favorite or kept row skipped.");
    expect(applies.requests[0]?.body).not.toHaveProperty("dismiss_marked", true);
  });
});

describe("DismissalEditor for a favorite or kept row", () => {
  it("confirms in the app that dismissing clears the markers, and sends the confirmation", async () => {
    const previews = requestLog();
    installDispositions({ onPreview: previews.record });
    render(
      <AppProviders>
        <DismissalEditor
          id="dismiss-marked"
          marked
          onClose={() => undefined}
          pendingDiscoveryIds={["pending-1"]}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    fireEvent.change(screen.getByLabelText("Dismissal reason"), {
      target: { value: "invalid_page" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Preview dismissal" }));

    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByText(/Dismissing it clears its markers/)).toBeTruthy();
    expect(previews.requests[0]?.body).toMatchObject({ dismiss_marked: true });
  });
});

describe("DismissalEditor opened from a duplicate suggestion", () => {
  it("refuses to preview a suggested job that has left the pending list, and says so", async () => {
    installDispositions({
      duplicateCandidates: [
        duplicateCandidate({
          id: "pending-3",
          employer: "Initech",
          title: "Data Analyst",
          discovery_url: "https://jobs.example.test/opening/3",
        }),
      ],
    });
    render(
      <AppProviders>
        <DismissalEditor
          id="dismiss-suggested"
          initialCategory="duplicate_pending"
          initialDuplicateOfPendingId="pending-2"
          onClose={() => undefined}
          pendingDiscoveryIds={["pending-1"]}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    expect((await screen.findByRole("alert")).textContent).toMatch(/no longer in the pending list/);
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "Preview dismissal" }).disabled,
    ).toBe(true);

    fireEvent.change(screen.getByLabelText("Duplicate of pending discovery"), {
      target: { value: "pending-3" },
    });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "Preview dismissal" }).disabled,
    ).toBe(false);
  });

  it("previews the suggested pending duplicate without a choice being made", async () => {
    const previews = requestLog();
    installDispositions({
      duplicateCandidates: [duplicateCandidate()],
      onPreview: previews.record,
      targetsFor: () => [],
    });
    render(
      <AppProviders>
        <DismissalEditor
          id="dismiss-suggested"
          initialCategory="duplicate_pending"
          initialDuplicateOfPendingId="pending-2"
          onClose={() => undefined}
          pendingDiscoveryIds={["pending-1"]}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    expect(screen.getByLabelText<HTMLSelectElement>("Dismissal reason").value).toBe(
      "duplicate_pending",
    );
    await screen.findByRole("option", { name: "Data Steward at Globex (Remote)" });
    expect(screen.getByLabelText<HTMLSelectElement>("Duplicate of pending discovery").value).toBe(
      "pending-2",
    );
    fireEvent.click(screen.getByRole("button", { name: "Preview dismissal" }));

    await waitFor(() =>
      expect(previews.requests.at(-1)?.body).toMatchObject({
        category: "duplicate_pending",
        duplicate_of_pending_id: "pending-2",
      }),
    );
  });
});

describe("DiscoveryDispositionManager", () => {
  it("does not claim nothing is dismissed while the list is still loading", async () => {
    installDispositions({ dismissedPending: true });

    render(
      <AppProviders>
        <DiscoveryDispositionManager
          discoveries={[discovery]}
          visibleDiscoveries={[discovery]}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    expect(await screen.findByText("Loading dismissed discoveries")).toBeTruthy();
    expect(screen.queryByText("No discoveries are currently dismissed.")).toBeNull();
  });

  it("pages a long dismissal list instead of rendering every row at once", async () => {
    const rows = Array.from({ length: 250 }, (_, index) =>
      dismissedDiscovery({
        id: `event-${index}`,
        pending_discovery_id: `pending-${index}`,
        employer: `Employer ${String(index).padStart(3, "0")}`,
        title: `Role ${String(index).padStart(3, "0")}`,
        discovery_url: `https://jobs.example.test/pending-${index}`,
        canonical_url: `https://jobs.example.test/pending-${index}`,
      }),
    );
    installDispositions({ dismissed: rows });

    render(
      <AppProviders>
        <DiscoveryDispositionManager
          discoveries={[discovery]}
          visibleDiscoveries={[discovery]}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    expect(await screen.findByText(/Showing 1-100 of 250 matching/)).toBeTruthy();
    const table = screen.getByRole("table", { name: "Dismissed job discoveries" });
    expect(within(table).getAllByRole("row")).toHaveLength(101);

    fireEvent.click(screen.getByRole("button", { name: "Next" }));

    expect(await screen.findByText(/Showing 101-200 of 250 matching/)).toBeTruthy();
    expect(screen.queryByText("Role 000")).toBeNull();
    expect(screen.getByText("Role 100")).toBeTruthy();
  });

  it("finds one dismissed row among many by employer and by category", async () => {
    installDispositions({
      dismissed: [
        dismissedDiscovery({
          id: "event-1",
          pending_discovery_id: "pending-1",
          title: "Platform Steward",
          employer: "Northwind Research",
          category: "out_of_scope_title",
          discovery_url: "https://jobs.example.test/pending-1",
          canonical_url: "https://jobs.example.test/pending-1",
        }),
        dismissedDiscovery({
          id: "event-2",
          pending_discovery_id: "pending-2",
          title: "Care Coordinator",
          employer: "Contoso Health",
          category: "closed_at_source",
          discovery_url: "https://jobs.example.test/pending-2",
          canonical_url: "https://jobs.example.test/pending-2",
        }),
      ],
    });

    render(
      <AppProviders>
        <DiscoveryDispositionManager
          discoveries={[discovery]}
          visibleDiscoveries={[discovery]}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    expect(await screen.findByText(/Platform Steward/)).toBeTruthy();
    const dismissedTable = screen.getByRole("table", { name: "Dismissed job discoveries" });
    expect(within(dismissedTable).getByRole("columnheader", { name: "Employer" })).toBeTruthy();
    expect(
      within(dismissedTable).getByRole("columnheader", { name: "Why dismissed" }),
    ).toBeTruthy();
    expect(within(dismissedTable).getByRole("columnheader", { name: "Actions" })).toBeTruthy();
    expect(within(dismissedTable).getAllByText("2026-08-30")).toHaveLength(2);
    expect(within(dismissedTable).getByText("Title outside scope")).toBeTruthy();
    expect(within(dismissedTable).getByText("out_of_scope_title")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Sort by Employer ascending" }));
    const ascendingFirstRow = within(dismissedTable).getAllByRole("row")[1];
    if (!ascendingFirstRow) throw new Error("Expected a dismissed row after the header.");
    expect(within(ascendingFirstRow).getByText("Contoso Health")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Sort by Employer descending" }));
    const descendingFirstRow = within(dismissedTable).getAllByRole("row")[1];
    if (!descendingFirstRow) throw new Error("Expected a dismissed row after the header.");
    expect(within(descendingFirstRow).getByText("Northwind Research")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Find a dismissed discovery"), {
      target: { value: "contoso" },
    });

    expect(screen.queryByText(/Platform Steward/)).toBeNull();
    expect(screen.getByText(/Care Coordinator/)).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Find a dismissed discovery"), {
      target: { value: "" },
    });
    fireEvent.change(screen.getByLabelText("Dismissal category"), {
      target: { value: "out_of_scope_title" },
    });

    expect(screen.getByText(/Platform Steward/)).toBeTruthy();
    expect(screen.queryByText(/Care Coordinator/)).toBeNull();
    expect(screen.getByText("Showing 1 of 2 dismissed discoveries.")).toBeTruthy();
  });

  it("says a dismissed row's employer is not stated instead of leaving the cell blank", async () => {
    installDispositions({
      dismissed: [
        dismissedDiscovery({
          id: "event-1",
          pending_discovery_id: "pending-1",
          title: "Platform Steward",
          employer: "",
          discovery_url: "https://jobs.example.test/pending-1",
          canonical_url: "https://jobs.example.test/pending-1",
        }),
      ],
    });
    render(
      <AppProviders>
        <DiscoveryDispositionManager
          discoveries={[discovery]}
          visibleDiscoveries={[discovery]}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    const dismissedTable = await screen.findByRole("table", { name: "Dismissed job discoveries" });
    expect(within(dismissedTable).getByText("Employer not stated")).toBeTruthy();
    expect(
      within(dismissedTable).getByRole("link", {
        name: "Platform Steward, employer not stated, listed on jobs.example.test",
      }),
    ).toBeTruthy();
  });

  it("opens a dismissed Himalayas row at its applicationLink and credits Himalayas", async () => {
    const applicationLink = "https://himalayas.app/companies/initech/jobs/platform-steward";
    installDispositions({
      dismissed: [
        dismissedDiscovery({
          title: "Platform Steward",
          employer: "Initech",
          discovery_url: applicationLink,
          canonical_url: "https://jobs.example.test/initech/88",
        }),
      ],
    });
    render(
      <AppProviders>
        <DiscoveryDispositionManager
          discoveries={[discovery]}
          visibleDiscoveries={[discovery]}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    const dismissedTable = within(
      await screen.findByRole("table", { name: "Dismissed job discoveries" }),
    );
    expect(
      dismissedTable.getByRole("link", { name: /^Platform Steward/ }).getAttribute("href"),
    ).toBe(applicationLink);
    expect(
      dismissedTable.getByRole("link", { name: "Job data from Himalayas" }).getAttribute("href"),
    ).toBe(applicationLink);
  });

  it("credits Arbeitnow on a dismissed row that opens an employer address", async () => {
    installDispositions({
      dismissed: [
        dismissedDiscovery({
          title: "Platform Steward",
          employer: "Initech",
          discovery_url: "https://www.arbeitnow.com/jobs/companies/initech/steward-2",
          canonical_url: "https://jobs.example.test/initech/88",
        }),
      ],
    });
    render(
      <AppProviders>
        <DiscoveryDispositionManager
          discoveries={[discovery]}
          visibleDiscoveries={[discovery]}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    const dismissedTable = within(
      await screen.findByRole("table", { name: "Dismissed job discoveries" }),
    );
    expect(
      dismissedTable.getByRole("link", { name: /^Platform Steward/ }).getAttribute("href"),
    ).toBe("https://jobs.example.test/initech/88");
    expect(
      dismissedTable.getByRole("link", { name: "Job data from Arbeitnow" }).getAttribute("href"),
    ).toBe("https://www.arbeitnow.com/");
  });

  it("says a dismissal can come from an evaluation refusal and names that origin", async () => {
    const refusal = {
      category: "office attendance",
      reason: "The posting requires four office days a week.",
      origin: "evaluation.refusal",
      selection_mode: "rule",
    } as const;
    installDispositions({
      dismissed: [
        dismissedDiscovery({
          ...refusal,
          title: "Platform Steward",
          location: "Northtown",
        }),
      ],
      history: [pendingDisposition(refusal)],
    });

    render(
      <AppProviders>
        <DiscoveryDispositionManager
          discoveries={[discovery]}
          visibleDiscoveries={[discovery]}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    // A refused evaluation dismisses the row, so the copy must say so.
    expect(screen.getByText(/automatically when an evaluation refuses a hard gate/)).toBeTruthy();
    expect(screen.queryByText(/is not an evaluation, refusal, or job archive/)).toBeNull();

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Show history for Platform Steward at Northwind Research",
      }),
    );

    expect(await screen.findByText("Automatic hard-gate refusal")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Dismissal origin"), {
      target: { value: "automatic" },
    });
    expect(screen.getByText("Showing 1 of 1 dismissed discoveries.")).toBeTruthy();
    expect(screen.getByText(/Restoring lets Full evaluation run past this condition/)).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Find a dismissed discovery"), {
      target: { value: "four office days" },
    });
    expect(screen.getByText(/Platform Steward/)).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Find a dismissed discovery"), {
      target: { value: "Automatic hard-gate refusal" },
    });
    expect(screen.getByText(/Platform Steward/)).toBeTruthy();
  });

  it("previews an arbitrary selected set through the shared disposition flow", async () => {
    const previews = requestLog();
    installDispositions({
      onPreview: previews.record,
      targetsFor: (selection) =>
        selection.pending_discovery_ids.map((id, index) =>
          dispositionTarget(id, { title: `Selected role ${index + 1}` }),
        ),
    });
    render(
      <AppProviders>
        <DismissSelection
          label="Dismiss selected (2)"
          pendingDiscoveryIds={["pending-1", "pending-2"]}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    const dismiss = screen.getByRole("button", { name: "Dismiss selected (2)" });
    expect(dismiss.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(dismiss);
    expect(dismiss.getAttribute("aria-expanded")).toBe("true");
    const category = screen.getByLabelText("Dismissal reason");
    expect(
      Array.from(category.querySelectorAll("option")).map((option) => [option.value, option.text]),
    ).toEqual([
      ["", "Choose a reason"],
      ["closed_at_source", "Closed at source"],
      ["out_of_scope_title", "Title outside scope"],
      ["out_of_scope_location", "Location outside scope"],
      ["out_of_scope_arrangement", "Work arrangement outside scope"],
      ["out_of_scope_travel", "Travel requirement outside scope"],
      ["out_of_scope_compensation", "Compensation outside scope"],
      ["invalid_page", "Not a job posting"],
      ["duplicate_pending", "Duplicate pending discovery"],
      ["duplicate_posting", "Duplicate posting"],
      ["employer_excluded", "Employer excluded"],
      ["current_evidence_unavailable", "Current evidence unavailable"],
      ["other", "Other reason"],
    ]);
    fireEvent.change(category, { target: { value: "current_evidence_unavailable" } });
    expect(screen.queryByLabelText("Other reason")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Preview dismissal" }));

    expect(
      (await screen.findByRole("list", { name: "Exact affected jobs" })).children,
    ).toHaveLength(2);
    expect(previews.requests.at(-1)?.body).toMatchObject({
      category: "current_evidence_unavailable",
      pending_discovery_ids: ["pending-1", "pending-2"],
      reason:
        "The posting was not evaluated: I chose to dismiss it because trustworthy current posting evidence remained unavailable.",
      selection_mode: "explicit",
    });
    expect(screen.getByText(/No suitability decision was made/)).toBeTruthy();
    expect(screen.getByText(/Every affected discovery can be restored manually/)).toBeTruthy();
  });

  it("requires prose only when the explicit category is other", async () => {
    const previews = requestLog();
    installDispositions({ onPreview: previews.record });
    render(
      <AppProviders>
        <DismissSelection
          label="Dismiss selected (1)"
          pendingDiscoveryIds={["pending-1"]}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Dismiss selected (1)" }));
    const preview = screen.getByRole<HTMLButtonElement>("button", { name: "Preview dismissal" });
    expect(preview.disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("Dismissal reason"), {
      target: { value: "other" },
    });
    expect(screen.getByLabelText("Other reason")).toBeTruthy();
    expect(preview.disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("Other reason"), {
      target: { value: "  A specific reason outside the standard choices.  " },
    });
    expect(preview.disabled).toBe(false);
    fireEvent.click(preview);

    await waitFor(() =>
      expect(previews.requests.at(-1)?.body).toMatchObject({
        category: "other",
        reason: "A specific reason outside the standard choices.",
      }),
    );
  });

  it("records a duplicate posting against a typed tracked-job reference", async () => {
    const previews = requestLog();
    installDispositions({
      jobs: [
        jobSummary({ job_posting_id: "job-42", title: "Data Steward", employer: "Globex" }),
        jobSummary({ job_posting_id: "job-43", title: "Growth Lead", employer: "Initech" }),
      ],
      onPreview: previews.record,
    });
    render(
      <AppProviders>
        <DismissSelection
          label="Dismiss selected (1)"
          pendingDiscoveryIds={["pending-1"]}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Dismiss selected (1)" }));
    fireEvent.change(screen.getByLabelText("Dismissal reason"), {
      target: { value: "duplicate_posting" },
    });
    const jobSearch = screen.getByLabelText("Find tracked job");
    await screen.findByRole("option", { name: "Data Steward at Globex" });
    fireEvent.change(jobSearch, { target: { value: "growth" } });
    expect(screen.queryByRole("option", { name: "Data Steward at Globex" })).toBeNull();
    expect(screen.getByRole("option", { name: "Growth Lead at Initech" })).toBeTruthy();
    fireEvent.change(jobSearch, { target: { value: "data" } });
    const preview = screen.getByRole<HTMLButtonElement>("button", { name: "Preview dismissal" });
    expect(preview.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Duplicate of tracked job"), {
      target: { value: "job-42" },
    });
    expect(preview.disabled).toBe(false);
    fireEvent.click(preview);

    await waitFor(() =>
      expect(previews.requests.at(-1)?.body).toMatchObject({
        category: "duplicate_posting",
        duplicate_of_job_id: "job-42",
        reason: "The posting duplicates another tracked opportunity.",
      }),
    );
  });

  it("records a pending duplicate against another active or dismissed pending discovery", async () => {
    const previews = requestLog();
    installDispositions({
      duplicateCandidates: [
        duplicateCandidate({
          id: "pending-1",
          employer: "Northwind Research",
          title: "Warehouse Supervisor",
          location: "Northtown",
          discovery_url: "https://jobs.example.test/opening/1",
        }),
        duplicateCandidate({ dismissed: true }),
      ],
      onPreview: previews.record,
    });
    render(
      <AppProviders>
        <DismissSelection
          label="Dismiss selected (1)"
          pendingDiscoveryIds={["pending-1"]}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Dismiss selected (1)" }));
    fireEvent.change(screen.getByLabelText("Dismissal reason"), {
      target: { value: "duplicate_pending" },
    });
    expect(screen.queryByRole("option", { name: /Warehouse Supervisor/ })).toBeNull();
    const retained = await screen.findByRole("option", {
      name: "Data Steward at Globex (Remote) (dismissed)",
    });
    fireEvent.change(screen.getByLabelText("Duplicate of pending discovery"), {
      target: { value: retained.getAttribute("value") },
    });
    fireEvent.click(screen.getByRole("button", { name: "Preview dismissal" }));

    await waitFor(() =>
      expect(previews.requests.at(-1)?.body).toMatchObject({
        category: "duplicate_pending",
        duplicate_of_pending_id: "pending-2",
        reason: "The posting duplicates another pending discovery.",
      }),
    );
  });

  it("previews exact variants before rule dismissal and restores a named dismissed job", async () => {
    const applies = requestLog();
    installDispositions({
      dismissed: [
        dismissedDiscovery({
          pending_discovery_id: "dismissed-1",
          category: "human_choice",
          reason: "Not the right lane.",
          employer: "Contoso Health",
          title: "Operations Coordinator",
          location: "South District",
          discovery_url: "https://jobs.example.test/dismissed-1",
          canonical_url: "https://jobs.example.test/dismissed-1",
        }),
      ],
      snapshotSha256: "a".repeat(64),
      onApply: applies.record,
      targetsFor: (selection) =>
        selection.action === "dismiss"
          ? [
              dispositionTarget("pending-1", {
                employer: "Contoso Health",
                title: "Platform Steward",
                location: "North District",
              }),
              dispositionTarget("variant-1", {
                primary_id: "pending-1",
                kind: "regional_variant",
                employer: "Contoso Health",
                title: "Platform Steward",
                location: "South District",
              }),
            ]
          : [
              dispositionTarget("dismissed-1", {
                employer: "Contoso Health",
                title: "Operations Coordinator",
                location: "South District",
              }),
            ],
    });
    render(
      <AppProviders>
        <DiscoveryDispositionManager
          discoveries={[discovery]}
          visibleDiscoveries={[discovery]}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Preview title excluded (1)" }));
    const confirmation = await screen.findByRole("dialog");
    expect(confirmation.textContent).toContain("Platform Steward at Contoso Health");
    expect(confirmation.textContent).toContain("South District) [regional variant]");
    expect(confirmation.textContent).not.toContain("pending-1");
    fireEvent.click(screen.getByRole("button", { name: "Dismiss discoveries" }));
    await waitFor(() => expect(applies.requests).toHaveLength(1));

    fireEvent.click(screen.getByText(/Dismissed discoveries/));
    expect(
      await screen.findByRole("link", { name: "Operations Coordinator at Contoso Health" }),
    ).toBeTruthy();
  });

  it("offers no category preview over rows the current view does not list", async () => {
    installDispositions();
    render(
      <AppProviders>
        <DiscoveryDispositionManager
          discoveries={[discovery]}
          visibleDiscoveries={[]}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    expect(await screen.findByText("Pending-list dispositions")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Preview title excluded/ })).toBeNull();
  });

  it("sends all discoveries shown by the queue filters as one typed cohort", async () => {
    const previews = requestLog();
    installDispositions({
      onPreview: previews.record,
      targetsFor: () => [
        dispositionTarget("pending-1", {
          discovery_url: discovery.discovery_url,
          canonical_url: discovery.canonical_url,
        }),
      ],
    });
    render(
      <AppProviders>
        <DiscoveryDispositionManager
          discoveries={[discovery]}
          visibleDiscoveries={[discovery]}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    expect(
      screen.getByText(
        /This cohort is the 1 discovery the current view lists, after every search, filter, marker, and page choice above\./,
      ),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss all shown (1)" }));
    fireEvent.change(screen.getByLabelText("Dismissal reason"), {
      target: { value: "employer_excluded" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Preview dismissal" }));

    expect(
      (await screen.findByRole("list", { name: "Exact affected jobs" })).textContent,
    ).toContain("Warehouse Supervisor at Northwind Research");
    expect(previews.requests.at(-1)?.body).toMatchObject({
      category: "employer_excluded",
      pending_discovery_ids: ["pending-1"],
      reason: "The employer is excluded from the current search.",
      selection_mode: "explicit",
    });
  });

  it("counts the never-read postings in a dismissal confirmation", async () => {
    const cohort = [
      { ...discovery, content_status: "not_yet_read" as const, id: "pending-1" },
      { ...discovery, content_status: "readable" as const, id: "pending-2" },
    ];
    installDispositions({ snapshotSha256: "e".repeat(64), targetsFor: cohortTargets(cohort) });
    render(
      <AppProviders>
        <DiscoveryDispositionManager
          discoveries={cohort}
          visibleDiscoveries={cohort}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Dismiss all shown (2)" }));
    fireEvent.change(screen.getByLabelText("Dismissal reason"), {
      target: { value: "closed_at_source" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Preview dismissal" }));

    expect((await screen.findByRole("dialog")).textContent?.replace(/\s+/g, " ")).toContain(
      "1 of these 2 were never read from the publisher",
    );
  });

  it("says nothing about never-read postings when the whole cohort is readable", async () => {
    const cohort = [
      { ...discovery, content_status: "readable" as const, id: "pending-1" },
      { ...discovery, content_status: "readable" as const, id: "pending-2" },
    ];
    installDispositions({ snapshotSha256: "f".repeat(64), targetsFor: cohortTargets(cohort) });
    render(
      <AppProviders>
        <DiscoveryDispositionManager
          discoveries={cohort}
          visibleDiscoveries={cohort}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Dismiss all shown (2)" }));
    fireEvent.change(screen.getByLabelText("Dismissal reason"), {
      target: { value: "closed_at_source" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Preview dismissal" }));

    await screen.findByRole("list", { name: "Exact affected jobs" });
    expect(screen.queryByText(/never read from the publisher/)).toBeNull();
  });

  it("says nothing about never-read postings when restoring a dismissal", async () => {
    installDispositions({
      dismissed: [
        dismissedDiscovery({
          category: "closed_at_source",
          reason: "The posting is closed at its source.",
          employer: "Adatum Logistics",
          location: "Northtown",
        }),
      ],
      snapshotSha256: "0".repeat(64),
      targetsFor: cohortTargets([discovery]),
    });
    render(
      <AppProviders>
        <DiscoveryDispositionManager
          discoveries={[discovery]}
          visibleDiscoveries={[discovery]}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Preview restore Warehouse Supervisor at Adatum Logistics",
      }),
    );

    await screen.findByRole("button", { name: "Restore discoveries" });
    expect(screen.queryByText(/never read from the publisher/)).toBeNull();
  });

  it("sends no marker call when a plain restore is confirmed", async () => {
    const { applies, markerPuts } = installRestoreAndKeep();
    renderManager();

    fireEvent.click(
      await screen.findByRole("button", { name: `Preview restore ${RESTORE_LABEL}` }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Restore discoveries" }));

    await waitFor(() => expect(applies.requests).toHaveLength(1));
    expect(markerPuts.requests).toHaveLength(0);
  });

  it.each([
    [["withdrawn-1"], "1 discovery is no longer listed by its source and stays dismissed."],
    [
      ["withdrawn-1", "withdrawn-2"],
      "2 discoveries are no longer listed by their source and stay dismissed.",
    ],
  ])("says how many withdrawn rows the restore leaves dismissed (%j)", async (ids, message) => {
    installRestoreAndKeep({ skippedWithdrawnIds: ids });
    renderManager();

    fireEvent.click(
      await screen.findByRole("button", { name: `Preview restore ${RESTORE_LABEL}` }),
    );

    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByText(message)).toBeTruthy();
  });

  it("restores a dismissed row and marks it kept with the typed note", async () => {
    const { applies, markerPuts } = installRestoreAndKeep();
    renderManager();

    fireEvent.click(
      await screen.findByRole("button", { name: `Restore and keep ${RESTORE_LABEL}` }),
    );
    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByText(/marked Kept/)).toBeTruthy();
    fireEvent.change(dialog.getByLabelText("Keep note (optional)"), {
      target: { value: "  Remote in practice  " },
    });
    fireEvent.click(dialog.getByRole("button", { name: "Restore and keep" }));

    await waitFor(() => expect(markerPuts.requests).toHaveLength(1));
    expect(applies.requests).toHaveLength(1);
    expect(applies.requests[0]?.body).toMatchObject({
      action: "restore",
      pending_discovery_ids: ["dismissed-1"],
    });
    expect(markerPuts.requests[0]?.url.pathname).toMatch(
      /\/pending-discoveries\/dismissed-1\/markers$/,
    );
    expect(markerPuts.requests[0]?.body).toEqual({
      favorite: false,
      kept: true,
      keep_note: "Remote in practice",
    } satisfies PendingMarkers);
  });

  it("sends neither the restore nor the markers when the confirmation is cancelled", async () => {
    const { applies, markerPuts, previews } = installRestoreAndKeep();
    renderManager();

    fireEvent.click(
      await screen.findByRole("button", { name: `Restore and keep ${RESTORE_LABEL}` }),
    );
    const dialog = within(await screen.findByRole("dialog"));
    fireEvent.click(dialog.getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(previews.requests).toHaveLength(1);
    expect(applies.requests).toHaveLength(0);
    expect(markerPuts.requests).toHaveLength(0);
  });

  it.each(["Cancel", "Restore discoveries"])(
    "returns focus to Preview restore, which disables itself during the preview, after %s",
    async (answer) => {
      const preview = held();
      const apply = held();
      installDispositions({
        dismissed: [dismissedDiscovery({ pending_discovery_id: "dismissed-1" })],
        onApply: apply.wait,
        onPreview: preview.wait,
      });
      renderManager();
      const trigger = await screen.findByRole<HTMLButtonElement>("button", {
        name: /^Preview restore /,
      });
      trigger.focus();
      await pressDisablingControl(trigger);
      preview.release();
      fireEvent.click(
        within(await screen.findByRole("dialog")).getByRole("button", { name: answer }),
      );
      if (answer !== "Cancel") {
        await waitFor(() => expect(trigger.disabled).toBe(true));
        expect(document.activeElement).not.toBe(trigger);
        apply.release();
      }

      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      await waitFor(() => expect(document.activeElement).toBe(trigger));
    },
  );

  it("lands on the dispositions panel when the control that started a restore is gone", async () => {
    installRestoreAndKeep();
    renderManager();
    const trigger = await screen.findByRole("button", {
      name: `Preview restore ${RESTORE_LABEL}`,
    });
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = within(await screen.findByRole("dialog"));
    trigger.closest("tr")?.remove();
    fireEvent.click(dialog.getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByText("Pending-list dispositions").closest("details"),
      ),
    );
  });

  it("says the row was restored but not kept when the marker call fails", async () => {
    const { applies } = installRestoreAndKeep({ failMarkers: true });
    renderManager();

    fireEvent.click(
      await screen.findByRole("button", { name: `Restore and keep ${RESTORE_LABEL}` }),
    );
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Restore and keep" }),
    );

    expect(
      await screen.findByText(`Restored ${RESTORE_LABEL} but did not mark it kept.`, {
        exact: false,
      }),
    ).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain("The markers could not be saved.");
    expect(applies.requests).toHaveLength(1);
  });

  it("keeps every exact target in a structured list for a large cohort", async () => {
    const largeCohort = Array.from({ length: 172 }, (_, index) => ({
      ...discovery,
      id: `pending-${index + 1}`,
      title: `Synthetic role ${index + 1}`,
    }));
    installDispositions({
      snapshotSha256: "c".repeat(64),
      targetsFor: () =>
        largeCohort.map((item) =>
          dispositionTarget(item.id, {
            employer: item.employer,
            title: item.title,
            location: item.location,
            discovery_url: item.discovery_url,
            canonical_url: item.canonical_url,
          }),
        ),
    });
    render(
      <AppProviders>
        <DiscoveryDispositionManager
          discoveries={largeCohort}
          visibleDiscoveries={largeCohort}
          workspaceId="workspace-1"
        />
      </AppProviders>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Dismiss all shown (172)" }));
    fireEvent.change(screen.getByLabelText("Dismissal reason"), {
      target: { value: "closed_at_source" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Preview dismissal" }));

    const exactTargets = await screen.findByRole("list", { name: "Exact affected jobs" });
    expect(exactTargets.classList).toContain("confirmation-target-list");
    expect(exactTargets.querySelectorAll("li")).toHaveLength(172);
    expect(exactTargets.textContent).toContain("Synthetic role 1 at Northwind Research");
    expect(exactTargets.textContent).toContain("Synthetic role 172 at Northwind Research");
    expect(screen.getByText("Exact affected jobs (172)")).toBeTruthy();
  });
});
