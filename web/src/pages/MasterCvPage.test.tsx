import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw/http";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type {
  Health,
  MasterCvContent,
  MasterCvEdit,
  MasterCvImpact,
  MasterCvImport,
  MasterCvRereadEstimate,
  MasterCvVersion,
  RequirementGaps,
} from "../api/client/types";
import type { components } from "../api/schema";
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

const ESTIMATE = { past_rereads: 2, average_usd: 0.04 } satisfies MasterCvRereadEstimate;

const IMPORTED = {
  workspace_id: "workspace-cv",
  artifact_id: "artifact-2",
  canonical_path: "/tmp/CV_Master.md",
  content_sha256: "cccccccccccccccc",
  entry_count: 7,
  master_cv_version_id: "version-2",
  method: "standardized",
  report_path: null,
  source_path: "/tmp/Resume.docx",
} satisfies MasterCvImport;

function health(freeMode: boolean): Health {
  return {
    status: "ok",
    version: "0.1.0",
    database_reachable: true,
    schema_current: true,
    current_revision: "0001",
    head_revision: "0001",
    database_exists: true,
    worker_available: true,
    free_mode: freeMode,
  } satisfies Health;
}

type Sent = { saves: unknown[]; imports: URLSearchParams[] };

function installHandlers(
  overrides: {
    impact?: Partial<MasterCvImpact>;
    save?: Partial<MasterCvEdit>;
    gaps?: RequirementGaps;
    versions?: MasterCvVersion[];
    freeMode?: boolean;
    importStatus?: (query: URLSearchParams) => number;
  } = {},
): Sent {
  const impact = { ...IMPACT, ...overrides.impact };
  const saved = { ...SAVED, ...overrides.save };
  const sent: Sent = { saves: [], imports: [] };
  server.use(
    http.get("*/api/v1/health", () => HttpResponse.json(health(overrides.freeMode ?? false))),
    http.get(`${WORKSPACE}/master-cv/reread-estimate`, () => HttpResponse.json(ESTIMATE)),
    http.post(`${WORKSPACE}/master-cv/import`, ({ request }) => {
      const query = new URL(request.url).searchParams;
      sent.imports.push(query);
      const status = overrides.importStatus?.(query) ?? 201;
      return status === 201
        ? HttpResponse.json(IMPORTED, { status })
        : HttpResponse.json({ detail: "This CV needs a paid re-read." }, { status });
    }),
    http.get(`${WORKSPACE}/master-cv/versions`, () =>
      HttpResponse.json(overrides.versions ?? [VERSION]),
    ),
    http.get(`${WORKSPACE}/master-cv/requirement-gaps`, () =>
      HttpResponse.json(overrides.gaps ?? NO_GAPS),
    ),
    http.post(`${WORKSPACE}/master-cv/impact`, () => HttpResponse.json(impact)),
    http.post(`${WORKSPACE}/master-cv`, async ({ request }) => {
      sent.saves.push(await request.json());
      return HttpResponse.json(saved);
    }),
    http.get(`${WORKSPACE}/master-cv`, () => HttpResponse.json(CONTENT)),
    http.get(`${WORKSPACE}/master-cv-proposals`, () => HttpResponse.json([])),
    http.post(`${WORKSPACE}/master-cv-proposals/refresh`, () =>
      HttpResponse.json({ promote_transferable: 0, add_missing: 0 }),
    ),
    http.get(`${WORKSPACE}/requirement-rulings`, () => HttpResponse.json({ rulings: [] })),
  );
  return sent;
}

function chooseFile(name: string) {
  const input = screen.getByLabelText("CV source");
  fireEvent.change(input, { target: { files: [new File(["synthetic"], name)] } });
}

function importFold() {
  return screen.getByRole<HTMLDetailsElement>("group", { name: "Import a version from a file" });
}

