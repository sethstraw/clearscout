import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw/http";
import { describe, expect, it } from "vitest";

import type { ScreenedOutTitle } from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { taskTypesHandler } from "../../test/taskTypes";
import { ScreenedOutTitles } from "./ScreenedOutTitles";
import { screenedOutTitle, screenedOutTitlePage } from "./sourceScanFixtures";

const ROUTE = "*/api/v1/workspaces/:workspaceId/screened-out-titles";

function useTitles(items: ScreenedOutTitle[]) {
  const queries: string[] = [];
  const reviewed: string[][] = [];
  let current = items;
  server.use(
    taskTypesHandler(),
    http.get(ROUTE, ({ request }) => {
      const search = new URL(request.url).search;
      queries.push(search);
      const needsReview = new URLSearchParams(search).get("needs_review");
      const shown =
        needsReview === "true"
          ? current.filter((item) => item.review_state !== "reviewed")
          : current;
      return HttpResponse.json(screenedOutTitlePage(shown, { all_count: current.length }));
    }),
    http.post(`${ROUTE}/review`, async ({ request }) => {
      const { ids } = (await request.json()) as { ids: string[] };
      reviewed.push(ids);
      current = current.map((item) =>
        ids.includes(item.id) ? { ...item, review_state: "reviewed" } : item,
      );
      return HttpResponse.json({ reviewed: ids.length });
    }),
  );
  return { queries, reviewed };
}

function renderTitles() {
  render(
    <AppProviders>
      <ScreenedOutTitles workspaceId="workspace-1" />
    </AppProviders>,
  );
}

const SALES = screenedOutTitle();
const NURSE = screenedOutTitle({
  id: "title-2",
  url: "https://boards.example.test/acme/jobs/2",
  title: "Charge Nurse",
  title_code: "title_excluded",
  title_subject: "nurse",
  review_state: "changed",
  reviewed_at: "2025-02-01T09:00:00Z",
});
const DONE = screenedOutTitle({
  id: "title-3",
  url: "https://boards.example.test/acme/jobs/3",
  title: "Account Executive",
  review_state: "reviewed",
  reviewed_at: "2025-02-01T09:00:00Z",
});

describe("ScreenedOutTitles", () => {
  it("lists new and changed titles first with their title finding and the review count", async () => {
    const { queries } = useTitles([SALES, NURSE, DONE]);
    renderTitles();

    const table = await screen.findByRole("table", { name: "Screened-out titles" });
    expect(screen.getByText("2 new or changed of 3 screened-out titles.")).toBeTruthy();
    const nurse = within(table).getByRole("row", { name: /Charge Nurse/ });
    expect(within(nurse).getByText("title excluded: nurse")).toBeTruthy();
    expect(within(nurse).getByText("Finding changed")).toBeTruthy();
    expect(within(table).queryByText("Account Executive")).toBeNull();
    expect(queries).toEqual(["?offset=0&limit=100&needs_review=true"]);
  });

  it("marks one title reviewed and drops it from the new-or-changed view", async () => {
    const { reviewed } = useTitles([SALES, NURSE]);
    renderTitles();

    fireEvent.click(
      await screen.findByRole("button", { name: "Mark Sales Director at Acme reviewed" }),
    );

    await waitFor(() => expect(screen.queryByText("Sales Director")).toBeNull());
    expect(reviewed).toEqual([["title-1"]]);
    expect(screen.getByText("1 new or changed of 2 screened-out titles.")).toBeTruthy();
  });

  it("marks every selected title reviewed at once", async () => {
    const { reviewed } = useTitles([SALES, NURSE]);
    renderTitles();

    fireEvent.click(
      await screen.findByRole("checkbox", { name: "Select every unreviewed title on this page" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Mark 2 selected reviewed" }));

    expect(
      await screen.findByText(
        "Every screened-out title has been reviewed against its current title finding.",
      ),
    ).toBeTruthy();
    expect(reviewed).toEqual([["title-1", "title-2"]]);
  });

  it("shows reviewed titles too when the review filter is set to all", async () => {
    const { queries } = useTitles([SALES, DONE]);
    renderTitles();

    fireEvent.change(await screen.findByRole("combobox", { name: "Review state" }), {
      target: { value: "all" },
    });

    const done = await screen.findByRole("row", { name: /Account Executive/ });
    expect(within(done).getByText("Reviewed")).toBeTruthy();
    expect(within(done).queryByRole("button", { name: /reviewed/ })).toBeNull();
    expect(queries.at(-1)).toBe("?offset=0&limit=100");
  });
});
