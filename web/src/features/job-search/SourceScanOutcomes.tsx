import { useState } from "react";
import { Link } from "react-router-dom";

import type { SourceScanOutcomeOptions } from "../../api/client";
import { useAddFoundPosting, useSourceScanOutcomes } from "../../api/hooks";
import { safeHttpUrl } from "../../api/safeUrl";
import type { components } from "../../api/schema";
import { SortableHeader } from "../../components/SortableHeader";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { nextSort, type SortDirection } from "../../components/sorting";
import { useDebouncedValue } from "../../hooks/useDebouncedValue";
import {
  reasonDestinationLabel,
  SpecialistIcReviewLink,
  type DiscoveryKind,
} from "./DiscoveryFunnelAudit";
import { usePostingRefresh } from "./usePostingRefresh";

const PAGE_SIZE = 100;
type OutcomeSort = SourceScanOutcomeOptions["sort"];
type CandidateKind = components["schemas"]["FunnelCandidateKind"];
type ScanOutcome = components["schemas"]["SourceScanOutcomeResponse"];

const SOURCE_CONFIGURATION_PATH = "/settings?tab=search&section=sources";

// A board lead records an input address; its URL may be a board or one posting.
const LINK_LABEL = {
  direct_posting: "View posting",
  board_listing: "View posting",
  board_lead: "Scanned address",
} satisfies Record<CandidateKind, string>;

function readable(value: string): string {
  return value.replaceAll("_", " ");
}

function exactReason(item: {
  detail: string;
  screening_findings: Array<{ code: string; dimension: string; subject: string }>;
}): string {
  if (item.screening_findings.length) {
    return item.screening_findings
      .map((finding) =>
        [readable(finding.dimension), readable(finding.code), finding.subject]
          .filter(Boolean)
          .join(": "),
      )
      .join("; ");
  }
  return item.detail || "No additional deterministic fact recorded.";
}

