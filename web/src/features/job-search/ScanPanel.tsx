import { useId, useState } from "react";

import {
  useEnqueueTask,
  useSearchParameters,
  useSources,
  useTask,
  useTasks,
} from "../../api/hooks";
import { text } from "../../api/json";
import { ConfirmedActionButton } from "../../components/ConfirmedActionButton";
import { Toggletip } from "../../components/Toggletip";
import { DiscoveryTable, type Discovery } from "./DiscoveryTable";
import { DiscoveryFunnelAudit } from "./DiscoveryFunnelAudit";
import { SourceScanSchedule } from "./SourceScanSchedule";
import { scanDay } from "./scanDay";
import type { Source } from "../../api/client/types";

/** Say what a board's retained scans have produced, so a dead board can be retired. */
function yieldNote(source: Source) {
  if (source.last_read_outcome === "policy_refused") {
    return "Refused by policy, so no sweep can read it";
  }
  // A posting kept on several scans counts once per scan.
  if (source.staged_count > 0) return `${source.staged_count} kept across scans`;
  if (source.last_listed_on) return "Listed, none kept";
  if (source.first_read_on) {
    return `No listings in ${scans(source.read_scan_count)} since ${scanDay(source.first_read_on)}`;
  }
  // No completed read yet: a partial read is not evidence the board lists nothing.
  if (source.incomplete_scan_count > 0) {
    return `Never read in full in ${scans(source.incomplete_scan_count)}; see Board scan history`;
  }
  if (source.failed_scan_count > 0) {
    return `Every read failed in ${scans(source.failed_scan_count)}; see Board scan history`;
  }
  return "Never read";
}

function scans(count: number) {
  return `${count} scan${count === 1 ? "" : "s"}`;
}

/** Name the day a board last listed anything when its latest completed read listed nothing. */
function listingLapse(source: Source) {
  const lastListed = source.last_listed_on;
  if (!lastListed || source.last_status !== "completed_empty") return null;
  return <p className="field-note">No listings since {scanDay(lastListed)}</p>;
}

