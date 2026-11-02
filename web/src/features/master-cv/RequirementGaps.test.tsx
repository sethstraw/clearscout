import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { RequirementGaps as GapSummary, RequirementRuling } from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { RequirementGaps } from "./RequirementGaps";

const WORKSPACE = "*/api/v1/workspaces/:workspaceId";

const GROUPING_RULE =
  "Requirements are grouped by their text after lowercasing and collapsing runs of whitespace to a single space. Grouping does not assert that two postings mean the same thing.";

type GapSource = GapSummary["gaps"][number]["raised_by"][number];

type GapSourceOverrides = {
  importance?: GapSource["importance"];
  requirementKey?: string;
  ruling?: GapSource["ruling"];
};

function gapSource(jobPostingId: string, overrides: GapSourceOverrides = {}): GapSource {
  return {
    job_posting_id: jobPostingId,
    employer: "Globex",
    title: "Staff Engineer",
    importance: overrides.importance ?? "must_have",
    evaluated_at: "2026-08-10T09:00:00Z",
    requirement_key: overrides.requirementKey ?? `req-${jobPostingId}`,
    ruling: overrides.ruling ?? null,
  };
}

const ONE_GAP = {
  evaluations_read: 3,
  gap_count: 1,
  omitted_count: 0,
  grouping_rule: GROUPING_RULE,
  gaps: [
    {
      text: "Five or more years owning a regulated data platform",
      raised_by: [gapSource("job-1"), gapSource("job-2", { importance: "nice_to_have" })],
    },
  ],
} satisfies GapSummary;

const RAISED_BY_COUNT = 2;

const NO_MATCHES = {
  evaluations_read: 3,
  gap_count: 0,
  omitted_count: 0,
  grouping_rule: GROUPING_RULE,
  gaps: [],
} satisfies GapSummary;

const MET_RULING = {
  requirement_key: "req-job-1",
  requirement_text: "Five or more years owning a regulated data platform",
  ruling: "met",
  note: null,
  updated_at: "2026-08-19T09:00:00Z",
} satisfies RequirementRuling;

interface RulingCall {
  method: string;
  key: string;
  body: unknown;
}

interface Recorded {
  /** The query string of every gaps read, so a filter change is visible. */
  queries: string[];
  rulings: RulingCall[];
}

/** Serve the gap reads in order, sticking on the last, and record every ruling write. */
function installGaps(responses: GapSummary | GapSummary[]): Recorded {
  const queue = Array.isArray(responses) ? [...responses] : [responses];
  const recorded: Recorded = { queries: [], rulings: [] };
  server.use(
    http.get(`${WORKSPACE}/master-cv/requirement-gaps`, ({ request }) => {
      recorded.queries.push(new URL(request.url).search);
      const next = queue.length > 1 ? queue.shift() : queue[0];
      return HttpResponse.json(next);
    }),
    http.get(`${WORKSPACE}/requirement-rulings`, () => HttpResponse.json({ rulings: [] })),
    http.put(`${WORKSPACE}/requirement-rulings/:requirementKey`, async ({ params, request }) => {
      recorded.rulings.push({
        method: "PUT",
        key: String(params.requirementKey),
        body: await request.json(),
      });
      return HttpResponse.json(MET_RULING);
    }),
    http.delete(`${WORKSPACE}/requirement-rulings/:requirementKey`, ({ params }) => {
      recorded.rulings.push({ method: "DELETE", key: String(params.requirementKey), body: null });
      return new HttpResponse(null, { status: 204 });
    }),
  );
  return recorded;
}

function renderGaps() {
  return render(
    <MemoryRouter>
      <AppProviders>
        <RequirementGaps workspaceId="workspace-cv" />
      </AppProviders>
    </MemoryRouter>,
  );
}

