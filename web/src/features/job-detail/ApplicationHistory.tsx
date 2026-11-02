import type { FormEventHandler } from "react";
import type { UseFormRegister } from "react-hook-form";
import { Link } from "react-router-dom";

import type { JobDetail, Note } from "../../api/client";
import { StatusBadge } from "../../components/StatusBadge";
import { applicationEventStatusValues, statusEntry } from "../../components/statuses";
import { HistoryPagination } from "./HistoryPagination";

export type ApplicationEventForm = { status: string; notes: string; occurred_at: string };

export function ApplicationHistory({
  appendError,
  appendPending,
  currentStatus,
  events,
  jobId,
  notes,
  onSubmit,
  register,
  eventCount,
  eventOffset,
  pageSize,
  onEventPageChange,
}: {
  appendError: string | null;
  appendPending: boolean;
  currentStatus: JobDetail["application_status"];
  events: JobDetail["application_events"];
  jobId: string;
  notes: Note[];
  onSubmit: FormEventHandler<HTMLFormElement>;
  register: UseFormRegister<ApplicationEventForm>;
  eventCount: number;
  eventOffset: number;
  pageSize: number;
  onEventPageChange: (offset: number) => void;
}) {
  const allowedStatuses = applicationEventStatusValues(currentStatus, eventCount > 0);
  return (
    <section className="detail-grid">
      <article className="panel">
        <h2>Append status</h2>
        <p className="muted">This creates a new event. It never edits the timeline below.</p>
        <form className="event-form" onSubmit={onSubmit}>
          <label>
            Status
            <select {...register("status")}>
              {allowedStatuses.map((value) => (
                <option key={value} value={value}>
                  {statusEntry(value).label}
                </option>
              ))}
            </select>
          </label>
          <label>
            When it happened (local time)
            <input required type="datetime-local" {...register("occurred_at")} />
          </label>
          <label>
            Note
            <textarea {...register("notes")} placeholder="What changed?" rows={4} />
          </label>
          {appendError && <p className="field-error">{appendError}</p>}
          <button className="button button--primary" disabled={appendPending} type="submit">
            {appendPending ? "Saving" : "Append event"}
          </button>
        </form>
      </article>
      <article className="panel">
        <h2>Timeline</h2>
        <ol className="timeline">
          {events.map((item) => (
            <li key={`event-${item.id}`}>
              <span className="timeline-dot" />
              <div>
                <div className="timeline-title">
                  <StatusBadge value={item.status} />
                  <time>{new Date(item.occurred_at).toLocaleString()}</time>
                </div>
                <p>{item.notes || "No note recorded."}</p>
                {item.event_type !== "status" && (
                  <small>
                    {item.event_type} of {item.corrects_event_id ?? item.reverses_event_id}
                  </small>
                )}
              </div>
            </li>
          ))}
        </ol>
        <HistoryPagination
          count={eventCount}
          label="Application events"
          offset={eventOffset}
          onPageChange={onEventPageChange}
          pageSize={pageSize}
        />
        {!events.length && <p>No application events.</p>}
      </article>
      {notes.length > 0 && (
        <article className="panel">
          <h2>Job notes</h2>
          <p className="muted">
            Notes are separate from application events and do not affect status.
          </p>
          <ul className="note-list">
            {notes.map((item) => (
              <li key={item.id}>
                <strong>{item.title || "Note"}</strong>
                <time>{new Date(item.created_at).toLocaleString()}</time>
                <p>{item.body}</p>
                <small>Never eligible as candidate evidence</small>
              </li>
            ))}
          </ul>
          <Link className="button button--quiet" to={`/jobs/${jobId}?tab=notes`}>
            Manage notes
          </Link>
        </article>
      )}
    </section>
  );
}
