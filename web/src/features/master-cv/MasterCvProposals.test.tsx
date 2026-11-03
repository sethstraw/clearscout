import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw/http";
import { describe, expect, it, vi } from "vitest";

import type { MasterCvProposal } from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { MasterCvProposals } from "./MasterCvProposals";

const WORKSPACE = "*/api/v1/workspaces/:workspaceId";

const PROPOSAL = {
  id: "proposal-1",
  kind: "add_missing",
  requirement_key: "req-add-1",
  requirement_text: "Five years of routing experience",
  evidence_note: null,
  posting_count: 4,
  first_seen_at: "2026-08-01T09:00:00Z",
  last_seen_at: "2026-08-18T09:00:00Z",
  proposed_at: "2026-08-18T10:00:00Z",
  state: "proposed",
  decided_at: null,
} satisfies MasterCvProposal;

const ACCEPTED = {
  ...PROPOSAL,
  state: "accepted",
  decided_at: "2026-08-19T09:00:00Z",
} satisfies MasterCvProposal;

function installProposals() {
  server.use(
    http.post(`${WORKSPACE}/master-cv-proposals/:proposalId/:ruling`, () =>
      HttpResponse.json(ACCEPTED),
    ),
    http.get(`${WORKSPACE}/master-cv-proposals`, () => HttpResponse.json([PROPOSAL])),
  );
}

describe("MasterCvProposals", () => {
  it("calls onAccepted with the accepted proposal after Accept is clicked", async () => {
    installProposals();
    const onAccepted = vi.fn();

    render(
      <AppProviders>
        <MasterCvProposals workspaceId="workspace-master-cv" onAccepted={onAccepted} />
      </AppProviders>,
    );

    const refreshButton = await screen.findByRole("button", { name: "Refresh" });
    const acceptButton = await screen.findByRole("button", { name: "Accept" });
    const rejectButton = screen.getByRole("button", { name: "Reject" });
    expect(refreshButton.classList).toContain("button--quiet");
    expect(acceptButton.classList).toContain("button--primary");
    expect(rejectButton.classList).toContain("button--danger");

    fireEvent.click(acceptButton);

    await waitFor(() => expect(onAccepted).toHaveBeenCalledTimes(1));
    expect(onAccepted).toHaveBeenCalledWith(PROPOSAL);
  });
});