describe("RequirementGaps", () => {
  it("renders the grouping rule the server stated", async () => {
    installGaps(ONE_GAP);
    renderGaps();

    await waitFor(() => expect(document.body.textContent).toContain(GROUPING_RULE));
  });

  it("threads the minimum-postings filter into the request", async () => {
    const recorded = installGaps([ONE_GAP, ONE_GAP]);
    renderGaps();

    const input = await screen.findByLabelText<HTMLInputElement>("Minimum postings");
    fireEvent.change(input, { target: { value: "3" } });

    await waitFor(() =>
      expect(recorded.queries.some((query) => query.includes("min_raised=3"))).toBe(true),
    );
  });

  it("threads the importance filter into the request", async () => {
    const recorded = installGaps([ONE_GAP, ONE_GAP, ONE_GAP]);
    renderGaps();

    const mustHave = await screen.findByLabelText<HTMLInputElement>("must have");
    fireEvent.click(mustHave);
    const responsibility = screen.getByLabelText<HTMLInputElement>("responsibility");
    fireEvent.click(responsibility);

    await waitFor(() => {
      const filtered = recorded.queries.filter(
        (query) =>
          query.includes("importance=must_have") && query.includes("importance=responsibility"),
      );
      expect(filtered.length).toBeGreaterThan(0);
    });
  });

  it("shows the filtered empty state when a filter returns no groups", async () => {
    installGaps([ONE_GAP, NO_MATCHES]);
    renderGaps();

    await screen.findByText("Five or more years owning a regulated data platform");

    const input = screen.getByLabelText<HTMLInputElement>("Minimum postings");
    fireEvent.change(input, { target: { value: "9" } });

    await waitFor(() =>
      expect(document.body.textContent).toContain("No requirements match these filters."),
    );
    expect(document.body.textContent).not.toContain(
      "Every requirement these evaluations settled was answered from this CV.",
    );
  });

  it("posts a met ruling on Save after opening the note field", async () => {
    const recorded = installGaps(ONE_GAP);
    renderGaps();

    const [firstMarkMet] = await screen.findAllByRole("button", { name: "Mark met" });
    expect(firstMarkMet).toBeDefined();
    fireEvent.click(firstMarkMet!);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(recorded.rulings).toEqual([
        { method: "PUT", key: "req-job-1", body: { ruling: "met", note: null } },
      ]),
    );
  });

  it.each([
    ["disputed_fit", "Disputed (fit) by you"],
    ["disputed_extraction", "Disputed (extraction) by you"],
  ] as const)("shows the dispute kind for %s", async (ruling, label) => {
    const disputed = {
      ...ONE_GAP,
      gaps: [
        {
          text: "Five or more years owning a regulated data platform",
          raised_by: [gapSource("job-1", { ruling })],
        },
      ],
    } satisfies GapSummary;
    installGaps(disputed);
    renderGaps();

    await screen.findByText(label);
  });

  it.each([
    ["Disputed (fit)", "disputed_fit", "paid fit re-match"],
    ["Disputed (extraction)", "disputed_extraction", "paid re-extraction"],
  ] as const)("saves %s with its paid-path explanation", async (label, ruling, explanation) => {
    const recorded = installGaps(ONE_GAP);
    renderGaps();

    const [action] = await screen.findAllByRole("button", { name: label });
    expect(action).toBeDefined();
    const describedBy = action!.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy!)?.textContent).toContain(explanation);
    fireEvent.click(action!);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(recorded.rulings.find((call) => call.method === "PUT")?.body).toMatchObject({
        ruling,
      }),
    );
  });

  it("sends a DELETE when Clear ruling is clicked on a disputed row", async () => {
    const disputed = {
      ...ONE_GAP,
      gaps: [
        {
          text: "Five or more years owning a regulated data platform",
          raised_by: [gapSource("job-1", { ruling: "disputed_fit" })],
        },
      ],
    } satisfies GapSummary;
    const recorded = installGaps(disputed);
    renderGaps();

    const clear = await screen.findByRole("button", { name: "Clear ruling" });
    fireEvent.click(clear);

    await waitFor(() =>
      expect(recorded.rulings).toEqual([{ method: "DELETE", key: "req-job-1", body: null }]),
    );
  });

  it("offers the ruling controls on every gap row", async () => {
    installGaps(ONE_GAP);
    renderGaps();

    await screen.findByText("Five or more years owning a regulated data platform");
    for (const name of ["Mark met", "Not applicable", "Disputed (fit)", "Disputed (extraction)"]) {
      expect(screen.getAllByRole("button", { name })).toHaveLength(RAISED_BY_COUNT);
    }
  });
});
