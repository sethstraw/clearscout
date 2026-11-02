import { useMemo, useState } from "react";

import {
  useRuleOnSearchArchetype,
  useRuleOnSearchCluster,
  useRuleOnSearchTitle,
  useSearchArchetypeProposals,
  useSearchClusterProposals,
  useSearchTitleProposals,
} from "../../api/hooks";
import type { components } from "../../api/schema";
import { useToast } from "../../providers/useToast";
import { ConfirmedActionButton } from "../../components/ConfirmedActionButton";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { TabPanel, Tabs } from "../../components/Tabs";
import { Toggletip } from "../../components/Toggletip";
import { PRODUCT_NAME } from "../../productName";

type SuggestionKind = "title" | "capability" | "domain" | "employer_archetype";
type SuggestionView = "proposed" | "accepted" | "rejected";
type Ruling = "accept" | "reject";
type ProposalRejectionReason = components["schemas"]["ProposalRejectionReason"];
type SearchArchetypeProposal = components["schemas"]["SearchArchetypeProposalResponse"];
type SearchClusterProposal = components["schemas"]["SearchClusterProposalResponse"];
type SearchProposalCapacity = components["schemas"]["SearchProposalCapacityResponse"];
type SearchTitleProposal = components["schemas"]["SearchTitleProposalResponse"];

type Suggestion = {
  because: string;
  correlationId: string | null;
  decidedAt: string | null;
  evidenceCount: number;
  id: string;
  isCurrentMasterCvVersion: boolean;
  kind: SuggestionKind;
  metadata?: string;
  name: string;
  origin: string;
  proposedAt: string;
  rejectionReason?: ProposalRejectionReason | null;
  state: SuggestionView;
};

const VIEWS = [
  { id: "proposed", label: "Needs review" },
  { id: "accepted", label: "Approved" },
  { id: "rejected", label: "Not using" },
] as const;

const GROUPS: ReadonlyArray<{
  description: string;
  kind: SuggestionKind;
  title: string;
}> = [
  {
    kind: "title",
    title: "Job titles",
    description:
      "Search labels for roles worth finding. A suggestion does not claim you previously held that title.",
  },
  {
    kind: "capability",
    title: "Work you do",
    description:
      "Capabilities describe the work itself. The first few you order rank postings; the rest still widen future searches.",
  },
  {
    kind: "domain",
    title: "Subject matter you know",
    description:
      "Domains describe what the work is about. The first few you order rank postings; the rest still widen future searches.",
  },
  {
    kind: "employer_archetype",
    title: "Employer types",
    description:
      "Kinds of organisation or work setting that can guide later searches, not a named employer, industry, or company size.",
  },
];

const REASON_LABELS: Record<ProposalRejectionReason, string> = {
  not_relevant: "Not relevant",
  wrong_level: "Wrong level",
  wrong_mandate: "Wrong kind of work",
  wrong_employer_type: "Wrong employer type",
  duplicate: "Duplicate",
  defer: "Decide later",
  other: "Another reason",
};

function titleSuggestion(item: SearchTitleProposal): Suggestion {
  const lane =
    item.lane === "specialist"
      ? "Role track: individual contributor or specialist"
      : item.lane === "leadership"
        ? "Role track: leadership"
        : undefined;
  return {
    because: item.reason,
    correlationId: item.correlation_id,
    decidedAt: item.decided_at,
    evidenceCount: item.evidence_ids.length,
    id: item.id,
    isCurrentMasterCvVersion: item.is_current_master_cv_version,
    kind: "title",
    metadata: lane,
    name: item.title,
    origin: item.origin,
    proposedAt: item.proposed_at,
    rejectionReason: item.rejection_reason,
    state: item.state,
  };
}

function archetypeSuggestion(item: SearchArchetypeProposal): Suggestion {
  return {
    because: item.because,
    correlationId: item.correlation_id,
    decidedAt: item.decided_at,
    evidenceCount: item.evidence_ids.length,
    id: item.id,
    isCurrentMasterCvVersion: item.is_current_master_cv_version,
    kind: "employer_archetype",
    name: item.name,
    origin: item.origin,
    proposedAt: item.proposed_at,
    rejectionReason: item.rejection_reason,
    state: item.state,
  };
}

