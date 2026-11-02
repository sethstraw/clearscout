import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type {
  MasterCvContent,
  MasterCvEdit,
  MasterCvImpact,
  MasterCvVersion,
  RequirementGaps,
} from "../api/client/types";
import { AppProviders } from "../providers";
import { server } from "../test/msw/server";
import { MasterCvPage } from "./MasterCvPage";

const WORKSPACE = "*/api/v1/workspaces/:workspaceId";

const CV = "# Summary\n\nDelivered 12 releases in 2025.\n";

const VERSION = {
  id: "version-1",
  content_sha256: "aaaaaaaaaaaaaaaa",
  byte_size: CV.length,
  effective_at: "2026-08-01T12:00:00Z",
  is_current: true,
  source_uri: "file:///synthetic/CV_Master.md",
} satisfies MasterCvVersion;

const CONTENT = {
  id: "version-1",
  content: CV,
  content_sha256: VERSION.content_sha256,
  byte_size: VERSION.byte_size,
  effective_at: VERSION.effective_at,
  is_current: true,
  source_uri: VERSION.source_uri,
} satisfies MasterCvContent;

type Citation = MasterCvImpact["citations"][number];

function citation(quote: string, drift: Omit<Citation["drift"], "text">): Citation {
  return {
    char_start: 0,
    char_end: quote.length,
    classification: "DIRECT",
    drift: { ...drift, text: quote },
    employer: "Example Ltd",
    entry_carried: true,
    evaluation_id: `evaluation-${quote}`,
    job_posting_id: "job-1",
    quote,
    title: "Staff Engineer",
  };
}

const IMPACT = {
  workspace_id: "workspace-cv",
  master_cv_version_id: "version-1",
  content_sha256: "bbbbbbbbbbbbbbbb",
  citation_count: 3,
  material_count: 1,
  changed_count: 1,
  unchanged_count: 1,
  not_found_count: 0,
  orphaned_count: 0,
  returned_count: 3,
  omitted_count: 0,
  identical: false,
  citations: [
    citation("Delivered 12 releases", {
      found: true,
      changed: true,
      material: true,
      reason: "A figure changed.",
    }),
    citation("Led the platform team", {
      found: true,
      changed: true,
      material: false,
      reason: "Wording only.",
    }),
    citation("Wrote the migration plan", {
      found: true,
      changed: false,
      material: false,
      reason: "No change.",
    }),
  ],
} satisfies MasterCvImpact;

const SAVED = {
  workspace_id: "workspace-cv",
  artifact_id: "artifact-1",
  canonical_path: "/tmp/CV_Master.md",
  content_sha256: "bbbbbbbbbbbbbbbb",
  created: true,
  entry_count: 9,
  master_cv_version_id: "version-2",
  method: "adopted",
  previous_master_cv_version_id: "version-1",
  report_path: null,
  source_path: "/tmp/CV_Master.md",
} satisfies MasterCvEdit;

const GROUPING_RULE =
  "Requirements are grouped by their text after lowercasing and collapsing runs of whitespace to a single space. Grouping does not assert that two postings mean the same thing.";

const NO_GAPS = {
  evaluations_read: 0,
  gap_count: 0,
  omitted_count: 0,
  gaps: [],
  grouping_rule: GROUPING_RULE,
} satisfies RequirementGaps;

type GapSource = RequirementGaps["gaps"][number]["raised_by"][number];

function gapSource(
  jobPostingId: string,
  employer: string,
  title: string,
  importance: GapSource["importance"] = "must_have",
): GapSource {
  return {
    job_posting_id: jobPostingId,
    employer,
    title,
    importance,
    evaluated_at: "2026-08-10T09:00:00Z",
    requirement_key: `req-${jobPostingId}`,
    ruling: null,
  };
}

function installHandlers(
  overrides: {
    impact?: Partial<MasterCvImpact>;
    save?: Partial<MasterCvEdit>;
    gaps?: RequirementGaps;
  } = {},
) {
  const impact = { ...IMPACT, ...overrides.impact };
  const saved = { ...SAVED, ...overrides.save };
  server.use(
    http.get(`${WORKSPACE}/master-cv/versions`, () => HttpResponse.json([VERSION])),
    http.get(`${WORKSPACE}/master-cv/requirement-gaps`, () =>
      HttpResponse.json(overrides.gaps ?? NO_GAPS),
    ),
    http.post(`${WORKSPACE}/master-cv/impact`, () => HttpResponse.json(impact)),
    http.post(`${WORKSPACE}/master-cv`, () => HttpResponse.json(saved)),
    http.get(`${WORKSPACE}/master-cv`, () => HttpResponse.json(CONTENT)),
    http.get(`${WORKSPACE}/master-cv-proposals`, () => HttpResponse.json([])),
    http.post(`${WORKSPACE}/master-cv-proposals/refresh`, () =>
      HttpResponse.json({ promote_transferable: 0, add_missing: 0 }),
    ),
    http.get(`${WORKSPACE}/requirement-rulings`, () => HttpResponse.json({ rulings: [] })),
  );
}

function renderPage() {
  window.localStorage.setItem("tailorcv.workspace", "workspace-cv");
  return render(
    <MemoryRouter>
      <AppProviders>
        <MasterCvPage />
      </AppProviders>
    </MemoryRouter>,
  );
}

async function openEditor() {
  const button = await screen.findByRole("button", { name: "Edit this CV" });
  fireEvent.click(button);
  await waitFor(() => expect(screen.getByRole("button", { name: "Review changes" })).toBeTruthy());
}

