import { fireEvent, render, screen } from "@testing-library/react";
import { HttpResponse, http } from "msw/http";
import { describe, expect, it, vi } from "vitest";

import type { RunCostHistory } from "../../api/client";
import type { PriceRefresh } from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { RunCostHistoryPanel } from "./RunCostHistoryPanel";

const WORKSPACE_ID = "workspace-1";

const history = [
  {
    task_type: "discovery.evaluate",
    currency: "USD",
    unit: "run",
    measured_runs: 4,
    unpriced_runs: 0,
    replayed_runs: 0,
    per_unit_low: "0.20",
    per_unit_median: "0.30",
    per_unit_high: "0.40",
  },
] satisfies RunCostHistory[];

const moved = {
  available: true,
  unavailable_reason: null,
  previous_snapshot: "2026-06-01",
  snapshot: "2026-09-01",
  source: "https://prices.example.test/data/prices.json",
  unchanged: [],
  dropped: [],
  changed: [
    {
      model: "demo-draft-model",
      provider: "openai",
      previous_input_usd_per_million: 2.5,
      input_usd_per_million: 2.0,
      previous_output_usd_per_million: 10.0,
      output_usd_per_million: 10.0,
      previous_cache_read_usd_per_million: 0.25,
      cache_read_usd_per_million: 0.25,
      previous_cache_write_usd_per_million: 3.0,
      cache_write_usd_per_million: 3.0,
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
} satisfies PriceRefresh;

function renderPanel(historyStatus = 200) {
  server.use(
    http.get("*/api/v1/workspaces/:workspaceId/spend-history", () =>
      historyStatus === 200
        ? HttpResponse.json(history)
        : HttpResponse.json({ detail: "Recorded costs are unavailable." }, { status: 503 }),
    ),
    http.post("*/api/v1/billing/prices/refresh", () => HttpResponse.json(moved)),
  );
  render(
    <AppProviders>
      <RunCostHistoryPanel workspaceId={WORKSPACE_ID} />
    </AppProviders>,
  );
}

describe("RunCostHistoryPanel", () => {
  it("offers the price refresh beside what runs have cost, and reports what moved", async () => {
    renderPanel();

    expect(await screen.findByText("What runs have cost")).toBeTruthy();
    expect(await screen.findByText("$0.3000")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Refresh prices from the catalog" }));
    fireEvent.click(await screen.findByRole("button", { name: "Refresh prices" }));

    expect(
      await screen.findByText("Price data 2026-09-01 is now in force. 1 shipped price moved."),
    ).toBeTruthy();
    expect(screen.getByRole("table", { name: "Prices that moved" }).textContent).toContain(
      "was $2.50, now $2.00",
    );
  });

  it("says a replayed run was left out rather than showing only the counted ones", async () => {
    server.use(
      http.get("*/api/v1/workspaces/:workspaceId/spend-history", () =>
        HttpResponse.json([{ ...history[0], replayed_runs: 2 }]),
      ),
      http.post("*/api/v1/billing/prices/refresh", () => HttpResponse.json(moved)),
    );
    render(
      <AppProviders>
        <RunCostHistoryPanel workspaceId={WORKSPACE_ID} />
      </AppProviders>,
    );

    expect(await screen.findByText("(2 replayed, so not counted)")).toBeTruthy();
  });

  it("keeps the price refresh reachable when recorded costs cannot be read", async () => {
    renderPanel(503);

    // The history query retries once after react-query's 1000ms backoff before the
    // problem detail shows, so drive that wait instead of waiting on it.
    vi.useFakeTimers();
    try {
      await vi.advanceTimersByTimeAsync(1_100);
    } finally {
      vi.useRealTimers();
    }
    expect(await screen.findByText("Recorded costs are unavailable.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Refresh prices from the catalog" })).toBeTruthy();
  });
});
