import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api, type JobDetail } from "../../api/client";
import { usePostingDiff, usePostingObservation } from "../../api/hooks";
import { safeHttpUrl } from "../../api/safeUrl";
import { AggregatorCreditLink } from "../../components/AggregatorCredit";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { statusEntry } from "../../components/statuses";
import { UnifiedDiff } from "../../components/UnifiedDiff";
import { HistoryPagination } from "./HistoryPagination";

type Observation = JobDetail["observations"][number];

export function PostingReader({
  jobId,
  workspaceId,
  latestObservation,
  observations,
  observationCount,
  observationOffset,
  pageSize,
  onObservationPageChange,
}: {
  jobId: string;
  workspaceId: string;
  latestObservation: JobDetail["latest_observation"];
  observations: Observation[];
  observationCount: number;
  observationOffset: number;
  pageSize: number;
  onObservationPageChange: (offset: number) => void;
}) {
  const [params, setParams] = useSearchParams();
  const available = observations.filter((item) => item.body_artifact_id);
  const requested = params.get("observation") ?? "";
  const selectedId =
    requested ||
    (latestObservation?.body_artifact_id ? latestObservation.id : "") ||
    available.at(-1)?.id ||
    "";
  const selectedOnPage = available.find((item) => item.id === selectedId);
  const compareId = params.get("compare") ?? "";
  const [mode, setMode] = useState<"rendered" | "raw">("rendered");
  const [search, setSearch] = useState("");
  const posting = usePostingObservation(workspaceId, selectedId);
  const diff = usePostingDiff(jobId, workspaceId, compareId, selectedId);
  const span = useMemo(() => {
    const start = Number(params.get("start"));
    const end = Number(params.get("end"));
    return Number.isInteger(start) && Number.isInteger(end) && start >= 0 && end > start
      ? { start, end }
      : null;
  }, [params]);

  if (!selectedId) {
    return (
      <section className="panel posting-empty">
        <h2>Posting body unavailable</h2>
        <EmptyState>
          This evaluation has no recoverable full posting body. ClearScout did not invent one.
        </EmptyState>
        <HistoryPagination
          count={observationCount}
          label="Posting observations"
          offset={observationOffset}
          onPageChange={onObservationPageChange}
          pageSize={pageSize}
        />
      </section>
    );
  }
  if (posting.isLoading) return <LoadingState label="Verifying posting body" />;
  if (posting.isError || !posting.data) return <ErrorState error={posting.error} />;
  const selectedObservation = selectedOnPage ?? posting.data;
  const text = mode === "raw" ? posting.data.body : posting.data.rendered_text;
  const evidenceSpan = mode === "raw" ? span : null;
  const sourceUrl = safeHttpUrl(posting.data.source_url);

  return (
    <section className="posting-layout">
      <article className="panel posting-reader">
        <header>
          <div>
            <h2>Original posting</h2>
            <p>
              Observed {new Date(posting.data.observed_at).toLocaleString()}
              {sourceUrl ? (
                <>
                  ,{" "}
                  <a href={sourceUrl} rel="noreferrer" target="_blank">
                    Source
                  </a>
                  {posting.data.aggregator ? (
                    <>
                      , <AggregatorCreditLink credit={posting.data.aggregator} />
                    </>
                  ) : null}
                </>
              ) : null}
            </p>
            {selectedObservation && (
              <dl className="fact-list">
                <div>
                  <dt>Fetched</dt>
                  <dd>
                    {selectedObservation.fetched_at
                      ? new Date(selectedObservation.fetched_at).toLocaleString()
                      : "Not stated"}
                  </dd>
                </div>
                <div>
                  <dt>HTTP status</dt>
                  <dd>{selectedObservation.http_status ?? "Not stated"}</dd>
                </div>
                <div>
                  <dt>Extraction</dt>
                  <dd>
                    {selectedObservation.extraction_method ?? "Not stated"}
                    {selectedObservation.robots_basis === "single_posting_read"
                      ? " (single page read; robots.txt disallows crawling)"
                      : ""}
                  </dd>
                </div>
                <div>
                  <dt>Body artifact</dt>
                  <dd>
                    {selectedObservation.body_artifact_id ? (
                      <a
                        href={api.artifactUrl(selectedObservation.body_artifact_id, workspaceId)}
                        rel="noreferrer"
                        target="_blank"
                      >
                        {selectedObservation.body_artifact_id}
                      </a>
                    ) : (
                      "Not stated"
                    )}
                  </dd>
                </div>
              </dl>
            )}
          </div>
          <StatusControls mode={mode} setMode={setMode} />
        </header>
        <div className="posting-toolbar">
          <label>
            Observation
            <select
              onChange={(event) => {
                const next = new URLSearchParams(params);
                next.set("observation", event.target.value);
                next.delete("start");
                next.delete("end");
                setParams(next);
              }}
              value={selectedId}
            >
              {!selectedOnPage && (
                <option value={selectedId}>
                  {new Date(selectedObservation.observed_at).toLocaleString()},{" "}
                  {statusEntry(selectedObservation.posting_status).label} (outside loaded page)
                </option>
              )}
              {available.map((item) => (
                <option key={item.id} value={item.id}>
                  {new Date(item.observed_at).toLocaleString()},{" "}
                  {statusEntry(item.posting_status).label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Find in posting
            <input
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search exact text"
              type="search"
              value={search}
            />
          </label>
        </div>
        <HistoryPagination
          count={observationCount}
          label="Posting observations"
          offset={observationOffset}
          onPageChange={onObservationPageChange}
          pageSize={pageSize}
        />
        {span && mode === "rendered" && (
          <p className="span-note">Switch to raw view to show the exact stored evidence span.</p>
        )}
        <pre className="posting-body" id={span ? `span-${span.start}-${span.end}` : undefined}>
          <HighlightedText text={text} search={search} span={evidenceSpan} />
        </pre>
      </article>
      {(available.length > 1 || compareId) && (
        <aside className="panel posting-diff">
          <h2>Compare observations</h2>
          <label>
            Earlier observation
            <select
              onChange={(event) => {
                const next = new URLSearchParams(params);
                if (event.target.value) next.set("compare", event.target.value);
                else next.delete("compare");
                setParams(next);
              }}
              value={compareId}
            >
              <option value="">Choose one</option>
              {compareId && !available.some((item) => item.id === compareId) && (
                <option value={compareId}>Evaluated posting</option>
              )}
              {available
                .filter((item) => item.id !== selectedId)
                .map((item) => (
                  <option key={item.id} value={item.id}>
                    {new Date(item.observed_at).toLocaleString()}
                  </option>
                ))}
            </select>
          </label>
          {diff.isFetching && <LoadingState label="Comparing verified bodies" />}
          {diff.isError && <ErrorState error={diff.error} />}
          {diff.data && (
            <>
              {diff.data.changed_facts.map((item) => (
                <dl className="diff-fact" key={item.field}>
                  <dt>{item.field.replaceAll("_", " ")}</dt>
                  <dd>
                    <del>{item.before || "Not stated"}</del>
                    <ins>{item.after || "Not stated"}</ins>
                  </dd>
                </dl>
              ))}
              <UnifiedDiff diff={diff.data.body_diff} label="Body text." />
            </>
          )}
        </aside>
      )}
    </section>
  );
}

function StatusControls({
  mode,
  setMode,
}: {
  mode: "rendered" | "raw";
  setMode: (value: "rendered" | "raw") => void;
}) {
  return (
    <div className="mode-toggle" aria-label="Posting display mode" role="group">
      <button
        aria-pressed={mode === "rendered"}
        className={mode === "rendered" ? "active" : ""}
        onClick={() => setMode("rendered")}
        type="button"
      >
        Rendered
      </button>
      <button
        aria-pressed={mode === "raw"}
        className={mode === "raw" ? "active" : ""}
        onClick={() => setMode("raw")}
        type="button"
      >
        Raw
      </button>
    </div>
  );
}

function HighlightedText({
  text,
  search,
  span,
}: {
  text: string;
  search: string;
  span: { start: number; end: number } | null;
}) {
  if (span && span.end <= text.length) {
    return (
      <>
        {text.slice(0, span.start)}
        <mark className="evidence-highlight">{text.slice(span.start, span.end)}</mark>
        {text.slice(span.end)}
      </>
    );
  }
  const needle = search.trim();
  if (!needle) return text;
  const lower = text.toLocaleLowerCase();
  const target = needle.toLocaleLowerCase();
  const parts = [];
  let cursor = 0;
  let match = lower.indexOf(target);
  while (match >= 0) {
    parts.push(text.slice(cursor, match));
    parts.push(<mark key={`${match}-${target}`}>{text.slice(match, match + target.length)}</mark>);
    cursor = match + target.length;
    match = lower.indexOf(target, cursor);
  }
  parts.push(text.slice(cursor));
  return parts;
}