describe("MasterCvPage", () => {
  it("will not import until a file has been chosen, and says so", async () => {
    installHandlers();
    renderPage();

    const button = await screen.findByRole("button", { name: "Import as new version" });
    expect(button.hasAttribute("disabled")).toBe(true);
    expect(screen.getByText("Choose a CV file first")).toBeTruthy();
  });

  it("opens the stored CV read-only", async () => {
    installHandlers();
    renderPage();

    expect(await screen.findByRole("button", { name: "Edit this CV" })).toBeTruthy();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 2, name: "Summary" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Review changes" })).toBeNull();
  });

  it("reports how many citations an edit would make material", async () => {
    installHandlers();
    renderPage();
    await openEditor();

    fireEvent.click(screen.getByRole("button", { name: "Review changes" }));

    await waitFor(() => expect(document.body.textContent).toContain("1 material,"));
    expect(document.body.textContent).toContain("Material (1)");
    expect(document.body.textContent).toContain("Delivered 12 releases");
  });

  it("does not offer a save until the edit has been reviewed", async () => {
    installHandlers();
    renderPage();
    await openEditor();

    expect(screen.queryByRole("button", { name: /Save as new version/ })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Review changes" }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Save as new version" })).toBeTruthy(),
    );
  });

  it("says plainly when a save appended nothing", async () => {
    installHandlers({
      impact: { identical: true },
      save: {
        created: false,
        master_cv_version_id: "version-1",
        previous_master_cv_version_id: null,
      },
    });
    renderPage();
    await openEditor();

    fireEvent.click(screen.getByRole("button", { name: "Review changes" }));
    const save = await screen.findByRole("button", { name: "Save as new version" });
    fireEvent.click(save);

    await waitFor(() => expect(document.body.textContent).toContain("Nothing was appended."));
  });

  it("diffs the stored version against the draft before any save", async () => {
    installHandlers();
    const { container } = renderPage();
    await openEditor();

    const textarea = container.querySelector("textarea") as HTMLTextAreaElement;
    fireEvent.change(textarea, {
      target: { value: "# Summary\n\nDelivered 40 releases in 2025.\n" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Review changes" }));

    await waitFor(() =>
      expect(document.body.textContent).toContain("-Delivered 12 releases in 2025."),
    );
    expect(document.body.textContent).toContain("+Delivered 40 releases in 2025.");
    expect(document.body.textContent).toContain("1 line added");
  });

  it("says how many affected citations were left out of the list", async () => {
    installHandlers({ impact: { citation_count: 13, returned_count: 3, omitted_count: 10 } });
    renderPage();
    await openEditor();

    fireEvent.click(screen.getByRole("button", { name: "Review changes" }));

    await waitFor(() => expect(document.body.textContent).toContain("10 more are"));
    expect(document.body.textContent).toContain("Listing 3 of 13");
  });

  it("groups an unanswered requirement by posting and links each one to its fit report", async () => {
    installHandlers({
      gaps: {
        evaluations_read: 3,
        gap_count: 2,
        omitted_count: 0,
        grouping_rule: GROUPING_RULE,
        gaps: [
          {
            text: "Five or more years owning a regulated data platform",
            raised_by: [
              gapSource("job-1", "Globex", "Staff Data Platform Lead"),
              gapSource("job-2", "Initech", "Principal Engineer, Platform", "nice_to_have"),
            ],
          },
          {
            text: "Shares a weekly on-call rotation",
            raised_by: [gapSource("job-3", "Northwind", "Engineering Manager", "responsibility")],
          },
        ],
      },
    });
    renderPage();

    const link = await screen.findByRole("link", { name: "Globex: Staff Data Platform Lead" });
    expect(link.getAttribute("href")).toBe("/jobs/job-1?tab=fit-report");
    expect(screen.getByText("2 postings")).toBeTruthy();
    expect(screen.getByText("1 posting")).toBeTruthy();
    expect(document.body.textContent).toContain("current evaluation of 3 postings");
    expect(document.body.textContent).toContain("must have, read on");
    expect(document.body.textContent).toContain("responsibility, read on");
  });

  it("says how many unanswered requirements the bounded list left out", async () => {
    installHandlers({
      gaps: {
        evaluations_read: 200,
        gap_count: 140,
        omitted_count: 40,
        grouping_rule: GROUPING_RULE,
        gaps: [
          {
            text: "Five or more years owning a regulated data platform",
            raised_by: [gapSource("job-1", "Globex", "Staff Data Platform Lead")],
          },
        ],
      },
    });
    renderPage();

    await waitFor(() => expect(document.body.textContent).toContain("Listing 1 of 140"));
    expect(document.body.textContent).toContain("40 more are not shown here");
  });

  it("does not call an answered CV a gap", async () => {
    installHandlers({
      gaps: {
        evaluations_read: 4,
        gap_count: 0,
        omitted_count: 0,
        grouping_rule: GROUPING_RULE,
        gaps: [],
      },
    });
    renderPage();

    await waitFor(() =>
      expect(document.body.textContent).toContain(
        "Every requirement these evaluations settled was answered from this CV.",
      ),
    );
  });

  it("says nothing has been evaluated rather than showing an empty gap list", async () => {
    installHandlers();
    renderPage();

    await waitFor(() =>
      expect(document.body.textContent).toContain("No posting has been evaluated against this CV"),
    );
  });
});