function clusterSuggestion(item: SearchClusterProposal): Suggestion {
  return {
    because: item.because,
    correlationId: item.correlation_id,
    decidedAt: item.decided_at,
    evidenceCount: item.evidence_ids.length,
    id: item.id,
    isCurrentMasterCvVersion: item.is_current_master_cv_version,
    kind: item.kind,
    metadata: item.terms.length ? `Suggested search terms: ${item.terms.join(", ")}` : undefined,
    name: item.name,
    origin: item.origin,
    proposedAt: item.proposed_at,
    rejectionReason: item.rejection_reason,
    state: item.state,
  };
}

// A near-duplicate title is packed into an existing title topic as another alias,
// so only the kinds whose names each become their own search anchor offer
// "duplicate". Deferring is the Save for later button, not a reason listed here.
function reasonsFor(kind: SuggestionKind): ProposalRejectionReason[] {
  if (kind === "title") return ["not_relevant", "wrong_level", "wrong_mandate", "other"];
  if (kind === "employer_archetype")
    return ["not_relevant", "wrong_employer_type", "duplicate", "other"];
  return ["not_relevant", "wrong_mandate", "duplicate", "other"];
}

function effectFor(kind: SuggestionKind): string {
  if (kind === "title")
    return "Later search campaigns, board scans, and discovery review may use this title when relevant.";
  if (kind === "employer_archetype")
    return "Later search campaigns and discovery review may use this employer context. Approving it does not start a paid search.";
  if (kind === "capability")
    return "Approving adds this to your capabilities list, where ranking reads the first few you order, and widens later searches.";
  return "Approving adds this to your domains list, where ranking reads the first few you order, and widens later searches.";
}

function originLabel(origin: string): string {
  return origin === "search.vocabulary" ? "search vocabulary" : PRODUCT_NAME;
}

function decisionStatus(item: Suggestion): string {
  if (item.state === "accepted") return "Approved for future searches";
  if (item.rejectionReason === "defer") return "Saved for later; not used";
  if (item.rejectionReason === "duplicate") return "Marked duplicate; not used";
  return item.rejectionReason
    ? `${REASON_LABELS[item.rejectionReason]}; not used`
    : "Not used in future searches";
}

function capacityLabel(capacity: SearchProposalCapacity): string {
  return `${capacity.active_count} awaiting review; inbox limit ${capacity.limit}; room for ${capacity.remaining} new`;
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(new Date(value));
}

