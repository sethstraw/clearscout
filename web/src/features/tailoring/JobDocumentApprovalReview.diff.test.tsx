import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { TailoringReview } from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { taskTypesHandler } from "../../test/taskTypes";
import { JobDocumentApprovalReview } from "./JobDocumentApprovalReview";
import { reviewDecision, reviewProposal } from "./tailoringFixtures";

const API = "*/api/v1";

type ReviewProposal = TailoringReview["proposals"][number];

const supported = reviewProposal({
  slot: "experience/role[0]/line[0]",
  text: "Led the analytics team and shipped the reporting platform",
  digest: "a".repeat(64),
  payload: {
    immutable: false,
    kind: "reframed",
    rationale: "",
    evidence: [
      { source: "cv-1", text: "Managed the analytics group and delivered reporting tools" },
    ],
  },
});

const unsupported = reviewProposal({
  id: "proposal-2",
  proposal_key: "k2",
  text: "Generalist engineer",
  source_ids: [],
  digest: "b".repeat(64),
  payload: { immutable: false, kind: "new", rationale: "", evidence: [] },
});

// An immutable identity line with no resolvable cited evidence: the batch must
// still include it because the program copies it verbatim.
const identity = reviewProposal({
  id: "proposal-3",
  proposal_key: "k3",
  slot: "header/name",
  text: "Alex Analyst",
  source_ids: [],
  digest: "c".repeat(64),
  payload: { immutable: true, kind: "preserved", rationale: "" },
});

function reviewHandlers(
  options: {
    proposals?: ReviewProposal[];
    checks?: TailoringReview["checks"];
    onDecide?: (id: string) => void;
    decideOk?: (id: string) => boolean;
  } = {},
) {
  const proposals = options.proposals ?? [supported, unsupported];
  server.use(
    taskTypesHandler(),
    http.post(`${API}/review-proposals/:proposalId/decisions`, ({ params }) => {
      const id = String(params.proposalId);
      options.onDecide?.(id);
      if (options.decideOk && !options.decideOk(id)) {
        return HttpResponse.json({ detail: "The proposal changed. Reload it." }, { status: 422 });
      }
      return HttpResponse.json(reviewDecision(reviewProposal({ id }), "accept"));
    }),
    http.get(`${API}/tailoring-runs/:runId/review`, ({ params }) =>
      HttpResponse.json({
        tailoring_run_id: String(params.runId),
        proposals,
        checks: options.checks,
      } satisfies TailoringReview),
    ),
    http.get(`${API}/workspaces/:workspaceId/spend-history`, () => HttpResponse.json([])),
  );
}

function renderReview() {
  return render(
    <AppProviders>
      <MemoryRouter>
        <JobDocumentApprovalReview jobId="job-1" runId="run-1" workspaceId="workspace-1" />
      </MemoryRouter>
    </AppProviders>,
  );
}

const acceptAllName = /Accept remaining evidence-backed and copied lines/;

describe("JobDocumentApprovalReview drafted-vs-source diff", () => {
  it("renders the resolved master-CV source text against the drafted line as a diff", async () => {
    reviewHandlers();
    const { container } = renderReview();

    // The cited source text renders in the diff, not just the evidence ids.
    expect(
      await screen.findByText(/Managed the analytics group and delivered reporting tools/),
    ).toBeTruthy();
    const removed = container.querySelector(".diff-line--del")?.textContent ?? "";
    const added = container.querySelector(".diff-line--add")?.textContent ?? "";
    expect(removed).toContain("Managed the analytics group and delivered reporting tools");
    expect(added).toContain("Led the analytics team and shipped the reporting platform");
  });

  it("accepts supported drafted lines but never an unsupported drafted line", async () => {
    const decided: string[] = [];
    reviewHandlers({ onDecide: (id) => decided.push(id) });
    renderReview();

    const acceptAll = await screen.findByRole("button", { name: acceptAllName });
    fireEvent.click(acceptAll);

    await waitFor(() => expect(decided).toContain("proposal-1"));
    expect(decided).not.toContain("proposal-2");
    // The unsupported drafted line is called out as still needing a manual decision.
    expect(screen.getByText(/still need a manual decision/)).toBeTruthy();
  });

  it("includes immutable verbatim identity lines in the batch accept", async () => {
    const decided: string[] = [];
    reviewHandlers({
      proposals: [supported, identity, unsupported],
      onDecide: (id) => decided.push(id),
    });
    renderReview();

    const acceptAll = await screen.findByRole("button", { name: acceptAllName });
    // The count covers the supported drafted line and the immutable identity line, not the unsupported one.
    expect(acceptAll.textContent).toContain("(2)");
    fireEvent.click(acceptAll);

    await waitFor(() => expect(decided).toContain("proposal-3"));
    expect(decided).toContain("proposal-1");
    expect(decided).not.toContain("proposal-2");
  });

  it("leaves a line a check flagged for review out of the batch accept", async () => {
    const decided: string[] = [];
    const flagged = reviewProposal({
      ...supported,
      id: "proposal-4",
      proposal_key: "k4",
      digest: "d".repeat(64),
      payload: { ...supported.payload, decision: "review" },
    });
    reviewHandlers({ proposals: [supported, flagged], onDecide: (id) => decided.push(id) });
    renderReview();

    const acceptAll = await screen.findByRole("button", { name: acceptAllName });
    expect(acceptAll.textContent).toContain("(1)");
    fireEvent.click(acceptAll);

    await waitFor(() => expect(decided).toContain("proposal-1"));
    expect(decided).not.toContain("proposal-4");
    expect(screen.getByText(/1 line flagged by a check needs a manual decision/)).toBeTruthy();
  });

  it("surfaces a partial batch failure instead of masking it", async () => {
    reviewHandlers({
      proposals: [supported, identity],
      decideOk: (id) => id !== "proposal-3",
    });
    renderReview();

    const acceptAll = await screen.findByRole("button", { name: acceptAllName });
    fireEvent.click(acceptAll);

    expect(await screen.findByText(/Accepted 1 line; 1 could not be accepted/)).toBeTruthy();
  });
});

describe("JobDocumentApprovalReview recorded checks", () => {
  it("lists a passing critic check's advisories under it and leaves a plain pass out", async () => {
    reviewHandlers({
      checks: [
        { check: "keyword coverage", outcome: "pass", detail: "80% covered", advisories: [] },
        {
          check: "critic",
          outcome: "pass",
          detail: "alignment 4/5",
          advisories: ["The Acme migration has no outcome."],
        },
      ],
    });
    renderReview();

    const critic = await screen.findByText(/PASS - critic: alignment 4\/5/);
    const advisory = screen.getByText("The Acme migration has no outcome.");
    expect(critic.closest("li")?.contains(advisory)).toBe(true);
    expect(screen.queryByText(/keyword coverage/)).toBeNull();
  });
});
