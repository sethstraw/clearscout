import { render, screen } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import type { ApplicationOption, Artifact } from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { documentArtifact } from "../tailoring/tailoringFixtures";
import { ApplicationFilesPage } from "./ApplicationFiles";

afterEach(() => {
  window.localStorage.clear();
});

const applicationOptions = [
  { id: "application-1", job_posting_id: "job-1", title: "Synthetic Role" },
] satisfies ApplicationOption[];

const importedResume = documentArtifact({
  id: "artifact-1",
  workspace_id: "workspace-files",
  logical_type: "user.attachment",
  mime_type: "application/pdf",
  byte_size: 1200,
  sha256: "a".repeat(64),
  created_at: "2026-08-06T12:00:00Z",
  last_verified_at: "2026-08-06T12:00:00Z",
  created_by: "import",
  provenance: { source: "external_import" },
  related_entity_type: "application",
  related_entity_id: "application-1",
  artifact_family_id: null,
  version_number: null,
  author_type: "human",
  inline_viewable: true,
  editable: false,
}) satisfies Artifact;

const submittedResume = {
  ...importedResume,
  id: "artifact-2",
  provenance: { source: "user_upload", submission_role: "resume", filename: "sent.pdf" },
} satisfies Artifact;

describe("ApplicationFilesPage", () => {
  it("shows verified immutable metadata and disables changes to imported files", async () => {
    window.localStorage.setItem("tailorcv.workspace", "workspace-files");
    server.use(
      http.get("*/api/v1/applications/options", () => HttpResponse.json(applicationOptions)),
      http.get("*/api/v1/artifacts", () => HttpResponse.json([importedResume, submittedResume])),
      http.get("*/api/v1/tags", () => HttpResponse.json([])),
      http.get("*/api/v1/tasks", () => HttpResponse.json([])),
    );

    render(
      <AppProviders>
        <MemoryRouter initialEntries={["/applications/application-1"]}>
          <Routes>
            <Route path="/applications/:applicationId" element={<ApplicationFilesPage />} />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );

    expect(await screen.findByText("Synthetic Role")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Back to applications" }).getAttribute("href")).toBe(
      "/applications",
    );
    expect(screen.getByRole("columnheader", { name: /Verification/ })).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: /Origin/ })).toBeTruthy();
    expect(screen.getAllByText("Verified")).toHaveLength(2);
    expect(screen.getByText("External Import, Human")).toBeTruthy();
    expect(screen.getByText("Submitted files")).toBeTruthy();
    expect(screen.getByText("Related files and drafts")).toBeTruthy();
    expect(screen.getByRole("cell", { name: "Submitted resume" })).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Verify" })).toHaveLength(2);
    expect(screen.getByRole("combobox", { name: "Tag this application" })).toBeTruthy();
  });
});
