import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { SourceScanOutcomePage } from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { TASK_TYPE_CATALOG } from "../../test/taskTypes";
import { DiscoveryFunnelAudit } from "./DiscoveryFunnelAudit";
import { SourceScanOutcomes } from "./SourceScanOutcomes";
import { sourceScanOutcome, sourceScanOutcomePage } from "./sourceScanFixtures";

const API = "*/api/v1";
const WORKSPACE = `${API}/workspaces/:workspaceId`;

const page = sourceScanOutcomePage();

/** The ledger read, plus the task catalog every paid affordance needs. */
function useOutcomes(answer: (url: URL) => SourceScanOutcomePage, requested?: URL[]) {
  server.use(
    http.get(`${API}/tasks/types`, () => HttpResponse.json(TASK_TYPE_CATALOG)),
    http.get(`${WORKSPACE}/source-scan-outcomes`, ({ request }) => {
      const url = new URL(request.url);
      requested?.push(url);
      return HttpResponse.json(answer(url));
    }),
  );
}

describe("SourceScanOutcomes", () => {
  it("avoids repeating an identical source name while preserving its identity", async () => {
    useOutcomes(() =>
      sourceScanOutcomePage({ items: [sourceScanOutcome({ source_name: "Acme" })] }),
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes taskId="task-1" workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );
    const table = await screen.findByRole("table");
    expect(within(table).getAllByText("Acme")).toHaveLength(1);
    expect(within(table).getByText("Same name as employer").getAttribute("title")).toBe("Acme");
  });

  it("shows and clears a linked source absent from retained options", async () => {
    const source = "North & West + Careers/Canada";
    const requested: URL[] = [];
    useOutcomes(
      (url) =>
        url.searchParams.has("source")
          ? sourceScanOutcomePage({ items: [], total: 0 })
          : sourceScanOutcomePage(),
      requested,
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes workspaceId="workspace-1" taskId="task-1" initialSource={source} />
        </AppProviders>
      </MemoryRouter>,
    );
    const filter = await screen.findByLabelText<HTMLSelectElement>("Source");
    expect(filter.value).toBe(source);
    expect(filter.selectedOptions[0]?.textContent).toContain(source);
    expect(requested[0]?.searchParams.get("source")).toBe(source);
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(await screen.findByRole("table")).toBeTruthy();
    expect(filter.value).toBe("");
    expect(requested.at(-1)?.searchParams.has("source")).toBe(false);
  });

  it("holds pagination while rapid search settles and while a page refetches", async () => {
    const requested: URL[] = [];
    let releasePage!: () => void;
    const heldPage = new Promise<void>((resolve) => {
      releasePage = resolve;
    });
    let held = false;
    server.use(
      http.get(`${API}/tasks/types`, () => HttpResponse.json(TASK_TYPE_CATALOG)),
      http.get(`${WORKSPACE}/source-scan-outcomes`, async ({ request }) => {
        const url = new URL(request.url);
        requested.push(url);
        if (url.searchParams.get("search") === "platform") {
          held = true;
          await heldPage;
          return HttpResponse.json(sourceScanOutcomePage({ total: 250 }));
        }
        return HttpResponse.json(
          sourceScanOutcomePage({
            total: 250,
            offset: Number(url.searchParams.get("offset") ?? 0),
          }),
        );
      }),
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes workspaceId="workspace-1" taskId="task-1" />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByRole("table");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("Showing 101-200 of 250 recorded outcomes.");
    const input = screen.getByRole("searchbox");
    fireEvent.change(input, { target: { value: "plat" } });
    fireEvent.change(input, { target: { value: "platform" } });
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Next" }).disabled).toBe(true);
    await waitFor(() => expect(held).toBe(true));
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Next" }).disabled).toBe(true);
    expect(
      requested
        .filter((url) => url.searchParams.has("search"))
        .map((url) => url.searchParams.get("search")),
    ).toEqual(["platform"]);
    expect(requested.at(-1)?.searchParams.get("offset")).toBe("0");
    releasePage();
    await waitFor(() =>
      expect(screen.getByRole<HTMLButtonElement>("button", { name: "Next" }).disabled).toBe(false),
    );
  });

  it("renders the accessible audit table with the full deterministic reason", async () => {
    useOutcomes(() => page);

    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes taskId="task-1" workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Screened candidates" })).toBeTruthy();
    expect(screen.getByText(/Candidate decisions recorded for this discovery task/)).toBeTruthy();
    expect(
      screen.getByText("Recorded all 1 candidate decision from this discovery task."),
    ).toBeTruthy();
    const table = screen.getByRole("table");
    expect(within(table).getByText("excluded employer")).toBeTruthy();
    expect(within(table).getByText("location: location outside: Berlin")).toBeTruthy();
    expect(
      within(table).getByRole("link", { name: "Data Product Lead" }).getAttribute("href"),
    ).toBe("https://acme.example/jobs/one");
    expect(screen.getByText("Showing 1-1 of 1 recorded outcome.")).toBeTruthy();
  });

  it("counts a campaign coverage claim over the candidates the task read", async () => {
    useOutcomes(() => page);

    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes
            discoveryKind="guided_campaign"
            taskId="task-1"
            workspaceId="workspace-1"
          />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(
        "Recorded a decision for all 1 candidate this task read. URLs the campaign captured but " +
          "did not read are in the Campaign report.",
      ),
    ).toBeTruthy();
    expect(
      screen.queryByText("Recorded all 1 candidate decision from this discovery task."),
    ).toBeNull();
  });

  it("adds a screened posting and queues its content read", async () => {
    const posts: { path: string; body: Record<string, unknown> }[] = [];
    const record = async (request: Request) => {
      posts.push({
        path: new URL(request.url).pathname,
        body: (await request.json()) as Record<string, unknown>,
      });
    };
    useOutcomes(() => page);
    server.use(
      http.post(`${WORKSPACE}/pending-discoveries`, async ({ request }) => {
        await record(request);
        return HttpResponse.json({
          outcome: "created",
          pending_discovery: { id: "pending-promoted" },
          posting_check: null,
        });
      }),
      http.post(`${API}/tasks`, async ({ request }) => {
        await record(request);
        return HttpResponse.json({ id: "refresh-task", status: "queued" });
      }),
      http.get(`${API}/tasks/refresh-task`, () =>
        HttpResponse.json({ id: "refresh-task", status: "completed", progress: {} }),
      ),
    );

    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes taskId="task-1" workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Add Data Product Lead at Acme to pending and read",
      }),
    );

    expect(await screen.findByText("Added; posting read finished")).toBeTruthy();
    expect(posts.map((post) => post.path)).toEqual(
      expect.arrayContaining([
        expect.stringMatching(/pending-discoveries$/),
        expect.stringMatching(/tasks$/),
      ]),
    );
    expect(posts.find((post) => post.path.endsWith("/tasks"))?.body).toMatchObject({
      task_type: "discovery.refresh_batch",
      payload: { pending_discovery_ids: ["pending-promoted"] },
    });
  });

  it("does not offer to add an outcome already staged by the scan", async () => {
    useOutcomes(() =>
      sourceScanOutcomePage({
        items: [sourceScanOutcome({ terminal_stage: "staged", reason: "staged" })],
        terminal_stages: ["staged"],
      }),
    );

    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes taskId="task-1" workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(await screen.findByText("Already added to pending")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /to pending and read/ })).toBeNull();
  });

  it("sends filter and sort changes to the task-scoped server ledger", async () => {
    const requested: URL[] = [];
    useOutcomes(() => page, requested);

    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes taskId="task-1" workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByRole("table");
    fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "excluded_employer" } });
    await screen.findByRole("table");
    fireEvent.click(screen.getByRole("button", { name: "Sort by Role ascending" }));

    await waitFor(() => {
      expect(
        requested.some(
          (url) =>
            url.searchParams.get("task_id") === "task-1" &&
            url.searchParams.get("reason") === "excluded_employer" &&
            url.searchParams.get("sort") === "title",
        ),
      ).toBe(true);
    });
    expect(
      (await screen.findByRole("columnheader", { name: /Role/ })).getAttribute("aria-sort"),
    ).toBe("ascending");
  });

  it("hides the Reason filter when it offers no distinct value to select on", async () => {
    useOutcomes(() => sourceScanOutcomePage({ reasons: ["staged"], terminal_stages: ["staged"] }));

    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes taskId="task-1" workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    await screen.findByRole("table");
    expect(screen.queryByLabelText("Reason")).toBeNull();
    expect(screen.getByLabelText("Outcome")).toBeTruthy();
  });

  it("marks the recovered-partial coverage note as a data limitation", async () => {
    useOutcomes(() =>
      sourceScanOutcomePage({
        capture_state: "recovered_partial",
        recorded_outcomes: 485,
        reported_outcomes: 3292,
      }),
    );

    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes taskId="task-1" workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const note = await screen.findByText(/omitted candidates cannot be reconstructed/);
    const count = screen.getByText(/Showing 1-1 of 1 recorded outcome/);
    expect(note.className).toBe("screened-outcomes__limit");
    expect(count.className).toBe("field-note screened-outcomes__note");
  });

  it("counts follow-up reads inside the one number the listing shows", async () => {
    useOutcomes(() =>
      sourceScanOutcomePage({
        items: [sourceScanOutcome(), sourceScanOutcome({ id: "outcome-2" })],
        recorded_outcomes: 2,
        follow_up_outcomes: 1,
        reported_outcomes: 1,
      }),
    );

    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes taskId="task-1" workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(
        "Recorded all 2 candidate decisions from this discovery task, 1 of them from later " +
          "reads that continued this scan.",
      ),
    ).toBeTruthy();
    expect(screen.getByText(/Showing 1-2 of 2 recorded outcomes/)).toBeTruthy();
  });

  it("names the campaign, not a scan, when follow-up reads continued it", async () => {
    useOutcomes(() =>
      sourceScanOutcomePage({
        items: [sourceScanOutcome(), sourceScanOutcome({ id: "outcome-2" })],
        recorded_outcomes: 2,
        follow_up_outcomes: 1,
        reported_outcomes: 1,
      }),
    );

    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes
            discoveryKind="guided_campaign"
            taskId="task-1"
            workspaceId="workspace-1"
          />
        </AppProviders>
      </MemoryRouter>,
    );

    const note = await screen.findByText(/Recorded a decision for all 2 candidates/);
    expect(note.textContent).toContain("1 of them from later reads that continued this campaign.");
    expect(note.textContent).not.toContain("this scan");
  });

  it("leaves a follow-up count off a running task and off an unreconstructable one", async () => {
    useOutcomes(() =>
      sourceScanOutcomePage({
        capture_state: "in_progress",
        recorded_outcomes: 1,
        follow_up_outcomes: 1,
        reported_outcomes: null,
      }),
    );

    const { unmount } = render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes taskId="task-1" workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const running = await screen.findByText(/The discovery task is still running/);
    expect(running.textContent).toBe(
      "The discovery task is still running. 1 candidate decision is available so far.",
    );
    unmount();

    useOutcomes(() =>
      sourceScanOutcomePage({
        capture_state: "recovered_partial",
        recorded_outcomes: 485,
        follow_up_outcomes: 4,
        reported_outcomes: 3292,
      }),
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes taskId="task-2" workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const limited = await screen.findByText(/omitted candidates cannot be reconstructed/);
    expect(limited.textContent).toBe(
      "Recovered 481 of 3292 task outcomes. The earlier task payload kept only a bounded set " +
        "of candidate details, so omitted candidates cannot be reconstructed. A further 4 rows " +
        "came from later reads that continued this scan.",
    );
  });

  it("states a backfilled ledger against what the task itself reported", async () => {
    useOutcomes(() =>
      sourceScanOutcomePage({
        capture_state: "recovered",
        recorded_outcomes: 7,
        follow_up_outcomes: 3,
        reported_outcomes: 4,
      }),
    );

    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes taskId="task-1" workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const note = await screen.findByText(/Recovered 4 of 4 recorded task outcomes/);
    expect(note.textContent).toBe(
      "Recovered 4 of 4 recorded task outcomes. A further 3 rows came from later reads that " +
        "continued this scan.",
    );
  });

  it("reports no coverage claim when the task kept no outcome totals", async () => {
    useOutcomes(() =>
      sourceScanOutcomePage({
        capture_state: "unavailable",
        recorded_outcomes: 2,
        reported_outcomes: null,
      }),
    );

    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes taskId="task-1" workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const note = await screen.findByText(/candidate-ledger coverage is unavailable/);
    expect(note.className).toBe("screened-outcomes__limit");
  });

  it("keeps a short ledger from counting follow-up rows against what the task reported", async () => {
    useOutcomes(() =>
      sourceScanOutcomePage({
        capture_state: "partial",
        recorded_outcomes: 5,
        follow_up_outcomes: 2,
        reported_outcomes: 4,
      }),
    );

    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes taskId="task-1" workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const limited = await screen.findByText(/not listed/);
    expect(limited.textContent).toBe(
      "Recorded 3 of 4 candidate decisions. Another decision is not listed: it repeated a " +
        "candidate listed here, or a later scan read the same URL and holds its newer " +
        "sighting. A further 2 rows came from later reads that continued this scan.",
    );
  });

  it("counts more than one repeated decision in the plural", async () => {
    useOutcomes(() =>
      sourceScanOutcomePage({
        capture_state: "partial",
        recorded_outcomes: 2,
        follow_up_outcomes: 0,
        reported_outcomes: 5,
      }),
    );

    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes taskId="task-1" workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const limited = await screen.findByText(/not listed/);
    expect(limited.textContent).toBe(
      "Recorded 2 of 5 candidate decisions. 3 other decisions are not listed: each repeated " +
        "a candidate listed here, or a later scan read the same URL and holds its newer " +
        "sighting.",
    );
  });

  it("claims no repeated decisions when the ledger is not shorter than the total", async () => {
    useOutcomes(() =>
      sourceScanOutcomePage({
        capture_state: "partial",
        recorded_outcomes: 4,
        follow_up_outcomes: 0,
        reported_outcomes: 4,
      }),
    );

    const { unmount } = render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes taskId="task-1" workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const even = await screen.findByText(/Recorded 4 of 4 candidate decisions/);
    expect(even.textContent).toBe("Recorded 4 of 4 candidate decisions.");
    unmount();

    useOutcomes(() =>
      sourceScanOutcomePage({
        capture_state: "partial",
        recorded_outcomes: 9,
        follow_up_outcomes: 0,
        reported_outcomes: 3,
      }),
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes taskId="task-2" workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const longer = await screen.findByText(/Recorded 9 of 3 candidate decisions/);
    expect(longer.textContent).toBe("Recorded 9 of 3 candidate decisions.");
  });

  it("keeps an absent task total out of a recovered coverage claim", async () => {
    useOutcomes(() =>
      sourceScanOutcomePage({
        capture_state: "recovered",
        recorded_outcomes: 3,
        follow_up_outcomes: 0,
        reported_outcomes: undefined,
      }),
    );

    const { unmount } = render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes taskId="task-1" workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(await screen.findByText(/candidate-ledger coverage is unavailable/)).toBeTruthy();
    unmount();

    useOutcomes(() =>
      sourceScanOutcomePage({
        capture_state: "recovered_partial",
        recorded_outcomes: 3,
        follow_up_outcomes: 0,
        reported_outcomes: undefined,
      }),
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes taskId="task-2" workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(await screen.findByText(/candidate-ledger coverage is unavailable/)).toBeTruthy();
  });

  it("labels a title-less board lead without claiming whether its URL is a board or posting", async () => {
    useOutcomes(() =>
      sourceScanOutcomePage({
        items: [
          sourceScanOutcome({
            id: "outcome-2",
            candidate_kind: "board_lead",
            url: "https://jobs.example/acme/jobs/product-lead",
            title: "",
            terminal_stage: "read",
            reason: "no_new_openings",
            screening_findings: [],
          }),
        ],
        candidate_kinds: ["board_lead"],
      }),
    );

    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes taskId="task-1" workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const table = await screen.findByRole("table");
    expect(within(table).getByRole("link", { name: "Scanned address" }).getAttribute("href")).toBe(
      "https://jobs.example/acme/jobs/product-lead",
    );
    expect(within(table).queryByRole("link", { name: "View posting" })).toBeNull();
    expect(within(table).queryByRole("link", { name: "Board page" })).toBeNull();
    expect(
      within(table).getByRole("link", { name: "Review source configuration" }).getAttribute("href"),
    ).toBe("/find?tab=boards");
  });

  it("offers the reason the funnel counted above, spelled the same way", async () => {
    useOutcomes(() =>
      sourceScanOutcomePage({
        items: [sourceScanOutcome({ reason: "seniority_outside_band" })],
        reasons: ["seniority_outside_band", "staged"],
      }),
    );

    render(
      <MemoryRouter>
        <AppProviders>
          <DiscoveryFunnelAudit
            value={{
              counts: { proposed: 4, staged: 0, rejected: 4 },
              total_outcomes: 4,
              omitted_outcomes: 0,
              omission_reason: "",
              outcomes: [],
              reason_counts: { seniority_outside_band: 4 },
            }}
          />
          <SourceScanOutcomes taskId="task-1" workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(screen.getByText(/4 screened on seniority/)).toBeTruthy();
    const filter = await screen.findByLabelText<HTMLSelectElement>("Reason");
    expect(within(filter).getByRole("option", { name: "screened on seniority" })).toBeTruthy();
    expect(within(filter).queryByRole("option", { name: "seniority outside band" })).toBeNull();
  });

  it("routes to the board list in the app rather than reloading the filtered ledger", async () => {
    useOutcomes(() =>
      sourceScanOutcomePage({
        items: [
          sourceScanOutcome({
            candidate_kind: "board_lead",
            reason: "no_new_openings",
            screening_findings: [],
            terminal_stage: "read",
            url: "",
          }),
        ],
        candidate_kinds: ["board_lead"],
      }),
    );

    render(
      <MemoryRouter initialEntries={["/ledger"]}>
        <AppProviders>
          <Routes>
            <Route
              element={<SourceScanOutcomes taskId="task-1" workspaceId="workspace-1" />}
              path="/ledger"
            />
            <Route element={<h1>Your boards</h1>} path="/find" />
          </Routes>
        </AppProviders>
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole("link", { name: "Review source configuration" }));

    expect(await screen.findByRole("heading", { name: "Your boards" })).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("spells a row's reason the way the filter that selects it is spelled", async () => {
    useOutcomes(() =>
      sourceScanOutcomePage({
        items: [sourceScanOutcome({ reason: "insufficient_title_evidence" })],
        reasons: ["insufficient_title_evidence", "staged"],
      }),
    );

    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes taskId="task-1" workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const table = await screen.findByRole("table");
    expect(within(table).getByRole("columnheader", { name: "Reason" })).toBeTruthy();
    expect(
      within(table).queryByRole("columnheader", { name: "Exact deterministic reason" }),
    ).toBeNull();
    expect(within(table).getByText("screened on title evidence")).toBeTruthy();
    expect(within(table).queryByText("insufficient title evidence")).toBeNull();
    const filter = await screen.findByLabelText<HTMLSelectElement>("Reason");
    expect(within(filter).getByRole("option", { name: "screened on title evidence" })).toBeTruthy();
  });

  it("says a campaign read nothing in the same words the funnel uses", async () => {
    useOutcomes(() =>
      sourceScanOutcomePage({
        items: [],
        sources: [],
        reasons: [],
        terminal_stages: [],
        candidate_kinds: [],
        reported_outcomes: 0,
      }),
    );

    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes
            discoveryKind="guided_campaign"
            taskId="task-1"
            workspaceId="workspace-1"
          />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(await screen.findByText("This campaign read no candidates.")).toBeTruthy();
    expect(screen.queryByText("This discovery task recorded no candidate decisions.")).toBeNull();
  });

  it("says a task recorded nothing rather than blaming filters nobody set", async () => {
    useOutcomes(() =>
      sourceScanOutcomePage({
        items: [],
        sources: [],
        reasons: [],
        terminal_stages: [],
        candidate_kinds: [],
        reported_outcomes: 0,
      }),
    );

    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes taskId="task-1" workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    expect(
      await screen.findByText("This discovery task recorded no candidate decisions."),
    ).toBeTruthy();
    expect(screen.queryByText(/Change or clear the filters/)).toBeNull();
  });

  it("routes a specialist IC review row to the setting that screened it", async () => {
    useOutcomes(() =>
      sourceScanOutcomePage({
        items: [
          sourceScanOutcome({
            reason: "insufficient_title_evidence",
            screening_findings: [
              {
                dimension: "title",
                code: "title_specialist_ic_review",
                subject: "Data systems architect",
              },
            ],
          }),
        ],
        reasons: ["insufficient_title_evidence"],
      }),
    );

    render(
      <MemoryRouter>
        <AppProviders>
          <SourceScanOutcomes taskId="task-1" workspaceId="workspace-1" />
        </AppProviders>
      </MemoryRouter>,
    );

    const table = await screen.findByRole("table");
    const nextAction = within(within(table).getAllByRole("row")[1]!).getAllByRole("cell").at(-1)!;
    expect(
      within(nextAction)
        .getByRole("link", { name: "Edit specialist IC review settings" })
        .getAttribute("href"),
    ).toBe("/settings?tab=search");
  });
});
