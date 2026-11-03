import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { components } from "../api/schema";
import { ClosingDates } from "./ClosingDates";

const publisher = {
  statements: [
    { wording: "Applications close: 2026-10-19", basis: "body_label", clarification: "" },
  ],
  needs_clarification: false,
  normalized_value: "2026-10-19",
  precision: "date",
  source_url: "https://example.test/jobs/lead",
  observed_at: "2026-10-08T12:00:00Z",
} satisfies components["schemas"]["PublisherClosingDateResponse"];

describe("ClosingDates", () => {
  it("omits empty compact dates and keeps the full editor out of the summary", () => {
    const view = render(<ClosingDates compact onSave={vi.fn()} />);
    expect(screen.queryByRole("group")).toBeNull();
    view.rerender(
      <ClosingDates compact publisher={publisher} now={new Date("2026-10-18T12:00:00Z")} />,
    );
    expect(screen.getByText("Publisher closing date: 2026-10-19")).toBeTruthy();
    expect(screen.queryByText("Publisher date evidence")).toBeNull();
    expect(screen.queryByText("Edit closing date")).toBeNull();
    view.rerender(
      <ClosingDates
        compact
        publisher={publisher}
        override="2026-10-21"
        now={new Date("2026-10-20T12:00:00Z")}
      />,
    );
    expect(screen.getByText("Your closing date: 2026-10-21")).toBeTruthy();
    expect(screen.getByText("Closing date passed; verify availability.")).toBeTruthy();
  });

  it("reads a board's stated listing end as the end of its listing, not a closing date", () => {
    const listingEnd = {
      statements: [
        {
          wording: "Listed until 2026-11-20 12:00 UTC",
          basis: "listed_until",
          clarification: "",
        },
      ],
      needs_clarification: false,
      normalized_value: "2026-11-20T12:00:00+00:00",
      precision: "timestamp",
      timezone: "+00:00",
      source_url: "",
    } satisfies components["schemas"]["PublisherClosingDateResponse"];
    const view = render(
      <ClosingDates compact publisher={listingEnd} now={new Date("2026-11-21T12:00:00Z")} />,
    );
    expect(
      screen.getByText("Listed on the board until: 2026-11-20T12:00:00+00:00", { exact: false }),
    ).toBeTruthy();
    expect(screen.getByText("The board listing ended.")).toBeTruthy();
    expect(screen.queryByText("Closing date passed; verify availability.")).toBeNull();
    view.rerender(<ClosingDates publisher={listingEnd} now={new Date("2026-11-21T12:00:00Z")} />);
    expect(screen.getByText("Listed on the board until")).toBeTruthy();
    expect(screen.queryByText("Publisher closing date")).toBeNull();
    fireEvent.click(screen.getByText("Publisher date evidence"));
    expect(screen.getByText("Listed until 2026-11-20 12:00 UTC")).toBeTruthy();
  });

  it("does not invent a compact date for rolling or conflicting publisher statements", () => {
    const view = render(
      <ClosingDates
        compact
        publisher={{ ...publisher, normalized_value: null, precision: null }}
      />,
    );
    expect(screen.getByText("Publisher closing date: Stated without a date")).toBeTruthy();
    view.rerender(<ClosingDates compact publisher={{ ...publisher, needs_clarification: true }} />);
    expect(screen.getByText("Publisher closing date: Needs clarification")).toBeTruthy();
    expect(screen.queryByText("Closing date passed; verify availability.")).toBeNull();
  });

  it("keeps publisher dates, personal dates and source evidence separate", () => {
    render(
      <ClosingDates
        publisher={publisher}
        override="2026-10-21"
        now={new Date("2026-10-20T12:00:00Z")}
      />,
    );
    expect(screen.getByText("Publisher closing date")).toBeTruthy();
    expect(screen.getByText("Your closing date")).toBeTruthy();
    expect(screen.getByText("2026-10-19")).toBeTruthy();
    expect(screen.getByText("2026-10-21")).toBeTruthy();
    expect(screen.getByText("Closing date passed; verify availability.")).toBeTruthy();
    fireEvent.click(screen.getByText("Publisher date evidence"));
    expect(screen.getByText("Applications close: 2026-10-19")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Publisher date source" }).getAttribute("href")).toBe(
      publisher.source_url,
    );
  });

  it("recomputes date passage without saving or inventing timestamp zones", () => {
    const save = vi.fn();
    const view = render(
      <ClosingDates publisher={publisher} now={new Date("2026-10-19T12:00:00Z")} onSave={save} />,
    );
    expect(screen.queryByText("Closing date passed; verify availability.")).toBeNull();
    view.rerender(
      <ClosingDates publisher={publisher} now={new Date("2026-10-20T12:00:00Z")} onSave={save} />,
    );
    expect(screen.getByText("Closing date passed; verify availability.")).toBeTruthy();
    view.rerender(
      <ClosingDates
        publisher={{
          ...publisher,
          normalized_value: "2026-10-19T17:00:00",
          precision: "timestamp",
        }}
        now={new Date("2026-10-20T12:00:00Z")}
        onSave={save}
      />,
    );
    expect(screen.getByText("2026-10-19T17:00:00")).toBeTruthy();
    expect(screen.getByText("Timezone not stated")).toBeTruthy();
    expect(screen.queryByText("Closing date passed; verify availability.")).toBeNull();
    expect(save).not.toHaveBeenCalled();
  });

  it("holds conflicting dates for clarification and keeps rolling wording", () => {
    const view = render(<ClosingDates publisher={{ ...publisher, needs_clarification: true }} />);
    expect(screen.getByText("Needs clarification")).toBeTruthy();
    expect(screen.queryByText("Closing date passed; verify availability.")).toBeNull();
    view.rerender(
      <ClosingDates
        publisher={{
          ...publisher,
          normalized_value: null,
          precision: null,
          statements: [{ wording: "Until filled", basis: "body_label", clarification: "" }],
        }}
      />,
    );
    expect(screen.getAllByText("Until filled").length).toBeGreaterThan(0);
    view.rerender(
      <ClosingDates publisher={{ needs_clarification: false, source_url: "", statements: [] }} />,
    );
    expect(screen.getByText("Not stated")).toBeTruthy();
  });

  it("retains a failed draft, saves for free and resets only the personal date", async () => {
    const save = vi.fn().mockRejectedValueOnce(new Error("Synthetic save failed; retry."));
    save.mockResolvedValue(undefined);
    const view = render(<ClosingDates publisher={publisher} onSave={save} />);
    const editor = screen.getByText("Edit closing date");
    fireEvent.click(editor);
    const input = screen.getByLabelText("Your closing date", { selector: "input" });
    fireEvent.change(input, { target: { value: "2026-10-21" } });
    input.focus();
    fireEvent.click(screen.getByRole("button", { name: "Save date" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Synthetic save failed");
    expect((input as HTMLInputElement).value).toBe("2026-10-21");
    expect(input.getAttribute("aria-describedby")).toContain(screen.getByRole("alert").id);
    view.rerender(<ClosingDates publisher={publisher} override="2026-10-22" onSave={save} />);
    expect((input as HTMLInputElement).value).toBe("2026-10-21");
    fireEvent.click(screen.getByRole("button", { name: "Save date" }));
    await waitFor(() => expect(save).toHaveBeenLastCalledWith("2026-10-21"));
    await waitFor(() => expect(document.activeElement).toBe(editor));
    fireEvent.click(editor);
    fireEvent.click(screen.getByRole("button", { name: "Use publisher date" }));
    await waitFor(() => expect(save).toHaveBeenLastCalledWith(null));
    expect(screen.getByText("2026-10-19")).toBeTruthy();
  });

  it("associates validation with the input and does not save an unanswered date", async () => {
    const save = vi.fn();
    render(<ClosingDates onSave={save} />);
    fireEvent.click(screen.getByText("Edit closing date"));
    fireEvent.click(screen.getByRole("button", { name: "Save date" }));
    const error = await screen.findByRole("alert");
    const input = screen.getByLabelText("Your closing date", { selector: "input" });
    expect(error.textContent).toContain("Enter a valid date");
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(input.getAttribute("aria-describedby")).toContain(error.id);
    expect(
      within(screen.getByRole("group", { name: "Closing dates" })).getByText("Not stated"),
    ).toBeTruthy();
    expect(save).not.toHaveBeenCalled();
  });
});
