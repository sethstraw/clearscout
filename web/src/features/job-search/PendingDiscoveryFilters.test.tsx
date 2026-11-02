import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { PendingDiscoveryFacets } from "../../api/client";
import { HELP } from "../../help/content";
import { PendingDiscoveryFilters } from "./PendingDiscoveryFilters";
import { pendingDiscoveryRow, pendingDiscoveryRows, queueFacets } from "./pendingDiscoveryFixtures";
import { DEFAULT_QUEUE_FILTERS, type PendingQueueFilters } from "./pendingDiscoveryFilterModel";

function filtersView(
  filters: PendingQueueFilters,
  changes: PendingQueueFilters[] = [],
  facets: PendingDiscoveryFacets = {
    ...queueFacets([]),
    constraint: [
      { value: "pursue_only", count: 1 },
      { value: "needs_clarification", count: 7 },
      { value: "investigate", count: 5 },
      { value: "needs_verification", count: 5 },
      { value: "uncertain_comp", count: 5 },
      { value: "excluded", count: 1 },
      { value: "excluded_only", count: 1 },
    ],
  },
  queueTotal = 9,
  strictUncertainty = true,
) {
  return (
    <PendingDiscoveryFilters
      countPending={false}
      employers={[]}
      facets={facets}
      filters={filters}
      laneStatus={null}
      matching={
        filters.constraint === "all"
          ? facets.constraint_total
          : (facets.constraint.find((entry) => entry.value === filters.constraint)?.count ?? 0)
      }
      onChange={(next) => changes.push(next)}
      onPageSizeChange={() => undefined}
      onSearchTextChange={() => undefined}
      onSortChange={() => undefined}
      onStrictUncertaintyChange={() => undefined}
      pageSize="50"
      queueTotal={queueTotal}
      searchText=""
      sortMode="relevance"
      strictUncertainty={strictUncertainty}
    />
  );
}

