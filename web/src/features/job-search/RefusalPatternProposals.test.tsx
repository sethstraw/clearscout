import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import type { RefusalPatternProposal } from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { RefusalPatternProposals } from "./RefusalPatternProposals";

function proposal(overrides: Partial<RefusalPatternProposal> = {}): RefusalPatternProposal {
  return {
    id: "proposal-1",
    condition: "work_authorization",
    normalized_reason: "us work authorization required",
    reason_examples: [
      "US work authorization required",
      " us   work authorization required ",
      "Visa sponsorship is unavailable",
    ],
    refusal_count: 3,
    refused_postings: [],
    first_seen_at: "2026-08-20T12:00:00Z",
    last_seen_at: "2026-08-22T12:00:00Z",
    state: "proposed",
    proposed_at: "2026-08-22T12:00:00Z",
    decided_at: null,
    ...overrides,
  };
}

/** Render the panel and collect "<proposal id>/<ruling>" for each settled pattern. */
function panel(items: RefusalPatternProposal[]): string[] {
  const rulings: string[] = [];
  server.use(
    http.get("*/api/v1/workspaces/:workspaceId/refusal-pattern-proposals", () =>
      HttpResponse.json(items),
    ),
    http.post(
      "*/api/v1/workspaces/:workspaceId/refusal-pattern-proposals/:proposalId/:ruling",
      ({ params }) => {
        rulings.push(`${String(params.proposalId)}/${String(params.ruling)}`);
        return HttpResponse.json(proposal({ state: "accepted" }));
      },
    ),
  );
  render(
    <AppProviders>
      <RefusalPatternProposals workspaceId="workspace-1" />
    </AppProviders>,
  );
  return rulings;
}

describe("RefusalPatternProposals", () => {
  it("keeps the heading and description in one header group, and content in the panel body", async () => {
    panel([proposal()]);

    const heading = await screen.findByRole("heading", { name: "Recurring refusal patterns" });
    const description = screen.getByText(/settle it here and it stops being proposed/);
    // Both belong to the same header cluster, not siblings of header's flex row.
    expect(heading.parentElement).toBe(description.parentElement);
    expect(heading.closest("header")).toBeTruthy();
    expect(description.closest("header")).toBeTruthy();

    const decidedSummary = await screen.findByText("Condition:");
    expect(decidedSummary.closest(".panel-body")).toBeTruthy();
  });

  it("labels the rule and evidence, removes tautological examples, and confirms acceptance", async () => {
    const rulings = panel([proposal()]);

    expect(await screen.findByText("Condition:")).toBeTruthy();
    expect(screen.getByText(/Work authorization/)).toBeTruthy();
    expect(screen.getByText("Repeated reason:")).toBeTruthy();
    expect(screen.getByText("Other wording:")).toBeTruthy();
    expect(screen.getByText("Visa sponsorship is unavailable")).toBeTruthy();
    expect(screen.queryByText("US work authorization required")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Settle pattern" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent).toContain(
      'you have seen "Work authorization: us work authorization required"',
    );
    expect(dialog.textContent).toContain("nothing is skipped");
    fireEvent.click(within(dialog).getByRole("button", { name: "Settle pattern" }));

    await waitFor(() => expect(rulings).toEqual(["proposal-1/accept"]));
  });

  it("offers no decision when the proposed rule itself cannot be read", async () => {
    panel([proposal({ normalized_reason: "   ", refusal_count: 0 })]);

    expect((await screen.findByRole("alert")).textContent).toContain("could not be read safely");
    expect(screen.queryByRole("button", { name: "Settle pattern" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Keep case by case" })).toBeNull();
  });

  it("offers acceptance on the repeated wording aggregation actually produces", async () => {
    panel([
      proposal({
        condition: "work_authorization",
        normalized_reason: "us work authorization required",
        reason_examples: ["US work authorization required", " us   work authorization required "],
      }),
    ]);

    expect(await screen.findByRole("button", { name: "Settle pattern" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Keep case by case" })).toBeTruthy();
    expect(screen.queryByText("Other wording:")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("lists the postings the pattern refused", async () => {
    panel([
      proposal({
        refused_postings: [
          { employer: "Acme", title: "Data Lead" },
          { employer: "Globex", title: "Platform Analyst" },
        ],
      }),
    ]);

    expect(await screen.findByText("Refused postings:")).toBeTruthy();
    expect(screen.getByText("Acme: Data Lead")).toBeTruthy();
    expect(screen.getByText("Globex: Platform Analyst")).toBeTruthy();
  });

  it("settles a pattern whose reason states only the verdict", async () => {
    const rulings = panel([
      proposal({
        condition: "arrangement",
        normalized_reason: "refused",
        reason_examples: ["refused"],
      }),
    ]);

    fireEvent.click(await screen.findByRole("button", { name: "Settle pattern" }));
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Settle pattern" }));

    await waitFor(() => expect(rulings).toEqual(["proposal-1/accept"]));
  });
});
