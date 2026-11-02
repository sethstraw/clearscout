import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { afterEach, describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";

import type {
  ScoreWeightSet,
  SearchParametersSaved,
  SearchParametersState,
} from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { requestLog } from "../../test/requestLog";
import { searchParametersState } from "../search-parameters/searchParameterFixtures";
import { ScoringPage } from "./Scoring";

const WEIGHTS = [
  { component: "compensation", weight: 0.25 },
  { component: "eligibility", weight: 0.25 },
  { component: "evidence", weight: 0 },
  { component: "fit", weight: 0.4 },
  { component: "opportunity", weight: 0.1 },
] satisfies ScoreWeightSet["weights"];

const POLICIES = [
  {
    id: "policy-1",
    name: "TailorCV default",
    version: 1,
    effective_at: "2026-08-07T00:00:00Z",
    is_shipped_default: true,
    is_active: true,
    weights: WEIGHTS,
  },
  {
    id: "policy-2",
    name: "Pay first",
    version: 1,
    effective_at: "2026-09-07T00:00:00Z",
    is_shipped_default: false,
    is_active: false,
    weights: WEIGHTS,
  },
] satisfies ScoreWeightSet[];

function stored(version: number, thesis: string, policy = ""): SearchParametersState {
  return searchParametersState({
    workspace_id: "policy-test",
    id: `parameters-${version}`,
    version,
    configured: true,
    content_sha256: `digest-${version}`,
    effective_at: "2026-10-01T12:00:00Z",
    parameters: {
      opportunity_thesis: thesis,
      score_weight_set: policy,
      working_day_shift_hours: 2,
      locations: [],
    },
  });
}

/** The policy list as the server marks it when the search names `named`. */
function policiesFor(named: string): ScoreWeightSet[] {
  const active = named || "TailorCV default";
  return POLICIES.map((item) => ({ ...item, is_active: item.name === active }));
}

function installApi(
  read: () => SearchParametersState,
  put: Parameters<typeof http.put>[1],
  policies: () => ScoreWeightSet[] = () => policiesFor(read().parameters.score_weight_set ?? ""),
) {
  window.localStorage.setItem("tailorcv.workspace", "policy-test");
  server.use(
    http.get("*/api/v1/workspaces/:workspaceId/score-weight-sets", () =>
      HttpResponse.json(policies()),
    ),
    http.get("*/api/v1/jobs", () =>
      HttpResponse.json({ items: [], total: 0, offset: 0, limit: 250 }),
    ),
    http.get("*/api/v1/job-search-parameters", () => HttpResponse.json(read())),
    http.put("*/api/v1/job-search-parameters", put),
  );
}

// The app client retries once after a second, which would race findBy's own timeout.
function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter>
      <AppProviders>
        <QueryClientProvider client={client}>
          <ScoringPage />
        </QueryClientProvider>
      </AppProviders>
    </MemoryRouter>,
  );
}

afterEach(() => window.localStorage.clear());

/**
 * Stored version 3 names no policy; choosing Pay first 409s, then the parameters and
 * policy reads after the conflict come from `latest` and `policies`.
 */
async function keepAfterConflict(
  latest: () => Response | Promise<Response>,
  policies: () => Response = () => HttpResponse.json(policiesFor("Pay first")),
) {
  let conflicted = false;
  window.localStorage.setItem("tailorcv.workspace", "policy-test");
  server.use(
    http.get("*/api/v1/workspaces/:workspaceId/score-weight-sets", () =>
      conflicted ? policies() : HttpResponse.json(policiesFor("")),
    ),
    http.get("*/api/v1/jobs", () =>
      HttpResponse.json({ items: [], total: 0, offset: 0, limit: 250 }),
    ),
    http.get("*/api/v1/job-search-parameters", () =>
      conflicted ? latest() : HttpResponse.json(stored(3, "Synthetic stored thesis")),
    ),
    http.put("*/api/v1/job-search-parameters", () => {
      conflicted = true;
      return HttpResponse.json(
        { detail: "Search parameters changed in another tab." },
        { status: 409 },
      );
    }),
  );
  renderPage();
  const select = await screen.findByRole("combobox", { name: /Scoring policy for this search/ });
  await screen.findByRole("option", { name: "Pay first" });
  fireEvent.change(select, { target: { value: "Pay first" } });
  fireEvent.click(screen.getByRole("button", { name: "Use this policy" }));
  fireEvent.click(await screen.findByRole("button", { name: "Keep the saved policy" }));
  return select;
}

const unavailable = () =>
  HttpResponse.json({ detail: "Saved parameters are unavailable." }, { status: 503 });

