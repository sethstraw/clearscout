import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { Artifact } from "../../api/client";
import { AppProviders } from "../../providers";
import { documentArtifact } from "./tailoringFixtures";
import { JobDocumentViewer } from "./JobDocumentViewer";

function imageArtifact(mime_type: string, inline_viewable: boolean): Artifact {
  return documentArtifact({
    logical_type: "posting.source.image",
    mime_type,
    byte_size: 1200,
    created_by: "import",
    provenance: { source: "external_import" },
    author_type: "human",
    inline_viewable,
    editable: false,
  });
}

describe("JobDocumentViewer", () => {
  it("previews an image the server serves inline", () => {
    render(
      <AppProviders>
        <JobDocumentViewer
          artifact={imageArtifact("image/png", true)}
          workspaceId="workspace-files"
        />
      </AppProviders>,
    );

    expect(screen.getByRole("img")).toBeTruthy();
  });

  it("offers an image the server serves only as a download instead of a broken preview", () => {
    render(
      <AppProviders>
        <JobDocumentViewer
          artifact={imageArtifact("image/svg+xml", false)}
          workspaceId="workspace-files"
        />
      </AppProviders>,
    );

    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByText("This file type is available as a verified download.")).toBeTruthy();
  });
});