export function SearchSuggestions({ workspaceId }: { workspaceId: string }) {
  const { showToast } = useToast();
  const [view, setView] = useState<SuggestionView>("proposed");
  const titleQuery = useSearchTitleProposals(workspaceId);
  const archetypeQuery = useSearchArchetypeProposals(workspaceId);
  const clusterQuery = useSearchClusterProposals(workspaceId);
  const titleRule = useRuleOnSearchTitle(workspaceId);
  const archetypeRule = useRuleOnSearchArchetype(workspaceId);
  const clusterRule = useRuleOnSearchCluster(workspaceId);

  const suggestions = useMemo(
    () => [
      ...(titleQuery.data?.items ?? []).map(titleSuggestion),
      ...(clusterQuery.data?.items ?? []).map(clusterSuggestion),
      ...(archetypeQuery.data?.items ?? []).map(archetypeSuggestion),
    ],
    [archetypeQuery.data?.items, clusterQuery.data?.items, titleQuery.data?.items],
  );
  const capacities = useMemo(() => {
    const all = [
      ...(titleQuery.data?.capacities ?? []),
      ...(clusterQuery.data?.capacities ?? []),
      ...(archetypeQuery.data?.capacities ?? []),
    ];
    return new Map(all.map((capacity) => [capacity.kind, capacity]));
  }, [archetypeQuery.data?.capacities, clusterQuery.data?.capacities, titleQuery.data?.capacities]);

  const counts = {
    proposed: suggestions.filter((item) => item.state === "proposed").length,
    accepted: suggestions.filter((item) => item.state === "accepted").length,
    rejected: suggestions.filter((item) => item.state === "rejected").length,
  };
  const tabs = VIEWS.map((item) => ({ ...item, label: `${item.label} (${counts[item.id]})` }));
  const capacitySummary = GROUPS.flatMap((group) => {
    const capacity = capacities.get(group.kind);
    return capacity
      ? [`${group.title}: ${capacity.active_count} awaiting review, limit ${capacity.limit}`]
      : [];
  }).join(" | ");
  const loading = titleQuery.isLoading || archetypeQuery.isLoading || clusterQuery.isLoading;

  const rule = (item: Suggestion, ruling: Ruling, reason?: ProposalRejectionReason | null) => {
    const variables = { proposalId: item.id, ruling, reason };
    const feedback = { onSuccess: () => showToast(`Decision saved for ${item.name}.`, "success") };
    if (item.kind === "title") titleRule.mutate(variables, feedback);
    else if (item.kind === "employer_archetype") archetypeRule.mutate(variables, feedback);
    else clusterRule.mutate(variables, feedback);
  };
  const ruleAll = (
    items: Suggestion[],
    ruling: Ruling,
    reason?: ProposalRejectionReason | null,
  ) => {
    for (const item of items) rule(item, ruling, reason);
  };
  const anyRulePending = titleRule.isPending || archetypeRule.isPending || clusterRule.isPending;
  const isPending = (item: Suggestion) => {
    if (item.kind === "title")
      return titleRule.isPending && titleRule.variables?.proposalId === item.id;
    if (item.kind === "employer_archetype")
      return archetypeRule.isPending && archetypeRule.variables?.proposalId === item.id;
    return clusterRule.isPending && clusterRule.variables?.proposalId === item.id;
  };

  return (
    <section aria-labelledby="search-suggestions-heading" className="panel search-suggestions">
      <header>
        <div>
          <h3 id="search-suggestions-heading">Suggestions from search vocabulary</h3>
          <p>
            Approve useful terms for later searches. Decisions save at once.{" "}
            <Toggletip label="How suggestions and review limits work">
              <p>
                A suggestion widens later searches only after you approve it. A search never uses a
                suggestion from its own run. Save for later keeps one without using it. Not using
                frees its review slot, and the same suggestion is not offered again. Duplicate is a
                Not using decision and merges or deletes nothing. Withdrawing an approval moves it
                to Not using. You can approve anything in Not using later. Approvals do not count
                against the review limit. Decisions change no existing jobs.
              </p>
              {capacitySummary ? (
                <p className="search-suggestion-capacity-summary">
                  Review inbox - {capacitySummary}. Approving or declining a suggestion frees its
                  review slot.
                </p>
              ) : null}
            </Toggletip>
          </p>
        </div>
      </header>
      <div className="panel-body search-suggestions__body">
        {loading ? <LoadingState label="Reading search suggestions" /> : null}
        {!loading ? (
          <>
            <Tabs
              id="search-suggestion-views"
              items={tabs}
              label="Search suggestion views"
              onSelect={(id) => setView(id as SuggestionView)}
              selected={view}
            />
            <TabPanel id="search-suggestion-views" selected={view}>
              {clusterQuery.error != null ? <ErrorState error={clusterQuery.error} /> : null}
              {GROUPS.map((group) => {
                const items = suggestions.filter(
                  (item) => item.kind === group.kind && item.state === view,
                );
                const capacity = capacities.get(group.kind);
                const queryError =
                  group.kind === "title"
                    ? titleQuery.error
                    : group.kind === "employer_archetype"
                      ? archetypeQuery.error
                      : null;
                const unavailable =
                  clusterQuery.error != null &&
                  (group.kind === "capability" || group.kind === "domain");
                if (!items.length && !queryError && !unavailable) return null;
                return (
                  <SuggestionGroup
                    bulkPending={anyRulePending}
                    capacity={capacity}
                    description={group.description}
                    error={queryError}
                    items={items}
                    key={group.kind}
                    onRule={rule}
                    onRuleAll={ruleAll}
                    pending={isPending}
                    title={group.title}
                    unavailable={unavailable}
                    view={view}
                  />
                );
              })}
              {suggestions.filter((item) => item.state === view).length === 0 &&
              !titleQuery.isError &&
              !archetypeQuery.isError &&
              !clusterQuery.isError ? (
                <EmptyState>
                  {view === "proposed"
                    ? "All suggestions are reviewed. Search vocabulary creates more only when it finds a new evidence-backed way to widen the search."
                    : view === "accepted"
                      ? "No suggestions are currently approved for future searches."
                      : "No suggestions are currently saved for later or rejected."}
                </EmptyState>
              ) : null}
              {view === "proposed" && counts.proposed === 0 ? (
                <a className="button button--quiet" href="/find?tab=web">
                  Search the web
                </a>
              ) : null}
            </TabPanel>
          </>
        ) : null}
        {titleRule.error != null ? <ErrorState error={titleRule.error} /> : null}
        {archetypeRule.error != null ? <ErrorState error={archetypeRule.error} /> : null}
        {clusterRule.error != null ? <ErrorState error={clusterRule.error} /> : null}
      </div>
    </section>
  );
}

