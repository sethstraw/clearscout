import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, delay, http } from "msw";
import { afterEach, describe, expect, it } from "vitest";

import type {
  LearningLoopMetrics,
  LearningLoopPolicy,
  LearningLoopPolicyState,
  RunCostHistory,
  TaskTypeInfo,
} from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { LearningLoopMetricsPanel } from "./LearningLoopMetricsPanel";

const SNAPSHOT = {
  workspace_id: "workspace-metrics",
  computed_at: "2026-08-19T09:00:00Z",
  top_n: 10,
  reproposed_rejected_identities: { numerator: 1, denominator: 4 },
  master_cv_proposal_acceptance: { numerator: 3, denominator: 3 },
  pending_queue_missed_matches: { numerator: 0, denominator: 0 },
} satisfies LearningLoopMetrics;

const POLICY = {
  enable_dispute_reextraction: false,
  enable_dispute_rematch: false,
  missed_requirement_penalty: -5,
  propose_after_count: 3,
  propose_window_days: 30,
  propose_after_ruling_count: 3,
  propose_missing_after_count: 3,
} satisfies LearningLoopPolicy;

const DISPUTE_TASK_TYPES = [
  {
    task_type: "posting.refresh_with_reextraction",
    paid: true,
    cost: "One extraction-tier call per invocation.",
  },
  {
    task_type: "posting.refresh_with_rematch",
    paid: true,
    cost: "One match-stage call per invocation.",
  },
] satisfies TaskTypeInfo[];

const DISPUTE_SPEND = [
  {
    task_type: "posting.refresh_with_reextraction",
    currency: "USD",
    unit: "run",
    measured_runs: 2,
    unpriced_runs: 0,
    replayed_runs: 0,
    per_unit_low: "0.0100",
    per_unit_median: "0.0100",
    per_unit_high: "0.0200",
  },
  {
    task_type: "posting.refresh_with_rematch",
    currency: "USD",
    unit: "run",
    measured_runs: 4,
    unpriced_runs: 0,
    replayed_runs: 0,
    per_unit_low: "0.0300",
    per_unit_median: "0.0300",
    per_unit_high: "0.0500",
  },
] satisfies RunCostHistory[];

function policyState(version: number, policy: LearningLoopPolicy): LearningLoopPolicyState {
  return {
    workspace_id: "workspace-metrics",
    version,
    content_sha256: "",
    effective_at: null,
    policy,
  };
}

function installPanel() {
  server.use(
    http.get("*/api/v1/workspaces/:workspaceId/learning-loop-metrics", () =>
      HttpResponse.json(SNAPSHOT),
    ),
    http.get("*/api/v1/tasks/types", () => HttpResponse.json(DISPUTE_TASK_TYPES)),
    http.get("*/api/v1/workspaces/:workspaceId/spend-history", () =>
      HttpResponse.json(DISPUTE_SPEND),
    ),
    http.get("*/api/v1/workspaces/:workspaceId/learning-loop-policy", () =>
      HttpResponse.json(policyState(0, POLICY)),
    ),
    http.put("*/api/v1/workspaces/:workspaceId/learning-loop-policy", async ({ request }) => {
      const body = (await request.json()) as { policy: LearningLoopPolicy };
      return HttpResponse.json(policyState(1, body.policy));
    }),
  );
}

function renderPanel() {
  render(
    <AppProviders>
      <LearningLoopMetricsPanel workspaceId="workspace-metrics" />
    </AppProviders>,
  );
}

afterEach(() => {
  window.localStorage.clear();
});

describe("LearningLoopMetricsPanel", () => {
  it("shows a loading state while the snapshot is in flight", () => {
    server.use(
      http.get("*/api/v1/*", async () => {
        await delay("infinite");
      }),
    );
    renderPanel();
    expect(screen.getByText("Reading learning-loop signals")).toBeTruthy();
  });

  it("renders every signal's counts and percentage when data arrives", async () => {
    installPanel();
    renderPanel();

    await waitFor(() => expect(screen.getByText("Learning-loop signals")).toBeTruthy());
    expect(screen.getByText("1 of 4 (25%)")).toBeTruthy();
    expect(screen.getByText("3 of 3 (100%)")).toBeTruthy();
    expect(screen.getByText(/Three rule-based counts/)).toBeTruthy();
    expect(screen.getByText("Top 10 pending matching missed requirements")).toBeTruthy();
  });

  it("says 'no data yet' when a signal has a zero denominator", async () => {
    installPanel();
    renderPanel();

    await waitFor(() => expect(screen.getByText("no data yet")).toBeTruthy());
  });

  it("names the measured charge beside each paid dispute toggle", async () => {
    installPanel();
    renderPanel();

    const reextractionNote = await screen.findByTestId("dispute-reextraction-cost");
    expect(screen.getByText(/Enabling either option does not itself spend/)).toBeTruthy();
    expect(reextractionNote.textContent).toContain("One extraction-tier call per invocation.");
    expect(reextractionNote.textContent).toContain(
      "About $0.01 for 1 run (range $0.01 to $0.02, from 2 runs).",
    );

    const rematchNote = screen.getByTestId("dispute-rematch-cost");
    expect(rematchNote.textContent).toContain("One match-stage call per invocation.");
    expect(rematchNote.textContent).toContain(
      "About $0.03 for 1 run (range $0.03 to $0.05, from 4 runs).",
    );
  });

  it("saves the two paid dispute paths independently", async () => {
    installPanel();
    renderPanel();

    const reextraction = await screen.findByRole<HTMLInputElement>("checkbox", {
      name: "Enable dispute re-extraction",
    });
    const rematch = screen.getByRole<HTMLInputElement>("checkbox", {
      name: "Enable fit re-match",
    });
    expect(reextraction.checked).toBe(false);
    expect(rematch.checked).toBe(false);

    fireEvent.click(rematch);
    const save = screen.getByRole<HTMLButtonElement>("button", { name: "Save dispute settings" });
    fireEvent.click(save);

    await waitFor(() => expect(save.disabled).toBe(true));
    expect(reextraction.checked).toBe(false);
    expect(rematch.checked).toBe(true);
    expect(screen.queryByText("Unsaved change")).toBeNull();
  });
});
