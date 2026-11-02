import { useRefusalPatternProposals, useRuleOnRefusalPattern } from "../../api/hooks/masterCv";
import type { RefusalPatternProposal } from "../../api/client";
import { ConfirmedActionButton } from "../../components/ConfirmedActionButton";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";

function normalized(value: string): string {
  return value.trim().toLocaleLowerCase().replaceAll(/\s+/g, " ");
}

function plainCondition(value: string): string {
  const words = value.trim().replaceAll(/[_-]+/g, " ");
  return words ? `${words[0]?.toLocaleUpperCase()}${words.slice(1)}` : "";
}

function isReadable(proposal: RefusalPatternProposal): boolean {
  return (
    Boolean(proposal.condition?.trim()) &&
    Boolean(proposal.normalized_reason?.trim()) &&
    Number.isInteger(proposal.refusal_count) &&
    proposal.refusal_count > 0 &&
    Array.isArray(proposal.reason_examples) &&
    ["proposed", "accepted", "rejected"].includes(proposal.state)
  );
}

function distinctExamples(proposal: RefusalPatternProposal): string[] {
  const repeatedReason = normalized(proposal.normalized_reason);
  const seen = new Set<string>();
  return proposal.reason_examples.flatMap((example) => {
    const trimmed = example.trim();
    const key = normalized(trimmed);
    if (!trimmed || key === repeatedReason || seen.has(key)) return [];
    seen.add(key);
    return [trimmed];
  });
}

/** Aggregated refusal patterns awaiting a ruling as a hard-gate exclusion. */
export function RefusalPatternProposals({ workspaceId }: { workspaceId: string }) {
  const proposals = useRefusalPatternProposals(workspaceId);
  const rule = useRuleOnRefusalPattern(workspaceId);
  if (proposals.isLoading) return <LoadingState label="Reading refusal patterns" />;
  if (proposals.isError) return <ErrorState error={proposals.error} />;
  const items = Array.isArray(proposals.data) ? proposals.data : [];
  const readable = items.filter(isReadable);
  const unreadableCount = items.length - readable.length;
  const pending = readable.filter((item) => item.state === "proposed");
  const decided = readable.filter((item) => item.state !== "proposed");
  return (
    <section className="panel" aria-labelledby="refusal-pattern-heading">
      <header>
        <div>
          <h2 id="refusal-pattern-heading">Recurring refusal patterns</h2>
          <p>
            When the same reason refused several postings, settle it here and it stops being
            proposed. Keeping it case by case leaves it free to recur. Screening and evaluation are
            unchanged either way.
          </p>
        </div>
      </header>
      <div className="panel-body">
        {rule.error != null && <ErrorState error={rule.error} />}
        {unreadableCount > 0 && (
          <p className="field-error" role="alert">
            {unreadableCount} proposal{unreadableCount === 1 ? "" : "s"} could not be read safely.
            No decision is available until both the proposed rule and its supporting refusals can be
            read.
          </p>
        )}
        {pending.length === 0 && unreadableCount === 0 ? (
          <EmptyState>No refusal pattern has recurred often enough to propose.</EmptyState>
        ) : pending.length > 0 ? (
          <ul className="refusal-pattern-list">
            {pending.map((item) => (
              <ProposalRow
                key={item.id}
                proposal={item}
                onAccept={() => rule.mutate({ proposalId: item.id, ruling: "accept" })}
                onReject={() => rule.mutate({ proposalId: item.id, ruling: "reject" })}
                disabled={rule.isPending}
              />
            ))}
          </ul>
        ) : null}
        {decided.length > 0 ? (
          <details className="refusal-pattern-history">
            <summary>Decided ({decided.length})</summary>
            <ul>
              {decided.map((item) => (
                <li key={item.id}>
                  <ProposalSummary proposal={item} />
                  <span className="refusal-pattern-verdict">{item.state}</span>
                </li>
              ))}
            </ul>
          </details>
        ) : null}
      </div>
    </section>
  );
}

function ProposalRow({
  proposal,
  onAccept,
  onReject,
  disabled,
}: {
  proposal: RefusalPatternProposal;
  onAccept: () => void;
  onReject: () => void;
  disabled: boolean;
}) {
  return (
    <li className="refusal-pattern-row">
      <ProposalSummary proposal={proposal} />
      <div className="refusal-pattern-actions">
        <ConfirmedActionButton
          className="button button--primary"
          disabled={disabled}
          onConfirm={onAccept}
          options={{
            title: "Settle this refusal pattern?",
            description: `This records that you have seen "${plainCondition(proposal.condition)}: ${proposal.normalized_reason}" and stops it being proposed again. Screening, evaluation, and the pending queue are unchanged, and nothing is skipped.`,
            confirmLabel: "Settle pattern",
          }}
        >
          Settle pattern
        </ConfirmedActionButton>
        <button
          className="button button--quiet"
          type="button"
          onClick={onReject}
          disabled={disabled}
        >
          Keep case by case
        </button>
      </div>
    </li>
  );
}

function ProposalSummary({ proposal }: { proposal: RefusalPatternProposal }) {
  const examples = distinctExamples(proposal);
  const postings = (proposal.refused_postings ?? []).map(({ employer, title }) =>
    employer.trim() ? `${employer.trim()}: ${title.trim()}` : title.trim(),
  );
  return (
    <div className="refusal-pattern-summary">
      <div className="refusal-pattern-condition">
        <strong>Condition:</strong> {plainCondition(proposal.condition)}
      </div>
      <div className="refusal-pattern-reason">
        <strong>Repeated reason:</strong> {proposal.normalized_reason}
      </div>
      <div>
        <strong>Refused postings:</strong>
      </div>
      {postings.length > 0 ? (
        <ul className="refusal-pattern-examples">
          {postings.map((posting, index) => (
            <li key={`${proposal.id}-posting-${index}`}>{posting}</li>
          ))}
        </ul>
      ) : (
        <div className="field-note">No posting this pattern refused is still held out.</div>
      )}
      {examples.length > 0 ? (
        <>
          <div>
            <strong>Other wording:</strong>
          </div>
          <ul className="refusal-pattern-examples">
            {examples.slice(0, 3).map((example) => (
              <li key={`${proposal.id}-${normalized(example)}`}>{example}</li>
            ))}
          </ul>
        </>
      ) : null}
      <div className="refusal-pattern-meta">
        Refused {proposal.refusal_count} time{proposal.refusal_count === 1 ? "" : "s"}
      </div>
    </div>
  );
}