function SuggestionGroup({
  bulkPending,
  capacity,
  description,
  error,
  items,
  onRule,
  onRuleAll,
  pending,
  title,
  unavailable,
  view,
}: {
  bulkPending: boolean;
  capacity: SearchProposalCapacity | undefined;
  description: string;
  error: unknown;
  items: Suggestion[];
  onRule: (item: Suggestion, ruling: Ruling, reason?: ProposalRejectionReason | null) => void;
  onRuleAll: (items: Suggestion[], ruling: Ruling, reason?: ProposalRejectionReason | null) => void;
  pending: (item: Suggestion) => boolean;
  title: string;
  unavailable: boolean;
  view: SuggestionView;
}) {
  if (error != null)
    return (
      <section className="search-suggestion-group">
        <h4>{title}</h4>
        <ErrorState error={error} />
      </section>
    );
  return (
    <section className="search-suggestion-group">
      <div className="search-suggestion-group__heading">
        <div>
          <h4>{title}</h4>
          <p>{description}</p>
        </div>
        {capacity ? <p className="search-suggestion-capacity">{capacityLabel(capacity)}</p> : null}
      </div>
      {!unavailable && view === "proposed" && items.length > 1 ? (
        <div className="button-row search-suggestion-bulk">
          <button
            className="button button--primary"
            disabled={bulkPending}
            onClick={() => onRuleAll(items, "accept")}
            type="button"
          >
            Approve all {items.length} in {title}
          </button>
          <button
            className="button button--quiet"
            disabled={bulkPending}
            onClick={() => onRuleAll(items, "reject", "defer")}
            type="button"
          >
            Dismiss all (save for later)
          </button>
        </div>
      ) : null}
      {unavailable ? (
        <p className="field-note">Unavailable while these suggestions could not be read.</p>
      ) : items.length ? (
        <ul
          aria-label={`${title}: ${VIEWS.find((item) => item.id === view)?.label}`}
          className="search-suggestion-list"
        >
          {items.map((item) => (
            <SuggestionCard
              item={item}
              key={`${item.kind}-${item.id}`}
              onRule={onRule}
              pending={pending(item)}
            />
          ))}
        </ul>
      ) : (
        <p className="field-note">No {title.toLocaleLowerCase()} in this view.</p>
      )}
    </section>
  );
}

function SuggestionCard({
  item,
  onRule,
  pending,
}: {
  item: Suggestion;
  onRule: (item: Suggestion, ruling: Ruling, reason?: ProposalRejectionReason | null) => void;
  pending: boolean;
}) {
  return (
    <li className="search-suggestion-card">
      <div className="search-suggestion-card__content">
        <div className="search-suggestion-card__title">
          <h5>{item.name}</h5>
          <span className={`search-suggestion-status search-suggestion-status--${item.state}`}>
            {decisionStatus(item)}
          </span>
        </div>
        {item.metadata ? <p className="search-suggestion-card__metadata">{item.metadata}</p> : null}
        <p>{effectFor(item.kind)}</p>
        <p className="search-suggestion-card__provenance">
          Suggested by {originLabel(item.origin)} on {formatDate(item.proposedAt)} - Based on{" "}
          {item.evidenceCount} master CV entr
          {item.evidenceCount === 1 ? "y" : "ies"}
          {item.correlationId ? " - Linked to a recorded search run" : ""}
          {item.decidedAt ? ` - Decision saved ${formatDate(item.decidedAt)}` : ""}
        </p>
        {!item.isCurrentMasterCvVersion ? (
          <p className="search-suggestion-card__version">
            This decision was made from an earlier master CV and still affects future searches. Its
            rationale came from that CV; undecided suggestions from older CVs are not carried
            forward.
          </p>
        ) : null}
        <details className="proposal-rationale">
          <summary aria-label={`Why ${PRODUCT_NAME} suggested ${item.name}`}>
            Why {PRODUCT_NAME} suggested this
          </summary>
          <p>{item.because}</p>
        </details>
      </div>
      <SuggestionActions item={item} onRule={onRule} pending={pending} />
    </li>
  );
}