describe("keeping the saved policy when the latest read does not succeed", () => {
  it("says the latest version is being read and states no policy while the read is held", async () => {
    let release = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    const select = await keepAfterConflict(async () => {
      await held;
      return HttpResponse.json(stored(5, "Synthetic thesis from another tab", "Pay first"));
    });

    expect(await screen.findByText("Reading the latest saved parameters.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Keep the saved policy" })).toHaveProperty(
      "disabled",
      true,
    );
    expect(screen.queryByText(/In force now: the default policy/)).toBeNull();
    expect(screen.getByText(/changed elsewhere/)).toBeTruthy();

    release();
    await waitFor(() => expect(screen.getAllByText("In force now: Pay first v1.")).toHaveLength(2));
    expect(select).toHaveProperty("value", "Pay first");
    expect(screen.queryByText(/changed elsewhere/)).toBeNull();
  });

  it("keeps the alert and the choice when the parameters read fails", async () => {
    const select = await keepAfterConflict(unavailable);

    expect(
      await screen.findByText(
        /Could not read the latest parameters: Saved parameters are unavailable/,
      ),
    ).toBeTruthy();
    expect(screen.getByText(/changed elsewhere/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry reading latest parameters" })).toBeTruthy();
    expect(select).toHaveProperty("value", "Pay first");
    expect(screen.queryByText(/In force now: the default policy/)).toBeNull();
  });

  it("keeps the alert and the choice when both reads fail", async () => {
    const select = await keepAfterConflict(unavailable, unavailable);

    expect(
      await screen.findByText(
        /Could not read the latest parameters: Saved parameters are unavailable/,
      ),
    ).toBeTruthy();
    expect(screen.getByText(/changed elsewhere/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry reading latest parameters" })).toBeTruthy();
    expect(select).toHaveProperty("value", "Pay first");
    expect(screen.queryByText(/In force now: the default policy/)).toBeNull();
  });
});

describe("scoring policy in force for the search", () => {
  it("saves only the chosen policy against the stored version", async () => {
    const saves = requestLog();
    const current = stored(3, "Synthetic stored thesis");
    installApi(
      () => current,
      async ({ request }) => {
        await saves.record(request);
        const body = (await request.json()) as SearchParametersSaved;
        return HttpResponse.json({
          id: "parameters-4",
          workspace_id: "policy-test",
          version: 4,
          content_sha256: "digest-4",
          effective_at: "2026-10-02T12:00:00Z",
          supersedes_id: "parameters-3",
          unchanged: false,
          parameters: body.parameters,
        } satisfies SearchParametersSaved);
      },
    );
    renderPage();

    const select = await screen.findByRole("combobox", { name: /Scoring policy for this search/ });
    await screen.findByRole("option", { name: "Pay first" });
    fireEvent.change(select, { target: { value: "Pay first" } });
    fireEvent.click(screen.getByRole("button", { name: "Use this policy" }));

    expect(await screen.findByText(/Saved as search parameters version 4/)).toBeTruthy();
    expect(saves.requests).toHaveLength(1);
    expect(saves.requests[0]?.body).toEqual({
      workspace_id: "policy-test",
      expected_version: 3,
      parameters: { ...current.parameters, score_weight_set: "Pay first" },
    });
  });

  it("shows a conflict without resaving, then retries only after reading the latest version", async () => {
    const saves = requestLog();
    let current = stored(3, "Synthetic stored thesis");
    installApi(
      () => current,
      async ({ request }) => {
        await saves.record(request);
        if (saves.requests.length === 1) {
          current = stored(5, "Synthetic thesis from another tab");
          return HttpResponse.json(
            { detail: "Search parameters changed in another tab." },
            { status: 409 },
          );
        }
        const body = (await request.json()) as SearchParametersSaved;
        return HttpResponse.json({
          id: "parameters-6",
          workspace_id: "policy-test",
          version: 6,
          content_sha256: "digest-6",
          effective_at: "2026-10-02T12:00:00Z",
          supersedes_id: "parameters-5",
          unchanged: false,
          parameters: body.parameters,
        } satisfies SearchParametersSaved);
      },
    );
    renderPage();

    const select = await screen.findByRole("combobox", { name: /Scoring policy for this search/ });
    await screen.findByRole("option", { name: "Pay first" });
    fireEvent.change(select, { target: { value: "Pay first" } });
    fireEvent.click(screen.getByRole("button", { name: "Use this policy" }));

    fireEvent.click(await screen.findByRole("button", { name: "Read latest saved parameters" }));
    const retry = await screen.findByRole("button", { name: "Use Pay first against version 5" });
    expect(screen.getByRole("button", { name: "Use this policy" })).toHaveProperty(
      "disabled",
      true,
    );
    expect(screen.queryByText("Read the latest saved parameters first")).toBeNull();
    expect(screen.getByText("Choose below whether to apply it to version 5")).toBeTruthy();
    expect(saves.requests).toHaveLength(1);

    fireEvent.click(retry);
    expect(await screen.findByText(/Saved as search parameters version 6/)).toBeTruthy();
    expect(saves.requests).toHaveLength(2);
    expect(saves.requests[1]?.body).toMatchObject({
      expected_version: 5,
      parameters: {
        opportunity_thesis: "Synthetic thesis from another tab",
        score_weight_set: "Pay first",
      },
    });
  });

  it("names the default policy and the policy it resolves to in both panels", async () => {
    installApi(
      () => stored(3, "Synthetic stored thesis"),
      () => HttpResponse.json({}),
    );
    renderPage();

    const lines = await screen.findAllByText(
      "In force now: the default policy, which resolves to TailorCV default v1 (shipped default).",
    );
    expect(lines).toHaveLength(1);
    expect(screen.getByText("In force now: TailorCV default v1 (shipped default).")).toBeTruthy();
    expect(screen.getByRole("option", { name: /^Default policy/ })).toBeTruthy();
  });

  it("keeping the saved policy after a conflict states what the latest version uses", async () => {
    let current = stored(3, "Synthetic stored thesis");
    installApi(
      () => current,
      () => {
        current = stored(5, "Synthetic thesis from another tab", "Pay first");
        return HttpResponse.json(
          { detail: "Search parameters changed in another tab." },
          { status: 409 },
        );
      },
    );
    renderPage();

    const select = await screen.findByRole("combobox", { name: /Scoring policy for this search/ });
    await screen.findByRole("option", { name: "Pay first" });
    fireEvent.change(select, { target: { value: "Pay first" } });
    fireEvent.click(screen.getByRole("button", { name: "Use this policy" }));
    fireEvent.click(await screen.findByRole("button", { name: "Keep the saved policy" }));

    expect(await screen.findByText("In force now: Pay first v1.")).toBeTruthy();
    expect(select).toHaveProperty("value", "Pay first");
    expect(screen.queryByText(/changed elsewhere/)).toBeNull();
    const stored_ = screen.getByRole("heading", { name: "Stored policies" }).nextElementSibling;
    const badge = within(stored_ as HTMLElement).getByText("In force");
    expect(badge.closest("li")?.textContent).toContain("Pay first v1");
  });

  it("says the latest version already uses the chosen policy and offers only dismissal", async () => {
    const saves = requestLog();
    let current = stored(3, "Synthetic stored thesis");
    installApi(
      () => current,
      async ({ request }) => {
        await saves.record(request);
        current = stored(5, "Synthetic thesis from another tab", "Pay first");
        return HttpResponse.json(
          { detail: "Search parameters changed in another tab." },
          { status: 409 },
        );
      },
    );
    renderPage();

    const select = await screen.findByRole("combobox", { name: /Scoring policy for this search/ });
    await screen.findByRole("option", { name: "Pay first" });
    fireEvent.change(select, { target: { value: "Pay first" } });
    fireEvent.click(screen.getByRole("button", { name: "Use this policy" }));
    fireEvent.click(await screen.findByRole("button", { name: "Read latest saved parameters" }));

    const alert = screen.getByText(/changed elsewhere/).closest<HTMLElement>("[role=alert]")!;
    expect(await within(alert).findByText("Saved version 5 already uses Pay first.")).toBeTruthy();
    expect(within(alert).queryByRole("button", { name: /against version/ })).toBeNull();
    expect(
      within(alert)
        .getAllByRole("button")
        .map((button) => button.textContent),
    ).toEqual(["Dismiss"]);
    fireEvent.click(within(alert).getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText(/changed elsewhere/)).toBeNull();
    expect(await screen.findAllByText("In force now: Pay first v1.")).toHaveLength(2);
    expect(saves.requests).toHaveLength(1);
  });

  it("shows a failed policy list read in the policy panel", async () => {
    installApi(
      () => stored(3, "Synthetic stored thesis"),
      () => HttpResponse.json({}),
    );
    server.use(
      http.get("*/api/v1/workspaces/:workspaceId/score-weight-sets", () =>
        HttpResponse.json({ detail: "Policies are unavailable." }, { status: 503 }),
      ),
    );
    renderPage();

    const panel = (await screen.findByRole("heading", { name: "Policy for your search" })).closest(
      "article",
    ) as HTMLElement;
    expect(
      await within(panel).findByText(
        /Could not read the scoring policies: Policies are unavailable/,
      ),
    ).toBeTruthy();
  });

  it("says a new policy is not in force while the search names another", async () => {
    installApi(
      () => stored(3, "Synthetic stored thesis", "Pay first"),
      () => HttpResponse.json({}),
    );
    server.use(
      http.post("*/api/v1/workspaces/:workspaceId/score-weight-sets", () =>
        HttpResponse.json({
          ...POLICIES[1]!,
          id: "policy-3",
          name: "Fit first",
          is_active: true,
        } satisfies ScoreWeightSet),
      ),
    );
    renderPage();

    fireEvent.change(await screen.findByRole("textbox", { name: "Policy name" }), {
      target: { value: "Fit first" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save as a new policy" }));

    expect(
      await screen.findByText(
        "Saved Fit first v1. It is not in force: this search names Pay first. Choose it under Policy for your search to use it.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/It applies to new evaluations only/)).toBeNull();
  });
});