function renderPage() {
  window.localStorage.setItem("clearscout.workspace", "workspace-cv");
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

  it("puts a file the model must re-read behind the paid mark and a confirm", async () => {
    const sent = installHandlers();
    renderPage();
    await screen.findByRole("button", { name: "Edit this CV" });

    chooseFile("Resume.docx");
    const button = await screen.findByRole<HTMLButtonElement>("button", {
      name: "Import as new version",
    });
    expect(within(button).getByText("Paid")).toBeTruthy();
    await waitFor(() => expect(within(button).getByText("about $0.04 per re-read")).toBeTruthy());
    await waitFor(() => expect(button.disabled).toBe(false));
    fireEvent.click(button);

    const dialog = await screen.findByRole("dialog", { name: "Re-read this CV with the model?" });
    expect(dialog.textContent).toContain("About $0.04 per re-read, from 2 past re-reads.");
    expect(sent.imports).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole("button", { name: "Re-read and import" }));

    await waitFor(() => expect(sent.imports).toHaveLength(1));
    expect(sent.imports[0]?.get("confirm_reread")).toBe("true");
    expect(await screen.findByText(/Ready: 7 citable entries/)).toBeTruthy();
  });

  it("says a first re-read has no cost to estimate from", async () => {
    installHandlers();
    server.use(
      http.get(`${WORKSPACE}/master-cv/reread-estimate`, () =>
        HttpResponse.json({ past_rereads: 0, average_usd: null } satisfies MasterCvRereadEstimate),
      ),
    );
    renderPage();
    await screen.findByRole("button", { name: "Edit this CV" });

    chooseFile("Resume.docx");
    const button = await screen.findByRole<HTMLButtonElement>("button", {
      name: "Import as new version",
    });
    await waitFor(() => expect(button.disabled).toBe(false));
    fireEvent.click(button);

    const dialog = await screen.findByRole("dialog", { name: "Re-read this CV with the model?" });
    expect(dialog.textContent).toContain("The cost is unknown until the first re-read.");
    expect(dialog.textContent).not.toContain("Charges may be higher.");
  });

  it("imports Markdown and plain text with a plain button", async () => {
    const sent = installHandlers();
    renderPage();
    await screen.findByRole("button", { name: "Edit this CV" });

    chooseFile("CV.txt");
    const button = await screen.findByRole("button", { name: "Import as new version" });
    expect(within(button).queryByText("Paid")).toBeNull();
    fireEvent.click(button);

    await waitFor(() => expect(sent.imports).toHaveLength(1));
    expect(sent.imports[0]?.get("confirm_reread")).toBe("false");
  });

  it("offers a confirmed re-read when the server refuses a text file", async () => {
    const sent = installHandlers({
      importStatus: (query) => (query.get("confirm_reread") === "true" ? 201 : 409),
    });
    renderPage();
    await screen.findByRole("button", { name: "Edit this CV" });

    chooseFile("notes.txt");
    fireEvent.click(await screen.findByRole("button", { name: "Import as new version" }));

    const paid = await screen.findByRole<HTMLButtonElement>("button", {
      name: "Import as new version",
      description: /Paid/,
    });
    await waitFor(() => expect(paid.disabled).toBe(false));
    fireEvent.click(paid);
    const dialog = await screen.findByRole("dialog", { name: "Re-read this CV with the model?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Re-read and import" }));

    await waitFor(() => expect(sent.imports).toHaveLength(2));
    expect(sent.imports.map((query) => query.get("confirm_reread"))).toEqual(["false", "true"]);
  });

  it("moves focus to the paid import when the server refuses a text file", async () => {
    installHandlers({ importStatus: () => 409 });
    renderPage();
    await screen.findByRole("button", { name: "Edit this CV" });

    chooseFile("notes.txt");
    fireEvent.click(await screen.findByRole("button", { name: "Import as new version" }));

    const paid = await screen.findByRole("button", {
      name: "Import as new version",
      description: /Paid/,
    });
    await waitFor(() => expect(document.activeElement).toBe(paid));
  });

  it("offers the free route in free mode when the server refuses a text file", async () => {
    installHandlers({ freeMode: true, importStatus: () => 409 });
    renderPage();
    await screen.findByRole("button", { name: "Edit this CV" });

    chooseFile("notes.txt");
    fireEvent.click(await screen.findByRole("button", { name: "Import as new version" }));

    const route = await screen.findByText(/Markdown or text in the CV format imports free/);
    // Only the transient error toast still carries the server text.
    expect(screen.getAllByText("This CV needs a paid re-read.")).toHaveLength(1);
    expect(route.textContent).toContain("Open Help on this page");
    await waitFor(() => expect(document.activeElement).toBe(route));
  });

  it("keeps the import result in view after the first import", async () => {
    let versions: MasterCvVersion[] = [];
    installHandlers();
    server.use(
      http.get(`${WORKSPACE}/master-cv/versions`, () => HttpResponse.json(versions)),
      http.post(`${WORKSPACE}/master-cv/import`, () => {
        versions = [VERSION];
        return HttpResponse.json(IMPORTED, { status: 201 });
      }),
    );
    renderPage();
    await screen.findByText("Nothing imported yet.");

    chooseFile("CV.md");
    fireEvent.click(screen.getByRole("button", { name: "Import as new version" }));

    expect(await screen.findByText(/Ready: 7 citable entries/)).toBeTruthy();
    await screen.findByRole("button", { name: "Edit this CV" });
    expect(importFold().open).toBe(true);
  });

  it("keeps a paid import off in free mode and says why", async () => {
    installHandlers({ freeMode: true });
    renderPage();
    await screen.findByRole("button", { name: "Edit this CV" });

    chooseFile("Resume.pdf");

    expect(await screen.findByText("Add a provider key to use paid actions.")).toBeTruthy();
    const button = screen.getByRole<HTMLButtonElement>("button", { name: "Import as new version" });
    expect(button.disabled).toBe(true);
  });

  it("opens the import form when there is no CV yet", async () => {
    installHandlers({ versions: [] });
    renderPage();

    await screen.findByText("Nothing imported yet.");
    expect(importFold().open).toBe(true);
  });

  it("saves an edit the parser rejects only through a confirmed paid re-read", async () => {
    const sent = installHandlers({ impact: { reread_reason: "it has no dated role" } });
    renderPage();
    await openEditor();

    fireEvent.click(screen.getByRole("button", { name: "Review changes" }));
    const button = await screen.findByRole<HTMLButtonElement>("button", {
      name: "Re-read and save",
    });
    expect(screen.queryByRole("button", { name: "Save as new version" })).toBeNull();
    expect(within(button).getByText("Paid")).toBeTruthy();
    await waitFor(() => expect(button.disabled).toBe(false));
    fireEvent.click(button);

    const dialog = await screen.findByRole("dialog", { name: "Re-read this CV with the model?" });
    expect(dialog.textContent).toContain("because it has no dated role");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(sent.saves).toHaveLength(0);

    fireEvent.click(button);
    const again = await screen.findByRole("dialog", { name: "Re-read this CV with the model?" });
    fireEvent.click(within(again).getByRole("button", { name: "Re-read and save" }));

    await waitFor(() => expect(sent.saves).toHaveLength(1));
    expect(sent.saves[0]).toEqual({ content: CV, confirm_reread: true });
  });

  it("names the provider-key fix when the server refuses a paid re-read save", async () => {
    installHandlers({ impact: { reread_reason: "it has no dated role" } });
    server.use(
      http.post(`${WORKSPACE}/master-cv`, () =>
        HttpResponse.json(
          {
            type: "urn:clearscout:problem:free-mode",
            title: "Free mode",
            status: 409,
            detail: "No provider API key is set, so paid actions are off.",
            correlation_id: "correlation-save",
            hint: "Add an API key in Settings > Provider.",
          } satisfies components["schemas"]["Problem"],
          { status: 409 },
        ),
      ),
    );
    renderPage();
    await openEditor();

    fireEvent.click(screen.getByRole("button", { name: "Review changes" }));
    const button = await screen.findByRole<HTMLButtonElement>("button", {
      name: "Re-read and save",
    });
    await waitFor(() => expect(button.disabled).toBe(false));
    fireEvent.click(button);
    const dialog = await screen.findByRole("dialog", { name: "Re-read this CV with the model?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Re-read and save" }));

    await waitFor(() =>
      expect(document.querySelector(".cv-layout .field-error")?.textContent).toBe(
        "Add a provider key to use paid actions. Open provider settings",
      ),
    );
    const refusal = document.querySelector(".cv-layout .field-error") as HTMLElement;
    expect(within(refusal).getByRole("link", { name: "Open provider settings" })).toBeTruthy();
  });

  it("keeps the paid re-read save off in free mode", async () => {
    installHandlers({ impact: { reread_reason: "it has no dated role" }, freeMode: true });
    renderPage();
    await openEditor();

    fireEvent.click(screen.getByRole("button", { name: "Review changes" }));

    expect(await screen.findByText("Add a provider key to use paid actions.")).toBeTruthy();
    const button = screen.getByRole<HTMLButtonElement>("button", { name: "Re-read and save" });
    expect(button.disabled).toBe(true);
    expect(
      screen.getByText(
        "This draft needs a model re-read because it has no dated role. Fix that in the draft to save it without a charge.",
      ),
    ).toBeTruthy();
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
    const sent = installHandlers({
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
    expect(sent.saves).toEqual([{ content: CV, confirm_reread: false }]);
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
