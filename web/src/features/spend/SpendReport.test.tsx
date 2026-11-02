import { render, screen, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import type { SpendSummary } from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { SpendReport } from "./SpendReport";

const call = {
  id: "call-1",
  stage: "jobspec",
  provider: "openai",
  requested_model: "gpt-terra",
  served_model: "gpt-terra",
  model: "gpt-terra",
  input_tokens: 10,
  output_tokens: 20,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
  reasoning_tokens: 0,
  duration_ms: 100,
  completion_reason: "stop",
  pricing_source: "configured",
  price_snapshot: "2026-09-01",
  cache_hit: false,
  amount_usd: 0.2,
  created_at: "2026-08-19T21:00:00Z",
} satisfies SpendSummary["recent"][number];

const summary = {
  total_usd: 0.5,
  call_count: 2,
  unpriced_calls: 0,
  currency: "USD",
  by_stage: [{ name: "jobspec", calls: 1, unpriced_calls: 0, amount_usd: 0.2 }],
  by_model: [{ name: "gpt-terra", calls: 2, unpriced_calls: 0, amount_usd: 0.5 }],
  recent: [call],
} satisfies SpendSummary;

function showSpend(spend: SpendSummary) {
  server.use(http.get("*/api/v1/workspaces/:workspaceId/spend", () => HttpResponse.json(spend)));
  render(
    <AppProviders>
      <SpendReport workspaceId="workspace-spend" />
    </AppProviders>,
  );
}

describe("SpendReport", () => {
  it("shows the total and the per-stage and per-model tables", async () => {
    showSpend(summary);

    expect(await screen.findByText("Spend by model")).toBeTruthy();
    expect(screen.getAllByText("$0.5000").length).toBe(2);
    expect(
      within(screen.getByRole("table", { name: "Recorded model spend by stage" })).getByText(
        "$0.2000",
      ),
    ).toBeTruthy();
    expect(screen.getByRole("table", { name: "Recent model calls" }).textContent).toContain(
      "gpt-terra",
    );
  });

  it("says how many calls blocked the total instead of repeating the count in its own row", async () => {
    showSpend({
      ...summary,
      total_usd: null,
      call_count: 12,
      unpriced_calls: 3,
      by_stage: [{ name: "jobspec", calls: 4, unpriced_calls: 3, amount_usd: null }],
      recent: [{ ...call, amount_usd: null, pricing_source: null }],
    });

    expect(await screen.findByText("Unknown, 3 of 12 calls have no configured price")).toBeTruthy();
    expect(
      screen.getByText(
        "Add prices for those models in Settings to see a total. A missing price is not a zero price.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText("Without a configured price")).toBeNull();
    expect(screen.getAllByText("no price").length).toBe(2);
  });

  it("agrees the verb with a single unpriced call", async () => {
    showSpend({ ...summary, total_usd: null, call_count: 12, unpriced_calls: 1 });

    expect(await screen.findByText("Unknown, 1 of 12 calls has no configured price")).toBeTruthy();
  });

  it("says no call is priced when every call is unpriced", async () => {
    showSpend({ ...summary, total_usd: null, call_count: 2, unpriced_calls: 2 });

    expect(await screen.findByText("Unknown, no call here has a configured price")).toBeTruthy();
  });

  it("names the price data a recorded cost came from, beside its price source", async () => {
    showSpend({
      ...summary,
      recent: [call, { ...call, id: "call-2", price_snapshot: null }],
    });

    const table = await screen.findByRole("table", { name: "Recent model calls" });
    expect(within(table).getByRole("columnheader", { name: "Price data" })).toBeTruthy();
    const rows = within(table).getAllByRole("row");
    expect(within(rows[1]!).getByText("2026-09-01")).toBeTruthy();
    expect(within(rows[2]!).getAllByText("Not recorded").length).toBe(1);
  });

  it("marks a zero served from the cache as cached", async () => {
    showSpend({ ...summary, recent: [{ ...call, cache_hit: true, amount_usd: 0 }] });

    const table = await screen.findByRole("table", { name: "Recent model calls" });
    expect(within(table).getByRole("columnheader", { name: "Cached" })).toBeTruthy();
    const row = within(table).getAllByRole("row")[1]!;
    expect(within(row).getByText("$0.0000")).toBeTruthy();
    expect(within(row).getByText("Yes")).toBeTruthy();
  });
});