describe("PendingDiscoveryFilters", () => {
  it("keeps unconfirmed matches in advanced filters by default and exposes screened out rows", () => {
    const rows = [
      pendingDiscoveryRow({ constraint_confidence: "needs_verification" }),
      pendingDiscoveryRow({ id: "pending-2", constraint_confidence: "uncertain_comp" }),
      pendingDiscoveryRow({ id: "pending-3", constraint_confidence: "excluded" }),
      pendingDiscoveryRow({ id: "pending-4", constraint_confidence: "pursue", favorite: true }),
    ];
    const changes: PendingQueueFilters[] = [];
    const view = render(filtersView(DEFAULT_QUEUE_FILTERS, changes, queueFacets(rows), 4, false));
    expect(screen.getByRole("checkbox", { name: "Separate uncertain postings" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Main queue (3)" }).ariaPressed).toBe("true");
    expect(screen.queryByRole("button", { name: /Unconfirmed matches/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "About uncertain postings" }));
    expect(
      within(screen.getByRole("dialog", { name: "About uncertain postings" })).getByText(
        /location or pay, stay in Main queue/,
      ),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Screened out (1)" }));
    expect(changes).toEqual([{ ...DEFAULT_QUEUE_FILTERS, constraint: "excluded_only" }]);
    view.rerender(
      filtersView(
        { ...DEFAULT_QUEUE_FILTERS, constraint: "excluded_only" },
        changes,
        queueFacets(rows),
        4,
        false,
      ),
    );
    expect(screen.getByRole("button", { name: "Screened out (1)" }).ariaPressed).toBe("true");
    fireEvent.click(screen.getByText(/^More filters/));
    const route = screen.getByRole("combobox", { name: "Constraint route" });
    expect(within(route).getByRole("option", { name: "Screened out (1)" })).toBeTruthy();
    expect(within(route).getByRole("option", { name: "Excluded (2)" })).toBeTruthy();
  });

  it("keeps identical main and unconfirmed counts in the advanced route and names its active filter", () => {
    const rows = pendingDiscoveryRows(116).map((row) =>
      pendingDiscoveryRow({
        ...row,
        constraint_confidence: "needs_verification",
        readiness: "no_body",
        content_status: "not_yet_read",
        retained_content_id: null,
      }),
    );
    const filters = { ...DEFAULT_QUEUE_FILTERS, readiness: "no_body" } as const;
    const changes: PendingQueueFilters[] = [];
    const facets = queueFacets(rows, new URLSearchParams({ readiness: "no_body" }));
    const view = render(filtersView(filters, changes, facets, rows.length, false));
    expect(screen.getByRole("button", { name: "Main queue (116)" }).ariaPressed).toBe("true");
    expect(screen.queryByRole("button", { name: /Unconfirmed matches/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Screened out (0)" })).toBeTruthy();
    const summary = screen.getByText("More filters");
    fireEvent.click(summary);
    const route = screen.getByRole("combobox", { name: "Constraint route" });
    expect(within(route).getByRole("option", { name: "Unconfirmed matches (116)" })).toBeTruthy();
    fireEvent.change(route, { target: { value: "needs_clarification" } });
    expect(changes).toEqual([{ ...filters, constraint: "needs_clarification" }]);
    view.rerender(
      filtersView(
        { ...filters, constraint: "needs_clarification" },
        changes,
        facets,
        rows.length,
        false,
      ),
    );
    fireEvent.click(summary);
    expect(summary.closest("details")?.open).toBe(false);
    expect(summary.textContent).toBe("More filters: Constraint route: Unconfirmed matches");
    fireEvent.click(summary);
    expect(
      screen.getByRole<HTMLSelectElement>("combobox", { name: "Constraint route" }).value,
    ).toBe("needs_clarification");
    expect(
      screen.getByRole<HTMLSelectElement>("combobox", { name: "Content readiness" }).value,
    ).toBe("no_body");
    expect(changes).toHaveLength(1);
  });

  it("explains that separating uncertainty keeps unconfirmed postings in their own group", () => {
    render(filtersView({ ...DEFAULT_QUEUE_FILTERS, constraint: "pursue_only" }));
    fireEvent.click(screen.getByRole("button", { name: "About uncertain postings" }));
    expect(
      screen.getByText(/Unconfirmed matches holds postings with missing or unconfirmed facts/),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Main queue (1)" }).ariaPressed).toBe("true");
    expect(screen.getByRole("button", { name: "Unconfirmed matches (7)" }).ariaPressed).toBe(
      "false",
    );
  });
  it("keeps primary controls visible and secondary filters in a closed native disclosure", () => {
    render(filtersView({ ...DEFAULT_QUEUE_FILTERS, constraint: "pursue_only" }));
    expect(screen.getByRole("searchbox", { name: "Search" })).toBeTruthy();
    for (const name of ["Content readiness", "Relevance verdict", "Sort by", "Rows per page"]) {
      expect(screen.getByRole("combobox", { name })).toBeTruthy();
    }
    const summary = screen.getByText("More filters");
    expect(summary.tagName).toBe("SUMMARY");
    const disclosure = summary.closest("details");
    expect(disclosure?.open).toBe(false);
    fireEvent.click(summary);
    expect(disclosure?.open).toBe(true);
    expect(screen.getByRole("combobox", { name: "Constraint route" })).toBeTruthy();
  });

  it("names active secondary filters and preserves their values when closed and reopened", () => {
    const filters = {
      ...DEFAULT_QUEUE_FILTERS,
      constraint: "excluded",
      reviewStatus: "out_of_date",
    } as const;
    const changes: PendingQueueFilters[] = [];
    const view = render(filtersView(filters, changes));
    const summary = screen.getByText(
      "More filters: Constraint route: Excluded; Review status: Review out of date",
    );
    fireEvent.click(summary);
    const route = screen.getByRole<HTMLSelectElement>("combobox", { name: "Constraint route" });
    fireEvent.change(route, { target: { value: "needs_clarification" } });
    expect(changes).toEqual([{ ...filters, constraint: "needs_clarification" }]);
    view.rerender(filtersView({ ...filters, constraint: "needs_clarification" }, changes));
    fireEvent.click(summary);
    expect(summary.closest("details")?.open).toBe(false);
    expect(summary.textContent).toBe(
      "More filters: Constraint route: Unconfirmed matches; Review status: Review out of date",
    );
    fireEvent.click(summary);
    expect(
      screen.getByRole<HTMLSelectElement>("combobox", { name: "Constraint route" }).value,
    ).toBe("needs_clarification");
    expect(screen.getByRole<HTMLSelectElement>("combobox", { name: "Review status" }).value).toBe(
      "out_of_date",
    );
    expect(changes).toHaveLength(1);
  });

  it("leaves view explanations to the Review help panel rather than the toolbar", () => {
    render(filtersView({ ...DEFAULT_QUEUE_FILTERS, constraint: "pursue_only" }));
    expect(screen.queryByText("About this view")).toBeNull();
    expect(screen.queryByText(/Track saves a job/)).toBeNull();
    const help = HELP["find.review"].body;
    expect(help).toMatch(/Separate uncertain postings/);
    expect(help).toMatch(/Screened out filter lists postings that conflict with your search rules/);
    expect(help).toMatch(/Refresh list reads from the top/);
    expect(help).toMatch(/Keys: j and k move, e evaluates, r reviews, x dismisses, u undoes/);
  });

  it("shows full clarification counts beyond 50 rows across content states", () => {
    const rows = pendingDiscoveryRows(72).map((row, index) => {
      const readiness = index % 3 === 0 ? "ready" : index % 3 === 1 ? "no_body" : "access_blocked";
      return pendingDiscoveryRow({
        ...row,
        constraint_confidence:
          index === 0
            ? "pursue"
            : index === 71
              ? "excluded"
              : (["investigate", "needs_verification", "uncertain_comp"] as const)[index % 3],
        favorite: index === 1 || index === 71,
        readiness,
        content_status:
          readiness === "ready"
            ? "readable"
            : readiness === "no_body"
              ? "not_yet_read"
              : "access_blocked",
        retained_content_id: readiness === "ready" ? row.retained_content_id : null,
      });
    });
    const filters = { ...DEFAULT_QUEUE_FILTERS, constraint: "pursue_only" } as const;
    const view = render(
      filtersView(
        filters,
        [],
        queueFacets(rows, new URLSearchParams({ constraint: "pursue_only" })),
        rows.length,
      ),
    );
    expect(screen.getByRole("button", { name: "Unconfirmed matches (70)" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Main queue (1)" })).toBeTruthy();
    view.rerender(
      filtersView(
        { ...filters, readiness: "ready" },
        [],
        queueFacets(rows, new URLSearchParams({ constraint: "pursue_only", readiness: "ready" })),
        rows.length,
      ),
    );
    expect(screen.getByRole("button", { name: "Unconfirmed matches (23)" })).toBeTruthy();
  });

  it("uses the clarification group count and preserves an explicit content scope", () => {
    const filters = {
      ...DEFAULT_QUEUE_FILTERS,
      constraint: "pursue_only",
      readiness: "no_body",
    } as const;
    const changes: PendingQueueFilters[] = [];
    render(filtersView(filters, changes));
    const groups = screen.getByRole("group", { name: "Uncertainty groups" });
    fireEvent.click(within(groups).getByRole("button", { name: "Unconfirmed matches (7)" }));
    expect(changes).toEqual([{ ...filters, constraint: "needs_clarification" }]);
    fireEvent.click(screen.getByText(/^More filters/));
    const route = screen.getByRole("combobox", { name: "Constraint route" });
    expect(
      within(route)
        .getAllByRole<HTMLOptionElement>("option")
        .map((option) => option.value),
    ).toEqual(["pursue_only", "needs_clarification", "excluded_only", "excluded"]);
  });

  it("labels the all-routes view for an added posting without selecting the strict main group", () => {
    render(filtersView({ ...DEFAULT_QUEUE_FILTERS, constraint: "all" }));
    expect(
      screen.getByText(
        "Showing all constraint routes to reveal the posting you added. Choose Main queue to return to strict view.",
      ),
    ).toBeTruthy();
    fireEvent.click(screen.getByText(/^More filters/));
    expect(
      screen.getByRole<HTMLSelectElement>("combobox", { name: "Constraint route" }).value,
    ).toBe("all");
    expect(
      screen.getByRole("button", { name: "Main queue (1)" }).getAttribute("aria-pressed"),
    ).toBe("false");
  });
});
