import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, delay, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  GuardrailVersion,
  Health,
  LearningLoopMetrics,
  LearningLoopPolicyState,
  PostingRefusal,
  PriceRefresh,
} from "../../api/client/types";
import { AppProviders } from "../../providers";
import { requestLog } from "../../test/requestLog";
import { server } from "../../test/msw/server";
import { TASK_TYPE_CATALOG } from "../../test/taskTypes";
import { SettingsPage } from "./Settings";
import {
  guardrailRules,
  guardrails,
  savedGuardrails,
  settingsHandlers,
  spendSummary,
  type SettingsReads,
} from "./settingsHandlers";

afterEach(() => {
  window.sessionStorage.clear();
});

const LEARNING_METRICS = {
  workspace_id: "workspace-settings",
  computed_at: "2026-09-19T09:00:00Z",
  top_n: 10,
  reproposed_rejected_identities: { numerator: 0, denominator: 0 },
  master_cv_proposal_acceptance: { numerator: 0, denominator: 0 },
  pending_queue_missed_matches: { numerator: 0, denominator: 0 },
} satisfies LearningLoopMetrics;

const LEARNING_POLICY = {
  workspace_id: "workspace-settings",
  version: 0,
  effective_at: null,
  content_sha256: "",
  policy: {
    enable_dispute_reextraction: false,
    enable_dispute_rematch: false,
    missed_requirement_penalty: -5,
    propose_after_count: 3,
    propose_window_days: 30,
    propose_after_ruling_count: 3,
    propose_missing_after_count: 3,
  },
} satisfies LearningLoopPolicyState;

function installSettings(reads: SettingsReads = {}) {
  server.use(...settingsHandlers(reads));
}

