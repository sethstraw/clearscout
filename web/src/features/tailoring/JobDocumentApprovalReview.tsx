import { useState } from "react";

import { api } from "../../api/client";
import type { TailoringReview } from "../../api/client/types";
import {
  useEnqueueTask,
  useTailoringReview,
  useTaskCosts,
  useWorkspaceMutation,
} from "../../api/hooks";
import { PaidAction } from "../../components/PaidAction";
import { StatusBadge } from "../../components/StatusBadge";
import { Toggletip } from "../../components/Toggletip";
import { formatUsd } from "../../components/text";
import { UnifiedDiff } from "../../components/UnifiedDiff";
import { unifiedDiff } from "../../components/diff";
import { useConfirmation } from "../../confirmations";

type ReviewProposal = TailoringReview["proposals"][number];

// "(not in this master CV)" is the text the backend emits for a cited id that no
// longer resolves; such an excerpt has no source to diff against.
const SOURCE_NOT_FOUND = "(not in this master CV)";

function resolvedSourceText(proposal: ReviewProposal): string {
  const cited = new Set(proposal.source_ids);
  return (proposal.payload.evidence ?? [])
    .filter((item) => cited.has(item.source) && item.text.trim() && item.text !== SOURCE_NOT_FOUND)
    .map((item) => item.text)
    .join("\n");
}

