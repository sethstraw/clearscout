import { useState } from "react";

import type { LearningLoopSignal } from "../../api/client";
import {
  useLearningLoopMetrics,
  useLearningLoopPolicy,
  useSaveLearningLoopPolicy,
  useTaskCosts,
} from "../../api/hooks";
import { ErrorState, LoadingState } from "../../components/States";

type SignalRow = {
  key:
    | "reproposed_rejected_identities"
    | "master_cv_proposal_acceptance"
    | "pending_queue_missed_matches";
  label: string;
  explanation: string;
};

export function LearningLoopMetricsPanel({ workspaceId }: { workspaceId: string }) {
  const metrics = useLearningLoopMetrics(workspaceId);

  if (metrics.isLoading) return <LoadingState label="Reading learning-loop signals" />;
  if (metrics.isError) return <ErrorState error={metrics.error} />;
  const data = metrics.data;
  if (!data || typeof data.top_n !== "number") return null;

  const topN = data.top_n;
  const rows: SignalRow[] = [
    {
      key: "reproposed_rejected_identities",
      label: "Rejected identities re-proposed",
      explanation:
        "Fraction of open proposals whose identity was already rejected under an earlier CV.",
    },
    {
      key: "master_cv_proposal_acceptance",
      label: "Master CV proposals accepted",
      explanation: "Fraction of aggregated CV proposals the workspace has accepted.",
    },
    {
      key: "pending_queue_missed_matches",
      label: `Top ${topN} pending matching missed requirements`,
      explanation:
        "Fraction of the top of the pending queue that mentions a requirement the CV has left unanswered.",
    },
  ];

  return (
    <article className="panel">
      <header>
        <div>
          <h2>Learning-loop signals</h2>
          <p>
            Three rule-based counts on the durable rows this workspace has written. A zero
            denominator means there is nothing yet to measure, not a failing loop.
          </p>
        </div>
      </header>
      <div className="panel-body">
        <dl className="settings-list">
          {rows.map((row) => (
            <div key={row.key}>
              <dt>{row.label}</dt>
              <dd>
                <div>
                  <SignalReadout signal={data[row.key]} />
                </div>
                <p className="field-note">{row.explanation}</p>
              </dd>
            </div>
          ))}
        </dl>
        <DisputeRefreshSetting workspaceId={workspaceId} />
      </div>
    </article>
  );
}

function DisputeRefreshSetting({ workspaceId }: { workspaceId: string }) {
  const policy = useLearningLoopPolicy(workspaceId);
  const savePolicy = useSaveLearningLoopPolicy(workspaceId);
  const costs = useTaskCosts(workspaceId);
  const [edited, setEdited] = useState<{
    reextraction: boolean;
    rematch: boolean;
  } | null>(null);

  if (policy.isLoading) return <LoadingState label="Reading the refresh setting" />;
  if (policy.isError) return <ErrorState error={policy.error} />;
  const current = policy.data?.policy;
  if (!current) return null;

  const reextraction = edited?.reextraction ?? current.enable_dispute_reextraction;
  const rematch = edited?.rematch ?? current.enable_dispute_rematch;
  const dirty =
    reextraction !== current.enable_dispute_reextraction ||
    rematch !== current.enable_dispute_rematch;
  return (
    <section>
      <h3>Re-check disputed requirements</h3>
      <p>
        Enabling either option does not itself spend. Each authorises a later paid task:
        re-extraction re-reads the posting, re-match re-checks fit against the current master CV.
        Both use your provider and record estimated costs when the action runs.
      </p>
      {savePolicy.isError && <ErrorState error={savePolicy.error} />}
      {savePolicy.data?.unchanged && (
        <p role="status">Identical to what was saved; nothing changed.</p>
      )}
      <label className="compact-field">
        <span id="dispute-reextraction-label">Enable dispute re-extraction</span>
        <input
          aria-labelledby="dispute-reextraction-label"
          checked={reextraction}
          onChange={(event) => setEdited({ reextraction: event.target.checked, rematch })}
          type="checkbox"
        />
        <small>Re-read disputed extraction requirements from the posting. Off by default.</small>
        {costs.loaded && costs.isPaid("posting.refresh_with_reextraction") && (
          <small className="field-note" data-testid="dispute-reextraction-cost">
            When a refresh with dispute re-extraction runs:{" "}
            {costs.cost("posting.refresh_with_reextraction")}{" "}
            {costs.confirmation("posting.refresh_with_reextraction")}
          </small>
        )}
      </label>
      <label className="compact-field">
        <span id="dispute-rematch-label">Enable fit re-match</span>
        <input
          aria-labelledby="dispute-rematch-label"
          checked={rematch}
          onChange={(event) => setEdited({ reextraction, rematch: event.target.checked })}
          type="checkbox"
        />
        <small>
          Re-match disputed fit requirements against the current master CV. Off by default.
        </small>
        {costs.loaded && costs.isPaid("posting.refresh_with_rematch") && (
          <small className="field-note" data-testid="dispute-rematch-cost">
            When a refresh with dispute re-match runs: {costs.cost("posting.refresh_with_rematch")}{" "}
            {costs.confirmation("posting.refresh_with_rematch")}
          </small>
        )}
      </label>
      <div className="button-row">
        <button
          className="button button--primary"
          disabled={!dirty || savePolicy.isPending}
          onClick={() =>
            savePolicy.mutate(
              {
                expected_version: policy.data?.version ?? 0,
                policy: {
                  ...current,
                  enable_dispute_reextraction: reextraction,
                  enable_dispute_rematch: rematch,
                },
              },
              { onSuccess: () => setEdited(null) },
            )
          }
          type="button"
        >
          {savePolicy.isPending ? "Saving" : "Save dispute settings"}
        </button>
        {dirty && <span className="field-note">Unsaved change</span>}
      </div>
    </section>
  );
}

function SignalReadout({ signal }: { signal: LearningLoopSignal }) {
  if (signal.denominator === 0) {
    return <span>no data yet</span>;
  }
  const percent = Math.round((signal.numerator / signal.denominator) * 100);
  return (
    <span>
      {signal.numerator} of {signal.denominator} ({percent}%)
    </span>
  );
}
