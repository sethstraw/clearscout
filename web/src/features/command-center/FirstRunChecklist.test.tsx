import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { useMasterCvVersions, useSearchParameters, useSources } from "../../api/hooks";
import { boardSource } from "../job-search/sourceScanFixtures";
import { server } from "../../test/msw/server";
import { AppProviders } from "../../providers";
import { FirstRunChecklist } from "./FirstRunChecklist";
import { firstRunHandlers } from "./firstRunFixtures";

const WORKSPACE_ID = "workspace-first-run";

/** Shows once the same cached reads the checklist uses have all arrived. */
function ReadsSettled() {
  const settled = [
    useMasterCvVersions(WORKSPACE_ID),
    useSearchParameters(WORKSPACE_ID),
    useSources(WORKSPACE_ID),
  ].every((query) => query.isSuccess);
  return settled ? <p>Setup reads settled</p> : null;
}

function renderChecklist() {
  render(
    <AppProviders>
      <MemoryRouter>
        <FirstRunChecklist workspaceId={WORKSPACE_ID} />
        <ReadsSettled />
      </MemoryRouter>
    </AppProviders>,
  );
}

describe("FirstRunChecklist", () => {
  it("links each unfinished step to where it is done", async () => {
    server.use(...firstRunHandlers());

    renderChecklist();

    const checklist = await screen.findByRole("region", { name: "Get started" });
    expect(
      within(checklist).getByText(
        "4 of 4 steps left. A Markdown CV is free. Other formats use your provider.",
      ),
    ).toBeTruthy();
    const links = within(checklist)
      .getAllByRole("link")
      .map((link) => [link.textContent, link.getAttribute("href")]);
    expect(links).toEqual([
      ["Add your CV", "/cv"],
      ["Add target titles", "/settings"],
      ["Add a job board", "/find?tab=boards"],
      ["Run your first scan", "/find?tab=boards"],
    ]);
  });

  it("marks finished steps done and keeps the rest as links", async () => {
    server.use(
      ...firstRunHandlers({
        cv: true,
        titles: ["Data Platform Lead"],
        boards: [boardSource({ enabled: true, scan_count: 0 })],
      }),
    );

    renderChecklist();

    const checklist = await screen.findByRole("region", { name: "Get started" });
    expect(
      within(checklist).getByText(
        "1 of 4 steps left. A Markdown CV is free. Other formats use your provider.",
      ),
    ).toBeTruthy();
    expect(within(checklist).queryByRole("link", { name: "Add your CV" })).toBeNull();
    expect(within(checklist).getAllByText("(done)")).toHaveLength(3);
    expect(
      within(checklist).getByRole("link", { name: "Run your first scan" }).getAttribute("href"),
    ).toBe("/find?tab=boards");
  });

  it("hides once every step is done", async () => {
    server.use(
      ...firstRunHandlers({
        cv: true,
        titles: ["Data Platform Lead"],
        boards: [boardSource({ enabled: true, scan_count: 2 })],
      }),
    );

    renderChecklist();

    await screen.findByText("Setup reads settled");
    expect(screen.queryByRole("region", { name: "Get started" })).toBeNull();
  });
});
