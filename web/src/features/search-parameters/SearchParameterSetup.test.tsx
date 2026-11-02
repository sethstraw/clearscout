import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { afterEach, describe, expect, it } from "vitest";

import type { SearchParameterOptions, SearchParametersSaved } from "../../api/client/types";
import { MemoryRouter } from "react-router-dom";

import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { requestLog } from "../../test/requestLog";
import { settingsHandlers } from "../settings/settingsHandlers";
import { searchParametersState } from "./searchParameterFixtures";
import { SearchParameterSetup } from "./SearchParameterSetup";

const WORKSPACE_ID = "workspace-settings";

const STORED = searchParametersState({
  workspace_id: WORKSPACE_ID,
  id: "parameters-1",
  version: 1,
  configured: true,
  content_sha256: "stored-digest",
  effective_at: "2026-09-01T12:00:00Z",
  parameters: {
    opportunity_thesis: "",
    score_weight_set: "",
    working_day_shift_hours: 0,
    locations: [
      {
        key: "ca-mb",
        country: "Canada",
        region: "Manitoba",
        metro: "",
        eligibility: "authorized",
        basis: "",
        aliases: [],
        arrangement: null,
        resident: false,
      },
    ],
  },
});

describe("SearchParameterSetup conflict recovery", () => {
  it.each(["walkthrough", "advanced"] as const)(
    "requires fresh review and explicit save after each conflict in %s mode",
    async (mode) => {
      let latest = STORED;
      const saves = requestLog();
      walkthroughReads(latest);
      server.use(
        http.get("*/api/v1/job-search-parameters", () => HttpResponse.json(latest)),
        http.put("*/api/v1/job-search-parameters", async ({ request }) => {
          await saves.record(request);
          const body = (await request.json()) as {
            parameters: SearchParametersSaved["parameters"];
            expected_version: number;
          };
          if (saves.requests.length <= 2) {
            latest = searchParametersState({
              ...STORED,
              id: `parameters-${saves.requests.length + 1}`,
              version: saves.requests.length + 1,
              parameters: {
                ...STORED.parameters,
                opportunity_thesis: `Saved version ${saves.requests.length + 1} description`,
              },
            });
            return HttpResponse.json(
              { detail: "Search parameters changed in another tab." },
              { status: 409 },
            );
          }
          latest = searchParametersState({
            ...latest,
            id: "parameters-4",
            version: 4,
            parameters: body.parameters,
          });
          return HttpResponse.json({
            id: "parameters-4",
            workspace_id: WORKSPACE_ID,
            version: 4,
            content_sha256: "saved-digest",
            effective_at: "2026-10-08T12:00:00Z",
            supersedes_id: "parameters-3",
            unchanged: false,
            parameters: body.parameters,
          } satisfies SearchParametersSaved);
        }),
      );
      render(
        <MemoryRouter>
          <AppProviders>
            <SearchParameterSetup mode={mode} workspaceId={WORKSPACE_ID} />
          </AppProviders>
        </MemoryRouter>,
      );
      await screen.findByRole("textbox", { name: "Target titles" });
      addTitle();
      if (mode === "walkthrough") {
        unrestrictedNext();
        unrestrictedNext();
        unrestrictedNext();
        unrestrictedNext();
      }
      const saveLabel =
        mode === "walkthrough" ? "Confirm and save preferences" : "Save as new version";
      fireEvent.click(screen.getByRole("button", { name: saveLabel }));
      for (const version of [2, 3]) {
        fireEvent.click(
          await screen.findByRole("button", { name: "Read latest saved preferences" }),
        );
        await screen.findByRole("heading", { name: `Saved preferences: version ${version}` });
        const comparison = screen.getByRole("region", { name: "Review conflicting preferences" });
        const stored = within(comparison).getByRole("region", {
          name: `Saved preferences: version ${version}`,
        });
        const preserved = within(comparison).getByRole("region", { name: "Preserved draft" });
        expect(
          within(stored).getByText(new RegExp(`Saved version ${version} description`)),
        ).toBeTruthy();
        expect(
          within(preserved).getByText(/Target titles: Implementation Consultant/),
        ).toBeTruthy();
        expect(screen.getByRole("button", { name: saveLabel })).toHaveProperty("disabled", true);
        expect(saves.requests).toHaveLength(version - 1);
        fireEvent.click(
          screen.getByRole("button", { name: `Use preserved draft against version ${version}` }),
        );
        expect(saves.requests).toHaveLength(version - 1);
        expect(screen.getByRole("button", { name: saveLabel })).toHaveProperty("disabled", false);
        fireEvent.click(screen.getByRole("button", { name: saveLabel }));
      }
      await screen.findByText(
        mode === "walkthrough" ? "Search preferences saved." : "Search parameters saved.",
      );
      expect(saves.requests).toHaveLength(3);
      expect(
        saves.requests.map(
          (request) => (request.body as { expected_version: number }).expected_version,
        ),
      ).toEqual([1, 2, 3]);
      expect(saves.requests[2]?.body).toMatchObject({
        parameters: {
          opportunity_thesis: "",
          seniority: { target_titles: [{ title: "Implementation Consultant", lane: null }] },
        },
      });
    },
  );

  it("saves the scoring policy of the version it saves against, not the draft's", async () => {
    let latest = STORED;
    const saves = requestLog();
    walkthroughReads(latest);
    server.use(
      http.get("*/api/v1/job-search-parameters", () => HttpResponse.json(latest)),
      http.put("*/api/v1/job-search-parameters", async ({ request }) => {
        await saves.record(request);
        const body = (await request.json()) as {
          parameters: SearchParametersSaved["parameters"];
        };
        if (saves.requests.length === 1) {
          latest = searchParametersState({
            ...STORED,
            id: "parameters-2",
            version: 2,
            parameters: { ...STORED.parameters, score_weight_set: "Pay first" },
          });
          return HttpResponse.json(
            { detail: "Search parameters changed in another tab." },
            { status: 409 },
          );
        }
        return HttpResponse.json({
          id: "parameters-3",
          workspace_id: WORKSPACE_ID,
          version: 3,
          content_sha256: "saved-digest",
          effective_at: "2026-10-08T12:00:00Z",
          supersedes_id: "parameters-2",
          unchanged: false,
          parameters: body.parameters,
        } satisfies SearchParametersSaved);
      }),
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <SearchParameterSetup workspaceId={WORKSPACE_ID} />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByRole("textbox", { name: "Target titles" });
    addTitle();
    fireEvent.click(screen.getByRole("button", { name: "Save as new version" }));
    fireEvent.click(await screen.findByRole("button", { name: "Read latest saved preferences" }));
    await screen.findByRole("heading", { name: "Saved preferences: version 2" });
    const preserved = screen.getByRole("region", { name: "Preserved draft" });
    expect(within(preserved).getByText(/Scoring policy: Pay first/)).toBeTruthy();
    expect(
      within(preserved).getByRole("link", { name: "How jobs are scored" }).getAttribute("href"),
    ).toBe("/find/scoring");
    fireEvent.click(screen.getByRole("button", { name: "Use preserved draft against version 2" }));
    fireEvent.click(screen.getByRole("button", { name: "Save as new version" }));

    await screen.findByText("Search parameters saved.");
    expect(saves.requests[1]?.body).toMatchObject({
      expected_version: 2,
      parameters: {
        score_weight_set: "Pay first",
        seniority: { target_titles: [{ title: "Implementation Consultant", lane: null }] },
      },
    });
  });

  it("keeps the draft and form available when reading latest preferences fails, then allows a deliberate retry", async () => {
    let latest = STORED;
    let failRead = false;
    const saves = requestLog();
    walkthroughReads(latest);
    server.use(
      http.get("*/api/v1/job-search-parameters", () =>
        failRead
          ? HttpResponse.json(
              { detail: "Saved preferences are temporarily unavailable." },
              { status: 503 },
            )
          : HttpResponse.json(latest),
      ),
      http.put("*/api/v1/job-search-parameters", async ({ request }) => {
        await saves.record(request);
        latest = searchParametersState({ ...STORED, version: 2 });
        return HttpResponse.json(
          { detail: "Search parameters changed in another tab." },
          { status: 409 },
        );
      }),
    );
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 15_000 } },
    });
    render(
      <MemoryRouter>
        <AppProviders>
          <QueryClientProvider client={client}>
            <SearchParameterSetup workspaceId={WORKSPACE_ID} />
          </QueryClientProvider>
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByRole("textbox", { name: "Target titles" });
    addTitle();
    fireEvent.click(screen.getByRole("button", { name: "Save as new version" }));
    const read = await screen.findByRole("button", { name: "Read latest saved preferences" });
    failRead = true;
    fireEvent.click(read);
    const comparison = await screen.findByRole("region", {
      name: "Review conflicting preferences",
    });
    await within(comparison).findByText(/Saved preferences are temporarily unavailable/);
    expect(screen.getByText("Implementation Consultant")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Save as new version" })).toHaveProperty(
      "disabled",
      true,
    );
    expect(saves.requests).toHaveLength(1);
    failRead = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry reading latest preferences" }));
    await screen.findByRole("heading", { name: "Saved preferences: version 2" });
    fireEvent.click(screen.getByRole("button", { name: "Use preserved draft against version 2" }));
    expect(screen.getByText("Implementation Consultant")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Save as new version" })).toHaveProperty(
      "disabled",
      false,
    );
    expect(saves.requests).toHaveLength(1);
    client.clear();
  });

  it("asks before discarding the preserved draft in favor of the latest saved preferences", async () => {
    let latest = STORED;
    const saves = requestLog();
    walkthroughReads(latest);
    server.use(
      http.get("*/api/v1/job-search-parameters", () => HttpResponse.json(latest)),
      http.put("*/api/v1/job-search-parameters", async ({ request }) => {
        await saves.record(request);
        latest = searchParametersState({
          ...STORED,
          version: 2,
          parameters: {
            ...STORED.parameters,
            seniority: {
              minimum: "",
              maximum: "",
              title_matching: "guided",
              target_titles: [{ title: "Quality Analyst", lane: null }],
            },
          },
        });
        return HttpResponse.json(
          { detail: "Search parameters changed in another tab." },
          { status: 409 },
        );
      }),
    );
    render(
      <MemoryRouter>
        <AppProviders>
          <SearchParameterSetup workspaceId={WORKSPACE_ID} />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByRole("textbox", { name: "Target titles" });
    addTitle();
    fireEvent.click(screen.getByRole("button", { name: "Save as new version" }));
    fireEvent.click(await screen.findByRole("button", { name: "Read latest saved preferences" }));
    await screen.findByRole("heading", { name: "Saved preferences: version 2" });
    fireEvent.click(screen.getByRole("button", { name: "Use latest saved preferences" }));
    const cancelled = await screen.findByRole("dialog", { name: "Discard the preserved draft?" });
    fireEvent.click(within(cancelled).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByText("Implementation Consultant")).toBeTruthy();
    expect(saves.requests).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Use latest saved preferences" }));
    const confirmed = await screen.findByRole("dialog", { name: "Discard the preserved draft?" });
    fireEvent.click(
      within(confirmed).getByRole("button", { name: "Use latest saved preferences" }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByText("Quality Analyst")).toBeTruthy();
    expect(screen.queryByText("Implementation Consultant")).toBeNull();
    expect(screen.getByRole("button", { name: "Save as new version" })).toHaveProperty(
      "disabled",
      true,
    );
    expect(saves.requests).toHaveLength(1);
  });
});

