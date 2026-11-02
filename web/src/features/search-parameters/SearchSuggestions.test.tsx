import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, delay, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import type {
  ProposalRejectionReason,
  SearchArchetypeProposal,
  SearchClusterProposal,
  SearchTitleProposal,
  SearchTitleProposalInbox,
} from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { SearchSuggestions } from "./SearchSuggestions";

// Keyed by the generated vocabulary, so a reason added to the wire contract
// fails typecheck here until the panel offers it somewhere. Deferring is the
// Save for later button, so it is not an option in the Don't use panel.
const EVERY_REASON: Record<ProposalRejectionReason, true> = {
  not_relevant: true,
  wrong_level: true,
  wrong_mandate: true,
  wrong_employer_type: true,
  duplicate: true,
  defer: true,
  other: true,
};

const WORKSPACE = "*/api/v1/workspaces/:workspaceId";

type Capacity = SearchTitleProposalInbox["capacities"][number];

const COMMON = {
  correlation_id: "task-1",
  decided_at: null,
  evidence_ids: ["private-entry-id"],
  is_current_master_cv_version: true,
  master_cv_version_id: "cv-version-1",
  origin: "search.vocabulary",
  proposed_at: "2026-08-10T00:00:00Z",
  rejection_reason: null,
};

const TITLES = [
  {
    ...COMMON,
    id: "title-1",
    lane: "specialist",
    reason: "Owned streaming ingestion.",
    state: "proposed",
    title: "Streaming Platform Engineer",
  },
  {
    ...COMMON,
    decided_at: "2026-08-11T00:00:00Z",
    id: "title-2",
    lane: null,
    reason: "Matches prior technical leadership.",
    state: "accepted",
    title: "Technical Program Lead",
  },
] satisfies SearchTitleProposal[];

const ARCHETYPES = [
  {
    ...COMMON,
    because: "Product infrastructure benefits from the candidate's background.",
    id: "archetype-1",
    name: "Developer platform company",
    state: "proposed",
  },
] satisfies SearchArchetypeProposal[];

const CLUSTERS = [
  {
    ...COMMON,
    because: "Repeated evidence of incident coordination.",
    id: "cluster-1",
    kind: "capability",
    name: "Incident leadership",
    state: "proposed",
    terms: ["incident command", "service recovery"],
  },
  {
    ...COMMON,
    because: "Supported retail logistics systems.",
    decided_at: "2026-08-10T12:00:00Z",
    id: "cluster-2",
    is_current_master_cv_version: false,
    kind: "domain",
    name: "Retail logistics",
    rejection_reason: "defer",
    state: "rejected",
    terms: [],
  },
] satisfies SearchClusterProposal[];

const TITLE_CAPACITY = {
  active_count: 1,
  kind: "title",
  limit: 8,
  remaining: 7,
} satisfies Capacity;

interface SuggestionReads {
  /** Hold every ruling open, for the one-card saving state. */
  rulingPending?: boolean;
  clusterFailure?: boolean;
}

