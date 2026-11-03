import { fireEvent, render, screen } from "@testing-library/react";
import { HttpResponse, http } from "msw/http";
import { describe, expect, it } from "vitest";

import type { PriceMove, PriceRefresh } from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { PriceRefreshControl } from "./PriceRefreshControl";

const SOURCE = "https://prices.example.test/data/prices.json";

function held(overrides: Partial<PriceMove> = {}): PriceMove {
  return {
    model: "demo-draft-model",
    provider: "openai",
    previous_input_usd_per_million: 2.5,
    input_usd_per_million: 2.5,
    previous_output_usd_per_million: 10.0,
    output_usd_per_million: 10.0,
    previous_cache_read_usd_per_million: 0.25,
    cache_read_usd_per_million: 0.25,
    previous_cache_write_usd_per_million: 3.0,
    cache_write_usd_per_million: 3.0,
    previous_search_usd_per_thousand_calls: 10.0,
    search_usd_per_thousand_calls: 10.0,
    previous_long_context_threshold: 200000,
    long_context_threshold: 200000,
    previous_long_context_input_multiplier: 2,
    long_context_input_multiplier: 2,
    previous_long_context_output_multiplier: 1.5,
    long_context_output_multiplier: 1.5,
    ...overrides,
  };
}

function refreshed(overrides: Partial<PriceRefresh> = {}): PriceRefresh {
  return {
    available: true,
    unavailable_reason: null,
    previous_snapshot: "2026-06-01",
    snapshot: "2026-09-01",
    source: SOURCE,
    changed: [],
    unchanged: [],
    dropped: [],
    ...overrides,
  };
}

function installRefresh(answer: () => HttpResponse<PriceRefresh | { detail: string }>) {
  server.use(http.post("*/api/v1/billing/prices/refresh", () => answer()));
}

function renderControl() {
  render(
    <AppProviders>
      <PriceRefreshControl />
    </AppProviders>,
  );
}

async function confirmPriceRefresh() {
  fireEvent.click(await screen.findByRole("button", { name: "Refresh prices from the catalog" }));
  fireEvent.click(await screen.findByRole("button", { name: "Refresh prices" }));
}

function movedRows() {
  return screen
    .getAllByRole("row")
    .map((row) =>
      Array.from(row.querySelectorAll("th,td"))
        .map((cell) => cell.textContent)
        .join(" | "),
    )
    .slice(1);
}

describe("PriceRefreshControl", () => {
  it("reads no published prices until the refresh is asked for and confirmed", async () => {
    let posts = 0;
    installRefresh(() => {
      posts += 1;
      return HttpResponse.json(refreshed({ unchanged: ["demo-draft-model"] }));
    });
    renderControl();

    expect(
      await screen.findByRole("button", { name: "Refresh prices from the catalog" }),
    ).toBeTruthy();
    expect(posts).toBe(0);
    await confirmPriceRefresh();

    expect(
      await screen.findByText(
        "Price data 2026-09-01 is now in force. No shipped price moved. 1 stayed the same.",
      ),
    ).toBeTruthy();
    expect(screen.getByText(`Read from ${SOURCE}, replacing 2026-06-01.`)).toBeTruthy();
    expect(screen.queryByRole("table", { name: "Prices that moved" })).toBeNull();
    expect(posts).toBe(1);
  });

  it("says a provider answers with a dated snapshot of the model asked for", async () => {
    installRefresh(() => HttpResponse.json(refreshed()));
    renderControl();

    expect(
      await screen.findByText(
        /A provider answers a request for a model with a dated snapshot of it, and that call is priced at the model's own rate\./,
      ),
    ).toBeTruthy();
  });

  it("shows a move in the token rates with the snapshot now in force", async () => {
    installRefresh(() =>
      HttpResponse.json(
        refreshed({
          changed: [held({ input_usd_per_million: 2.0, output_usd_per_million: 8.0 })],
        }),
      ),
    );
    renderControl();

    await confirmPriceRefresh();

    expect(
      await screen.findByText("Price data 2026-09-01 is now in force. 1 shipped price moved."),
    ).toBeTruthy();
    expect(movedRows()).toEqual([
      "demo-draft-model | openai | Input per million tokens | was $2.50, now $2.00",
      "demo-draft-model | openai | Output per million tokens | was $10.00, now $8.00",
    ]);
  });

  it("shows a move in a cache, search, or long-context rate as well", async () => {
    installRefresh(() =>
      HttpResponse.json(
        refreshed({
          changed: [
            held({
              cache_read_usd_per_million: 0.5,
              cache_write_usd_per_million: null,
              search_usd_per_thousand_calls: 12.5,
              long_context_threshold: null,
              long_context_input_multiplier: 3,
              long_context_output_multiplier: 1.75,
            }),
          ],
        }),
      ),
    );
    renderControl();

    await confirmPriceRefresh();
    await screen.findByRole("table", { name: "Prices that moved" });

    expect(movedRows()).toEqual([
      "demo-draft-model | openai | Cache read per million tokens | was $0.25, now $0.50",
      "demo-draft-model | openai | Cache write per million tokens | was $3.00, now not published",
      "demo-draft-model | openai | Hosted search per thousand calls | was $10.00, now $12.50",
      "demo-draft-model | openai | Long-context threshold | was 200000 tokens, now no long-context tier",
      "demo-draft-model | openai | Long-context input multiplier | was 2x, now 3x",
      "demo-draft-model | openai | Long-context output multiplier | was 1.5x, now 1.75x",
    ]);
  });

  it("names a model the refreshed data stopped pricing and says it keeps its rate", async () => {
    installRefresh(() =>
      HttpResponse.json(refreshed({ dropped: ["demo-retired-model", "demo-legacy-model"] })),
    );
    renderControl();

    await confirmPriceRefresh();

    expect(
      await screen.findByText(
        "The refreshed data prices 2 models no longer: demo-retired-model, demo-legacy-model. " +
          "Each keeps the rate it already had.",
      ),
    ).toBeTruthy();
  });

  it("says the prices in force are unchanged when the published prices cannot be read", async () => {
    installRefresh(() =>
      HttpResponse.json({ detail: "The price data host did not answer." }, { status: 503 }),
    );
    renderControl();

    await confirmPriceRefresh();

    expect(
      await screen.findByText(
        "The published prices could not be read. The prices already in force are unchanged; none was lost.",
      ),
    ).toBeTruthy();
    expect(screen.getByText("The price data host did not answer.")).toBeTruthy();
  });

  it("reports the server's own reason when the data covers no billable model", async () => {
    installRefresh(() =>
      HttpResponse.json(
        refreshed({
          available: false,
          unavailable_reason: "The published data carries no rate for any billable model.",
          snapshot: "2026-06-01",
        }),
      ),
    );
    renderControl();

    await confirmPriceRefresh();

    expect(
      await screen.findByText("The published data carries no rate for any billable model."),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "The prices already in force are unchanged; none was lost. Price data 2026-06-01 stays in force.",
      ),
    ).toBeTruthy();
  });
});