afterEach(() => {
  window.sessionStorage.clear();
});

describe("SearchParameterSetup", () => {
  it("returns keyboard focus to the walkthrough heading when advancing and going back", async () => {
    walkthroughReads();
    renderWalkthrough();
    await screen.findByRole("heading", { name: "Role preferences" });
    addTitle();
    unrestrictedNext();
    const heading = screen.getByRole("heading", { name: "Basic search walkthrough" });
    expect(document.activeElement).toBe(heading);
    expect(heading.getAttribute("aria-describedby")).toBe("search-walkthrough-progress");
    expect(document.getElementById("search-walkthrough-progress")?.textContent).toContain(
      "Places and residence",
    );
    unrestrictedNext();
    expect(document.activeElement).toBe(heading);
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(document.activeElement).toBe(heading);
    expect(document.getElementById("search-walkthrough-progress")?.textContent).toContain(
      "Places and residence",
    );
  });
  it("shows target titles up front, outside every folded section", async () => {
    server.use(...settingsHandlers({ parameters: STORED }));

    render(
      <MemoryRouter>
        <AppProviders>
          <SearchParameterSetup workspaceId={WORKSPACE_ID} />
        </AppProviders>
      </MemoryRouter>,
    );

    const panel = await screen.findByRole("region", { name: "Target titles" });
    const inputs = screen.getAllByRole("textbox", { name: "Target titles" });
    expect(inputs).toHaveLength(1);
    expect(panel.contains(inputs[0]!)).toBe(true);
    expect(panel.closest("details")).toBeNull();
  });

  it("saves the place marked as home as the person's residence", async () => {
    const saves = requestLog();
    server.use(
      ...settingsHandlers({ parameters: STORED }),
      http.put("*/api/v1/job-search-parameters", async ({ request }) => {
        await saves.record(request);
        return HttpResponse.json({
          id: "parameters-2",
          workspace_id: WORKSPACE_ID,
          version: 2,
          content_sha256: "saved-digest",
          effective_at: "2026-09-02T12:00:00Z",
          supersedes_id: "parameters-1",
          unchanged: false,
          parameters: STORED.parameters,
        } satisfies SearchParametersSaved);
      }),
    );

    render(
      <MemoryRouter>
        <AppProviders>
          <SearchParameterSetup workspaceId={WORKSPACE_ID} />
        </AppProviders>
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole("checkbox", { name: /^I live here/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save as new version" }));

    await waitFor(() => expect(saves.requests).toHaveLength(1));
    expect(saves.requests[0]?.body).toMatchObject({
      workspace_id: WORKSPACE_ID,
      expected_version: 1,
      parameters: { locations: [{ key: "ca-mb", resident: true }] },
    });
  });

  it("holds the save while a number on the working step is out of range", async () => {
    server.use(...settingsHandlers({ parameters: STORED }));

    render(
      <MemoryRouter>
        <AppProviders>
          <SearchParameterSetup workspaceId={WORKSPACE_ID} />
        </AppProviders>
      </MemoryRouter>,
    );

    const travel = await screen.findByRole("spinbutton", { name: "Most travel you will take" });
    fireEvent.change(travel, { target: { value: "150" } });

    expect(screen.getByRole("button", { name: "Save as new version" })).toHaveProperty(
      "disabled",
      true,
    );
    fireEvent.change(travel, { target: { value: "15" } });
    expect(screen.getByRole("button", { name: "Save as new version" })).toHaveProperty(
      "disabled",
      false,
    );
  });
});

const OPTIONS = {
  arrangements: [],
  levels: ["junior", "senior"],
  lanes: [],
  bases: ["annual", "hourly"],
  eligibility: ["authorized", "sponsorship required", "ineligible"],
  employment_types: ["full_time", "contract"],
  acceptance: ["preferred", "acceptable", "refused"],
  caps: { titles: 8, archetypes: 6, capabilities: 5, domains: 5, cluster_terms: 10 },
} satisfies SearchParameterOptions;

function walkthroughReads(parameters = searchParametersState({ workspace_id: WORKSPACE_ID })) {
  server.use(
    http.get("*/api/v1/job-search-parameters/options", () => HttpResponse.json(OPTIONS)),
    ...settingsHandlers({ parameters }),
  );
}

function renderWalkthrough() {
  return render(
    <MemoryRouter>
      <AppProviders>
        <SearchParameterSetup mode="walkthrough" workspaceId={WORKSPACE_ID} />
      </AppProviders>
    </MemoryRouter>,
  );
}

function addTitle(title = "Implementation Consultant") {
  const input = screen.getByRole("textbox", { name: "Target titles" });
  fireEvent.change(input, { target: { value: title } });
  fireEvent.keyDown(input, { key: "Enter" });
}

function unrestrictedNext() {
  fireEvent.click(screen.getByRole("button", { name: "No restriction in this section" }));
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
}

describe("SearchParameterSetup basic walkthrough", () => {
  it("requires a manual target title independently of explicit unrestricted preferences", async () => {
    walkthroughReads();
    renderWalkthrough();
    await screen.findByRole("heading", { name: "Role preferences" });
    expect(screen.queryByRole("button", { name: "Save as new version" })).toBeNull();
    expect(screen.queryByText(/Advanced matching/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Add a capability" })).toBeNull();
    expect(screen.getByRole("button", { name: "Next" })).toHaveProperty("disabled", true);
    fireEvent.click(screen.getByRole("button", { name: "No restriction in this section" }));
    expect(screen.getByRole("button", { name: "Next" })).toHaveProperty("disabled", true);
    addTitle();
    expect(screen.getByRole("button", { name: "Next" })).toHaveProperty("disabled", true);
    unrestrictedNext();
    expect(screen.getByRole("heading", { name: "Places and residence" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Next" })).toHaveProperty("disabled", true);
  });

  it("preserves unfinished answers and decisions when skipped and returned to", async () => {
    walkthroughReads();
    const view = renderWalkthrough();
    await screen.findByRole("heading", { name: "Role preferences" });
    addTitle();
    unrestrictedNext();
    fireEvent.click(screen.getByRole("button", { name: "Add a place" }));
    fireEvent.change(screen.getByRole("combobox", { name: /^Country/ }), {
      target: { value: "Canada" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    expect(screen.getByText(/Walkthrough paused/)).toBeTruthy();
    view.unmount();
    renderWalkthrough();
    await screen.findByRole("button", { name: "Resume basic walkthrough" });
    fireEvent.click(screen.getByRole("button", { name: "Resume basic walkthrough" }));
    expect(screen.getByRole("combobox", { name: /^Country/ })).toHaveProperty(
      "value",
      String("Canada"),
    );
    expect(screen.getByRole("combobox", { name: /^Standing/ })).toHaveProperty("value", String(""));
    expect(screen.queryByRole("textbox", { name: /^Key/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByText("Implementation Consultant")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Next" })).toHaveProperty("disabled", false);
  });

  it("reviews and saves explicit unrestricted sections without inventing geography or authorization", async () => {
    walkthroughReads();
    const saves = requestLog();
    server.use(
      http.put("*/api/v1/job-search-parameters", async ({ request }) => {
        await saves.record(request);
        const body = (await request.json()) as { parameters: SearchParametersSaved["parameters"] };
        return HttpResponse.json({
          id: "parameters-1",
          workspace_id: WORKSPACE_ID,
          version: 1,
          content_sha256: "saved-digest",
          effective_at: "2026-10-08T12:00:00Z",
          supersedes_id: null,
          unchanged: false,
          parameters: body.parameters,
        } satisfies SearchParametersSaved);
      }),
    );
    renderWalkthrough();
    await screen.findByRole("heading", { name: "Role preferences" });
    addTitle();
    unrestrictedNext();
    unrestrictedNext();
    unrestrictedNext();
    unrestrictedNext();
    expect(screen.getByRole("heading", { name: "Review your search preferences" })).toBeTruthy();
    expect(
      screen.getByText(/No named places; no geography or authorization facts added/),
    ).toBeTruthy();
    expect(screen.getByText(/Target titles steer discovery/)).toBeTruthy();
    expect(saves.requests).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Confirm and save preferences" }));
    await screen.findByText("Search preferences saved.");
    expect(saves.requests).toHaveLength(1);
    const body = saves.requests[0]?.body as {
      parameters: Record<string, unknown>;
      expected_version: number;
    };
    expect(body.expected_version).toBe(0);
    expect(body.parameters).not.toHaveProperty("walkthrough");
    expect(body.parameters).toMatchObject({
      locations: [],
      compensation: [],
      working_time_zone: null,
      seniority: {
        minimum: "",
        maximum: "",
        target_titles: [{ title: "Implementation Consultant", lane: null }],
        title_matching: "guided",
      },
      arrangement: { remote: null, hybrid: null, on_site: null, relocation: null },
    });
  });

  it("requires deliberate authorization and reads residence, attendance, pay, and zero shift accurately", async () => {
    walkthroughReads();
    renderWalkthrough();
    await screen.findByRole("heading", { name: "Role preferences" });
    addTitle();
    unrestrictedNext();
    fireEvent.click(screen.getByRole("button", { name: "Add a place" }));
    fireEvent.change(screen.getByRole("combobox", { name: /^Country/ }), {
      target: { value: "Canada" },
    });
    expect(screen.getByRole("combobox", { name: /^Standing/ })).toHaveProperty("value", String(""));
    expect(screen.getByRole("button", { name: "Use these answers" })).toHaveProperty(
      "disabled",
      true,
    );
    fireEvent.change(screen.getByRole("combobox", { name: /^Standing/ }), {
      target: { value: "sponsorship required" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: /^I live here/ }));
    fireEvent.click(screen.getByRole("button", { name: "Use these answers" }));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.click(screen.getByRole("radio", { name: "Remote: preferred" }));
    fireEvent.change(screen.getByRole("combobox", { name: "Time zone you normally work from" }), {
      target: { value: "Mountain" },
    });
    expect(
      screen.getByRole("spinbutton", { name: "Hours you can shift your working day" }),
    ).toHaveProperty("value", String(0));
    fireEvent.change(screen.getByRole("spinbutton", { name: "Most travel you will take" }), {
      target: { value: "150" },
    });
    expect(screen.getByRole("button", { name: "Use these answers" })).toHaveProperty(
      "disabled",
      true,
    );
    fireEvent.change(screen.getByRole("spinbutton", { name: "Most travel you will take" }), {
      target: { value: "10" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: "Office days only if travel is paid" }), {
      target: { value: "yes" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: "Would you relocate" }), {
      target: { value: "no" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Use these answers" }));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.click(screen.getByRole("button", { name: "Add a band" }));
    const place = screen.getByRole("combobox", { name: "Place" });
    expect(within(place).getByRole("option", { name: "Canada" })).toBeTruthy();
    fireEvent.change(screen.getByRole("textbox", { name: "Currency" }), {
      target: { value: "CAD" },
    });
    fireEvent.change(screen.getByRole("spinbutton", { name: "Minimum" }), {
      target: { value: "75000" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Use these answers" }));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText(/Canada: sponsorship required; residence: yes/)).toBeTruthy();
    expect(screen.getByText(/Mountain; shift allowance: 0 hours/)).toBeTruthy();
    expect(screen.getByText(/Remote: preferred/)).toBeTruthy();
    expect(
      screen.getByText(/Travel: at most 10%; office travel must be paid: yes; relocate: no/),
    ).toBeTruthy();
    expect(screen.getByText(/Canada: CAD annual; minimum 75000/)).toBeTruthy();
  });

  it("shows a later unfinished draft as paused after saving and reopening in the same page", async () => {
    const parameters = searchParametersState({
      workspace_id: WORKSPACE_ID,
      id: "parameters-1",
      version: 1,
      configured: true,
      parameters: {
        opportunity_thesis: "",
        score_weight_set: "",
        working_day_shift_hours: 0,
        seniority: {
          minimum: "",
          maximum: "",
          title_matching: "guided",
          target_titles: [{ title: "Implementation Consultant", lane: null }],
        },
      },
    });
    walkthroughReads(parameters);
    const saves = requestLog();
    server.use(
      http.put("*/api/v1/job-search-parameters", async ({ request }) => {
        await saves.record(request);
        const body = (await request.json()) as { parameters: SearchParametersSaved["parameters"] };
        return HttpResponse.json({
          id: "parameters-1",
          workspace_id: WORKSPACE_ID,
          version: 1,
          content_sha256: "saved-digest",
          effective_at: "2026-10-08T12:00:00Z",
          supersedes_id: null,
          unchanged: true,
          parameters: body.parameters,
        } satisfies SearchParametersSaved);
      }),
    );
    renderWalkthrough();
    await screen.findByRole("heading", { name: "Role preferences" });
    unrestrictedNext();
    unrestrictedNext();
    unrestrictedNext();
    unrestrictedNext();
    fireEvent.click(screen.getByRole("button", { name: "Confirm and save preferences" }));
    await screen.findByText("Search preferences saved.");
    expect(saves.requests).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Open basic walkthrough" }));
    expect(screen.getByText("Implementation Consultant")).toBeTruthy();
    addTitle("Data Analyst");
    fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    expect(screen.queryByText("Search preferences saved.")).toBeNull();
    expect(screen.getByText(/Walkthrough paused/)).toBeTruthy();
    expect(saves.requests).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Resume basic walkthrough" }));
    expect(screen.getByText("Implementation Consultant")).toBeTruthy();
    expect(screen.getByText("Data Analyst")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Next" })).toHaveProperty("disabled", true);
  });

  it("retains an old-version draft on save conflict and keeps advanced settings visible on review", async () => {
    walkthroughReads(
      searchParametersState({
        ...STORED,
        parameters: {
          ...STORED.parameters,
          score_weight_set: "synthetic-policy",
          focus: {
            capabilities: [
              { name: "Clinical data", because: "Synthetic preference", terms: ["clinical"] },
            ],
          },
        },
      }),
    );
    server.use(
      http.put("*/api/v1/job-search-parameters", () =>
        HttpResponse.json({ detail: "Search parameters changed in another tab." }, { status: 409 }),
      ),
    );
    renderWalkthrough();
    await screen.findByRole("heading", { name: "Role preferences" });
    addTitle();
    unrestrictedNext();
    unrestrictedNext();
    unrestrictedNext();
    unrestrictedNext();
    expect(screen.getByText(/Scoring policy: synthetic-policy/)).toBeTruthy();
    expect(screen.getByText(/Capabilities: Clinical data/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Confirm and save preferences" }));
    await within(screen.getByRole("region", { name: "Basic search walkthrough" })).findByText(
      /Search parameters changed in another tab/,
    );
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByText("Implementation Consultant")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    fireEvent.click(screen.getByRole("button", { name: "Open advanced editor" }));
    expect(screen.getByRole("button", { name: "Add a capability" })).toBeTruthy();
    expect(screen.getByRole("textbox", { name: "Capability name" })).toHaveProperty(
      "value",
      "Clinical data",
    );
  });
});
