import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { PendingDuplicateSuggestion } from "../../api/client";
import { PendingDuplicateSuggestions } from "./PendingDuplicateSuggestions";
import { copiesWorthKeepingInstead, sharesPostingText } from "./pendingDuplicateSuggestionModel";

const DISMISS_LABEL = "Keep this one, dismiss the job you are reviewing";

const boardCopy = {
  pending_discovery_id: "pending-board",
  employer: "Globex",
  title: "Data Steward",
  location: "Northtown",
  discovery_url: "https://careers.globex.example.test/jobs/4412",
  signal: "retained_body_digest",
  keeper_pending_discovery_id: "pending-board",
  keeper_basis: "employer_board_source",
  dismissed: false,
} satisfies PendingDuplicateSuggestion;

describe("PendingDuplicateSuggestions", () => {
  it("names the suggested keeper and hands its id to the dismissal", () => {
    const onDismiss = vi.fn();
    render(
      <PendingDuplicateSuggestions
        onDismissAsDuplicateOf={onDismiss}
        pendingDiscoveryId="pending-aggregator"
        suggestions={[boardCopy]}
      />,
    );

    expect(
      screen.getByText(
        /same opening as the found job below\. The one marked Keep is suggested, because it comes from a saved employer board/,
      ),
    ).toBeTruthy();
    const table = screen.getByRole("table", { name: "Possible duplicates of this found job" });
    expect(within(table).getByText("Same posting text, from a different site")).toBeTruthy();
    expect(within(table).getByText("Keep")).toBeTruthy();
    fireEvent.click(within(table).getByRole("button", { name: DISMISS_LABEL }));

    expect(onDismiss).toHaveBeenCalledWith("pending-board");
  });

  it("offers no dismissal when this row is the one to keep, and says where to act", () => {
    render(
      <PendingDuplicateSuggestions
        onDismissAsDuplicateOf={() => undefined}
        pendingDiscoveryId="pending-board"
        suggestions={[
          {
            ...boardCopy,
            pending_discovery_id: "pending-aggregator",
            keeper_basis: "first_seen",
            dismissed: true,
          },
        ]}
      />,
    );

    expect(
      screen.getByText(
        /Keep this one, because it was found first\. Dismiss the others from their own rows/,
      ),
    ).toBeTruthy();
    expect(screen.getByText("Dismissed")).toBeTruthy();
    expect(screen.queryByRole("button", { name: DISMISS_LABEL })).toBeNull();
  });

  it("offers no dismissal on a match that rests on a guessed employer", () => {
    render(
      <PendingDuplicateSuggestions
        onDismissAsDuplicateOf={() => undefined}
        pendingDiscoveryId="pending-aggregator"
        suggestions={[{ ...boardCopy, signal: "title_and_place_hint" }]}
      />,
    );

    expect(
      screen.getByText("Same title and place; the employer on one of them is a guess"),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: DISMISS_LABEL })).toBeNull();
  });

  it("says the employer is not stated instead of naming the ATS tenant", () => {
    render(
      <PendingDuplicateSuggestions
        onDismissAsDuplicateOf={() => undefined}
        pendingDiscoveryId="pending-aggregator"
        suggestions={[
          {
            ...boardCopy,
            employer: "",
            discovery_url: "https://job-boards.greenhouse.io/globex/jobs/4412",
          },
        ]}
      />,
    );

    const table = screen.getByRole("table", { name: "Possible duplicates of this found job" });
    expect(within(table).getByText("Employer not stated")).toBeTruthy();
    expect(within(table).getByText("Listed on job-boards.greenhouse.io")).toBeTruthy();
    expect(within(table).queryByText("globex")).toBeNull();
    expect(
      within(table).getByRole("link", {
        name: "Data Steward, employer not stated, listed on job-boards.greenhouse.io",
      }),
    ).toBeTruthy();
  });

  it("renders nothing for a row with no suggestion", () => {
    const { container } = render(
      <PendingDuplicateSuggestions
        onDismissAsDuplicateOf={() => undefined}
        pendingDiscoveryId="pending-board"
        suggestions={[]}
      />,
    );

    expect(container.textContent).toBe("");
  });

  it("counts only equal posting text as worth a chip", () => {
    expect(sharesPostingText([boardCopy])).toBe(true);
    expect(sharesPostingText([{ ...boardCopy, signal: "title_and_employer" }])).toBe(false);
  });

  it("warns about paying twice only for an open copy suggested to keep instead", () => {
    expect(copiesWorthKeepingInstead("pending-aggregator", [boardCopy])).toBe(1);
    expect(copiesWorthKeepingInstead("pending-board", [boardCopy])).toBe(0);
    expect(
      copiesWorthKeepingInstead("pending-aggregator", [{ ...boardCopy, dismissed: true }]),
    ).toBe(0);
    expect(
      copiesWorthKeepingInstead("pending-aggregator", [
        { ...boardCopy, signal: "title_and_place_hint" },
      ]),
    ).toBe(0);
  });
});