/** Review every candidate decision captured by one discovery task. */
export function SourceScanOutcomes({
  workspaceId,
  taskId,
  initialSource = "",
  source: controlledSource,
  onSourceChange,
  discoveryKind = "board_scan",
}: {
  workspaceId: string;
  taskId: string;
  initialSource?: string;
  source?: string;
  onSourceChange?: (source: string) => void;
  discoveryKind?: DiscoveryKind;
}) {
  const [search, setSearch] = useState("");
  const [localSource, setLocalSource] = useState(initialSource);
  const source = controlledSource ?? localSource;
  const setSource = (value: string) => {
    setLocalSource(value);
    onSourceChange?.(value);
  };
  const [reason, setReason] = useState<SourceScanOutcomeOptions["reason"]>("");
  const [terminalStage, setTerminalStage] = useState<SourceScanOutcomeOptions["terminalStage"]>("");
  const [candidateKind, setCandidateKind] = useState<SourceScanOutcomeOptions["candidateKind"]>("");
  const [sort, setSort] = useState<OutcomeSort>("employer");
  const [direction, setDirection] = useState<SortDirection>("asc");
  const [offset, setOffset] = useState(0);
  const [promotedIds, setPromotedIds] = useState<Set<string>>(new Set());
  const [promotingId, setPromotingId] = useState<string | null>(null);
  const addFound = useAddFoundPosting(workspaceId);
  const refresh = usePostingRefresh(workspaceId);
  // The input updates immediately; the query waits for a pause so search does
  // not fire a request per keystroke.
  const debouncedSearch = useDebouncedValue(search, 250);
  const options = {
    taskId,
    search: debouncedSearch,
    source,
    reason,
    terminalStage,
    candidateKind,
    sort,
    direction,
    offset,
    limit: PAGE_SIZE,
  };
  const outcomes = useSourceScanOutcomes(workspaceId, options);
  const page = outcomes.data;
  const update = (change: () => void) => {
    change();
    setOffset(0);
  };
  const sortBy = (column: OutcomeSort) => {
    const next = nextSort(sort, direction, column);
    setSort(next.key);
    setDirection(next.direction);
    setOffset(0);
  };
  const promote = async (item: ScanOutcome, url: string) => {
    setPromotingId(item.id);
    try {
      const added = await addFound.mutateAsync({
        url,
        title: item.title,
        employer: item.employer,
        location: item.location,
        check_publisher_facts: false,
      });
      setPromotedIds((current) => new Set(current).add(item.id));
      await refresh.refresh([added.pending_discovery.id]);
    } catch {
      // The owning mutation or refresh hook renders the exact failure below.
    }
  };

  if (outcomes.isError) return <ErrorState error={outcomes.error} />;
  // With placeholderData the prior page stays put through a refetch, so a full
  // spinner shows only on the first load, when there is no page to keep.
  if (!page) return <LoadingState label="Reading screened candidates" />;
  const pageStart = page.total === 0 ? 0 : page.offset + 1;
  const changing = outcomes.isFetching || search !== debouncedSearch;
  const hasFilters = Boolean(search || source || reason || terminalStage || candidateKind);
  const pageEnd = Math.min(page.offset + page.limit, page.total);
  const coverage = ((): { text: string; limited: boolean } => {
    const reported = page.reported_outcomes;
    const followUp = page.follow_up_outcomes;
    const run = discoveryKind === "guided_campaign" ? "campaign" : "scan";
    // reported_outcomes covers only what this task's own reads reported, so a
    // comparison against it drops the rows its follow-up reads added.
    const own = page.recorded_outcomes - followUp;
    const alsoRead = followUp
      ? ` A further ${followUp} row${followUp === 1 ? "" : "s"} came from later reads that continued this ${run}.`
      : "";
    if (page.capture_state === "complete" && reported !== null) {
      // The listing shows every row this task owns, so the coverage claim counts
      // them all, and the follow-up reads are a share of that one number.
      const listed = page.recorded_outcomes;
      const continued = followUp
        ? `, ${followUp} of them from later reads that continued this ${run}`
        : "";
      // A guided campaign records an outcome only for a URL it read, so the
      // total is the read count; a board scan records one per proposal.
      return {
        text:
          discoveryKind === "guided_campaign"
            ? `Recorded a decision for all ${listed} candidate${listed === 1 ? "" : "s"} this task read${continued}. URLs the campaign captured but did not read are in the Campaign report.`
            : `Recorded all ${listed} candidate decision${listed === 1 ? "" : "s"} from this discovery task${continued}.`,
        limited: false,
      };
    }
    if (page.capture_state === "recovered" && reported != null) {
      return {
        text: `Recovered ${own} of ${reported} recorded task outcomes.${alsoRead}`,
        limited: false,
      };
    }
    if (page.capture_state === "recovered_partial" && reported != null) {
      return {
        text: `Recovered ${own} of ${reported} task outcomes. The earlier task payload kept only a bounded set of candidate details, so omitted candidates cannot be reconstructed.${alsoRead}`,
        limited: true,
      };
    }
    if (page.capture_state === "partial" && reported != null) {
      // A repeat folds into the row the ledger already holds, and a sighting a
      // later scan repeated is pruned, so the shortfall is never a lost URL.
      const repeated = reported - own;
      const folded =
        repeated === 1
          ? " Another decision is not listed: it repeated a candidate listed here, or a later scan read the same URL and holds its newer sighting."
          : ` ${repeated} other decisions are not listed: each repeated a candidate listed here, or a later scan read the same URL and holds its newer sighting.`;
      return {
        text: `Recorded ${own} of ${reported} candidate decisions.${repeated > 0 ? folded : ""}${alsoRead}`,
        limited: true,
      };
    }
    if (page.capture_state === "in_progress") {
      return {
        text: `The discovery task is still running. ${page.recorded_outcomes} candidate decision${page.recorded_outcomes === 1 ? " is" : "s are"} available so far.`,
        limited: false,
      };
    }
    return {
      text: "This discovery task did not retain full outcome totals, so candidate-ledger coverage is unavailable.",
      limited: true,
    };
  })();

  return (
    <section
      className="panel"
      id="screened-candidates"
      aria-labelledby="screened-candidates-heading"
    >
      <header>
        <div>
          <h2 id="screened-candidates-heading">Screened candidates</h2>
          <p>
            Candidate decisions recorded for this discovery task. The listing body, CV, and model
            output are not stored here. A row is not a recommendation to apply. Employer is the
            recorded organisation; Source is the configured source name. Identical names are shown
            once.
          </p>
        </div>
      </header>
      <section className="toolbar" aria-label="Screened candidate filters">
        <label className="search-field">
          <span>Search screened candidates</span>
          <input
            onChange={(event) => update(() => setSearch(event.target.value))}
            placeholder="Employer, role, place, or source"
            type="search"
            value={search}
          />
        </label>
        <label>
          <span>Source</span>
          <select onChange={(event) => update(() => setSource(event.target.value))} value={source}>
            <option value="">All sources</option>
            {source && !page.sources.includes(source) && (
              <option value={source}>{source} (no retained outcomes)</option>
            )}
            {page.sources.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Candidate type</span>
          <select
            onChange={(event) =>
              update(() =>
                setCandidateKind(event.target.value as SourceScanOutcomeOptions["candidateKind"]),
              )
            }
            value={candidateKind}
          >
            <option value="">All candidate types</option>
            {page.candidate_kinds.map((value) => (
              <option key={value} value={value}>
                {readable(value)}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Outcome</span>
          <select
            onChange={(event) =>
              update(() =>
                setTerminalStage(event.target.value as SourceScanOutcomeOptions["terminalStage"]),
              )
            }
            value={terminalStage}
          >
            <option value="">All outcomes</option>
            {page.terminal_stages.map((value) => (
              <option key={value} value={value}>
                {readable(value)}
              </option>
            ))}
          </select>
        </label>
        {page.reasons.length > 1 && (
          <label>
            <span>Reason</span>
            <select
              onChange={(event) =>
                update(() => setReason(event.target.value as SourceScanOutcomeOptions["reason"]))
              }
              value={reason}
            >
              <option value="">All reasons</option>
              {page.reasons.map((value) => (
                <option key={value} value={value}>
                  {reasonDestinationLabel(value)}
                </option>
              ))}
            </select>
          </label>
        )}
        <button
          className="button button--quiet"
          type="button"
          disabled={!hasFilters}
          onClick={() =>
            update(() => {
              setSearch("");
              setSource("");
              setReason("");
              setTerminalStage("");
              setCandidateKind("");
            })
          }
        >
          Clear filters
        </button>
      </section>
      <p
        className={
          coverage.limited ? "screened-outcomes__limit" : "field-note screened-outcomes__note"
        }
        role="note"
      >
        {coverage.text}
      </p>
      <p aria-live="polite" className="field-note screened-outcomes__note">
        Showing {pageStart}-{pageEnd} of {page.total} recorded outcome{page.total === 1 ? "" : "s"}.
      </p>
      {addFound.isError ? <ErrorState error={addFound.error} /> : null}
      {promotingId && promotedIds.has(promotingId) && refresh.error ? (
        <p className="field-error" role="alert">
          The posting was added to pending, but its read could not be queued:{" "}
          {refresh.error.message}
        </p>
      ) : null}
      {promotingId && promotedIds.has(promotingId) && refresh.status === "failed" ? (
        <p className="field-error" role="alert">
          The posting was added to pending, but its read failed:{" "}
          {refresh.failureMessage || "No failure detail was returned."}
        </p>
      ) : null}
      {page.items.length ? (
        <div className="table-wrap">
          <table className="screened-outcomes-table" aria-busy={outcomes.isFetching}>
            <thead>
              <tr>
                <SortableHeader
                  activeKey={sort}
                  column="employer"
                  direction={direction}
                  label="Employer"
                  onSort={sortBy}
                />
                <SortableHeader
                  activeKey={sort}
                  column="title"
                  direction={direction}
                  label="Role"
                  onSort={sortBy}
                />
                <SortableHeader
                  activeKey={sort}
                  column="location"
                  direction={direction}
                  label="Place"
                  onSort={sortBy}
                />
                <SortableHeader
                  activeKey={sort}
                  column="terminal_stage"
                  direction={direction}
                  label="Outcome"
                  onSort={sortBy}
                />
                <SortableHeader
                  activeKey={sort}
                  column="reason"
                  direction={direction}
                  label="Reason"
                  onSort={sortBy}
                />
                <th scope="col">Source</th>
                <th scope="col">Next action</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((item) => {
                const url = safeHttpUrl(item.url);
                const isActivePromotion = item.id === promotingId;
                return (
                  <tr key={item.id}>
                    <th scope="row">{item.employer || "Not stated"}</th>
                    <td>
                      {url ? (
                        <a href={url} rel="noreferrer" target="_blank">
                          {item.title || LINK_LABEL[item.candidate_kind]}
                        </a>
                      ) : (
                        item.title || "Not stated"
                      )}
                    </td>
                    <td>{item.location || "Not stated"}</td>
                    <td>{readable(item.terminal_stage)}</td>
                    <td>
                      {reasonDestinationLabel(item.reason)}
                      <p className="field-note">{exactReason(item)}</p>
                    </td>
                    <td>
                      {item.source_name && item.source_name === item.employer ? (
                        <span title={item.source_name}>Same name as employer</span>
                      ) : (
                        item.source_name
                      )}
                    </td>
                    <td>
                      {item.terminal_stage === "staged" ? (
                        <span className="field-note">Already added to pending</span>
                      ) : url && item.candidate_kind !== "board_lead" ? (
                        <button
                          aria-label={`Add ${item.title || "posting"} at ${item.employer || "the recorded employer"} to pending and read`}
                          className="button button--quiet"
                          disabled={
                            addFound.isPending || refresh.running || promotedIds.has(item.id)
                          }
                          onClick={() => void promote(item, url)}
                          type="button"
                        >
                          {promotedIds.has(item.id) && isActivePromotion && refresh.running
                            ? "Added; reading posting..."
                            : promotedIds.has(item.id) && isActivePromotion
                              ? refresh.status === "completed"
                                ? "Added; posting read finished"
                                : "Added to pending"
                              : promotedIds.has(item.id)
                                ? "Added to pending"
                                : "Add to pending and read"}
                        </button>
                      ) : (
                        <Link className="button button--quiet" to={SOURCE_CONFIGURATION_PATH}>
                          Review source configuration
                        </Link>
                      )}
                      <SpecialistIcReviewLink findings={item.screening_findings} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState>
          {hasFilters
            ? "No screened candidates match these filters. Change or clear the filters to inspect other candidates from this discovery task."
            : discoveryKind === "guided_campaign"
              ? "This campaign read no candidates."
              : "This discovery task recorded no candidate decisions."}
        </EmptyState>
      )}
      {page.total > page.limit && (
        <nav className="pagination" aria-label="Screened candidate pages">
          <button
            className="button button--quiet"
            disabled={changing || page.offset === 0}
            onClick={() => setOffset(Math.max(0, page.offset - page.limit))}
            type="button"
          >
            Previous
          </button>
          <button
            className="button button--quiet"
            disabled={changing || page.offset + page.limit >= page.total}
            onClick={() => setOffset(page.offset + page.limit)}
            type="button"
          >
            Next
          </button>
        </nav>
      )}
    </section>
  );
}
