import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw/http";
import { describe, expect, it } from "vitest";

import type { OpenAiActualSpend } from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { OpenAiActualSpendPanel } from "./OpenAiActualSpendPanel";

const RECONCILED = {
  available: true,
  unavailable_reason: null,
  start_time: 1730419200,
  end_time: 1731024000,
  currency: "USD",
  openai_actual_usd: 6.5,
  recorded_usd: 5.0,
  recorded_call_count: 2,
  unpriced_call_count: 0,
  drift_usd: 1.5,
  scope_note: "This is an organization-level cross-check.",
} satisfies OpenAiActualSpend;

/** The paths the panel posted to, so the confirmed refresh is visible. */
function installReconciliation(spend: OpenAiActualSpend): string[] {
  const posted: string[] = [];
  server.use(
    http.post("*/api/v1/billing/openai/reconciliation", ({ request }) => {
      posted.push(new URL(request.url).pathname);
      return HttpResponse.json(spend);
    }),
  );
  return posted;
}

function renderPanel() {
  render(
    <AppProviders>
      <OpenAiActualSpendPanel />
    </AppProviders>,
  );
}

function refresh() {
  fireEvent.click(screen.getByRole("button", { name: "Refresh actual spend from OpenAI" }));
  fireEvent.click(screen.getByRole("button", { name: "Refresh actual spend" }));
}

describe("OpenAiActualSpendPanel", () => {
  it("compares organization and local totals in cents after a confirmed refresh", async () => {
    const posted = installReconciliation(RECONCILED);
    renderPanel();

    refresh();

    expect(await screen.findByText("$6.50")).toBeTruthy();
    expect(screen.getByText("$1.50")).toBeTruthy();
    expect(screen.getByText("Organization total minus ClearScout recorded cost")).toBeTruthy();
    expect(screen.getByText(/does not isolate ClearScout accounting errors/)).toBeTruthy();
    await waitFor(() => expect(posted).toEqual(["/api/v1/billing/openai/reconciliation"]));
  });

  it("reports the unavailable reason when no admin key is configured", async () => {
    installReconciliation({
      ...RECONCILED,
      available: false,
      unavailable_reason: "No OpenAI organization admin key is configured.",
      openai_actual_usd: null,
      recorded_usd: null,
      drift_usd: null,
    });
    renderPanel();

    refresh();

    expect(await screen.findByText(/No OpenAI organization admin key is configured/)).toBeTruthy();
  });

  it("explains why an unpriced call withholds both the total and comparison", async () => {
    installReconciliation({
      ...RECONCILED,
      recorded_usd: null,
      drift_usd: null,
      unpriced_call_count: 1,
      scope_note: "Organization-wide comparison.",
    });
    renderPanel();

    refresh();

    expect(await screen.findAllByText("Withheld - a call had no configured price")).toHaveLength(2);
    expect(screen.queryByText("-")).toBeNull();
  });
});
