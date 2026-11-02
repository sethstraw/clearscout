import { Fragment, useMemo, useState } from "react";

import type { SourceScanHistoryOptions } from "../../api/client";
import type { SourceScan } from "../../api/client/types";
import { useContinueSourceScan, useSourceScans, useSources } from "../../api/hooks";
import { safeHttpUrl } from "../../api/safeUrl";
import { StatusBadge } from "../../components/StatusBadge";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { screenedCandidatesHref } from "./pendingDiscoveryFilterModel";
import { SOURCE_SCAN_STATUS_VALUES, statusEntry } from "../../components/statuses";
import { scanDay } from "./scanDay";

const SCAN_HISTORY_PAGE_SIZE = 50;
const CHIP_COLLAPSE_THRESHOLD = 6;
const DETAIL_FAILURE_PREFIX = "detail_failure_examples: ";
const DETAIL_COUNTS_DESCRIPTION =
  "When interruption counts are recorded, started attempts equal read, failed, and interrupted requests. Read includes closed publisher records. Failed requests finished unsuccessfully. Interrupted requests were cut off by a read deadline after starting. Counts at read deadlines may count a row more than once and are separate from remaining deferred links.";
// Column count used by the row-alert spanning cell; keep in sync with the <thead> below.
const SCAN_HISTORY_COLUMN_COUNT = 7;

function skipReasonLabel(reason: string): string {
  if (reason === "missing_required_posting_identity") return "missing required posting identity";
  if (reason === "bounded_result_cap") return "outside the bounded result cap";
  if (reason === "incomplete_workday_listing") return "Workday listing changed during the read";
  if (reason === "repeated_workday_page") return "Workday repeated a page";
  if (reason === "duplicate_workday_identity") return "Workday repeated posting rows";
  if (reason === "incomplete_oracle_listing") return "Oracle listing not certified";
  return reason.replaceAll("_", " ");
}

const READ_KIND_LABELS: Record<SourceScan["kind"], string> = {
  "title-filter": "board scan",
  "guided-search": "guided search read",
  other: "read of another kind",
};

function keptMarker(scan: SourceScan): string {
  const kind = READ_KIND_LABELS[scan.kind];
  if (scan.kept_as === "latest_complete") return `Latest complete ${kind}`;
  return scan.kept_as === "latest_and_complete" ? `Latest ${kind}, complete` : `Latest ${kind}`;
}

function formatDuration(ms: number): string {
  if (ms <= 0) return "0.0s";
  if (ms >= 60000) {
    const minutes = Math.floor(ms / 60000);
    const seconds = Math.round((ms % 60000) / 1000);
    return `${minutes}m ${seconds}s`;
  }
  return `${(ms / 1000).toFixed(1)}s`;
}

const STATUS_FILTER_OPTIONS: {
  value: NonNullable<SourceScanHistoryOptions["status"]>;
  label: string;
}[] = [
  { value: "", label: "All statuses" },
  ...SOURCE_SCAN_STATUS_VALUES.map((value) => ({ value, label: statusEntry(value).label })),
];

function scanStartTime(scannedOn: string, startedAt: string): string | null {
  if (!startedAt) return null;
  if (startedAt === `${scannedOn}T00:00:00Z`) return null;
  const parsed = new Date(startedAt);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.toISOString().slice(11, 16);
}

function detailFailureNotes(notes: string): { examples: string[]; rest: string } {
  if (!notes.startsWith(DETAIL_FAILURE_PREFIX)) return { examples: [], rest: notes };
  const newline = notes.indexOf("\n");
  const boundary = newline === -1 ? notes.length : newline;
  const examples = notes
    .slice(DETAIL_FAILURE_PREFIX.length, boundary)
    .split("|")
    .map((value) => value.trim())
    .filter(Boolean);
  const rest = newline === -1 ? "" : notes.slice(newline + 1);
  return { examples, rest };
}

