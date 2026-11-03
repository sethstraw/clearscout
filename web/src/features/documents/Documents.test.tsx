import { render, screen } from "@testing-library/react";
import { HttpResponse, http } from "msw/http";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { Artifact } from "../../api/client/types";
import { AppProviders } from "../../providers";
import { jobList, jobSummary } from "../job-ledger/jobFixtures";
import { server } from "../../test/msw/server";
import { documentArtifact } from "../tailoring/tailoringFixtures";
import { DocumentsPage } from "./Documents";

const JOB = jobSummary({ title: "Staff Data Platform Lead" });

const RESUME = documentArtifact({ id: "artifact-1", byte_size: 2048, provenance: {} });

function renderPage(artifacts: Artifact[]) {
  window.localStorage.setItem("clearscout.workspace", "workspace-1");
  server.use(
    http.get("*/api/v1/artifacts", () => HttpResponse.json(artifacts)),
    http.get("*/api/v1/jobs", ({ request }) => {
      // The server filters by application_status, so the handler must too.
      const asked = new URL(request.url).searchParams.getAll("application_status");
      const items = asked.length && !asked.includes(JOB.application_status) ? [] : [JOB];
      return HttpResponse.json(jobList(items));
    }),
  );
  return render(
    <MemoryRouter>
      <AppProviders>
        <DocumentsPage />
      </AppProviders>
    </MemoryRouter>,
  );
}

describe("DocumentsPage", () => {
  it("lists a job holding a tailored document even when the role is not being pursued", async () => {
    renderPage([RESUME]);

    expect(await screen.findByText("Staff Data Platform Lead")).toBeTruthy();
    expect(screen.getByText("Not applied")).toBeTruthy();
  });

  it("leaves out a job whose only stored files are evidence rather than documents", async () => {
    renderPage([documentArtifact({ id: "artifact-2", logical_type: "posting.body.text" })]);

    expect(await screen.findByText(/No tailored documents yet/)).toBeTruthy();
    expect(screen.queryByText("Staff Data Platform Lead")).toBeNull();
  });

  it("keeps imported files with no job reachable in a closed fold", async () => {
    renderPage([
      RESUME,
      documentArtifact({
        id: "artifact-orphan",
        related_entity_type: null,
        related_entity_id: null,
        provenance: { stored_path: "earlier-tool/cover-letter.md" },
      }),
    ]);

    const summary = await screen.findByText(/Imported files not tied to a job \(1\)/);
    const fold = summary.closest("details");
    expect(fold?.open).toBe(false);
    expect(fold?.contains(screen.getByRole("table", { name: /^Imported files from / }))).toBe(true);
  });
});