/** The parsed body of every ruling the panel posted. */
function installSuggestions(reads: SuggestionReads = {}): unknown[] {
  const ruled: unknown[] = [];
  const rule = async (
    request: Request,
    body: SearchTitleProposal | SearchArchetypeProposal | SearchClusterProposal,
  ) => {
    if (reads.rulingPending) await delay("infinite");
    ruled.push(request.headers.get("Content-Type") ? await request.json() : null);
    return HttpResponse.json(body);
  };
  server.use(
    http.post(`${WORKSPACE}/search-title-proposals/:proposalId/:ruling`, ({ request }) =>
      rule(request, { ...TITLES[0]!, rejection_reason: "defer", state: "rejected" }),
    ),
    http.post(`${WORKSPACE}/search-archetype-proposals/:proposalId/:ruling`, ({ request }) =>
      rule(request, { ...ARCHETYPES[0]!, rejection_reason: "duplicate", state: "rejected" }),
    ),
    http.post(`${WORKSPACE}/search-cluster-proposals/:proposalId/:ruling`, ({ request }) =>
      rule(request, { ...CLUSTERS[0]!, rejection_reason: "defer", state: "rejected" }),
    ),
    http.get(`${WORKSPACE}/search-title-proposals`, () =>
      HttpResponse.json({ capacities: [TITLE_CAPACITY], items: TITLES }),
    ),
    http.get(`${WORKSPACE}/search-archetype-proposals`, () =>
      HttpResponse.json({
        capacities: [{ active_count: 1, kind: "employer_archetype", limit: 6, remaining: 5 }],
        items: ARCHETYPES,
      }),
    ),
    http.get(`${WORKSPACE}/search-cluster-proposals`, () => {
      if (reads.clusterFailure) {
        return HttpResponse.json({ detail: "Cluster suggestions unavailable" }, { status: 500 });
      }
      return HttpResponse.json({
        capacities: [
          { active_count: 1, kind: "capability", limit: 6, remaining: 5 },
          { active_count: 0, kind: "domain", limit: 6, remaining: 6 },
        ],
        items: CLUSTERS,
      });
    }),
  );
  return ruled;
}

function panel() {
  render(
    <AppProviders>
      <SearchSuggestions workspaceId="workspace-suggestions" />
    </AppProviders>,
  );
}

