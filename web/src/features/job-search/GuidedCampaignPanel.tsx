import { useEffect, useId, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  LATEST_SEARCH_TASK_WINDOW,
  useAbandonGuidedCampaignAttempt,
  useEnqueueTask,
  useGuidedCampaignPreview,
  useGuidedCampaignProviderConfiguration,
  useLatestGuidedCampaignReport,
  useTasks,
  usePendingDiscoveries,
  useRetryTask,
  useSearchArchetypeProposals,
  useSearchParameters,
  useSearchTitleProposals,
  useTaskCosts,
} from "../../api/hooks";
import type {
  GuidedCampaignPreview,
  GuidedCampaignReason,
  GuidedCampaignReport,
  GuidedCampaignStagedBasis,
} from "../../api/client";
import { queryKeys } from "../../api/hooks/keys";
import { useDebouncedValue } from "../../hooks/useDebouncedValue";
import { safeHttpUrl } from "../../api/safeUrl";
import { ConfirmedActionButton } from "../../components/ConfirmedActionButton";
import { PaidAction } from "../../components/PaidAction";
import { Toggletip } from "../../components/Toggletip";
import { formatUsd, UNKNOWN_AMOUNT } from "../../components/text";
import { DiscoveryFunnelAudit } from "./DiscoveryFunnelAudit";
import { SourceScanOutcomes } from "./SourceScanOutcomes";
import { SLOW_RUN_NOTICE, useSearchPanelTask, useSlowNotice } from "./useSearchPanelTask";

function nonnegativeInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

type CampaignRunProgress = {
  completed: number | null;
  current: number | null;
  total: number | null;
  itemLabel: string;
};

function campaignRunProgress(progress: Record<string, unknown>): CampaignRunProgress {
  const current = nonnegativeInteger(progress.current);
  const total = nonnegativeInteger(progress.total);
  const reportedCompleted = nonnegativeInteger(progress.completed);
  const completed = reportedCompleted ?? (current === null ? null : Math.max(0, current - 1));
  const validRange =
    total === null ||
    ((current === null || current <= total) && (completed === null || completed <= total));
  return {
    completed: validRange ? completed : null,
    current: validRange ? current : null,
    total: validRange ? total : null,
    itemLabel: typeof progress.item_label === "string" ? progress.item_label.trim() : "",
  };
}

function CampaignRunStatus({
  progress,
  updatedAt,
}: {
  progress: Record<string, unknown>;
  updatedAt: string;
}) {
  const slow = useSlowNotice(updatedAt);
  const run = campaignRunProgress(progress);
  return (
    <>
      <div className="guided-campaign-progress" role="status">
        <p>
          <strong>Running the search campaign</strong>
        </p>
        {run.completed !== null && run.total !== null && run.total > 0 ? (
          <>
            <progress
              aria-label="Running the search campaign progress"
              max={run.total}
              value={Math.min(run.completed, run.total)}
            />
            <p>
              {run.completed} of {run.total} finished.
              {run.itemLabel ? ` Now: ${run.itemLabel}.` : ""}
            </p>
          </>
        ) : (
          <p>
            {run.itemLabel ? `Now: ${run.itemLabel}.` : "Waiting for the next progress update."}
          </p>
        )}
        {slow && <p className="field-note">{SLOW_RUN_NOTICE}</p>}
      </div>
    </>
  );
}

function placeLabel(place: { country: string; metro: string; region: string }): string {
  return (
    [place.metro, place.region, place.country].filter(Boolean).join(", ") || "Unspecified place"
  );
}

/** Plain labels for the closed campaign vocabularies; unknown values fall back to raw text. */
const CAMPAIGN_VOCAB_LABELS: Record<string, string> = {
  paused_uncertain: "paused, an attempt is uncertain",
  unvisited: "not yet searched",
  unattributable: "searched, not tied to this branch",
  known_posting: "already a known posting",
  registered_board: "already a registered board",
  board_admitted: "board admitted",
  screened_out: "screened out",
  held_out: "held out",
  policy_refused: "refused by publisher policy",
  pending_read: "waiting on a publisher read",
};

function vocabLabel(value: string): string {
  return CAMPAIGN_VOCAB_LABELS[value] ?? value.replaceAll("_", " ");
}

const STAGED_BASIS_LABELS: Record<GuidedCampaignStagedBasis, string> = {
  title_evidence: "Matched a saved title",
  body_focus_term: "Matched a saved focus term",
  model_selected: "Model selected (unverified)",
};

// The report stores the provider-normalized finish, which is not a schema enum.
const FINISH_LABELS: Record<string, string> = {
  complete: "Finished normally",
  truncated: "Cut off by the output limit",
  refused: "Refused by the model",
  filtered: "Stopped by a content filter",
  failed: "Failed at the provider",
  cancelled: "Cancelled",
  unknown: "Stopped for an unstated reason",
};

// The provider does not say which query in a search call returned a URL.
const CALL_WIDE_BRANCHES_NOTE =
  "Branches name every query in the search call that returned the URL, not the one query that found it.";

function finishLabel(finish: string | null): string {
  if (!finish) return "-";
  return FINISH_LABELS[finish] ?? finish;
}