/** Show retained board scan outcomes with the source settings that governed each read. */
export function SourceScanHistory({ workspaceId }: { workspaceId: string }) {
  const [offset, setOffset] = useState(0);
  const [expandedChipRows, setExpandedChipRows] = useState<Record<string, boolean>>({});
  const [sourceFilter, setSourceFilter] = useState("");
  const [statusFilter, setStatusFilter] =
    useState<NonNullable<SourceScanHistoryOptions["status"]>>("");
  const scans = useSourceScans(workspaceId, {
    offset,
    limit: SCAN_HISTORY_PAGE_SIZE,
    sourceId: sourceFilter,
    status: statusFilter,
  });
  const sources = useSources(workspaceId);
  const continueScan = useContinueSourceScan(workspaceId);
  const sourceOptions = useMemo(
    () =>
      [...(sources.data?.items ?? [])].sort((left, right) => left.name.localeCompare(right.name)),
    [sources.data],
  );
  if (scans.isLoading) return <LoadingState label="Reading board scan history" />;
  if (scans.isError) return <ErrorState error={scans.error} />;
  const page = scans.data;
  const filtered = Boolean(sourceFilter || statusFilter);
  const filters = (
    <div className="button-row">
      <label>
        <span className="field-note">Source</span>{" "}
        <select
          onChange={(event) => {
            setOffset(0);
            setSourceFilter(event.target.value);
          }}
          value={sourceFilter}
        >
          <option value="">All sources</option>
          {sourceOptions.map((source) => (
            <option key={source.id} value={source.id}>
              {source.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span className="field-note">Status</span>{" "}
        <select
          onChange={(event) => {
            setOffset(0);
            setStatusFilter(event.target.value as typeof statusFilter);
          }}
          value={statusFilter}
        >
          {STATUS_FILTER_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
  return (
    <section className="panel" aria-labelledby="source-scan-history-heading">
      <header>
        <div>
          <h2 id="source-scan-history-heading">Board scan history</h2>
          <p>
            For each source and kind of read, the latest read and the latest complete read, with
            what each published and what was retained.
          </p>
        </div>
      </header>
      {(page?.total ?? 0) > 1 || filtered ? filters : null}
      {page?.items.length ? (
        <>
          <p className="field-note">
            Listed = raw records the board reported. Read = parsed into listings. Kept = retained
            after screening. New = not already in the queue.
          </p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Source</th>
                  <th scope="col">Scanned</th>
                  <th scope="col">Result</th>
                  <th
                    scope="col"
                    title="Listed by board / read / kept after screening / new to the queue"
                  >
                    Listed / read / kept / new
                  </th>
                  <th scope="col">Duration</th>
                  <th scope="col">Notes</th>
                  <th scope="col">Candidate review</th>
                </tr>
              </thead>
              <tbody>
                {page.items.map((scan) => {
                  const url = safeHttpUrl(scan.source_url);
                  const nonzeroSkips = Object.entries(scan.skip_reasons).filter(
                    ([, count]) => count > 0,
                  );
                  const collapsible = nonzeroSkips.length > CHIP_COLLAPSE_THRESHOLD;
                  const expanded = expandedChipRows[scan.id] ?? false;
                  const visibleSkips =
                    collapsible && !expanded
                      ? nonzeroSkips.slice(0, CHIP_COLLAPSE_THRESHOLD)
                      : nonzeroSkips;
                  const nonzeroFailureReasons = Object.entries(scan.detail_failure_reasons).filter(
                    ([, count]) => count > 0,
                  );
                  const { examples: failureExamples, rest: displayedNotes } = detailFailureNotes(
                    scan.notes,
                  );
                  return (
                    <Fragment key={scan.id}>
                      <tr>
                        <th scope="row">
                          <div>
                            {url ? (
                              <a href={url} rel="noreferrer" target="_blank">
                                {scan.source_name}
                              </a>
                            ) : (
                              scan.source_name
                            )}
                          </div>
                          <span className="field-note">
                            {scan.employer} - P{scan.priority} -{" "}
                            {scan.source_type.replaceAll("_", " ")}
                          </span>
                          <p className="field-note">{keptMarker(scan)}</p>
                        </th>
                        <td>
                          {scanDay(scan.scanned_on)}
                          {scan.status !== "omitted"
                            ? (() => {
                                const started = scanStartTime(
                                  scan.scanned_on,
                                  scan.scan_started_at,
                                );
                                return started ? (
                                  <p className="field-note">Started at {started} UTC</p>
                                ) : null;
                              })()
                            : null}
                        </td>
                        <td>
                          <StatusBadge value={scan.status} />
                        </td>
                        <td>
                          {scan.status === "omitted" ? (
                            <>Not scanned. Reason: {scan.notes || "unspecified"}.</>
                          ) : (
                            <>
                              {scan.raw_count} listed / {scan.read_count} read / {scan.result_count}{" "}
                              kept / {scan.new_count} new
                            </>
                          )}
                          {scan.status !== "omitted" && nonzeroSkips.length > 0 ? (
                            <ul
                              className="chip-row"
                              aria-label="Skipped-posting counts by dimension"
                            >
                              {visibleSkips.map(([reason, count]) => (
                                <li className="chip" key={reason}>
                                  {skipReasonLabel(reason)}: {count}
                                </li>
                              ))}
                              {collapsible ? (
                                <li>
                                  <button
                                    className="button button--quiet"
                                    onClick={() =>
                                      setExpandedChipRows((current) => ({
                                        ...current,
                                        [scan.id]: !expanded,
                                      }))
                                    }
                                    type="button"
                                    aria-expanded={expanded}
                                  >
                                    {expanded
                                      ? "Show fewer"
                                      : `Show ${nonzeroSkips.length - CHIP_COLLAPSE_THRESHOLD} more`}
                                  </button>
                                </li>
                              ) : null}
                            </ul>
                          ) : null}
                          {scan.status !== "omitted" &&
                          (scan.detail_attempted_count > 0 ||
                            scan.detail_capped_count > 0 ||
                            (scan.detail_interrupted_count ?? 0) > 0) ? (
                            <p
                              className="field-note"
                              title={DETAIL_COUNTS_DESCRIPTION}
                              aria-description={DETAIL_COUNTS_DESCRIPTION}
                            >
                              Recorded detail attempts: {scan.detail_attempted_count} started,{" "}
                              {scan.detail_read_count} read, {scan.detail_failed_count} failed.
                              Interrupted: {scan.detail_interrupted_count ?? "Not recorded"}. Not
                              attempted at read deadlines: {scan.detail_capped_count}.{" "}
                              <span>
                                These counts describe recorded read attempts, not the remaining
                                links. A row can be deferred more than once.
                              </span>
                            </p>
                          ) : null}
                        </td>
                        <td>{formatDuration(scan.duration_ms)}</td>
                        <td>
                          {displayedNotes || "No note recorded."}
                          {scan.follow_up_state === "waiting" ? (
                            <div className="scan-history__follow-up">
                              <span className="field-note">
                                {scan.frontier_pending_count} deferred link
                                {scan.frontier_pending_count === 1 ? "" : "s"} need
                                {scan.frontier_pending_count === 1 ? "s" : ""} your read
                                {scan.frontier_failed_count > 0
                                  ? `, ${scan.frontier_failed_count} exhausted`
                                  : ""}
                                .
                              </span>
                              <button
                                className="button button--quiet"
                                disabled={
                                  continueScan.isPending && continueScan.variables === scan.id
                                }
                                onClick={() => continueScan.mutate(scan.id)}
                                type="button"
                              >
                                {continueScan.isPending && continueScan.variables === scan.id
                                  ? "Reading"
                                  : `Read deferred publisher links (${scan.frontier_pending_count})`}
                              </button>
                              {continueScan.isError && continueScan.variables === scan.id ? (
                                <p className="field-error">{continueScan.error.message}</p>
                              ) : null}
                              {continueScan.isSuccess && continueScan.variables === scan.id ? (
                                <p className="field-note">
                                  Queued. The deferred links are read on the next worker pass.
                                </p>
                              ) : null}
                            </div>
                          ) : null}
                        </td>
                        <td>
                          {scan.task_id ? (
                            <a
                              className="button button--quiet"
                              href={screenedCandidatesHref(scan.task_id, scan.source_name)}
                            >
                              Review screened candidates
                            </a>
                          ) : (
                            <span className="field-note">Candidate ledger unavailable</span>
                          )}
                        </td>
                      </tr>
                      {scan.read_outcome === "policy_refused" ? (
                        <tr className="row-alert">
                          <td colSpan={SCAN_HISTORY_COLUMN_COUNT} className="row-alert__body">
                            <div role="status">
                              <strong>Automatic coverage unavailable:</strong> the publisher's
                              policy refused this board read. Zero openings were not inferred, and
                              the source remains recorded for later recovery.
                            </div>
                          </td>
                        </tr>
                      ) : null}
                      {scan.follow_up_state === "active" ||
                      scan.follow_up_state === "superseded" ? (
                        <tr className="row-note">
                          <td colSpan={SCAN_HISTORY_COLUMN_COUNT} className="row-note__body">
                            <div role="status">
                              <strong>Deferred publisher links:</strong>{" "}
                              {scan.follow_up_state === "active"
                                ? `Reading publisher links automatically (${scan.frontier_completed_count} read so far, ${scan.automatic_pending_count} queued).`
                                : "A newer scan of this source replaced this follow-up."}
                            </div>
                          </td>
                        </tr>
                      ) : null}
                      {scan.follow_up_state !== "waiting" && scan.frontier_failed_count > 0 ? (
                        <tr className="row-alert">
                          <td colSpan={SCAN_HISTORY_COLUMN_COUNT} className="row-alert__body">
                            <div role="status">
                              <strong>Exhausted publisher links:</strong>{" "}
                              {scan.frontier_failed_count} failed after repeated attempts.
                            </div>
                          </td>
                        </tr>
                      ) : null}
                      {scan.detail_failed_count > 0 ? (
                        <tr className="row-alert">
                          <td colSpan={SCAN_HISTORY_COLUMN_COUNT} className="row-alert__body">
                            <div role="alert">
                              <strong>Detail-read failures:</strong>{" "}
                              {nonzeroFailureReasons.length === 0
                                ? `${scan.detail_failed_count} failed with an unclassified reason.`
                                : nonzeroFailureReasons
                                    .map(
                                      ([reason, count]) =>
                                        `${count} ${reason.replaceAll("_", " ")}`,
                                    )
                                    .join(", ")}
                              {failureExamples.length > 0 ? (
                                <ul className="row-alert__examples">
                                  {failureExamples.map((example) => (
                                    <li key={example}>
                                      <code>{example}</code>
                                    </li>
                                  ))}
                                </ul>
                              ) : null}
                            </div>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="button-row">
            <button
              className="button button--quiet"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - SCAN_HISTORY_PAGE_SIZE))}
              type="button"
            >
              Newer scans
            </button>
            <span className="field-note">
              {offset + 1}-{Math.min(offset + page.items.length, page.total)} of {page.total}
            </span>
            <button
              className="button button--quiet"
              disabled={offset + page.items.length >= page.total}
              onClick={() => setOffset(offset + SCAN_HISTORY_PAGE_SIZE)}
              type="button"
            >
              Older scans
            </button>
          </div>
        </>
      ) : filtered ? (
        <EmptyState>No latest or latest complete read matches these filters.</EmptyState>
      ) : (
        <EmptyState>No board scan has been recorded.</EmptyState>
      )}
    </section>
  );
}