/** Scan saved boards and report their recorded outcomes. */
export function ScanPanel({ workspaceId }: { workspaceId: string }) {
  const [sourceFilter, setSourceFilter] = useState("");
  const [sourceSelection, setSourceSelection] = useState<string[]>([]);
  const [selectionNotice, setSelectionNotice] = useState("");
  const [freshDetails, setFreshDetails] = useState(false);
  const detailHelpId = useId();
  const bulkReasonId = useId();
  const scan = useEnqueueTask(workspaceId);
  const tasks = useTasks(workspaceId);
  const sources = useSources(workspaceId);
  const parameters = useSearchParameters(workspaceId);
  const titlesMissing =
    parameters.isSuccess &&
    !(parameters.data.parameters?.seniority?.target_titles ?? []).some((item) => item.title.trim());
  const latestScan = (tasks.data ?? [])
    .filter((item) => item.task_type === "source.scan")
    .slice()
    .sort((left, right) => right.created_at.localeCompare(left.created_at))[0];
  const displayedTaskId = scan.data?.id ?? latestScan?.id ?? "";
  const scanTask = useTask(workspaceId, displayedTaskId);
  const running = scan.isPending || ["queued", "running"].includes(scanTask.data?.status ?? "");
  const sourceItems = sources.data?.items ?? [];
  const enabledSources = sourceItems.filter((item) => item.enabled);
  const progress = scanTask.data?.progress ?? {};
  const discoveries = (
    Array.isArray(progress.discoveries) ? progress.discoveries : []
  ) as Discovery[];
  const withdrawn = (Array.isArray(progress.withdrawn) ? progress.withdrawn : []) as {
    employer: string;
    title: string;
  }[];
  const withdrawnOnRead = Number(progress.withdrawn_on_read ?? 0);
  const completedReads = Array.isArray(progress.scanned) ? progress.scanned.length : 0;
  const failedReads = Array.isArray(progress.failed) ? progress.failed.length : 0;
  const attemptedSources = completedReads + failedReads;
  const stagedCount = Number(progress.new_discoveries ?? 0);
  const busySources = Array.isArray(progress.already_in_progress)
    ? progress.already_in_progress
    : [];
  const resumedSources = Array.isArray(progress.resumed) ? progress.resumed : [];
  const outcomeNames = (items: unknown[]) =>
    items
      .map((item) =>
        item && typeof item === "object" && "source" in item
          ? text(item.source, "Unknown source")
          : "Unknown source",
      )
      .join(", ");
  const progressPhase = typeof progress.phase === "string" ? progress.phase : "scanning_sources";
  const progressCurrent = Number(progress.current ?? progress.completed ?? 0);
  // A run that named sources is measured against that selection, not the current enabled scope.
  const selectedIds = scanTask.data?.source_ids ?? [];
  const selectedNames = selectedIds.map(
    (id) => sourceItems.find((item) => item.id === id)?.name ?? id,
  );
  const scopeCount = selectedIds.length || enabledSources.length;
  const progressTotal = Number(progress.total ?? scopeCount);
  const progressSource = typeof progress.source === "string" ? progress.source : "";
  const progressLabel =
    progressPhase === "reading_content"
      ? "Reading posting content"
      : progressSource
        ? `Scanning ${progressSource}`
        : "Scanning sources";
  const scanDisabled = scan.isPending || sources.isLoading || sources.isError || titlesMissing;
  const bulkScanDisabled = scanDisabled || enabledSources.length === 0;
  const bulkScanReason =
    sources.isSuccess && sourceItems.length === 0 ? (
      <>
        <a href="#source-add-heading">Add a board</a> below to scan.
      </>
    ) : sources.isSuccess && enabledSources.length === 0 ? (
      "Turn on sweeping for a board below to scan."
    ) : titlesMissing ? (
      "Add a target title to scan."
    ) : null;
  const filteredSources = sourceItems.filter((item) =>
    item.name.toLowerCase().includes(sourceFilter.trim().toLowerCase()),
  );
  const selectedSources = sourceItems.filter((source) => sourceSelection.includes(source.id));
  const otherActiveScans = (tasks.data ?? []).filter(
    (task) =>
      task.task_type === "source.scan" &&
      task.id !== displayedTaskId &&
      ["queued", "running"].includes(task.status),
  );
  const scopeDetails = (items: Source[]) => (
    <div className="table-wrap scan-sources-confirm">
      <p>
        {freshDetails
          ? "Fresh details requested: read eligible posting details again for this scan."
          : "Recent posting details may be reused for up to seven days."}{" "}
        Dismissed and retired postings stay unread. Publisher policy and pacing still apply. A
        failed fresh read does not make the retained content fresh.
      </p>
      <table aria-label="Sources this scan will read" className="data-table">
        <thead>
          <tr>
            <th scope="col">Source</th>
            <th scope="col">Reading condition</th>
          </tr>
        </thead>
        <tbody>
          {items.map((source) => (
            <tr key={source.id}>
              <th scope="row">{source.name}</th>
              <td>{source.terms.condition || "No reading condition recorded"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
  const start = (sourceIds: string[] = []) =>
    scan.mutate({
      workspace_id: workspaceId,
      task_type: "source.scan",
      payload: { source_ids: sourceIds, ...(freshDetails ? { fresh_details: true } : {}) },
    });
  return (
    <section className="panel scan-panel">
      <header>
        <div>
          <h2>Scan your boards</h2>
          <p>
            A scan reads your boards and sends titles that match yours to Review.{" "}
            <Toggletip label="What a scan can read">
              Scans read board APIs or readable publisher pages. A recognized board does not promise
              permission or full coverage, so check each board&apos;s last read.
            </Toggletip>
          </p>
          {titlesMissing && (
            <p>
              Scans need at least one target title.{" "}
              <a href="/settings?tab=search">Add target titles in Settings &gt; Search</a>, or{" "}
              <a href="?tab=review">add one posting link</a> instead.
            </p>
          )}
        </div>
        <div className="button-row">
          <div data-tour="find-scan">
            <ConfirmedActionButton
              ariaDescribedBy={bulkScanReason ? bulkReasonId : undefined}
              className="button button--primary"
              disabled={bulkScanDisabled}
              onConfirm={() => start()}
              options={{
                title: "Scan the enabled boards?",
                description: `Reads ${enabledSources.length} enabled source${enabledSources.length === 1 ? "" : "s"}. Complete board reads can mark linked openings no longer listed; bounded aggregator reads cannot. Boards already being read are skipped.`,
                confirmLabel: "Scan boards",
                details: scopeDetails(enabledSources),
              }}
            >
              {scan.isPending ? "Submitting scan" : "Scan now"}
            </ConfirmedActionButton>
            {bulkScanReason ? (
              <small className="field-note" id={bulkReasonId}>
                {bulkScanReason}
              </small>
            ) : (
              <small className="field-note">All enabled sources</small>
            )}
          </div>
        </div>
      </header>
      <div className="panel-body">
        <details className="scan-options">
          <summary>Advanced</summary>
          <label>
            <input
              aria-describedby={detailHelpId}
              checked={freshDetails}
              onChange={(event) => setFreshDetails(event.target.checked)}
              type="checkbox"
            />{" "}
            Read fresh posting details for this scan
          </label>
          <p className="field-note" id={detailHelpId}>
            Reads posting details again instead of reusing ones up to seven days old.{" "}
            <Toggletip label="About fresh details">
              Dismissed and retired postings stay unread. Policy and pacing still apply. A failed
              fresh read leaves retained content and its timestamp unchanged. This option does not
              change your schedule.
            </Toggletip>
          </p>
        </details>
        {freshDetails && (
          <p className="field-note">Fresh posting details requested for this scan.</p>
        )}
        {scan.data && <p className="field-note">Showing the scan you just requested.</p>}
        {(scanTask.data || otherActiveScans.length > 0) && (
          <p className="field-note">
            Scan information comes from the recent task window.{" "}
            <a href="/tasks">View scans in Activity</a>
          </p>
        )}
        {otherActiveScans.length > 0 && (
          <section aria-label="Other active board scans in recent tasks">
            {otherActiveScans.map((task) => {
              const names = task.source_ids.length
                ? task.source_ids
                    .map((id) => sourceItems.find((source) => source.id === id)?.name ?? id)
                    .join(", ")
                : "All enabled sources";
              const current = Number(task.progress.current ?? task.progress.completed ?? 0);
              const total = Number(
                task.progress.total ?? (task.source_ids.length || enabledSources.length),
              );
              const phase =
                task.progress.phase === "reading_content"
                  ? "Reading posting content"
                  : typeof task.progress.source === "string" && task.progress.source
                    ? `Scanning ${task.progress.source}`
                    : "Scanning sources";
              return (
                <p key={task.id} role="status">
                  Other active scan: {names} - {task.status}.
                  {task.status === "running" ? ` ${phase}: ${current}/${total}.` : ""}
                </p>
              );
            })}
          </section>
        )}
        {sources.isError && (
          <p className="field-error" role="alert">
            Could not load the board-scan scope.{" "}
            <button
              className="button button--quiet"
              onClick={() => void sources.refetch()}
              type="button"
            >
              Retry
            </button>
          </p>
        )}
        {scanTask.data?.status === "queued" && (
          <p role="status">
            Queued board scan:{" "}
            {selectedNames.length ? selectedNames.join(", ") : "all enabled sources"}.
          </p>
        )}
        {running && scanTask.data?.status === "running" && (
          <p role="status">
            {progressLabel}: {progressCurrent}/{progressTotal}
          </p>
        )}
        {sources.isSuccess && sourceItems.length > 0 && (
          <details>
            <summary>
              Sources ({enabledSources.length} enabled of {sourceItems.length}): scan all, selected,
              or one
            </summary>
            <section className="toolbar" aria-label="Scan source filters">
              <label className="search-field">
                <span>Search sources</span>
                <input
                  onChange={(event) => {
                    setSourceFilter(event.target.value);
                    if (sourceSelection.length > 0) {
                      setSourceSelection([]);
                      setSelectionNotice("Selection cleared because the source filter changed.");
                    }
                  }}
                  placeholder="Source name"
                  type="search"
                  value={sourceFilter}
                />
              </label>
            </section>
            <div className="button-row" aria-label="Scan source selection">
              <button
                className="button button--quiet"
                disabled={filteredSources.length === 0}
                onClick={() => {
                  setSourceSelection(filteredSources.map((source) => source.id));
                  setSelectionNotice("");
                }}
                type="button"
              >
                Select displayed sources
              </button>
              <button
                className="button button--quiet"
                disabled={selectedSources.length === 0}
                onClick={() => {
                  setSourceSelection([]);
                  setSelectionNotice("");
                }}
                type="button"
              >
                Clear selection
              </button>
              <ConfirmedActionButton
                className="button button--primary"
                disabled={scanDisabled || selectedSources.length === 0}
                onConfirm={() => start(selectedSources.map((source) => source.id))}
                options={{
                  title: "Scan the selected sources?",
                  description: `Reads exactly ${selectedSources.length} selected source${selectedSources.length === 1 ? "" : "s"}, including any excluded from bulk scans. Complete board reads can mark linked openings no longer listed; bounded aggregator reads cannot. Boards already being read are skipped.`,
                  confirmLabel: "Scan selected boards",
                  details: scopeDetails(selectedSources),
                }}
              >
                Scan selected sources
              </ConfirmedActionButton>
            </div>
            <p id="scan-source-selection" className="field-note" aria-live="polite">
              {selectedSources.length} source{selectedSources.length === 1 ? "" : "s"} selected.
            </p>
            <p className="field-note">
              Selection applies to this scan only; it does not change which sources are enabled.
            </p>
            {selectionNotice && <p role="status">{selectionNotice}</p>}
            {filteredSources.length > 0 ? (
              <div className="table-wrap">
                <table aria-label="Scan sources" className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">Select</th>
                      <th scope="col">Source</th>
                      <th scope="col">Included in bulk scans</th>
                      <th scope="col">What it has produced</th>
                      <th scope="col">Reading condition</th>
                      <th scope="col">Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredSources.map((source) => (
                      <tr key={source.id}>
                        <td>
                          <input
                            aria-label={`Select ${source.name} for scan`}
                            aria-describedby="scan-source-selection"
                            checked={sourceSelection.includes(source.id)}
                            onChange={(event) => {
                              setSourceSelection((current) =>
                                event.target.checked
                                  ? [...current, source.id]
                                  : current.filter((id) => id !== source.id),
                              );
                              setSelectionNotice("");
                            }}
                            type="checkbox"
                          />
                        </td>
                        <th scope="row">{source.name}</th>
                        <td>{source.enabled ? "Yes" : "No"}</td>
                        <td>
                          <span>{yieldNote(source)}</span>
                          {listingLapse(source)}
                        </td>
                        <td>{source.terms.condition || "No reading condition recorded"}</td>
                        <td>
                          <ConfirmedActionButton
                            ariaLabel={`Scan ${source.name}`}
                            className="button button--quiet"
                            disabled={scanDisabled}
                            onConfirm={() => start([source.id])}
                            options={{
                              title: `Scan ${source.name}?`,
                              description: source.terms.condition
                                ? `Reads ${source.name} alone, under its reading condition: ${source.terms.condition.replace(/\.+$/, "")}. A complete board read can mark linked openings no longer listed; a bounded aggregator read cannot. Boards already being read are skipped.`
                                : `Reads ${source.name} alone. No reading condition is recorded. Boards already being read are skipped.`,
                              confirmLabel: "Scan board",
                              details: scopeDetails([source]),
                            }}
                          >
                            Scan
                          </ConfirmedActionButton>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p>No source matches that name.</p>
            )}
          </details>
        )}
        {scan.isError && (
          <p className="field-error" role="alert">
            {scan.error.message}
          </p>
        )}
        {scanTask.data?.status === "failed" && (
          <p className="field-error" role="alert">
            Scan failed: {scanTask.data.error_message}
          </p>
        )}
        {scanTask.data?.status === "completed" && (
          <>
            {selectedNames.length === 1 ? (
              <p role="status">
                {failedReads > 0
                  ? `Could not read ${selectedNames[0]}; no postings recorded.`
                  : completedReads > 0
                    ? `Read ${selectedNames[0]}: ${stagedCount} new posting${stagedCount === 1 ? "" : "s"} recorded and routed.`
                    : busySources.length > 0
                      ? `${selectedNames[0]} already has a read in progress; this run did not read it again.`
                      : resumedSources.length > 0
                        ? `Retained work for ${selectedNames[0]} was queued to resume; this run did not read the board again.`
                        : `No board read was recorded for ${selectedNames[0]} in this run.`}
              </p>
            ) : (
              <p role="status">
                Attempted {attemptedSources} source{attemptedSources === 1 ? "" : "s"} in this run:{" "}
                {completedReads} source{completedReads === 1 ? "" : "s"}{" "}
                {completedReads === 1 ? "was" : "were"} read and {failedReads} could not be read;{" "}
                {text(progress.new_discoveries, "0")} new posting
                {Number(progress.new_discoveries ?? 0) === 1 ? "" : "s"} recorded and routed.
              </p>
            )}
            {selectedNames.length > 1 && (
              <p className="field-note">This run selected {selectedNames.join(", ")}.</p>
            )}
            {selectedNames.length !== 1 && busySources.length > 0 && (
              <p role="status">
                Already in progress: {outcomeNames(busySources)}. This run did not read these boards
                again.
              </p>
            )}
            {selectedNames.length !== 1 && resumedSources.length > 0 && (
              <p role="status">
                Retained work queued to resume: {outcomeNames(resumedSources)}. These boards were
                not read again by this run.
              </p>
            )}
            {selectedNames.length === 0 && attemptedSources !== enabledSources.length && (
              <p className="field-note">
                The current scan scope has {enabledSources.length} enabled source
                {enabledSources.length === 1 ? "" : "s"}; this completed run attempted{" "}
                {attemptedSources}. The count changes when sources are enabled, disabled, added, or
                retired between runs.
              </p>
            )}
            {stagedCount > 0 && (
              <section aria-labelledby="scan-next-decision">
                <h3 id="scan-next-decision">New candidate records</h3>
                <p>
                  {stagedCount} posting{stagedCount === 1 ? "" : "s"} routed to the ready, recovery,
                  or screened-out view.
                </p>
                <p>
                  <a className="button button--primary" href="?tab=review">
                    Go to Review
                  </a>
                </p>
                {discoveries.length > 0 && (
                  <details className="scan-staged">
                    <summary>
                      Sample of this scan's recorded outcomes
                      {discoveries.length === stagedCount
                        ? ""
                        : ` (${discoveries.length} of ${stagedCount} shown)`}
                    </summary>
                    <p className="field-note">
                      Includes rows your settings screened out. No row is dismissed silently.
                    </p>
                    <DiscoveryTable discoveries={discoveries} />
                  </details>
                )}
              </section>
            )}
            <details className="scan-options">
              <summary>Scan audit</summary>
              {withdrawn.length > 0 && (
                <details className="scan-withdrawn">
                  <summary>
                    {withdrawn.length} opening{withdrawn.length === 1 ? " is" : "s are"} no longer
                    listed and stayed in the queue marked withdrawn
                  </summary>
                  <div className="table-wrap">
                    <table aria-label="Openings no longer listed" className="data-table">
                      <thead>
                        <tr>
                          <th scope="col">Role</th>
                          <th scope="col">Employer</th>
                        </tr>
                      </thead>
                      <tbody>
                        {withdrawn.map((item, index) => (
                          <tr key={`${item.employer}-${item.title}-${index}`}>
                            <th scope="row">{item.title}</th>
                            <td>{item.employer}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </details>
              )}
              {withdrawnOnRead > 0 && (
                <p className="field-note">
                  Reading posting content during this run found {withdrawnOnRead} posting
                  {withdrawnOnRead === 1 ? "" : "s"} closed by the publisher;{" "}
                  {withdrawnOnRead === 1 ? "that row is" : "those rows are"} marked withdrawn in the
                  queue.
                </p>
              )}
              <DiscoveryFunnelAudit value={progress.funnel} />
              <p>
                <a
                  className="button button--quiet"
                  href={`?tab=boards&scan=${encodeURIComponent(scanTask.data.id)}#screened-candidates`}
                >
                  Open this scan in History
                </a>
              </p>
            </details>
          </>
        )}
      </div>
      <SourceScanSchedule
        workspaceId={workspaceId}
        sources={sourceItems}
        selectedSourceIds={sourceSelection}
      />
    </section>
  );
}
