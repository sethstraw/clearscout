import { Link } from "react-router-dom";
import { useState } from "react";

import type { Job } from "../../api/client";
import { useAppendApplicationEvent, useApplicationPipeline } from "../../api/hooks";
import { Page } from "../../components/Page";
import { reviewScores, reviewScoreSource } from "../../components/reviewScores";
import { compareValues, type SortDirection } from "../../components/sorting";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { StatusBadge } from "../../components/StatusBadge";
import {
  applicationEventStatusValues,
  PURSUED_STATUSES,
  statusEntry,
} from "../../components/statuses";
import { useWorkspace } from "../../providers";

const columns = PURSUED_STATUSES;
type ApplicationSort = "employer" | "title" | "fit_score" | "overall_score" | "evaluated_at";

export function ApplicationsPage() {
  const { workspaceId } = useWorkspace();
  const [sortKey, setSortKey] = useState<ApplicationSort>("fit_score");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  // Only the four statuses this board renders, filtered by the server.
  const jobs = useApplicationPipeline(workspaceId, columns);
  if (jobs.isLoading) return <LoadingState label="Opening application board" />;
  if (jobs.isError) return <ErrorState error={jobs.error} />;
  const items = jobs.data?.items ?? [];
  return (
    <Page eyebrow="Apply" title="Applications" description="Where each application stands.">
      <section className="toolbar application-sort" aria-label="Application sorting">
        <label>
          <span>Sort applications</span>
          <select
            onChange={(event) => setSortKey(event.target.value as ApplicationSort)}
            value={sortKey}
          >
            <option value="overall_score">Overall score</option>
            <option value="fit_score">Fit score</option>
            <option value="employer">Employer</option>
            <option value="title">Role</option>
            <option value="evaluated_at">Evaluated date</option>
          </select>
        </label>
        <button
          className="button button--quiet"
          onClick={() => setSortDirection((current) => (current === "asc" ? "desc" : "asc"))}
          type="button"
        >
          {sortDirection === "asc" ? "Ascending" : "Descending"}
        </button>
      </section>
      <section className="kanban" aria-label="Application pipeline">
        {columns.map((status) => {
          const matching = items
            .filter((job) => job.application_status === status)
            .sort((left, right) =>
              compareValues(
                applicationSortValue(left, sortKey),
                applicationSortValue(right, sortKey),
                sortDirection,
              ),
            );
          return (
            <section className="kanban-column" key={status}>
              <header>
                <h2>
                  <StatusBadge value={status} />
                </h2>
                <span
                  aria-label={`${matching.length} ${matching.length === 1 ? "application" : "applications"}`}
                  className="kanban-column__count"
                >
                  {matching.length}
                </span>
              </header>
              <div className="kanban-stack">
                {matching.map((job) => (
                  <ApplicationCard job={job} key={job.job_posting_id} workspaceId={workspaceId} />
                ))}
                {!matching.length && <EmptyState>Nothing here.</EmptyState>}
              </div>
            </section>
          );
        })}
      </section>
      {!items.length && <EmptyState>No prepared or submitted applications yet.</EmptyState>}
    </Page>
  );
}

function ApplicationCard({ job, workspaceId }: { job: Job; workspaceId: string }) {
  const append = useAppendApplicationEvent(workspaceId, job.job_posting_id);
  const scores = reviewScores(job);
  const allowedStatuses = applicationEventStatusValues(
    job.application_status,
    job.application_event_count > 0,
  );
  return (
    <div className="application-entry">
      <Link className="application-card" to={`/jobs/${job.job_posting_id}`}>
        <span>{job.employer}</span>
        <strong>{job.title}</strong>
        <div>
          <small title={reviewScoreSource(scores.fit)}>
            Fit {scores.fit.value ?? "not stated"}
          </small>
          {scores.overall.value !== null && (
            <small title={reviewScoreSource(scores.overall)}>
              Overall {scores.overall.source === "minimum" ? "Min. " : ""}
              {scores.overall.value}
            </small>
          )}
        </div>
      </Link>
      <div className="application-links">
        <Link className="application-files-link" to={`/jobs/${job.job_posting_id}?tab=documents`}>
          Open related files
        </Link>
        {job.application_id && (
          <Link className="application-files-link" to={`/applications/${job.application_id}`}>
            Open application files
          </Link>
        )}
      </div>
      <label className="application-move">
        <span className="visually-hidden">Move {job.title} to another status</span>
        <select
          disabled={append.isPending}
          onChange={(event) => {
            const status = event.target.value;
            if (!status) return;
            append.mutate({ status, occurred_at: new Date().toISOString(), notes: "" });
          }}
          value=""
        >
          <option value="">{append.isPending ? "Recording" : "Move to another status"}</option>
          {allowedStatuses.map((status) => (
            <option key={status} value={status}>
              {statusEntry(status).label}
            </option>
          ))}
        </select>
      </label>
      {append.isError && <p className="field-error">{append.error.message}</p>}
    </div>
  );
}

function applicationSortValue(job: Job, sort: ApplicationSort) {
  if (sort === "evaluated_at") return job.evaluated_at ? Date.parse(job.evaluated_at) : null;
  if (sort === "fit_score") return reviewScores(job).fit.value;
  if (sort === "overall_score") return reviewScores(job).overall.value;
  return job[sort];
}