function SuggestionActions({
  item,
  onRule,
  pending,
}: {
  item: Suggestion;
  onRule: (item: Suggestion, ruling: Ruling, reason?: ProposalRejectionReason | null) => void;
  pending: boolean;
}) {
  const [reason, setReason] = useState<ProposalRejectionReason>(reasonsFor(item.kind)[0]!);
  if (item.state === "accepted") {
    return (
      <ConfirmedActionButton
        ariaLabel={`Withdraw approval for ${item.name}`}
        className="button button--danger"
        disabled={pending}
        onConfirm={() => onRule(item, "reject")}
        options={{
          title: `Withdraw approval for ${item.name}?`,
          description:
            "Later searches stop using this, and it moves to Not using. Existing jobs are not removed.",
          confirmLabel: "Withdraw approval",
          danger: true,
        }}
      >
        {pending ? "Saving" : "Withdraw approval"}
      </ConfirmedActionButton>
    );
  }
  if (item.state === "rejected") {
    return (
      <button
        aria-label={`Approve ${item.name} for future searches`}
        className="button button--primary"
        disabled={pending}
        onClick={() => onRule(item, "accept")}
        type="button"
      >
        {pending ? "Saving" : "Approve for future searches"}
      </button>
    );
  }
  return (
    <div className="search-suggestion-actions">
      <div className="button-row">
        <button
          aria-label={`Approve ${item.name} for future searches`}
          className="button button--primary"
          disabled={pending}
          onClick={() => onRule(item, "accept")}
          type="button"
        >
          {pending ? "Saving" : "Approve for future searches"}
        </button>
        <button
          aria-label={`Save ${item.name} for later without using it`}
          className="button button--quiet"
          disabled={pending}
          onClick={() => onRule(item, "reject", "defer")}
          type="button"
        >
          Save for later (not used)
        </button>
      </div>
      <details className="search-suggestion-reject">
        <summary aria-label={`Do not use ${item.name}`}>Don't use this</summary>
        <RejectionControls
          item={item}
          onRule={onRule}
          pending={pending}
          reason={reason}
          setReason={setReason}
        />
      </details>
    </div>
  );
}

function RejectionControls({
  item,
  onRule,
  pending,
  reason,
  setReason,
}: {
  item: Suggestion;
  onRule: (item: Suggestion, ruling: Ruling, reason?: ProposalRejectionReason | null) => void;
  pending: boolean;
  reason: ProposalRejectionReason;
  setReason: (reason: ProposalRejectionReason) => void;
}) {
  return (
    <div className="search-suggestion-rejection-controls">
      <label>
        <span>Why shouldn't future searches use this?</span>
        <select
          aria-label={`Reason not to use ${item.name}`}
          disabled={pending}
          onChange={(event) => setReason(event.target.value as ProposalRejectionReason)}
          value={reason}
        >
          {reasonsFor(item.kind).map((value) => (
            <option key={value} value={value}>
              {REASON_LABELS[value]}
            </option>
          ))}
        </select>
      </label>
      <div className="button-row">
        <button
          aria-label={`Don't use ${item.name}`}
          className="button button--danger"
          disabled={pending}
          onClick={() => onRule(item, "reject", reason)}
          type="button"
        >
          {pending ? "Saving" : "Don't use"}
        </button>
        {reasonsFor(item.kind).includes("duplicate") ? (
          <button
            aria-label={`Do not use ${item.name} because it is a duplicate`}
            className="button button--quiet"
            disabled={pending}
            onClick={() => onRule(item, "reject", "duplicate")}
            type="button"
          >
            Don't use - duplicate
          </button>
        ) : null}
      </div>
    </div>
  );
}
