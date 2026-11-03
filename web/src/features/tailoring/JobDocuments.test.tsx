import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw/http";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type {
  Artifact,
  ArtifactDiff,
  CitationCheck,
  TailoringPreflight,
  TailoringReview,
  Task,
} from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { taskTypesHandler } from "../../test/taskTypes";
import { JobDocuments } from "./JobDocuments";
import { documentArtifact, reviewDecision, reviewProposal } from "./tailoringFixtures";

const API = "*/api/v1";

const artifactA = documentArtifact({ sha256: "a".repeat(16) });

const artifactB = documentArtifact({
  id: "artifact-b",
  logical_type: "cover_letter.markdown",
  sha256: "b".repeat(16),
  artifact_family_id: "family-b",
});

const APPROVE = "Generate or reuse the approved CV and letter";

const CONTENT_A = "# Summary\n\nContent from artifact A";

const DIFF =
  "--- v1\n+++ v2\n@@ -1,3 +1,4 @@\n # Summary\n-Old line\n+New line\n+Another new line\n";

const REUSE_AVAILABLE = {
  reuse_available: true,
  tailoring_run_id: "run-approved",
  artifact_ids: ["approved-1"],
  cost_usd: 0,
} satisfies TailoringPreflight;

const APPROVE_TASK = {
  id: "task-approve",
  workspace_id: "workspace-1",
  task_type: "workflow.approve",
  status: "queued",
  attempt_count: 0,
  job_posting_ids: ["job-1"],
  pending_discovery_ids: [],
  source_ids: [],
  progress: {},
  cancellation_requested_at: null,
  error_category: null,
  error_message: null,
  created_at: "2026-08-07T12:00:00Z",
  started_at: null,
  updated_at: "2026-08-07T12:00:00Z",
  completed_at: null,
} satisfies Task;

interface DocumentReads {
  artifacts?: Artifact[];
  content?: Record<string, string>;
  preflight?: TailoringPreflight;
  review?: () => TailoringReview;
  onDecision?: (proposalId: string) => void;
}

interface Recorded {
  /** Every write the page made, so a test can assert none happened yet. */
  posts: string[];
  savedVersions: unknown[];
  enqueued: unknown[];
  preflightQueries: string[];
}

function documentHandlers(reads: DocumentReads = {}): Recorded {
  const recorded: Recorded = { posts: [], savedVersions: [], enqueued: [], preflightQueries: [] };
  const artifacts = reads.artifacts ?? [artifactA, artifactB];
  const content = reads.content ?? {
    "artifact-a": CONTENT_A,
    "artifact-b": "Content from artifact B",
  };
  const post = (request: Request) => recorded.posts.push(new URL(request.url).pathname);
  server.use(
    taskTypesHandler(),
    http.get(`${API}/artifacts`, () => HttpResponse.json(artifacts)),
    http.get(`${API}/artifacts/:artifactId/content`, ({ params }) =>
      HttpResponse.text(content[String(params.artifactId)] ?? ""),
    ),
    http.get(`${API}/artifacts/:artifactId/versions`, ({ params }) =>
      HttpResponse.json(artifacts.filter((artifact) => artifact.id === params.artifactId)),
    ),
    http.get(`${API}/artifacts/:artifactId/diff/:otherId`, ({ params }) =>
      HttpResponse.json({
        diff: DIFF,
        from_artifact_id: String(params.artifactId),
        to_artifact_id: String(params.otherId),
      } satisfies ArtifactDiff),
    ),
    http.post(`${API}/artifacts/:artifactId/citation-check`, ({ params, request }) => {
      post(request);
      return HttpResponse.json({
        artifact_id: String(params.artifactId),
        broken: [],
        requires_override: false,
      } satisfies CitationCheck);
    }),
    http.post(`${API}/artifacts/:artifactId/versions`, async ({ params, request }) => {
      post(request);
      recorded.savedVersions.push(await request.json());
      return HttpResponse.json(
        documentArtifact({ id: String(params.artifactId), version_number: 2 }),
      );
    }),
    http.get(`${API}/tailoring-runs/:runId/review`, ({ params }) =>
      HttpResponse.json(
        reads.review?.() ?? { tailoring_run_id: String(params.runId), proposals: [] },
      ),
    ),
    http.post(`${API}/review-proposals/:proposalId/decisions`, ({ params, request }) => {
      post(request);
      reads.onDecision?.(String(params.proposalId));
      return HttpResponse.json(reviewDecision(reviewProposal(), "accept"));
    }),
    http.get(`${API}/workspaces/:workspaceId/jobs/:jobId/tailoring-preflight`, ({ request }) => {
      recorded.preflightQueries.push(new URL(request.url).search);
      return HttpResponse.json(reads.preflight ?? REUSE_AVAILABLE);
    }),
    http.get(`${API}/workspaces/:workspaceId/spend-history`, () => HttpResponse.json([])),
    http.post(`${API}/tasks`, async ({ request }) => {
      post(request);
      recorded.enqueued.push(await request.json());
      return HttpResponse.json(APPROVE_TASK);
    }),
  );
  return recorded;
}

