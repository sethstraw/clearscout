import { render, screen } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { SpendSummary } from "../api/client/types";
import { AppProviders } from "../providers";
import { server } from "../test/msw/server";
import { SpendPage } from "./SpendPage";

const summary = {
  total_usd: 0.5,
  call_count: 2,
  unpriced_calls: 0,
  currency: "USD",
  by_stage: [{ name: "jobspec", calls: 1, unpriced_calls: 0, amount_usd: 0.2 }],
  by_model: [{ name: "gpt-terra", calls: 2, unpriced_calls: 0, amount_usd: 0.5 }],
  recent: [],
} satisfies SpendSummary;

describe("SpendPage", () => {
  it("titles the route and reports the selected workspace's spend", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-spend");
    const requested: string[] = [];
    server.use(
      http.get("*/api/v1/tasks", () => HttpResponse.json([])),
      http.get("*/api/v1/workspaces/:workspaceId/spend", ({ params }) => {
        requested.push(String(params.workspaceId));
        return HttpResponse.json(summary);
      }),
    );
    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/spend"]}>
          <SpendPage />
        </MemoryRouter>
      </AppProviders>,
    );

    expect(await screen.findByRole("heading", { name: "Model spend", level: 1 })).toBeTruthy();
    expect(await screen.findByText("Spend by model")).toBeTruthy();
    expect(requested).toEqual(["workspace-spend"]);
  });
});