describe("SettingsPage", () => {
  it("shows health and workspace metadata without exposing credentials", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-settings");
    installSettings({
      spendHistory: [
        {
          task_type: "workflow.tailor",
          currency: "USD",
          unit: "run",
          measured_runs: 1,
          unpriced_runs: 0,
          replayed_runs: 0,
          per_unit_low: "0.125",
          per_unit_median: "0.125",
          per_unit_high: "0.125",
        },
      ],
      spend: spendSummary({
        total_usd: 0.125,
        call_count: 1,
        by_model: [{ name: "gpt-synthetic", calls: 1, amount_usd: 0.125, unpriced_calls: 0 }],
        recent: [
          {
            id: "model-call-1",
            stage: "match",
            model: "gpt-synthetic",
            provider: "openai",
            requested_model: "gpt-requested",
            served_model: "gpt-synthetic",
            amount_usd: 0.125,
            pricing_source: "configured",
            price_snapshot: null,
            cache_hit: true,
            input_tokens: 100,
            output_tokens: 50,
            cache_read_tokens: 75,
            cache_write_tokens: 25,
            reasoning_tokens: 30,
            duration_ms: 250,
            completion_reason: "stop",
            created_at: "2026-08-16T12:00:00Z",
          },
        ],
      }),
    });

    render(
      <AppProviders>
        <MemoryRouter>
          <SettingsPage />
        </MemoryRouter>
      </AppProviders>,
    );

    expect(await screen.findByRole("tab", { name: "Search", selected: true })).toBeTruthy();
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "Search",
      "Provider",
      "Drafting",
      "Costs",
      "Notes & tags",
      "Advanced",
    ]);
    expect(await screen.findByText("Search setup")).toBeTruthy();
    expect(screen.queryByRole("tab", { name: "Sources" })).toBeNull();
    expect(screen.getByText("Advanced search controls").closest("details")?.open).toBe(false);
    expect(screen.getByRole("heading", { name: "Optional guided search (paid)" })).toBeTruthy();
    expect(screen.getByText(/no job is ruled out by them/)).toBeTruthy();
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "Save as new version" }).disabled,
    ).toBe(true);

    const thesis = screen.getByRole("textbox", { name: /Opportunity thesis/ });
    fireEvent.change(thesis, {
      target: { value: "Own technical direction with bounded delivery scope." },
    });
    fireEvent.click(screen.getByRole("tab", { name: "Drafting" }));
    fireEvent.click(screen.getByRole("tab", { name: "Search" }));
    expect(
      (await screen.findByRole<HTMLTextAreaElement>("textbox", { name: /Opportunity thesis/ }))
        .value,
    ).toBe("Own technical direction with bounded delivery scope.");

    fireEvent.click(screen.getByRole("tab", { name: "Provider" }));
    expect(await screen.findByText("Credentials stay behind the API.")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open setup walkthrough" }).getAttribute("href")).toBe(
      "/welcome",
    );
    expect(document.body.textContent).not.toContain("sk-");

    fireEvent.click(screen.getByRole("tab", { name: "Costs" }));
    expect(await screen.findByText("What runs have cost")).toBeTruthy();
    expect(await screen.findByText("$0.1250")).toBeTruthy();
    expect(await screen.findByText("Spend by model")).toBeTruthy();
    expect(screen.getAllByRole("main")).toHaveLength(1);
  });

  it("keeps health, workspace details, and the data browser under Advanced > Diagnostics", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-settings");
    installSettings();
    server.use(
      http.get("*/api/v1/workspaces/:workspaceId/learning-loop-metrics", () =>
        HttpResponse.json(LEARNING_METRICS),
      ),
      http.get("*/api/v1/workspaces/:workspaceId/learning-loop-policy", () =>
        HttpResponse.json(LEARNING_POLICY),
      ),
      http.get("*/api/v1/openapi.json", () => delay("infinite")),
    );

    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/settings?tab=advanced&section=diagnostics"]}>
          <SettingsPage />
        </MemoryRouter>
      </AppProviders>,
    );

    expect(await screen.findByText("TailorCV 0.1.0")).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Diagnostics", selected: true })).toBeTruthy();
    expect(screen.getByText("Synthetic Search")).toBeTruthy();
    expect(screen.getByText("Not running")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Activity" }).getAttribute("href")).toBe("/tasks");
    // The bare path costs a redirect, and jsdom cannot tell an anchor from a
    // router link here, so the trailing slash is the part a test can hold.
    expect(screen.getByRole("link", { name: "Data browser" }).getAttribute("href")).toBe("/admin/");
    expect(screen.getByText("Reading the generated API contract")).toBeTruthy();
  });

  it("lists refused postings under Advanced > Posting refusals", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-settings");
    installSettings();
    server.use(
      http.get("*/api/v1/workspaces/:workspaceId/posting-refusals", () =>
        HttpResponse.json([
          {
            id: "refusal-1",
            job_posting_id: "job-1",
            pending_discovery_id: null,
            posting_url: "https://boards.example.test/jobs/1",
            employer: "Example",
            title: "Restricted role",
            condition: "Work authorization",
            reason: "The posting requires an authorization the workspace does not state.",
            refused_at: "2026-08-13T12:00:00Z",
            search_parameter_set_id: "parameters-7",
            search_parameter_version: 7,
          },
          {
            id: "refusal-2",
            job_posting_id: null,
            pending_discovery_id: null,
            posting_url: null,
            employer: "Example",
            title: "Unversioned role",
            condition: "Location",
            reason: "The posting is outside every saved place.",
            refused_at: "2026-08-12T12:00:00Z",
            search_parameter_set_id: null,
            search_parameter_version: null,
          },
        ] satisfies PostingRefusal[]),
      ),
    );

    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/settings?tab=advanced&section=refusals"]}>
          <SettingsPage />
        </MemoryRouter>
      </AppProviders>,
    );

    expect(await screen.findByText("Refusal history")).toBeTruthy();
    expect(
      screen.getByRole("link", { name: "Restricted role at Example" }).getAttribute("href"),
    ).toBe("https://boards.example.test/jobs/1");
    expect(screen.queryByRole("link", { name: "Unversioned role at Example" })).toBeNull();
    expect(screen.getByText("v7")).toBeTruthy();
    expect(screen.getByText("No saved version")).toBeTruthy();
  });

  it("lists saved guardrail versions", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-settings");
    const saved = {
      version: 2,
      content_sha256: "synthetic-digest",
      effective_at: "2026-08-13T12:00:00Z",
      rules: guardrailRules({ protected: ["OpenAI"] }),
    };
    installSettings({
      guardrails: guardrails({ ...saved, configured: true }),
      guardrailHistory: [
        {
          ...saved,
          id: "guardrail-version-2",
          created_by: "workspace",
          origin: "workspace",
          supersedes_id: "guardrail-version-1",
        },
      ],
      spend: spendSummary({ total_usd: 0.125, call_count: 1 }),
    });

    render(
      <AppProviders>
        <MemoryRouter>
          <SettingsPage />
        </MemoryRouter>
      </AppProviders>,
    );

    fireEvent.click(await screen.findByRole("tab", { name: "Drafting" }));
    expect(await screen.findByText("Guardrail history")).toBeTruthy();
    expect(screen.getByText("Version 2")).toBeTruthy();
    expect(screen.getByText(/Effective .* from workspace/)).toBeTruthy();
  });

  it("renders a safe problem detail for a critical health failure", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-settings");
    installSettings({
      onHealthRequest: () =>
        HttpResponse.json(
          { detail: "Database schema needs an explicit upgrade." },
          { status: 503, headers: { "Content-Type": "application/problem+json" } },
        ),
    });

    render(
      <AppProviders>
        <MemoryRouter>
          <SettingsPage />
        </MemoryRouter>
      </AppProviders>,
    );

    // The health query retries once after react-query's default 1000ms backoff
    // before the problem detail shows, so drive that wait with fake timers.
    vi.useFakeTimers();
    try {
      await vi.advanceTimersByTimeAsync(1_100);
    } finally {
      vi.useRealTimers();
    }
    expect(await screen.findByText("Database schema needs an explicit upgrade.")).toBeTruthy();
    expect(screen.getByRole("alert")).toBeTruthy();
  });

  it("edits workspace guardrails as one rule per line with optimistic versioning", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-settings");
    const saves = requestLog();
    installSettings({
      health: {
        status: "ok",
        version: "0.1.0",
        database_reachable: true,
        database_exists: true,
        schema_current: true,
        current_revision: "0001",
        head_revision: "0001",
        worker_available: true,
        free_mode: false,
      } satisfies Health,
      guardrails: guardrails({
        version: 3,
        content_sha256: "current-digest",
        effective_at: "2026-08-10T21:00:00Z",
        configured: true,
        rules: guardrailRules({
          protected: ["OpenAI"],
          required: ["Canada"],
          forbidden: ["world-class"],
          work_authorization: "Authorized to work in Canada",
          distinctions: ["Led delivery; did not own the platform."],
        }),
      }),
      onSaveGuardrails: saves.record,
      savedGuardrails: savedGuardrails({
        version: 4,
        rules: guardrailRules({
          protected: ["OpenAI"],
          required: ["Canada"],
          forbidden: ["world-class", "ninja"],
          work_authorization: "Authorized to work in Canada",
          distinctions: ["Led delivery; did not own the platform."],
          first_person: true,
        }),
      }),
    });

    render(
      <AppProviders>
        <MemoryRouter>
          <SettingsPage />
        </MemoryRouter>
      </AppProviders>,
    );

    fireEvent.click(await screen.findByRole("tab", { name: "Drafting" }));
    expect(await screen.findByText("Claim guardrails")).toBeTruthy();
    expect(screen.getByText(/cannot add a fact to your record/)).toBeTruthy();
    expect(screen.getByLabelText("Protected phrases")).toHaveProperty("value", "OpenAI");
    fireEvent.change(screen.getByLabelText("Forbidden phrases"), {
      target: { value: "world-class\n\nninja" },
    });
    fireEvent.click(screen.getByLabelText("Allow first person"));
    const discard = screen
      .getAllByRole<HTMLButtonElement>("button", { name: "Discard changes" })
      .find((button) => !button.disabled);
    if (!discard) throw new Error("Expected the changed guardrails to be discardable");
    expect(discard.classList).toContain("button--danger");
    fireEvent.click(discard);
    expect(screen.getByRole("dialog", { name: "Discard these guardrail changes?" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByLabelText("Forbidden phrases")).toHaveProperty(
      "value",
      "world-class\n\nninja",
    );
    fireEvent.click(screen.getByRole("button", { name: "Save guardrails" }));

    await waitFor(() =>
      expect(saves.requests.at(-1)?.body).toMatchObject({
        expected_version: 3,
        rules: { forbidden: ["world-class", "ninja"], first_person: true },
      }),
    );
    expect(await screen.findByText(/Version 4/)).toBeTruthy();
  });

  it("clears every phrase list when the textarea is emptied", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-settings");
    const saves = requestLog();
    installSettings({
      guardrails: guardrails({
        version: 1,
        content_sha256: "prior-digest",
        effective_at: "2026-08-21T11:00:00Z",
        configured: true,
        rules: guardrailRules({
          protected: ["OpenAI"],
          required: ["Canada"],
          forbidden: ["world-class synergy"],
          distinctions: ["Familiarity is not production."],
        }),
      }),
      onSaveGuardrails: saves.record,
      savedGuardrails: savedGuardrails({
        id: "guardrail-version-2",
        version: 2,
        content_sha256: "cleared-digest",
        effective_at: "2026-08-21T11:33:00Z",
        supersedes_id: "guardrail-version-1",
        rules: guardrailRules({ work_authorization: "US citizen" }),
      }),
    });

    render(
      <AppProviders>
        <MemoryRouter>
          <SettingsPage />
        </MemoryRouter>
      </AppProviders>,
    );

    fireEvent.click(await screen.findByRole("tab", { name: "Drafting" }));
    expect(await screen.findByText("Claim guardrails")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Forbidden phrases"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Protected phrases"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Required phrases"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Distinctions"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Work authorization line"), {
      target: { value: "US citizen" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save guardrails" }));

    await waitFor(() =>
      expect(saves.requests.at(-1)?.body).toMatchObject({
        expected_version: 1,
        rules: {
          forbidden: [],
          protected: [],
          required: [],
          distinctions: [],
          work_authorization: "US citizen",
        },
      }),
    );
    expect(await screen.findByText(/Version 2/)).toBeTruthy();
    expect(screen.getByLabelText("Forbidden phrases")).toHaveProperty("value", "");
  });

  it("shows a saved guardrail version in the history beside it", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-settings");
    const rules = guardrailRules({ forbidden: ["world-class"] });
    const saved = guardrails({
      version: 1,
      content_sha256: "saved-digest",
      effective_at: "2026-08-18T18:38:03Z",
      configured: true,
      rules,
    });
    // The history is empty until the save writes a version.
    let history: GuardrailVersion[] = [];
    installSettings({
      guardrails: guardrails({
        ...saved,
        version: 0,
        configured: false,
        rules: guardrailRules(),
      }),
      guardrailHistory: () => history,
      savedGuardrails: savedGuardrails({
        version: saved.version,
        content_sha256: saved.content_sha256,
        effective_at: "2026-08-18T18:38:03Z",
        rules,
      }),
      onSaveGuardrails: () => {
        history = [
          {
            id: "guardrail-version-1",
            version: saved.version,
            content_sha256: saved.content_sha256,
            effective_at: "2026-08-18T18:38:03Z",
            created_by: "local_user",
            origin: "api",
            supersedes_id: null,
            rules,
          },
        ];
        return Promise.resolve();
      },
    });

    render(
      <AppProviders>
        <MemoryRouter>
          <SettingsPage />
        </MemoryRouter>
      </AppProviders>,
    );

    fireEvent.click(await screen.findByRole("tab", { name: "Drafting" }));
    expect(await screen.findByText("Guardrail history")).toBeTruthy();
    expect(screen.getByText(/No guardrail version has been saved/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Forbidden phrases"), {
      target: { value: "world-class" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save guardrails" }));

    expect(await screen.findByText(/from api/)).toBeTruthy();
  });

  it("states the actual matching semantics for each phrase type", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-settings");
    installSettings();

    render(
      <AppProviders>
        <MemoryRouter>
          <SettingsPage />
        </MemoryRouter>
      </AppProviders>,
    );

    fireEvent.click(await screen.findByRole("tab", { name: "Drafting" }));
    expect(await screen.findByText("Claim guardrails")).toBeTruthy();
    expect(
      screen.getByText(
        "Case-insensitive match with whitespace collapsed. If the phrase (or an equivalent" +
          " spacing) appears, it must be written verbatim wherever it appears.",
      ),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Case-sensitive substring match. Each phrase must appear at least once in the drafted" +
          " resume; approval refuses regeneration when one is missing.",
      ),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Matched in any case, as whole words, with runs of whitespace collapsed. Any" +
          " occurrence in the drafted resume or cover letter fails the check.",
      ),
    ).toBeTruthy();
    expect(
      screen.getByText(
        /Matching uses these distinctions to decide whether evidence answers a requirement/,
      ),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Replaces any work-authorization line found in the master CV's contact block. Leave" +
          " blank to keep the CV's line.",
      ),
    ).toBeTruthy();
  });

  it("offers the price refresh on the Costs tab", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-settings");
    let posts = 0;
    installSettings();
    server.use(
      http.get("*/api/v1/tasks/types", () => HttpResponse.json(TASK_TYPE_CATALOG)),
      http.post("*/api/v1/billing/prices/refresh", () => {
        posts += 1;
        return HttpResponse.json({
          available: true,
          unavailable_reason: null,
          previous_snapshot: "2026-06-01",
          snapshot: "2026-09-01",
          source: "https://prices.example.test/data/prices.json",
          unchanged: [],
          dropped: ["demo-retired-model"],
          changed: [
            {
              model: "demo-draft-model",
              provider: "openai",
              previous_input_usd_per_million: 2.5,
              input_usd_per_million: 2.0,
              previous_output_usd_per_million: 10.0,
              output_usd_per_million: 10.0,
              previous_cache_read_usd_per_million: null,
              cache_read_usd_per_million: null,
              previous_cache_write_usd_per_million: null,
              cache_write_usd_per_million: null,
              previous_search_usd_per_thousand_calls: null,
              search_usd_per_thousand_calls: null,
              previous_long_context_threshold: null,
              long_context_threshold: null,
              previous_long_context_input_multiplier: 1,
              long_context_input_multiplier: 1,
              previous_long_context_output_multiplier: 1,
              long_context_output_multiplier: 1,
            },
          ],
        } satisfies PriceRefresh);
      }),
    );

    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/settings?tab=costs"]}>
          <SettingsPage />
        </MemoryRouter>
      </AppProviders>,
    );

    const refresh = await screen.findByRole("button", {
      name: "Refresh prices from the catalog",
    });
    expect(posts).toBe(0);
    fireEvent.click(refresh);
    fireEvent.click(await screen.findByRole("button", { name: "Refresh prices" }));

    expect(
      await screen.findByText("Price data 2026-09-01 is now in force. 1 shipped price moved."),
    ).toBeTruthy();
    expect(screen.getByRole("table", { name: "Prices that moved" }).textContent).toContain(
      "was $2.50, now $2.00",
    );
    expect(screen.getByText(/prices one model no longer: demo-retired-model/)).toBeTruthy();
    expect(posts).toBe(1);
  });
});