function pluralize(count: number, singular: string, plural = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : plural}`;
}

/** Branches the plan actually assigned to an attempt; the rest stay listed and unsearched. */
function assignedBranches(preview: GuidedCampaignPreview): number {
  return preview.attempts.reduce((total, attempt) => total + attempt.branch_positions.length, 0);
}

/** Hosted searches the planned attempts are capped at, which is at or under the bound. */
function plannedSearches(preview: GuidedCampaignPreview): number {
  return preview.attempts.reduce((total, attempt) => total + attempt.search_cap, 0);
}

const WARNING_EXPLANATIONS: Record<GuidedCampaignReason, { label: string; action: string }> = {
  incomplete_attempts: {
    label: "Some attempts never finished processing.",
    action: "Run another campaign to cover their branches.",
  },
  abandoned_attempt: {
    label: "An uncertain attempt was abandoned, so this campaign is incomplete.",
    action: "Run another campaign to cover its branches.",
  },
  superseded: {
    label: "A newer approved campaign closed this one before it finished.",
    action: "See the newer campaign for current coverage.",
  },
  uncertain_attempt: {
    label: "An attempt's outcome could not be confirmed.",
    action: "Open Attempts below to abandon it; the campaign then closes as incomplete.",
  },
  truncated_response: {
    label: "A response was cut off before it finished.",
    action: "Its branches may need another run to get full coverage.",
  },
  search_overrun: {
    label: "The campaign used more hosted searches than it planned.",
    action: "Every planned attempt still ran; review search counts in Attempts below.",
  },
  protected_family_unsearched: {
    label: "A protected branch family was never searched.",
    action: "Run another campaign to cover it.",
  },
};

function CampaignCostOutlook({ preview }: { preview: GuidedCampaignPreview }) {
  const estimate = preview.estimated_cost_cents;
  const sample =
    preview.estimate_basis === "provider_model"
      ? `completed campaigns on ${preview.provider} ${preview.model}`
      : "completed campaigns";
  return (
    <p aria-label="Campaign cost estimate" className="field-note">
      {estimate === null
        ? "No completed campaign has a recorded cost, so there is no estimate yet."
        : `Estimated ${formatUsd(estimate, { cents: true, places: 2 })} (${pluralize(preview.attempts.length, "attempt")} at the median of ${sample}).`}{" "}
      {preview.planned_search_fee_cents === null
        ? "The hosted-search fee for this model is unpriced."
        : `Hosted-search fees: about ${formatUsd(preview.planned_search_fee_cents, { cents: true, places: 2 })}.`}{" "}
      Cost never stops the campaign.
    </p>
  );
}

/**
 * Coverage branches and their prior outcome, before spending anything.
 */
function CampaignPreviewTables({ preview }: { preview: GuidedCampaignPreview }) {
  return (
    <>
      <p aria-label="Campaign plan facts" className="field-note">
        Plan compiled by {preview.compiler_version}. Each attempt is capped at{" "}
        {pluralize(preview.bounds.searches_per_attempt, "search", "searches")} and{" "}
        {preview.bounds.max_output_tokens.toLocaleString()} output tokens.
      </p>
      <div className="table-wrap guided-campaign-narrow-table">
        <table aria-label="Campaign coverage branches" className="data-table">
          <thead>
            <tr>
              <th scope="col">Label</th>
              <th scope="col">Category</th>
              <th scope="col">Guaranteed coverage</th>
              <th scope="col">Markets</th>
              <th scope="col">Search query</th>
              <th scope="col">Attempt</th>
              <th scope="col">Prior outcome</th>
              <th scope="col">Last searched</th>
            </tr>
          </thead>
          <tbody>
            {preview.branches.map((branch) => (
              <tr key={branch.key}>
                <td>{branch.label}</td>
                <td>{branch.priority_family}</td>
                <td>{branch.protected ? "Yes" : "No"}</td>
                <td>{branch.markets.join(", ") || "Any actionable market"}</td>
                <td>
                  {branch.floor_query ||
                    "No fixed query; the model writes its own searches for this branch"}
                </td>
                <td>{branch.attempt_position ?? "Deferred"}</td>
                <td>
                  {branch.prior_outcome ? vocabLabel(branch.prior_outcome) : "Not yet searched"}
                </td>
                <td>
                  {branch.last_searched_at
                    ? new Date(branch.last_searched_at).toLocaleDateString()
                    : "-"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="table-wrap guided-campaign-narrow-table">
        <table aria-label="Campaign attempt grouping" className="data-table">
          <thead>
            <tr>
              <th scope="col">Attempt</th>
              <th scope="col">Branches</th>
              <th scope="col">Search cap</th>
            </tr>
          </thead>
          <tbody>
            {preview.attempts.map((attempt) => (
              <tr key={attempt.position}>
                <td>{attempt.position}</td>
                <td>{attempt.branch_positions.join(", ")}</td>
                <td>{attempt.search_cap}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

/**
 * Everything the latest campaign found, spent, and left unsettled, regardless
 * of which task last ran.
 */
function CampaignReportTables({
  report,
  workspaceId,
}: {
  report: GuidedCampaignReport;
  workspaceId: string;
}) {
  const abandon = useAbandonGuidedCampaignAttempt(workspaceId);
  const retryProcessing = useEnqueueTask(workspaceId);
  const tasks = useTasks(workspaceId);
  const retryUnfinished = (tasks.data ?? []).some(
    (item) =>
      item.task_type === "search.guided_campaign_retry" &&
      (item.status === "queued" || item.status === "running"),
  );
  const stranded = report.stranded_processing_attempts;
  const uncertainAttempts = report.attempts.filter((item) => item.state === "uncertain");
  const abandonButton = (attempt: (typeof report.attempts)[number]) => {
    const pendingHere = abandon.isPending && abandon.variables?.position === attempt.position;
    const failedHere = abandon.isError && abandon.variables?.position === attempt.position;
    return (
      <div>
        <ConfirmedActionButton
          ariaLabel={`Abandon attempt ${attempt.position}`}
          className="button button--danger"
          disabled={pendingHere}
          onConfirm={() =>
            abandon.mutate({ campaignId: report.campaign_id, position: attempt.position })
          }
          options={{
            title: `Abandon attempt ${attempt.position}?`,
            danger: true,
            description:
              "This abandons the attempt and closes the campaign as incomplete; it cannot be " +
              "undone. Any cost already recorded for it stays. Run a new campaign to cover the " +
              "branches it did not finish.",
            confirmLabel: "Abandon attempt",
          }}
        >
          {pendingHere ? "Abandoning..." : "Abandon attempt"}
        </ConfirmedActionButton>
        {failedHere && <p className="field-error">{abandon.error.message}</p>}
      </div>
    );
  };
  return (
    <>
      <p aria-label="Campaign report status" role="status">
        Status: {vocabLabel(report.status)}. {pluralize(report.findings.length, "new finding")},{" "}
        {pluralize(report.boards_admitted.length, "board")} admitted,{" "}
        {pluralize(report.known_posting_count, "posting")} already known,{" "}
        {pluralize(report.registered_board_count, "board")} already registered.
      </p>
      <p className="field-note">
        Used {report.model_calls_used} of {report.max_model_calls} model calls and{" "}
        {report.hosted_searches_used} of {report.max_hosted_searches} hosted searches. Recorded cost{" "}
        {formatUsd(report.cost_cents, { cents: true, places: 2, unknown: UNKNOWN_AMOUNT })}.
      </p>
      {uncertainAttempts.length > 0 && (
        <div className="field-error" role="alert">
          {uncertainAttempts.map((attempt) => (
            <div key={attempt.position}>
              <p>
                Attempt {attempt.position} could not be confirmed
                {attempt.error_category ? ` (${attempt.error_category.replaceAll("_", " ")})` : ""}.
              </p>
              {abandonButton(attempt)}
            </div>
          ))}
        </div>
      )}
      {stranded > 0 && (
        <div className="field-error" role="alert">
          <p>
            {pluralize(stranded, "captured attempt")} from an earlier campaign{" "}
            {stranded === 1 ? "was" : "were"} not processed after a local failure. Retrying reads
            the publisher pages again and makes no model call.
          </p>
          <button
            className="button"
            disabled={retryProcessing.isPending || retryUnfinished}
            onClick={() =>
              retryProcessing.mutate({
                workspace_id: workspaceId,
                task_type: "search.guided_campaign_retry",
                payload: {},
              })
            }
            type="button"
          >
            {retryProcessing.isPending
              ? "Queueing..."
              : retryUnfinished
                ? "Retry queued"
                : "Retry processing"}
          </button>
          {retryProcessing.isError && (
            <p className="field-error">{retryProcessing.error.message}</p>
          )}
        </div>
      )}
      {report.warnings.length > 0 && (
        <ul>
          {report.warnings.map((warning) => {
            const explanation = WARNING_EXPLANATIONS[warning];
            return <li key={warning}>{`${explanation.label} ${explanation.action}`}</li>;
          })}
        </ul>
      )}
      <details open={uncertainAttempts.length > 0}>
        <summary>Report details</summary>
        {report.findings.length > 0 && (
          <details open>
            <summary>Findings ({report.findings.length})</summary>
            <p className="field-note">{CALL_WIDE_BRANCHES_NOTE}</p>
            <div className="table-wrap guided-campaign-narrow-table">
              <table aria-label="Campaign findings" className="data-table">
                <thead>
                  <tr>
                    <th scope="col">Posting</th>
                    <th scope="col">Staged basis</th>
                    <th scope="col">Branches</th>
                    <th scope="col">Model rationale (not verified)</th>
                  </tr>
                </thead>
                <tbody>
                  {report.findings.map((finding) => {
                    const url = safeHttpUrl(finding.url);
                    const label = `${finding.title} at ${finding.employer}`;
                    return (
                      <tr key={finding.captured_url_id}>
                        <td>
                          {url ? (
                            <a href={url} rel="noreferrer" target="_blank">
                              {label}
                            </a>
                          ) : (
                            label
                          )}
                          {finding.location ? ` (${finding.location})` : ""}
                        </td>
                        <td>{STAGED_BASIS_LABELS[finding.staged_basis]}</td>
                        <td>{finding.branch_labels.join(", ") || "-"}</td>
                        <td className="field-note">{finding.model_rationale ?? "-"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </details>
        )}
        {report.boards_admitted.length > 0 && (
          <details>
            <summary>Boards admitted ({report.boards_admitted.length})</summary>
            <p className="field-note">{CALL_WIDE_BRANCHES_NOTE}</p>
            <div className="table-wrap guided-campaign-narrow-table">
              <table aria-label="Campaign boards admitted" className="data-table">
                <thead>
                  <tr>
                    <th scope="col">Board</th>
                    <th scope="col">Posting staged from its listings</th>
                    <th scope="col">Branches</th>
                  </tr>
                </thead>
                <tbody>
                  {report.boards_admitted.flatMap((board) => {
                    const url = safeHttpUrl(board.url);
                    const boardCell = url ? (
                      <a href={url} rel="noreferrer" target="_blank">
                        {board.employer || board.url}
                      </a>
                    ) : (
                      board.employer || board.url
                    );
                    const postings =
                      board.admitted_postings.length > 0 ? board.admitted_postings : [null];
                    return postings.map((posting, index) => (
                      <tr
                        key={`${board.source_id ?? board.url}-${posting?.pending_discovery_id ?? index}`}
                      >
                        {index === 0 && <td rowSpan={postings.length}>{boardCell}</td>}
                        <td>
                          {posting
                            ? `${posting.title}${posting.location ? ` (${posting.location})` : ""}`
                            : "-"}
                        </td>
                        {index === 0 && (
                          <td rowSpan={postings.length}>{board.branch_labels.join(", ") || "-"}</td>
                        )}
                      </tr>
                    ));
                  })}
                </tbody>
              </table>
            </div>
          </details>
        )}
        {report.not_staged.length > 0 && (
          <details>
            <summary>URLs not staged ({report.not_staged.length})</summary>
            <div className="table-wrap guided-campaign-narrow-table">
              <table aria-label="Campaign URLs not staged" className="data-table">
                <thead>
                  <tr>
                    <th scope="col">URL</th>
                    <th scope="col">Outcome</th>
                    <th scope="col">Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {report.not_staged.map((item, index) => (
                    <tr key={`${item.url}-${index}`}>
                      <td className="path-value">{item.url}</td>
                      <td>{vocabLabel(item.outcome)}</td>
                      <td>{item.detail ?? "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        )}
        <details>
          <summary>Branch coverage ({report.branches.length})</summary>
          <div className="table-wrap guided-campaign-narrow-table">
            <table aria-label="Campaign branch coverage" className="data-table">
              <thead>
                <tr>
                  <th scope="col">Label</th>
                  <th scope="col">Category</th>
                  <th scope="col">Guaranteed coverage</th>
                  <th scope="col">Attempt</th>
                  <th scope="col">Outcome</th>
                  <th scope="col">Matched queries</th>
                </tr>
              </thead>
              <tbody>
                {report.branches.map((branch) => (
                  <tr key={branch.position}>
                    <td>{branch.label}</td>
                    <td>{branch.priority_family}</td>
                    <td>{branch.protected ? "Yes" : "No"}</td>
                    <td>{branch.attempt_position ?? "Deferred"}</td>
                    <td>{vocabLabel(branch.outcome)}</td>
                    <td>{branch.matched_queries.join(", ") || "None"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
        <details open={uncertainAttempts.length > 0}>
          <summary>Attempts ({report.attempts.length})</summary>
          <div className="table-wrap guided-campaign-narrow-table">
            <table aria-label="Campaign attempts" className="data-table">
              <thead>
                <tr>
                  <th scope="col">Action</th>
                  <th scope="col">Attempt</th>
                  <th scope="col">State</th>
                  <th scope="col">Searches</th>
                  <th scope="col">Pages opened</th>
                  <th scope="col">How it ended</th>
                  <th scope="col">Found, not selected</th>
                  <th scope="col">Cost</th>
                </tr>
              </thead>
              <tbody>
                {report.attempts.map((attempt) => (
                  <tr key={attempt.position}>
                    <td>{attempt.state === "uncertain" ? "See the action above." : "-"}</td>
                    <td>{attempt.position}</td>
                    <td>{vocabLabel(attempt.state)}</td>
                    <td>{attempt.hosted_search_count}</td>
                    <td>{attempt.page_action_count}</td>
                    <td>{finishLabel(attempt.finish)}</td>
                    <td>{attempt.captured_not_selected ?? "-"}</td>
                    <td>{formatUsd(attempt.cost_cents, { cents: true, places: 2 })}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      </details>
    </>
  );
}

/**
 * Searches the web for postings beyond saved boards, under an approved cost cap.
 */
export function GuidedCampaignPanel({ workspaceId }: { workspaceId: string }) {
  const queryClient = useQueryClient();
  const boundsId = useId();
  const [selectedTaskId, setSelectedTaskId] = useState("");
  const [modelCallsOverride, setModelCallsOverride] = useState<number | null>(null);
  const [hostedSearchesOverride, setHostedSearchesOverride] = useState<number | null>(null);
  const [modelCallsDraft, setModelCallsDraft] = useState("");
  const [hostedSearchesDraft, setHostedSearchesDraft] = useState("");
  const [draftsInitialized, setDraftsInitialized] = useState(false);
  const [modelCallsEdited, setModelCallsEdited] = useState(false);
  const [hostedSearchesEdited, setHostedSearchesEdited] = useState(false);
  const [defaultBounds, setDefaultBounds] = useState<{
    maxModelCalls: number;
    maxHostedSearches: number;
  } | null>(null);
  const [lastGoodPreview, setLastGoodPreview] = useState<GuidedCampaignPreview | null>(null);
  const debouncedModelCallsDraft = useDebouncedValue(modelCallsDraft, 400);
  const debouncedHostedSearchesDraft = useDebouncedValue(hostedSearchesDraft, 400);
  const costs = useTaskCosts(workspaceId);
  const campaignConfiguration = useGuidedCampaignProviderConfiguration(workspaceId, true);
  const search = useEnqueueTask(workspaceId);
  const retry = useRetryTask(workspaceId);
  const tasks = useTasks(workspaceId);
  const pendingDiscoveries = usePendingDiscoveries(workspaceId, 1);
  const parameters = useSearchParameters(workspaceId);
  const preview = useGuidedCampaignPreview(workspaceId, modelCallsOverride, hostedSearchesOverride);
  const latestReport = useLatestGuidedCampaignReport(workspaceId);
  const titleProposals = useSearchTitleProposals(workspaceId);
  const archetypeProposals = useSearchArchetypeProposals(workspaceId);
  // Set during render so the drafts take the first loaded bounds and later fetches leave them.
  if (!draftsInitialized && preview.data) {
    if (!modelCallsEdited) setModelCallsDraft(String(preview.data.bounds.max_model_calls));
    if (!hostedSearchesEdited) {
      setHostedSearchesDraft(String(preview.data.bounds.max_hosted_searches));
    }
    setDefaultBounds({
      maxModelCalls: preview.data.bounds.max_model_calls,
      maxHostedSearches: preview.data.bounds.max_hosted_searches,
    });
    setDraftsInitialized(true);
  }
  if (preview.data && preview.data !== lastGoodPreview) {
    setLastGoodPreview(preview.data);
  }
  const parsePositiveInt = (text: string): number | null => {
    const trimmed = text.trim();
    if (!/^\d+$/.test(trimmed)) return null;
    const value = Number(trimmed);
    return value >= 1 ? value : null;
  };
  const modelCallsParsed = parsePositiveInt(debouncedModelCallsDraft);
  if (modelCallsParsed !== null && modelCallsParsed !== modelCallsOverride) {
    setModelCallsOverride(modelCallsParsed);
  }
  const hostedSearchesParsed = parsePositiveInt(debouncedHostedSearchesDraft);
  if (hostedSearchesParsed !== null && hostedSearchesParsed !== hostedSearchesOverride) {
    setHostedSearchesOverride(hostedSearchesParsed);
  }
  const boundsEditable = draftsInitialized || preview.isError;
  const boundDraftError = (draft: string, edited: boolean): string | null =>
    edited && parsePositiveInt(draft) === null ? "Enter a whole number of 1 or more." : null;
  const boundHint = (fallback: number | undefined): string =>
    fallback === undefined
      ? "Whole number, 1 or more."
      : `Whole number, 1 or more. Default ${fallback}.`;
  const modelCallsDraftError = boundDraftError(modelCallsDraft, modelCallsEdited);
  const hostedSearchesDraftError = boundDraftError(hostedSearchesDraft, hostedSearchesEdited);
  const resetBoundsToDefault = () => {
    setModelCallsEdited(false);
    setHostedSearchesEdited(false);
    setModelCallsOverride(null);
    setHostedSearchesOverride(null);
    if (defaultBounds) {
      setModelCallsDraft(String(defaultBounds.maxModelCalls));
      setHostedSearchesDraft(String(defaultBounds.maxHostedSearches));
    }
  };
  // Stale means the loaded preview's bounds do not match what the inputs
  // currently show, not merely that a debounce timer is still pending.
  const modelCallsDraftNow = parsePositiveInt(modelCallsDraft);
  const hostedSearchesDraftNow = parsePositiveInt(hostedSearchesDraft);
  const previewMatchesInputs =
    preview.data !== undefined &&
    (modelCallsDraftNow === null || modelCallsDraftNow === preview.data.bounds.max_model_calls) &&
    (hostedSearchesDraftNow === null ||
      hostedSearchesDraftNow === preview.data.bounds.max_hosted_searches);
  const previewStale = preview.isFetching || preview.isPlaceholderData || !previewMatchesInputs;
  const taskRows = tasks.data ?? [];
  const campaignRows = taskRows.filter((item) => item.task_type === "search.guided_campaign");
  const latestCampaign = campaignRows
    .slice()
    .sort((left, right) => right.created_at.localeCompare(left.created_at))[0];
  const campaignOutOfView = !latestCampaign && taskRows.length >= LATEST_SEARCH_TASK_WINDOW;
  const taskId = selectedTaskId || latestCampaign?.id || "";
  const { task, queued, running, failedTask, terminal, taskCost } = useSearchPanelTask(
    workspaceId,
    taskId,
  );
  const queueing = search.isPending;
  const retrying = retry.isPending;
  const taskStateUnavailable = tasks.isError || Boolean(taskId && task.isError);
  const awaitingTaskState =
    (!tasks.isError && tasks.data === undefined) ||
    Boolean(taskId && !task.isError && task.data === undefined);
  const busy =
    queueing || retrying || queued || running || awaitingTaskState || taskStateUnavailable;
  const progress = task.data?.progress ?? {};
  useEffect(() => {
    if (terminal) {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.latestGuidedCampaignReport(workspaceId),
      });
      void queryClient.invalidateQueries({
        queryKey: queryKeys.pendingQueuePrefix(workspaceId),
      });
    }
  }, [queryClient, taskId, terminal, workspaceId]);
  const scopeReady = [parameters, titleProposals, archetypeProposals].every(
    (query) => query.data !== undefined && !query.isError,
  );
  const previewComplete = Boolean(lastGoodPreview) && lastGoodPreview?.branches.length === 0;
  const savedTitles = parameters.data?.parameters.seniority?.target_titles ?? [];
  const acceptedTitles = (titleProposals.data?.items ?? []).filter(
    (item) => item.state === "accepted",
  );
  const places = parameters.data?.parameters.locations ?? [];
  const acceptedArchetypes = (archetypeProposals.data?.items ?? []).filter(
    (item) => item.state === "accepted",
  );
  const stagedCount = latestReport.data?.findings.length ?? 0;
  const inFlightLabel = (idle: string) =>
    queueing ? "Queueing search" : queued ? "Search queued" : running ? "Search running" : idle;
  const webConfirmationDescription = lastGoodPreview
    ? `Runs up to ${lastGoodPreview.bounds.max_model_calls} model calls and ${plannedSearches(lastGoodPreview)} hosted searches over ${assignedBranches(lastGoodPreview)} of ${pluralize(lastGoodPreview.branches.length, "branch", "branches")} in ${pluralize(lastGoodPreview.attempts.length, "attempt")} on ${lastGoodPreview.provider} ${lastGoodPreview.model}. ${costs.confirmation("search.guided_campaign", lastGoodPreview.attempts.length)}`
    : costs.confirmation("search.guided_campaign");
  const webAction = (
    <div>
      <PaidAction
        compact
        confirmation={{
          title: "Search the web for new jobs?",
          description: webConfirmationDescription,
          details: <p className="field-note">{campaignConfiguration.description}</p>,
          confirmLabel: "Search the web",
        }}
        costs={costs}
        disabled={
          busy ||
          !lastGoodPreview ||
          previewComplete ||
          previewStale ||
          preview.isError ||
          Boolean(modelCallsDraftError) ||
          Boolean(hostedSearchesDraftError) ||
          !costs.loaded ||
          !campaignConfiguration.loaded ||
          campaignConfiguration.isError ||
          !scopeReady
        }
        label={
          !retrying && (queueing || queued || running)
            ? inFlightLabel("Search the web for new jobs")
            : preview.isFetching || (!preview.isError && previewStale)
              ? "Updating preview"
              : "Search the web for new jobs"
        }
        onAct={() => {
          const approved = preview.data;
          if (!approved || approved.branches.length === 0) return;
          search.mutate(
            {
              workspace_id: workspaceId,
              task_type: "search.guided_campaign",
              payload: {
                campaign_manifest_sha256: approved.manifest_sha256,
                campaign_cycle: approved.cycle,
                max_model_calls: approved.bounds.max_model_calls,
                max_hosted_searches: approved.bounds.max_hosted_searches,
              },
            },
            { onSuccess: (nextTask) => setSelectedTaskId(nextTask.id) },
          );
        }}
        taskType="search.guided_campaign"
      />
      {campaignConfiguration.isError ? (
        <p className="field-error" aria-label="Web search provider status" role="status">
          {campaignConfiguration.heldBecause === "undescribable"
            ? "Web search answered, but its provider and model could not be read, so paid work is held until they can be shown. Check the provider and models in "
            : "Web search configuration is unavailable. Check the provider and models in "}
          <a href="/settings?tab=workspace">Settings</a>, then reload.
        </p>
      ) : !campaignConfiguration.loaded ? (
        <p className="field-note" aria-label="Web search provider status" role="status">
          Reading the web search provider and models before payment.
        </p>
      ) : null}
      {lastGoodPreview?.supersedes_campaign_id && (
        <p className="field-note" aria-label="Unfinished campaign notice">
          An earlier campaign did not finish. Starting this one collects any response it already
          paid for, then closes it as incomplete.
        </p>
      )}
    </div>
  );
  return (
    <div className="guided-campaign-layout">
      <section className="panel guided-campaign-start" aria-label="Start a new web search">
        <header>
          <div>
            <h2>Search the web</h2>
            <div>
              Find postings and boards beyond your saved boards.{" "}
              <Toggletip label="About a running web search">
                While a search runs, other paid tasks wait. You cannot stop it from here.
              </Toggletip>
            </div>
          </div>
          <div className="button-row">{webAction}</div>
        </header>
        <div className="panel-body">
          <p aria-label="Guided campaign plan" className="field-note">
            {lastGoodPreview
              ? previewComplete
                ? "Every coverage branch has already been searched. Accept a suggested term below or edit search preferences to add more."
                : `Plan ready: ${assignedBranches(lastGoodPreview)} of ${pluralize(lastGoodPreview.branches.length, "branch", "branches")} in ${pluralize(lastGoodPreview.attempts.length, "attempt")}.`
              : preview.isError
                ? "The campaign preview could not be read."
                : "Reading the campaign preview before payment."}
          </p>
          {preview.isError && (
            <p className="field-error">
              The campaign preview could not be {lastGoodPreview ? "refreshed" : "read"}:{" "}
              {preview.error.message}{" "}
              <button
                className="button button--quiet"
                onClick={() => void preview.refetch()}
                type="button"
              >
                Retry
              </button>
            </p>
          )}
          {preview.isError && !lastGoodPreview && (
            <p className="field-note">
              Guided search requires a master CV and provider access.{" "}
              <a className="text-button" href="/cv">
                Import a master CV
              </a>{" "}
              or{" "}
              <a className="text-button" href="/settings?tab=provider">
                Configure provider access in Settings
              </a>
              , then retry the preview.
            </p>
          )}
          {lastGoodPreview && <CampaignCostOutlook preview={lastGoodPreview} />}
          <details>
            <summary>Advanced</summary>
            <div className="button-row">
              <label className="compact-field">
                <span>Model calls</span>
                <input
                  aria-describedby={`${boundsId}-model-calls`}
                  aria-invalid={Boolean(modelCallsDraftError) || undefined}
                  aria-label="Model calls"
                  disabled={!boundsEditable}
                  min={1}
                  onChange={(event) => {
                    setModelCallsEdited(true);
                    setModelCallsDraft(event.target.value);
                  }}
                  type="number"
                  value={modelCallsDraft}
                />
                {modelCallsDraftError ? (
                  <p className="field-error" id={`${boundsId}-model-calls`} role="alert">
                    {modelCallsDraftError}
                  </p>
                ) : (
                  <small id={`${boundsId}-model-calls`}>
                    {boundHint(defaultBounds?.maxModelCalls)}
                  </small>
                )}
              </label>
              <label className="compact-field">
                <span>Hosted searches</span>
                <input
                  aria-describedby={`${boundsId}-hosted-searches`}
                  aria-invalid={Boolean(hostedSearchesDraftError) || undefined}
                  aria-label="Hosted searches"
                  disabled={!boundsEditable}
                  min={1}
                  onChange={(event) => {
                    setHostedSearchesEdited(true);
                    setHostedSearchesDraft(event.target.value);
                  }}
                  type="number"
                  value={hostedSearchesDraft}
                />
                {hostedSearchesDraftError ? (
                  <p className="field-error" id={`${boundsId}-hosted-searches`} role="alert">
                    {hostedSearchesDraftError}
                  </p>
                ) : (
                  <small id={`${boundsId}-hosted-searches`}>
                    {boundHint(defaultBounds?.maxHostedSearches)}
                  </small>
                )}
              </label>
              <button
                className="button button--quiet"
                disabled={!defaultBounds}
                onClick={resetBoundsToDefault}
                type="button"
              >
                Reset to defaults
              </button>
            </div>
            {lastGoodPreview && (
              <details>
                <summary>Review the campaign preview</summary>
                <CampaignPreviewTables preview={lastGoodPreview} />
              </details>
            )}
            {scopeReady && (
              <dl aria-label="Search scope">
                <div>
                  <dt>Target titles</dt>
                  <dd>{savedTitles.map((item) => item.title).join(", ") || "None saved"}</dd>
                </div>
                <div>
                  <dt>Accepted future titles</dt>
                  <dd>{acceptedTitles.map((item) => item.title).join(", ") || "None accepted"}</dd>
                </div>
                <div>
                  <dt>Places</dt>
                  <dd>{places.map(placeLabel).join("; ") || "No places configured"}</dd>
                </div>
                <div>
                  <dt>Accepted employer kinds</dt>
                  <dd>
                    {acceptedArchetypes.map((item) => item.name).join(", ") || "None accepted"}
                  </dd>
                </div>
              </dl>
            )}
          </details>
          <p className="field-note">
            <a href="/find?tab=boards">Scan your saved boards</a>.{" "}
            <a href="/settings?tab=search">Edit search preferences</a>.{" "}
            <a href="/settings?tab=costs">See spending in Settings</a>.
          </p>
          {!scopeReady &&
            ([parameters, titleProposals, archetypeProposals].some((query) => query.isError) ? (
              <p className="field-error">
                The saved search scope could not be read, so this paid search cannot start.
              </p>
            ) : (
              <p aria-label="Guided campaign setup status" role="status">
                Reading the saved search scope before payment.
              </p>
            ))}
          {search.isError && <p className="field-error">{search.error.message}</p>}
          {retry.isError && <p className="field-error">{retry.error.message}</p>}
          {taskStateUnavailable && (
            <p className="field-error">
              Recent guided search status could not be read. Reload before starting another paid
              search.
            </p>
          )}
        </div>
      </section>
      {latestReport.isError ? (
        <section className="panel" aria-label="Campaign report">
          <header>
            <h2>Campaign report</h2>
          </header>
          <div className="panel-body">
            <p className="field-error">
              The latest campaign report could not be read.{" "}
              <button
                className="button button--quiet"
                onClick={() => void latestReport.refetch()}
                type="button"
              >
                Retry
              </button>
            </p>
          </div>
        </section>
      ) : latestReport.data === null ? null : (
        <section className="panel" aria-label="Campaign report">
          <header>
            <h2>Campaign report</h2>
          </header>
          <div className="panel-body">
            {latestReport.data === undefined ? (
              <p>Reading the latest campaign report.</p>
            ) : (
              <>
                <section aria-labelledby="campaign-next-decision">
                  <h3 id="campaign-next-decision">Next decision</h3>
                  {stagedCount === 0 ? (
                    <p>
                      Nothing reached the pending queue. Review the branch and attempt details below
                      before deciding whether to pay for another search.
                    </p>
                  ) : pendingDiscoveries.isError ? (
                    <p>
                      This run originally staged {pluralize(stagedCount, "posting")}, but the
                      current pending queue could not be read. Reload before deciding whether to pay
                      for another search.
                    </p>
                  ) : pendingDiscoveries.data === undefined ? (
                    <p>
                      This run originally staged {pluralize(stagedCount, "posting")}. Reading the
                      current pending queue.
                    </p>
                  ) : pendingDiscoveries.data.length > 0 ? (
                    <p>
                      This run originally staged {pluralize(stagedCount, "posting")}. Review what is
                      waiting before you search again.{" "}
                      <a className="button button--primary" href="/find?tab=review">
                        Open Review
                      </a>
                    </p>
                  ) : (
                    <p>
                      This run originally staged {pluralize(stagedCount, "posting")}. None remain
                      pending; they have since been evaluated, dismissed, or archived.
                    </p>
                  )}
                </section>
                <CampaignReportTables report={latestReport.data} workspaceId={workspaceId} />
              </>
            )}
          </div>
        </section>
      )}
      {(taskId || queueing || retrying || campaignOutOfView) && (
        <section className="panel guided-campaign-result" aria-label="Latest search">
          <header>
            <h2>
              {running || queued || queueing || retrying ? "Search in progress" : "Latest search"}
            </h2>
            {taskId && <a href="/tasks">Task history</a>}
          </header>
          <div className="panel-body">
            {failedTask && !retrying && (
              <div className="field-error" role="alert">
                <p>
                  Search failed: {failedTask.error_message || "No failure detail was returned."}
                </p>
                <p>Review the failure and recorded cost before retrying.</p>
              </div>
            )}
            {queueing ? (
              <p aria-label="Guided campaign queue status" role="status">
                Queueing search.
              </p>
            ) : retrying ? (
              <p aria-label="Guided campaign queue status" role="status">
                Queueing retry.
              </p>
            ) : queued ? (
              <p aria-label="Guided campaign queue status" role="status">
                Queued - waiting for the local worker.
              </p>
            ) : running ? (
              <>
                <CampaignRunStatus
                  key={task.data?.updated_at}
                  progress={progress}
                  updatedAt={task.data?.updated_at ?? new Date().toISOString()}
                />
                {(task.data?.attempt_count ?? 0) > 1 && (
                  <p className="field-note">
                    The worker recovered this task and is running attempt {task.data?.attempt_count}
                    .
                  </p>
                )}
              </>
            ) : null}
            {terminal && (
              <>
                {task.data?.status === "cancelled" && <p>Search cancelled.</p>}
                <p aria-label="Task billing status" className="field-note" role="status">
                  {taskCost}
                </p>
                <div className="button-row">
                  {failedTask && (!busy || retrying) && (
                    <PaidAction
                      compact
                      confirmation={{
                        title: "Retry this guided search?",
                        description: `${taskCost} This repeats the failed search. ${costs.confirmation("search.guided_campaign")}`,
                        details: <p className="field-note">{campaignConfiguration.description}</p>,
                        confirmLabel: "Retry guided search",
                      }}
                      costs={costs}
                      disabled={
                        busy ||
                        !costs.loaded ||
                        !campaignConfiguration.loaded ||
                        campaignConfiguration.isError
                      }
                      label={retrying ? "Queueing retry" : "Retry this search"}
                      onContinue={() => {
                        retry.mutate(failedTask.id, {
                          onSuccess: (nextTask) => setSelectedTaskId(nextTask.id),
                        });
                      }}
                      taskType="search.guided_campaign"
                    />
                  )}
                </div>
              </>
            )}
            {campaignOutOfView && (
              <p aria-label="Guided campaign history status" role="status">
                Earlier recorded guided searches may be out of view; only the most recent{" "}
                {LATEST_SEARCH_TASK_WINDOW} tasks were read.
              </p>
            )}
            {task.data?.status === "completed" && (
              <>
                <p className="field-note">
                  See the Campaign report above for findings and coverage.
                </p>
                <DiscoveryFunnelAudit discoveryKind="guided_campaign" value={progress.funnel} />
                <p className="field-note">
                  Every candidate this run read is listed under Screened candidates below, where you
                  can search, filter, and sort it.
                </p>
              </>
            )}
            {taskId ? (
              <SourceScanOutcomes
                discoveryKind="guided_campaign"
                taskId={taskId}
                workspaceId={workspaceId}
              />
            ) : null}
          </div>
        </section>
      )}
    </div>
  );
}
