import {
  useMasterCvProposals,
  useRefreshMasterCvProposals,
  useRuleOnMasterCvProposal,
} from "../../api/hooks/masterCv";
import type { MasterCvProposal } from "../../api/client/types";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";

export function MasterCvProposals({
  workspaceId,
  onAccepted,
}: {
  workspaceId: string;
  onAccepted?: (proposal: MasterCvProposal) => void;
}) {
  const proposals = useMasterCvProposals(workspaceId);
  const refresh = useRefreshMasterCvProposals(workspaceId);
  const rule = useRuleOnMasterCvProposal(workspaceId);

  const pending = proposals.data?.filter((item) => item.state === "proposed") ?? [];
  const decided = proposals.data?.filter((item) => item.state !== "proposed") ?? [];
  return (
    <section aria-labelledby="cv-proposals-heading" className="cv-proposals">
      <div className="cv-proposals-header">
        <h3 id="cv-proposals-heading">Master CV proposals</h3>
        <button
          className="button button--quiet"
          type="button"
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
        >
          {refresh.isPending ? "Aggregating..." : "Refresh"}
        </button>
      </div>
      <p className="cv-proposals-caveat">
        Two kinds live in one queue: promote a transferable claim already ruled Met on many
        postings, or add a claim for a requirement recurring across postings you have never ruled.
        Accept opens the master CV editor; the system never writes the entry for you.
      </p>
      {proposals.isLoading ? <LoadingState /> : null}
      {proposals.isError ? <ErrorState error={proposals.error} /> : null}
      {proposals.data && pending.length === 0 ? (
        <EmptyState>
          No pending proposals. Refresh runs the aggregation against the current rulings and
          requirements; a proposal appears when the same identity recurs.
        </EmptyState>
      ) : null}
      {pending.length > 0 ? (
        <ul className="cv-proposal-list">
          {pending.map((item) => (
            <ProposalRow
              key={item.id}
              proposal={item}
              onAccept={() =>
                rule.mutate(
                  { proposalId: item.id, ruling: "accept" },
                  { onSuccess: () => onAccepted?.(item) },
                )
              }
              onReject={() => rule.mutate({ proposalId: item.id, ruling: "reject" })}
              disabled={rule.isPending}
            />
          ))}
        </ul>
      ) : null}
      {decided.length > 0 ? (
        <details className="cv-proposal-history">
          <summary>Decided ({decided.length})</summary>
          <ul>
            {decided.map((item) => (
              <li key={item.id}>
                <ProposalSummary proposal={item} />
                <span className="cv-proposal-verdict">{item.state}</span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  );
}

function ProposalRow({
  proposal,
  onAccept,
  onReject,
  disabled,
}: {
  proposal: MasterCvProposal;
  onAccept: () => void;
  onReject: () => void;
  disabled: boolean;
}) {
  return (
    <li className="cv-proposal-row">
      <ProposalSummary proposal={proposal} />
      <div className="cv-proposal-actions">
        <button
          className="button button--primary"
          type="button"
          onClick={onAccept}
          disabled={disabled}
        >
          Accept
        </button>
        <button
          className="button button--danger"
          type="button"
          onClick={onReject}
          disabled={disabled}
        >
          Reject
        </button>
      </div>
    </li>
  );
}

function ProposalSummary({ proposal }: { proposal: MasterCvProposal }) {
  const kindLabel =
    proposal.kind === "promote_transferable" ? "Promote transferable" : "Add missing";
  return (
    <div className="cv-proposal-summary">
      <div className="cv-proposal-kind">{kindLabel}</div>
      <div className="cv-proposal-text">{proposal.requirement_text}</div>
      {proposal.evidence_note ? (
        <div className="cv-proposal-note">Evidence: {proposal.evidence_note}</div>
      ) : null}
      <div className="cv-proposal-meta">
        Seen across {proposal.posting_count} posting{proposal.posting_count === 1 ? "" : "s"}
      </div>
    </div>
  );
}
