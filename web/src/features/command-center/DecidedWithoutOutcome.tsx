import { useState } from "react";

import type { Job } from "../../api/client";
import { useAppendApplicationEvent } from "../../api/hooks";
import { EmptyState } from "../../components/States";
import { StatusBadge } from "../../components/StatusBadge";

const VISIBLE_ROWS = 6;
const DAY_MS = 86_400_000;

/** Whole days between the stored decision and the render clock. */
function decidedAge(evaluatedAt: string, now: number): string {
  const days = Math.floor((now - Date.parse(evaluatedAt)) / DAY_MS);
  if (days <= 0) return "Decided today";
  return `Decided ${days} ${days === 1 ? "day" : "days"} ago`;
}

function DecidedRow({ job, now, workspaceId }: { job: Job; now: number; workspaceId: string }) {
  const append = useAppendApplicationEvent(workspaceId, job.job_posting_id);
  const closed = job.posting_status === "closed";
  const record = (status: string) =>
    append.mutate({ status, occurred_at: new Date().toISOString(), notes: "" });
  const recordApplied = (
    <button
      aria-label={`Record applied for ${job.title}`}
      className="button button--quiet"
      disabled={append.isPending}
      onClick={() => record("applied")}
      type="button"
    >
      Record applied
    </button>
  );
  const recordClosure = (
    <button
      aria-label={`Closed before I applied for ${job.title}`}
      className="button button--quiet"
      disabled={append.isPending}
      onClick={() => record("closed_before_application")}
      type="button"
    >
      Closed before I applied
    </button>
  );
  return (
    <div className="decided-row">
      <div className="decided-identity">
        <strong>{job.title}</strong>
        <span>{job.employer}</span>
        <div className="decided-meta">
          {job.decision && <StatusBadge value={job.decision} />}
          {job.evaluated_at && <span>{decidedAge(job.evaluated_at, now)}</span>}
          {closed ? <span className="decided-closed">Closed at publisher</span> : null}
        </div>
      </div>
      <div className="decided-actions">
        {closed ? (
          <>
            {recordClosure}
            {recordApplied}
          </>
        ) : (
          <>
            {recordApplied}
            {recordClosure}
          </>
        )}
      </div>
      {append.isError && <p className="field-error">{append.error.message}</p>}
    </div>
  );
}

/** Jobs holding a strong decision that the ledger has no outcome for. */
export function DecidedWithoutOutcome({
  jobs,
  count,
  workspaceId,
}: {
  jobs: Job[];
  count: number;
  workspaceId: string;
}) {
  const [expanded, setExpanded] = useState(false);
  // One clock read for the whole card, so every row ages against the same instant.
  const [now] = useState(() => Date.now());
  const closedCount = jobs.filter((job) => job.posting_status === "closed").length;
  const visible = expanded ? jobs : jobs.slice(0, VISIBLE_ROWS);
  return (
    <section className="panel decided-panel">
      <header>
        <div>
          <h2>A decision with no recorded outcome</h2>
          <p>
            {count} decided apply now or premium target.
            {closedCount > 0 ? ` ${closedCount} closed at the publisher while waiting.` : ""}{" "}
            History appends, so a mis-click is corrected by recording the right outcome.
          </p>
        </div>
      </header>
      {visible.map((job) => (
        <DecidedRow job={job} key={job.job_posting_id} now={now} workspaceId={workspaceId} />
      ))}
      {jobs.length > VISIBLE_ROWS && (
        <button
          className="button button--quiet decided-expand"
          onClick={() => setExpanded((open) => !open)}
          type="button"
        >
          {expanded ? "Show six" : `Show all ${jobs.length}`}
        </button>
      )}
      {!jobs.length && <EmptyState>Every strong decision has a recorded outcome.</EmptyState>}
    </section>
  );
}