describe("SearchSuggestions", () => {
  it("explains the bounded, future-facing inbox and groups proposed suggestions", async () => {
    installSuggestions();
    panel();

    expect(
      await screen.findByRole("heading", { name: "Suggestions from search vocabulary" }),
    ).toBeTruthy();
    expect(screen.getByText(/Decisions save at once/)).toBeTruthy();
    expect(await screen.findByText("Streaming Platform Engineer")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "How suggestions and review limits work" }));
    expect(screen.getByText(/widens later searches only after you approve it/i)).toBeTruthy();
    expect(screen.getByText(/merges or deletes nothing/)).toBeTruthy();
    expect(screen.getByText(/Review inbox - Job titles: 1 awaiting review, limit 8/)).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Needs review (3)" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Approved (1)" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Not using (1)" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Job titles" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Work you do" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Employer types" })).toBeTruthy();
    expect(screen.getByText("1 awaiting review; inbox limit 8; room for 7 new")).toBeTruthy();
    expect(screen.getAllByText(/Based on 1 master CV entry/)).toHaveLength(3);
    expect(screen.queryByText("private-entry-id")).toBeNull();

    const titleCard = screen.getByText("Streaming Platform Engineer").closest("li");
    expect(titleCard).toBeTruthy();
    expect(
      within(titleCard!).getByText("Role track: individual contributor or specialist"),
    ).toBeTruthy();
    expect(within(titleCard!).getByText(/Later search campaigns, board scans/)).toBeTruthy();
    const rationale = within(titleCard!).getByLabelText(
      "Why TailorCV suggested Streaming Platform Engineer",
    );
    fireEvent.click(rationale);
    expect(within(titleCard!).getByText("Owned streaming ingestion.")).toBeTruthy();
  });

  it("separates accepted vocabulary from review inbox capacity", async () => {
    installSuggestions();
    panel();
    expect(await screen.findByText("Streaming Platform Engineer")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "How suggestions and review limits work" }));
    expect(screen.getByText(/Approvals do not count against the review limit/)).toBeTruthy();
    expect(screen.getByText("1 awaiting review; inbox limit 8; room for 7 new")).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Approved (1)" })).toBeTruthy();
    expect(screen.queryByText(/active set is reduced/)).toBeNull();
  });

  it("shows durable used and deferred decisions with reversible actions", async () => {
    installSuggestions();
    panel();

    fireEvent.click(await screen.findByRole("tab", { name: "Approved (1)" }));
    expect(screen.getByText("Technical Program Lead")).toBeTruthy();
    expect(screen.getByText("Approved for future searches")).toBeTruthy();
    expect(screen.getByLabelText("Withdraw approval for Technical Program Lead")).toBeTruthy();

    fireEvent.click(screen.getByRole("tab", { name: "Not using (1)" }));
    expect(screen.getByText("Retail logistics")).toBeTruthy();
    expect(screen.getByText("Saved for later; not used")).toBeTruthy();
    expect(screen.getByText(/decision was made from an earlier master CV/i)).toBeTruthy();
    expect(
      screen.getByText(/undecided suggestions from older CVs are not carried forward/i),
    ).toBeTruthy();
    expect(screen.getByText(/Decision saved Aug 10, 2026/i)).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Approve Retail logistics for future searches" }),
    ).toBeTruthy();
  });

  it("offers every rejection reason the backend accepts, and defers outside them", async () => {
    installSuggestions();
    panel();
    await screen.findByText("Streaming Platform Engineer");

    const optionsFor = (name: string) => {
      const card = screen.getAllByText(name)[0]!.closest("li")!;
      fireEvent.click(within(card).getByLabelText(`Do not use ${name}`));
      const select = within(card).getByLabelText<HTMLSelectElement>(`Reason not to use ${name}`);
      return [...select.options].map((option) => option.value);
    };
    const title = optionsFor("Streaming Platform Engineer");
    const cluster = optionsFor("Incident leadership");
    const archetype = optionsFor("Developer platform company");

    expect([...new Set([...title, ...cluster, ...archetype])].sort()).toEqual(
      Object.keys(EVERY_REASON)
        .filter((reason) => reason !== "defer")
        .sort(),
    );
    expect(title).not.toContain("defer");
    expect(title).not.toContain("duplicate");
    expect(cluster).toContain("duplicate");
    expect(archetype).toContain("duplicate");
    const card = screen.getByText("Streaming Platform Engineer").closest("li")!;
    expect(
      within(card).getByRole("button", {
        name: "Save Streaming Platform Engineer for later without using it",
      }),
    ).toBeTruthy();
  });

  it("records decide-later and duplicate as truthful structured rejections", async () => {
    const ruled = installSuggestions();
    panel();
    const card = (await screen.findByText("Streaming Platform Engineer")).closest("li");
    expect(card).toBeTruthy();

    fireEvent.click(
      within(card!).getByRole("button", { name: /Save .* for later without using it/ }),
    );
    await waitFor(() => expect(ruled).toEqual([{ reason: "defer" }]));

    const moreRuled = installSuggestions();
    panel();
    const archetypeCard = (await screen.findAllByText("Developer platform company"))[0]!.closest(
      "li",
    );
    fireEvent.click(within(archetypeCard!).getByLabelText("Do not use Developer platform company"));
    fireEvent.click(
      within(archetypeCard!).getByRole("button", {
        name: "Do not use Developer platform company because it is a duplicate",
      }),
    );
    await waitFor(() => expect(moreRuled[0]).toEqual({ reason: "duplicate" }));
  });

  it("disables only the card whose ruling is being saved", async () => {
    installSuggestions({ rulingPending: true });
    panel();

    const first = await screen.findByRole("button", {
      name: "Approve Streaming Platform Engineer for future searches",
    });
    const second = screen.getByRole("button", {
      name: "Approve Developer platform company for future searches",
    });
    fireEvent.click(first);

    await waitFor(() => expect(first.hasAttribute("disabled")).toBe(true));
    expect(second.hasAttribute("disabled")).toBe(false);
  });

  it("shows one shared cluster failure without claiming either cluster group is empty", async () => {
    installSuggestions({ clusterFailure: true });
    panel();

    // The query client retries once after react-query's default 1000ms backoff
    // before the error shows, so drive that wait with fake timers.
    vi.useFakeTimers();
    try {
      await vi.advanceTimersByTimeAsync(1_100);
    } finally {
      vi.useRealTimers();
    }
    expect(await screen.findByText(/Cluster suggestions unavailable|Request failed/)).toBeTruthy();
    expect(
      screen.getAllByText("Unavailable while these suggestions could not be read."),
    ).toHaveLength(2);
    expect(screen.queryByText("No work you do in this view.")).toBeNull();
    expect(screen.queryByText("No subject matter you know in this view.")).toBeNull();
  });
});
