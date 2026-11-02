import { render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { SourceScanHistory } from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { taskTypesHandler } from "../../test/taskTypes";
import { ScreenedOutSummary } from "./ScreenedOutSummary";
import {
  screenedOutTitle,
  screenedOutTitlePage,
  sourceScan,
  sourceScanHistory,
} from "./sourceScanFixtures";

function useScanHistory(
  items: SourceScanHistory["items"],
  total = items.length,
  titles = screenedOutTitlePage([]),
) {
  const queries: string[] = [];
  server.use(
    taskTypesHandler(),
    http.get("*/api/v1/workspaces/:workspaceId/screened-out-titles", () =>
      HttpResponse.json(titles),
    ),
    http.get("*/api/v1/workspaces/:workspaceId/source-scans", ({ request }) => {
      queries.push(new URL(request.url).search);
      return HttpResponse.json({ ...sourceScanHistory(items), total, limit: 250 });
    }),
  );
  return queries;
}

function scanRecord(id: string, sourceName: string, screenedOutCount: number) {
  return sourceScan({
    id,
    task_id: `task-${id}`,
    source_name: sourceName,
    screened_out_count: screenedOutCount,
  });
}

describe("ScreenedOutSummary", () => {
  it("tabulates each source's latest screened-out listings, largest first, omitting zero counts", async () => {
    const queries = useScanHistory([
      scanRecord("acme-latest", "Acme Robotics", 3),
      scanRecord("globex-latest", "Globex", 0),
      scanRecord("initech-latest", "Initech", 12),
    ]);
    render(
      <AppProviders>
        <ScreenedOutSummary workspaceId="workspace-1" />
      </AppProviders>,
    );

    expect(
      await screen.findByText("Screened out in latest scans (15 listings from 2 sources)"),
    ).toBeTruthy();
    const table = screen.getByRole("table", { name: "Screened-out listings by source" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getByRole("rowheader").textContent)).toEqual([
      "Initech",
      "Acme Robotics",
    ]);
    const acme = within(table).getByRole("row", { name: /Acme Robotics/ });
    expect(within(acme).getByRole("cell", { name: "3" })).toBeTruthy();
    expect(
      within(acme).getByRole("link", { name: "Review screened candidates" }).getAttribute("href"),
    ).toBe("?tab=boards&scan=task-acme-latest&source=Acme%20Robotics#screened-candidates");
    expect(queries).toEqual(["?offset=0&limit=250&latest_only=true"]);
  });

  it("says when the screened-out table covers only the first page of scanned sources", async () => {
    useScanHistory([scanRecord("acme-latest", "Acme Robotics", 3)], 260);
    render(
      <AppProviders>
        <ScreenedOutSummary workspaceId="workspace-1" />
      </AppProviders>,
    );

    expect(
      await screen.findByText(
        "Screened out in latest scans (3 listings from 1 source); covers the first 1 of 260 scanned sources",
      ),
    ).toBeTruthy();
  });

  it("names how many screened-out titles across all scans are new or changed", async () => {
    useScanHistory(
      [scanRecord("acme-latest", "Acme Robotics", 3)],
      1,
      screenedOutTitlePage([screenedOutTitle()], { all_count: 9, needs_review_count: 4 }),
    );
    render(
      <AppProviders>
        <ScreenedOutSummary workspaceId="workspace-1" />
      </AppProviders>,
    );

    expect(
      await screen.findByText(
        "Screened out in latest scans (3 listings from 1 source); 4 new or changed titles to review",
      ),
    ).toBeTruthy();
  });

  it("omits the screened-out line and table when the latest scans screened nothing out", async () => {
    const queries = useScanHistory([scanRecord("acme-latest", "Acme Robotics", 0)]);
    render(
      <AppProviders>
        <ScreenedOutSummary workspaceId="workspace-1" />
      </AppProviders>,
    );

    await waitFor(() => expect(queries).toHaveLength(1));
    // Flush the resolved fetch through the query so this checks the settled
    // state, not a still-loading one that would also render nothing.
    vi.useFakeTimers();
    try {
      await vi.advanceTimersByTimeAsync(0);
    } finally {
      vi.useRealTimers();
    }
    expect(screen.queryByRole("table", { name: "Screened-out listings by source" })).toBeNull();
    expect(screen.queryByText(/Screened out in latest scans/)).toBeNull();
  });
});