export function JobDocumentApprovalReview({
  jobId,
  runId,
  workspaceId,
}: {
  jobId: string;
  runId: string;
  workspaceId: string;
}) {
  const confirm = useConfirmation();
  const costs = useTaskCosts(workspaceId);
  const review = useTailoringReview(workspaceId, runId);
  const enqueue = useEnqueueTask(workspaceId);
  const [replacements, setReplacements] = useState<Record<string, string>>({});
  const [preflightPending, setPreflightPending] = useState(false);
  const [preflightError, setPreflightError] = useState("");
  const [batchPending, setBatchPending] = useState(false);
  const [batchResult, setBatchResult] = useState<{ accepted: number; failed: number } | null>(null);
  const decide = useWorkspaceMutation(
    workspaceId,
    ({
      proposalId,
      digest,
      decision,
    }: {
      proposalId: string;
      digest: string;
      decision: "accept" | "reject" | "replace";
    }) =>
      api.decideProposal(proposalId, {
        workspace_id: workspaceId,
        proposal_digest: digest,
        decision,
        replacement_text: decision === "replace" ? (replacements[proposalId] ?? "") : null,
        replacement_source_ids:
          decision === "replace"
            ? (review.data?.proposals.find((item) => item.id === proposalId)?.source_ids ?? [])
            : [],
      }),
  );
  if (review.isLoading)
    return (
      <section className="review-panel">
        <p>Opening line-by-line review</p>
      </section>
    );
  if (review.isError || !review.data)
    return (
      <section className="review-panel">
        <p className="field-error">{review.error?.message ?? "Review unavailable."}</p>
      </section>
    );
  const completed = review.data.proposals.filter(
    (proposal) => proposal.decisions.length > 0,
  ).length;
  const checks = review.data.checks ?? [];
  const priorities = checks.filter(
    (check) => check.outcome !== "pass" || (check.advisories ?? []).length > 0,
  );
  const orderedProposals = [...review.data.proposals].sort((left, right) => {
    const priority = (proposal: typeof left) =>
      proposal.payload.immutable ? 3 : proposal.decisions.length ? 2 : proposal.text ? 1 : 0;
    return priority(left) - priority(right);
  });
  // Batch only the lines a reviewer never argues with: drafted lines whose cited
  // evidence resolves, and verbatim immutable identity lines. A line a check flagged
  // defaults to "review", and it waits for a manual decision like unsupported lines.
  const undecided = review.data.proposals.filter((proposal) => proposal.decisions.length === 0);
  const flagged = undecided.filter((proposal) => proposal.payload.decision === "review");
  const batchAcceptable = undecided.filter(
    (proposal) =>
      proposal.payload.decision !== "review" &&
      (Boolean(proposal.payload.immutable) || resolvedSourceText(proposal).length > 0),
  );
  const manualOnly = undecided.filter(
    (proposal) =>
      proposal.payload.decision !== "review" &&
      !proposal.payload.immutable &&
      resolvedSourceText(proposal).length === 0,
  );
  const acceptRemainingSupported = async () => {
    setBatchPending(true);
    const results = await Promise.allSettled(
      batchAcceptable.map((proposal) =>
        decide.mutateAsync({
          proposalId: proposal.id,
          digest: proposal.digest,
          decision: "accept",
        }),
      ),
    );
    const accepted = results.filter((result) => result.status === "fulfilled").length;
    setBatchResult({ accepted, failed: results.length - accepted });
    setBatchPending(false);
  };
  const confirmApprovedPackage = async () => {
    setPreflightPending(true);
    setPreflightError("");
    try {
      const preflight = await api.tailoringPreflight(workspaceId, jobId, runId);
      const approved = await confirm({
        title: preflight.reuse_available
          ? "Reuse this exact approved package?"
          : "Generate this approved package?",
        description: preflight.reuse_available
          ? `Exact approved package found. Reusing it does not call the provider and adds no provider charge. Preflight cost: ${formatUsd(preflight.cost_usd ?? 0)}. ${costs.confirmation("workflow.approve")}`
          : `No exact approved package is available. Preflight did not find a reusable package cost. ${costs.confirmation("workflow.approve")}`,
        confirmLabel: preflight.reuse_available
          ? "Reuse approved package"
          : "Generate approved package",
      });
      if (approved) {
        enqueue.mutate({
          workspace_id: workspaceId,
          task_type: "workflow.approve",
          payload: {
            job_posting_id: jobId,
            cover_letter: true,
            regenerate: false,
            approval_tailoring_run_id: runId,
          },
        });
      }
    } catch (error) {
      setPreflightError(
        error instanceof Error ? error.message : "Approved-package cost could not be checked.",
      );
    } finally {
      setPreflightPending(false);
    }
  };
  return (
    <section className="review-panel">
      <header>
        <div>
          <h3>Approval review</h3>
          <div>
            {completed} of {review.data.proposals.length} lines explicitly decided.{" "}
            <Toggletip label="About line decisions">
              Each decision is bound to its proposal digest, so a changed line needs a new decision.
            </Toggletip>
          </div>
          <button
            disabled={batchAcceptable.length === 0 || batchPending || decide.isPending}
            onClick={() => void acceptRemainingSupported()}
            type="button"
          >
            Accept remaining evidence-backed and copied lines ({batchAcceptable.length})
          </button>
          {batchResult && (
            <p className="field-note" role="status">
              Accepted {batchResult.accepted} line{batchResult.accepted === 1 ? "" : "s"}
              {batchResult.failed > 0
                ? `; ${batchResult.failed} could not be accepted and still need a decision.`
                : "."}
            </p>
          )}
          {manualOnly.length > 0 && (
            <div className="field-note">
              {manualOnly.length} drafted line{manualOnly.length === 1 ? "" : "s"} cite no
              resolvable master-CV evidence and still need a manual decision.{" "}
              <Toggletip label="About supported and copied lines">
                Supported means a line&apos;s cited evidence resolves to master-CV text. Copied
                lines are the identity fields the program carries verbatim.
              </Toggletip>
            </div>
          )}
          {flagged.length > 0 && (
            <p className="field-note">
              {flagged.length} line{flagged.length === 1 ? "" : "s"} flagged by a check{" "}
              {flagged.length === 1 ? "needs" : "need"} a manual decision.
            </p>
          )}
        </div>
        <PaidAction
          costs={costs}
          disabled={
            completed !== review.data.proposals.length || enqueue.isPending || preflightPending
          }
          label={
            preflightPending ? "Checking reuse" : "Generate or reuse the approved CV and letter"
          }
          onPrepare={() => {
            void confirmApprovedPackage();
          }}
          taskType="workflow.approve"
        />
      </header>
      <div className="field-note">
        <h4>Before using this application</h4>
        <Toggletip label="What to check before you apply">
          Compare ownership, qualifications, dates, and metrics with the cited sources. Layout is
          not confirmed here, so render the final resume and check its page count and legibility.
          Lines needing evidence or a decision come first; copied identity fields follow.
        </Toggletip>
        <p>
          Structural checks:{" "}
          {checks.length
            ? checks.some((check) => check.outcome === "fail")
              ? "failed; repair the reported issues"
              : "no recorded failures"
            : "unknown for this historical run"}
          . Human decisions: {completed} of {review.data.proposals.length} lines decided.
        </p>
        {priorities.length > 0 && (
          <ul>
            {priorities.map((check, index) => (
              <li key={`${check.check}-${index}`}>
                {check.outcome.toUpperCase()} - {check.check}: {check.detail}
                {(check.advisories ?? []).length > 0 && (
                  <ul>
                    {check.advisories?.map((advisory, position) => (
                      <li key={position}>{advisory}</li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
      <ol>
        {orderedProposals.map((proposal) => {
          const latest = proposal.decisions.at(-1);
          const immutable = Boolean(proposal.payload.immutable);
          return (
            <li key={proposal.id}>
              <div>
                <span>{proposal.slot}</span>
                <strong>{proposal.text || "Omitted line"}</strong>
                <small>
                  {proposal.source_ids.length
                    ? `Evidence: ${proposal.source_ids.join(", ")}`
                    : "No cited evidence"}
                </small>
                {(() => {
                  const sourceText = resolvedSourceText(proposal);
                  if (!sourceText) return null;
                  return (
                    <UnifiedDiff
                      diff={unifiedDiff(sourceText, proposal.text)}
                      label="Drafted line against master-CV source."
                    />
                  );
                })()}
                {proposal.payload.rationale && <p>{proposal.payload.rationale}</p>}
              </div>
              <div className="review-decision">
                <StatusBadge value={latest?.decision ?? "undecided"} />
                <button
                  onClick={() =>
                    decide.mutate({
                      proposalId: proposal.id,
                      digest: proposal.digest,
                      decision: "accept",
                    })
                  }
                  type="button"
                >
                  Accept
                </button>
                <button
                  disabled={immutable}
                  onClick={() =>
                    decide.mutate({
                      proposalId: proposal.id,
                      digest: proposal.digest,
                      decision: "reject",
                    })
                  }
                  type="button"
                >
                  Reject
                </button>
                {!immutable && (
                  <>
                    <input
                      aria-label={`Replacement for ${proposal.slot}`}
                      onChange={(event) =>
                        setReplacements((current) => ({
                          ...current,
                          [proposal.id]: event.target.value,
                        }))
                      }
                      placeholder="Evidence-backed replacement"
                      value={replacements[proposal.id] ?? ""}
                    />
                    <button
                      disabled={!replacements[proposal.id]?.trim()}
                      onClick={() =>
                        decide.mutate({
                          proposalId: proposal.id,
                          digest: proposal.digest,
                          decision: "replace",
                        })
                      }
                      type="button"
                    >
                      Replace
                    </button>
                  </>
                )}
              </div>
            </li>
          );
        })}
      </ol>
      {preflightError && <p className="field-error">{preflightError}</p>}
      {decide.isError && <p className="field-error">{decide.error.message}</p>}
    </section>
  );
}