function renderDocuments() {
  window.localStorage.setItem("clearscout.workspace", "workspace-1");
  return render(
    <AppProviders>
      <MemoryRouter>
        <Routes>
          <Route path="/" element={<JobDocuments jobId="job-1" workspaceId="workspace-1" />} />
        </Routes>
      </MemoryRouter>
    </AppProviders>,
  );
}

async function openEditor(container: HTMLElement) {
  await waitFor(() => {
    expect(container.querySelector("textarea")).toBeTruthy();
  });
  return container.querySelector("textarea") as HTMLTextAreaElement;
}

describe("JobDocuments", () => {
  it("links an empty job file list to Studio", async () => {
    documentHandlers({ artifacts: [] });
    renderDocuments();

    expect(await screen.findByText(/Generate a package in Studio/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open Studio" }).getAttribute("href")).toBe(
      "/jobs/job-1?tab=studio",
    );
  });

  it("says the file is saved locally and that typesetting belongs to Pressume", async () => {
    documentHandlers();
    renderDocuments();

    expect(await screen.findByRole("button", { name: "Save as PDF or text" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Render PDF or text" })).toBeNull();
    expect(screen.getByText(/writes the file to this machine/)).toBeTruthy();
    expect(screen.getByText(/ClearScout does not typeset a resume/)).toBeTruthy();
  });

  it("shows correct artifact hash when switching between documents", async () => {
    documentHandlers();
    renderDocuments();

    await waitFor(() => {
      expect(screen.getByText(/aaaaaaaaaaaa/)).toBeTruthy();
    });

    fireEvent.click(screen.getByRole("button", { name: /Cover letter/ }));

    await waitFor(() => {
      expect(screen.getByText(/bbbbbbbbbbbb/)).toBeTruthy();
    });
    expect(screen.queryByText(/aaaaaaaaaaaa/)).toBeNull();
  });

  it("shows JSON artifacts as an expandable structured document", async () => {
    const jsonArtifact = documentArtifact({
      id: "artifact-json",
      logical_type: "tailoring.manifest.json",
      mime_type: "application/json",
    });
    documentHandlers({
      artifacts: [jsonArtifact],
      content: { "artifact-json": '{"score": 82, "evidence": ["cv.role.1"]}' },
    });
    renderDocuments();

    expect(await screen.findByText("score")).toBeTruthy();
    expect(screen.getByText("82")).toBeTruthy();
    expect(screen.getByText("evidence")).toBeTruthy();
    expect(screen.getByText('"cv.role.1"')).toBeTruthy();
    expect(document.querySelector(".json-document pre")).toBeNull();
  });

  it("keeps nested JSON closed", async () => {
    const jsonArtifact = documentArtifact({
      id: "artifact-json",
      logical_type: "tailoring.manifest.json",
      mime_type: "application/json",
    });
    documentHandlers({
      artifacts: [jsonArtifact],
      content: { "artifact-json": '{"outer":{"inner":1}}' },
    });
    renderDocuments();

    await screen.findByText("outer");
    const nodes = document.querySelectorAll(".json-node");
    expect(nodes[0]?.hasAttribute("open")).toBe(true);
    expect(nodes[1]?.hasAttribute("open")).toBe(false);
  });

  it("does not render invalid JSON as document content", async () => {
    const jsonArtifact = documentArtifact({
      id: "artifact-json",
      logical_type: "tailoring.manifest.json",
      mime_type: "application/json",
    });
    documentHandlers({
      artifacts: [jsonArtifact],
      content: { "artifact-json": "{not valid JSON}" },
    });
    renderDocuments();

    expect((await screen.findByRole("alert")).textContent).toContain("invalid");
    expect(screen.queryByText("{not valid JSON}")).toBeNull();
  });

  it("summarises a version diff", async () => {
    documentHandlers();
    const { container } = renderDocuments();
    await openEditor(container);

    const buttons = await screen.findAllByRole("button", { name: "Diff" });
    fireEvent.click(buttons[0] as HTMLElement);

    await waitFor(() => {
      expect(screen.getByText("2 lines added")).toBeTruthy();
    });
    expect(screen.getByText("1 line removed")).toBeTruthy();
    expect(screen.getByText("+New line")).toBeTruthy();
  });

  it("warns before unloading and saves unsaved Markdown with the keyboard shortcut", async () => {
    const recorded = documentHandlers();
    const { container } = renderDocuments();
    const editor = await openEditor(container);

    fireEvent.change(editor, { target: { value: `${CONTENT_A}\n\nHuman edit` } });
    expect(screen.getByText("Unsaved changes")).toBeTruthy();

    const beforeUnload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(beforeUnload);
    expect(beforeUnload.defaultPrevented).toBe(true);

    fireEvent.keyDown(window, { key: "s", ctrlKey: true });

    await waitFor(() => {
      expect(recorded.savedVersions).toEqual([
        {
          author_type: "human",
          content: `${CONTENT_A}\n\nHuman edit`,
          override_reason: "",
          workspace_id: "workspace-1",
        },
      ]);
    });
  });

  it("confirms that an approved package may reuse a completed run before enqueueing", async () => {
    const recorded = documentHandlers({
      artifacts: [documentArtifact({ sha256: "a".repeat(16), tailoring_run_id: "run-1" })],
    });
    renderDocuments();

    const approvePackage = await screen.findByRole("button", { name: APPROVE });
    await waitFor(() => expect(approvePackage).toHaveProperty("disabled", false));
    fireEvent.click(approvePackage);

    expect(await screen.findByText(/Exact approved package found/)).toBeTruthy();
    expect(screen.getByText(/does not call the provider and adds no provider charge/)).toBeTruthy();
    expect(recorded.preflightQueries).toEqual([
      "?cover_letter=true&approval_tailoring_run_id=run-1",
    ]);
    expect(recorded.posts).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Reuse approved package" }));

    await waitFor(() => {
      expect(recorded.enqueued).toMatchObject([{ task_type: "workflow.approve" }]);
    });
  });

  it("shows a recorded decision on the reviewed line once it is accepted", async () => {
    const proposal = reviewProposal();
    let decided = false;
    documentHandlers({
      artifacts: [documentArtifact({ sha256: "a".repeat(16), tailoring_run_id: "run-1" })],
      onDecision: () => {
        decided = true;
      },
      review: () => ({
        tailoring_run_id: "run-1",
        proposals: [
          decided ? { ...proposal, decisions: [reviewDecision(proposal, "accept")] } : proposal,
        ],
      }),
    });
    renderDocuments();

    expect(
      await screen.findByText("0 of 1 lines explicitly decided.", { exact: false }),
    ).toBeTruthy();
    expect(screen.getByText("Undecided")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));

    expect(await screen.findByText("Accepted")).toBeTruthy();
    expect(screen.queryByText("Undecided")).toBeNull();
    expect(screen.getByText("1 of 1 lines explicitly decided.", { exact: false })).toBeTruthy();
  });

  it("shows a preflight miss and recorded approval cost when no exact reuse exists", async () => {
    const recorded = documentHandlers({
      artifacts: [documentArtifact({ sha256: "a".repeat(16), tailoring_run_id: "run-1" })],
      preflight: {
        reuse_available: false,
        tailoring_run_id: null,
        artifact_ids: [],
        cost_usd: null,
      },
    });
    renderDocuments();

    const approvePackage = await screen.findByRole("button", { name: APPROVE });
    await waitFor(() => expect(approvePackage).toHaveProperty("disabled", false));
    fireEvent.click(approvePackage);

    expect(await screen.findByText(/No exact approved package is available/)).toBeTruthy();
    expect(screen.getByRole("dialog").textContent).toMatch(
      /Preflight did not find a reusable package cost/,
    );
    expect(recorded.posts).toEqual([]);
  });
});
