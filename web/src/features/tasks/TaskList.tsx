import { useState } from "react";

import type { Task } from "../../api/client";
import {
  taskBillingSummary,
  useCancelTask,
  useGuidedCampaignProviderConfiguration,
  useRetryTask,
  useResumeTask,
  useSearchVocabularyProviderConfiguration,
  useTaskBilling,
  useTaskCosts,
  useTasks,
} from "../../api/hooks";
import type { SearchProviderConfiguration } from "../../api/hooks/tasks";
import { text } from "../../api/json";
import { attemptCount, taskKindOf, taskSpendDisclosure } from "../../api/taskNames";
import { ConfirmedActionButton } from "../../components/ConfirmedActionButton";
import { ErrorNotice } from "../../components/ErrorNotice";
import { Page } from "../../components/Page";
import { PaidAction } from "../../components/PaidAction";
import { StatusBadge } from "../../components/StatusBadge";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { formatUsd } from "../../components/text";
import { useWorkspace } from "../../providers";

// Every count, list size, state, and verdict is named by rule, so only a key
// whose own name would mislead a person is listed here.
const PROGRESS_LABELS: Record<string, string> = {
  unverified: "Could not verify",
  content_readable: "Posting text readable",
  content_unreadable: "Posting text unreadable",
  content_kept: "Readable text kept after a failed re-read",
  member_count: "Jobs compared",
  scanned: "Sources scanned",
  pending: "Still pending",
  policy_deferred: "Held by policy",
  cost_usd: "Cost",
  count: "Links in this batch",
  processed_attempts: "Search attempts reprocessed",
  withdrawn_on_read: "Queued rows withdrawn once their posting was fetched",
  refresh_fallback: "Re-read failed, used the stored posting text",
};

// A detail a person does not act on mid-run, kept in the payload disclosure.
const PAYLOAD_ONLY = new Set(["evaluation_version"]);

// A label that states the whole fact, so its value is not repeated after it.
const STATED_BY_LABEL = new Set(["refresh_fallback"]);

/** Spell an enum-like value in words, leaving URLs, hosts, and free text as reported. */
function humanized(value: string): string {
  return /^[a-z0-9]+(_[a-z0-9]+)*$/.test(value) ? value.replaceAll("_", " ") : value;
}

/** The label for one snake_case payload key or group name. */
function progressLabel(key: string): string {
  const words = key.replaceAll("_", " ");
  return PROGRESS_LABELS[key] ?? words.charAt(0).toUpperCase() + words.slice(1);
}

/** Lists the sentence does not count: identifiers, and a bucket another key already counts. */
function uncountedLists(entries: [string, unknown][]): Set<string> {
  const counts = entries.filter((entry): entry is [string, number] => typeof entry[1] === "number");
  return new Set(
    entries
      .filter(
        ([key, value]) =>
          Array.isArray(value) &&
          (key.endsWith("_ids") ||
            counts.some(([name, count]) => key.startsWith(`${name}_`) && count === value.length)),
      )
      .map(([key]) => key),
  );
}

/** A list of texts, such as a campaign's stop reasons, named by its own contents. */
function listContentsLine(key: string, value: unknown[]): string | null {
  if (!value.length || !value.every((item) => typeof item === "string" && item.length > 0)) {
    return null;
  }
  const items = (value as string[]).map(humanized);
  return `${progressLabel(key)}: ${items.join(", ")}`;
}

/** One named count, list size, state, or text, or null when the value reads elsewhere. */
function namedProgressLine(key: string, value: unknown, uncounted: Set<string>): string | null {
  if (PAYLOAD_ONLY.has(key)) return null;
  const label = progressLabel(key);
  if (typeof value === "number" && !key.endsWith("_id")) {
    return `${label} ${key.endsWith("_usd") ? formatUsd(value) : String(value)}`;
  }
  if (Array.isArray(value)) {
    if (uncounted.has(key) || listContentsLine(key, value)) return null;
    return `${label} ${value.length}`;
  }
  if (typeof value === "boolean") return `${label}: ${value ? "yes" : "no"}`;
  if (typeof value === "string" && !key.endsWith("_id") && value.length > 0) {
    return STATED_BY_LABEL.has(key) ? label : `${label}: ${humanized(value)}`;
  }
  return null;
}

interface CountGroup {
  key: string;
  label: string;
  rows: [string, number][];
}

/** The group and count rows of an object whose every value is a number. */
function countRows(value: unknown): [string, number][] | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const entries = Object.entries(value as Record<string, unknown>);
  if (!entries.length || !entries.every(([, count]) => typeof count === "number")) return null;
  return entries as [string, number][];
}

/** Count groups at the top of the payload and one level inside an object such as the funnel. */
function countGroups(entries: [string, unknown][]): CountGroup[] {
  const groups = groupsByPath(entries);
  const repeated = new Set(
    groups
      .map((group) => group.label)
      .filter((label, index, labels) => labels.indexOf(label) !== index),
  );
  // Two groups can humanize to one label, which leaves their tables indistinguishable.
  return groups.map((group) =>
    repeated.has(group.label) ? { ...group, label: `${group.label} (${group.key})` } : group,
  );
}

function groupsByPath(entries: [string, unknown][]): CountGroup[] {
  return entries.flatMap(([key, value]) => {
    const rows = countRows(value);
    if (rows) return [{ key, label: progressLabel(key), rows }];
    if (typeof value !== "object" || value === null || Array.isArray(value)) return [];
    return Object.entries(value as Record<string, unknown>).flatMap(([group, nested]) => {
      const nestedRows = countRows(nested);
      return nestedRows
        ? [{ key: `${key}.${group}`, label: progressLabel(`${key}_${group}`), rows: nestedRows }]
        : [];
    });
  });
}

const CONTINUATION_BATCH_LABELS: [string, string][] = [
  ["read", "read"],
  ["matched", "title match"],
  ["no_title_match", "no title match"],
  ["refused", "refused on a hard constraint"],
  ["staged", "newly queued"],
  ["content_resolved", "body on file"],
];

function countedParts(progress: Record<string, unknown>, labels: [string, string][]): string[] {
  return labels.flatMap(([key, label]) =>
    typeof progress[key] === "number" ? [`${progress[key]} ${label}`] : [],
  );
}

const CONTINUATION_LINK_LABELS: [string, string][] = [
  ["completed", "read"],
  ["queued", "queued"],
  ["failed", "failed"],
  ["policy_deferred", "need your read"],
];

const CONTINUATION_KEYS = new Set([
  ...CONTINUATION_BATCH_LABELS.map(([key]) => key),
  ...CONTINUATION_LINK_LABELS.map(([key]) => key),
  "pending",
]);

/** Publisher link counts cover only the source's known frontier. */
function continuationLines(progress: Record<string, unknown>, active: boolean): string[] {
  const batch = countedParts(progress, CONTINUATION_BATCH_LABELS);
  const manual = typeof progress.policy_deferred === "number" ? progress.policy_deferred : 0;
  // Pending includes the rows only a person can read, which have their own count.
  const queued = typeof progress.pending === "number" ? progress.pending - manual : undefined;
  const links = countedParts({ ...progress, queued }, CONTINUATION_LINK_LABELS);
  return [
    batch.length ? `Openings this batch: ${batch.join(", ")}` : "",
    links.length ? `Publisher links for this source: ${links.join(", ")}` : "",
    active && links.length
      ? "Queued links are those found so far. The final remaining total is unknown because pages can reveal more links."
      : "",
  ].filter(Boolean);
}

function formatProgressValue(value: unknown): string {
  if (Array.isArray(value)) {
    if (value.length === 0) return "0";
    const entries: unknown[] = value;
    if (
      entries.every(
        (item) =>
          typeof item === "object" &&
          item !== null &&
          "reason" in item &&
          ("job_posting_id" in item || "pending_discovery_id" in item),
      )
    ) {
      return entries.map(taskItemFailure).join("; ");
    }
    if (entries.every((item) => typeof item === "object" && item !== null && "reason" in item)) {
      const reasons = new Map<string, number>();
      for (const item of entries as { reason: unknown }[]) {
        const reason = humanized(text(item.reason));
        reasons.set(reason, (reasons.get(reason) ?? 0) + 1);
      }
      const parts = Array.from(reasons.entries()).map(([reason, count]) => `${reason} (${count})`);
      return `${value.length} (${parts.join(", ")})`;
    }
    if (entries.every((item) => typeof item === "string")) return entries.join(", ");
    return String(value.length);
  }
  if (typeof value === "object" && value !== null) {
    const counts = Object.entries(value as Record<string, unknown>).flatMap(([name, count]) =>
      typeof count === "number" ? [`${count} ${humanized(name)}`] : [],
    );
    if (counts.length > 0 && counts.length === Object.keys(value).length) {
      return counts.join(", ");
    }
    return JSON.stringify(value);
  }
  return String(value);
}

/** What the payload block still has to show for one key, or null when nothing is left. */
function payloadEntry(
  key: string,
  value: unknown,
  uncounted: Set<string>,
): [string, unknown] | null {
  // A list the sentence names by its count or its contents is spent; an identifier
  // list and a by-reason breakdown read here.
  if (Array.isArray(value)) {
    if (uncounted.has(key)) return [key, value];
    if (listContentsLine(key, value)) return null;
    return formatProgressValue(value) === String(value.length) ? null : [key, value];
  }
  if (namedProgressLine(key, value, uncounted) !== null || countRows(value)) return null;
  if (typeof value !== "object" || value === null) return [key, value];
  const kept = Object.entries(value as Record<string, unknown>).filter(
    ([, nested]) => countRows(nested) === null,
  );
  return kept.length ? [key, Object.fromEntries(kept)] : null;
}

function taskItemFailure(value: unknown): string | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const id = text(
    record.job_posting_id ?? record.pending_discovery_id ?? record.id,
    "Unknown item",
  );
  const message = humanized(
    text(record.error_message ?? record.note ?? record.reason, "No detail returned."),
  );
  return `${id}: ${message}`;
}

interface ItemRow {
  id: string;
  reason: string;
  detail: string;
}

/** The rows of a per-item list whose records name their row, a reason code, and a detail. */
function itemRows(value: unknown, reasonKey: string, missingDetail: string): ItemRow[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item: unknown) => {
    if (typeof item !== "object" || item === null || Array.isArray(item)) return [];
    const record = item as Record<string, unknown>;
    return [
      {
        id: text(record.pending_discovery_id ?? record.job_posting_id, "Unknown row"),
        reason: humanized(text(record[reasonKey], "unknown")),
        detail: text(record.detail, missingDetail),
      },
    ];
  });
}

function ItemRowsTable({ label, rows }: { label: string; rows: ItemRow[] }) {
  return (
    <div className="table-wrap">
      <table aria-label={label} className="data-table">
        <thead>
          <tr>
            <th scope="col">Row</th>
            <th scope="col">Reason</th>
            <th scope="col">Detail</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.id}:${row.reason}`}>
              <th scope="row">{row.id}</th>
              <td>{row.reason}</td>
              <td>{row.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const NO_PROVIDER_CONFIGURATION: SearchProviderConfiguration = {
  description: "",
  isError: false,
  loaded: true,
  heldBecause: null,
};

function TaskArticle({
  cancel,
  costs,
  campaignConfiguration,
  vocabularyConfiguration,
  retry,
  task,
  workspaceId,
}: {
  cancel: ReturnType<typeof useCancelTask>;
  costs: ReturnType<typeof useTaskCosts>;
  campaignConfiguration: SearchProviderConfiguration;
  vocabularyConfiguration: SearchProviderConfiguration;
  retry: ReturnType<typeof useRetryTask>;
  task: Task;
  workspaceId: string;
}) {
  const [reviewingPaidRetry, setReviewingPaidRetry] = useState(false);
  const resume = useResumeTask(workspaceId);
  const active = ["queued", "running"].includes(task.status);
  const cancellationRequested = active && task.cancellation_requested_at !== null;
  const interrupted = ["failed", "cancelled"].includes(task.status);
  const sourceScan = task.task_type === "source.scan";
  const sourceContinuation = task.task_type === "source.continue";
  const sourceRecovery = sourceScan || sourceContinuation;
  const freshScan = sourceScan && !task.source_resume_from_task_id;
  const retryable = interrupted && !sourceRecovery;
  const paid = costs.isPaid(task.task_type);
  const providerConfigurationRequired =
    task.task_type === "search.guided_campaign" || task.task_type === "search.vocabulary";
  const providerConfiguration =
    task.task_type === "search.guided_campaign"
      ? campaignConfiguration
      : task.task_type === "search.vocabulary"
        ? vocabularyConfiguration
        : NO_PROVIDER_CONFIGURATION;
  const providerConfigurationUnavailable =
    providerConfigurationRequired &&
    (!providerConfiguration.loaded || providerConfiguration.isError);
  const createdAt = new Date(task.created_at).toLocaleString();
  const taskReference = task.id.slice(0, 8);
  const kind = taskKindOf(task);
  const instanceName = kind.name;
  const taskLabel = `${instanceName} created ${createdAt}, task ${taskReference}`;
  const billing = useTaskBilling(
    workspaceId,
    task.id,
    retryable && costs.loaded && !costs.isError && paid && reviewingPaidRetry,
  );
  const billingSummary =
    retryable && costs.loaded && !costs.isError && paid && reviewingPaidRetry
      ? taskBillingSummary(billing.data, billing.isError)
      : "";
  const billingPending = billing.data === undefined && !billing.isError;
  const confirmationDescription = [
    billingSummary,
    "Retrying repeats provider work and can incur another charge.",
    providerConfigurationRequired ? providerConfiguration.description : "",
    taskSpendDisclosure(task.task_type),
    costs.confirmation(task.task_type),
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <article aria-label={taskLabel}>
      <div className="task-icon">{task.task_type.slice(0, 1).toUpperCase()}</div>
      <div className="task-copy">
        <div>
          <strong>{instanceName}</strong>
          <StatusBadge value={task.status} />
        </div>
        <p className="task-detail">{kind.detail}</p>
        <TaskProgress
          active={active}
          error={task.error_message}
          progress={task.progress}
          running={task.status === "running"}
          taskType={task.task_type}
        />
        {cancellationRequested && (
          <p className="field-note" role="status">
            Cancellation requested. The worker will stop at its next safe cancellation point.
          </p>
        )}
        {billingSummary && (
          <p aria-label={`Billing for ${taskLabel}`} className="field-note" role="status">
            {billingSummary}
          </p>
        )}
        {resume.isError && (
          <p className="field-error" role="alert">
            Resume failed: {resume.error.message}
          </p>
        )}
        {retry.isError && retry.variables === task.id && (
          <p className="field-error" role="alert">
            {freshScan ? "Scan again" : "Retry"} failed: <ErrorNotice error={retry.error} />
          </p>
        )}
        {cancel.isError && cancel.variables === task.id && (
          <p className="field-error" role="alert">
            Cancel failed: {cancel.error.message}
          </p>
        )}
        {reviewingPaidRetry &&
          providerConfigurationRequired &&
          !providerConfiguration.loaded &&
          !providerConfiguration.isError && (
            <p aria-label={`Provider status for ${taskLabel}`} className="field-note" role="status">
              Reading the provider model and effort.
            </p>
          )}
        {interrupted && sourceContinuation && (
          <p className="field-note">
            This read stays incomplete at least until its deferred links are read, and board scan
            history keeps it only while no newer read of the same kind exists for this source.{" "}
            <a className="text-button" href="/find?tab=boards#source-scan-history-heading">
              Open board scan history to read its deferred links
            </a>
          </p>
        )}
        <span>
          Created {createdAt}, {attemptCount(task.attempt_count)}, task {taskReference}
        </span>
      </div>
      {active && !cancellationRequested && (
        <ConfirmedActionButton
          ariaLabel={`Cancel ${taskLabel}`}
          className="button button--danger"
          disabled={cancel.isPending}
          onConfirm={() => cancel.mutate(task.id)}
          options={{
            title: `Cancel ${taskLabel}?`,
            description: `This cancels ${taskLabel}. The worker will stop at its next safe cancellation point. Completed commits are not rolled back.`,
            confirmLabel: "Cancel task",
            danger: true,
          }}
        >
          Cancel
        </ConfirmedActionButton>
      )}
      {interrupted && sourceScan && (
        <ConfirmedActionButton
          ariaLabel={`Resume ${taskLabel}`}
          className="button button--quiet"
          disabled={resume.isPending}
          onConfirm={() => resume.mutate(task.id)}
          options={{
            title: `Resume ${taskLabel}?`,
            description:
              "Continues the retained source work and unread publisher links from this batch. Completed reads stay recorded.",
            confirmLabel: "Resume retained work",
          }}
        >
          Resume
        </ConfirmedActionButton>
      )}
      {interrupted && freshScan && (
        <ConfirmedActionButton
          ariaLabel={`Scan again ${taskLabel}`}
          className="button button--quiet"
          disabled={retry.isPending}
          onConfirm={() => retry.mutate(task.id)}
          options={{
            title: `Scan again ${taskLabel}?`,
            description:
              task.source_ids.length > 0
                ? "Starts a fresh scan of the same selected boards with current search settings. Boards are read again."
                : "Starts a fresh scan of all boards enabled now with current search settings. Boards are read again.",
            confirmLabel: "Start fresh scan",
          }}
        >
          Scan again
        </ConfirmedActionButton>
      )}
      {interrupted && sourceScan && !freshScan && (
        <a className="button button--quiet" href="/find?tab=boards">
          Choose a fresh scan
        </a>
      )}
      {retryable && !costs.loaded && !costs.isError && (
        <button
          aria-label={`Checking retry cost for ${taskLabel}`}
          className="button button--quiet"
          disabled
          type="button"
        >
          Checking retry cost
        </button>
      )}
      {retryable && costs.isError && (
        <button
          aria-label={`Retry unavailable for ${taskLabel}`}
          className="button button--quiet"
          disabled
          type="button"
        >
          Retry unavailable
        </button>
      )}
      {retryable && costs.loaded && !costs.isError && paid && !reviewingPaidRetry && (
        <button
          aria-label={`Review retry for ${taskLabel}`}
          className="button button--quiet"
          onClick={() => setReviewingPaidRetry(true)}
          type="button"
        >
          Review retry
        </button>
      )}
      {retryable && costs.loaded && !costs.isError && paid && reviewingPaidRetry && (
        <PaidAction
          ariaLabel={`Retry task: ${taskLabel}`}
          compact
          confirmation={{
            title: `Retry ${taskLabel}?`,
            description: confirmationDescription,
            confirmLabel: "Confirm retry",
          }}
          costs={costs}
          disabled={retry.isPending || billingPending || providerConfigurationUnavailable}
          label="Retry task"
          onContinue={() => retry.mutate(task.id)}
          taskType={task.task_type}
        />
      )}
      {retryable && costs.loaded && !costs.isError && !paid && (
        <button
          aria-label={`Retry ${taskLabel}`}
          className="button button--quiet"
          disabled={retry.isPending}
          onClick={() => retry.mutate(task.id)}
          type="button"
        >
          Retry
        </button>
      )}
    </article>
  );
}

export function TasksPage() {
  const { workspaceId } = useWorkspace();
  const tasks = useTasks(workspaceId);
  const costs = useTaskCosts(workspaceId);
  const hasRetryableCampaign = tasks.data?.some(
    (task) =>
      task.task_type === "search.guided_campaign" && ["failed", "cancelled"].includes(task.status),
  );
  const hasRetryableVocabulary = tasks.data?.some(
    (task) =>
      task.task_type === "search.vocabulary" && ["failed", "cancelled"].includes(task.status),
  );
  const campaignConfiguration = useGuidedCampaignProviderConfiguration(
    workspaceId,
    Boolean(hasRetryableCampaign),
  );
  const vocabularyConfiguration = useSearchVocabularyProviderConfiguration(
    workspaceId,
    Boolean(hasRetryableVocabulary),
  );
  const cancel = useCancelTask(workspaceId);
  const retry = useRetryTask(workspaceId);
  const hasRetryableTask = tasks.data?.some(
    (task) =>
      ["failed", "cancelled"].includes(task.status) &&
      task.task_type !== "source.scan" &&
      task.task_type !== "source.continue",
  );
  if (tasks.isLoading) return <LoadingState label="Reading task history" />;
  if (tasks.isError) return <ErrorState error={tasks.error} />;
  return (
    <Page
      density="dense"
      eyebrow="Background work"
      help="activity"
      title="Activity"
      description="See what is queued, running, complete, or failed, including work that survived a restart."
    >
      {costs.isError && hasRetryableTask && (
        <p className="field-error" role="alert">
          Retry cost information is unavailable. Reload this page or review Spend before retrying.
        </p>
      )}
      {((campaignConfiguration.isError && hasRetryableCampaign) ||
        (vocabularyConfiguration.isError && hasRetryableVocabulary)) && (
        <p className="field-error" role="alert">
          Search provider information is unavailable. Reload this page or review the provider
          settings before retrying.
        </p>
      )}
      <section className="panel task-history">
        {(tasks.data ?? []).map((task) => (
          <TaskArticle
            cancel={cancel}
            costs={costs}
            campaignConfiguration={campaignConfiguration}
            vocabularyConfiguration={vocabularyConfiguration}
            key={task.id}
            retry={retry}
            task={task}
            workspaceId={workspaceId}
          />
        ))}
        {!tasks.data?.length && (
          <EmptyState>
            No tasks yet. Evaluation, tailoring, research, and file verification will appear here.
          </EmptyState>
        )}
      </section>
    </Page>
  );
}

/** Named counts and per-item failure details, with other payload behind a disclosure. */
function TaskProgress({
  active,
  error,
  progress,
  running,
  taskType,
}: {
  active: boolean;
  error: string | null;
  progress: Record<string, unknown>;
  running: boolean;
  taskType: Task["task_type"];
}) {
  const current = typeof progress.current === "number" ? progress.current : null;
  const total = typeof progress.total === "number" ? progress.total : null;
  const step = current !== null && total !== null ? `${current} of ${total}` : null;
  const continuation = taskType === "source.continue";
  const lines = continuation ? continuationLines(progress, active) : [];
  const failures = Array.isArray(progress.failures)
    ? progress.failures.map(taskItemFailure).filter((detail): detail is string => detail !== null)
    : [];
  // A body read can fail on a row whose publisher outcome stands, so these carry the
  // error text without claiming the row failed.
  const contentFailures = Array.isArray(progress.content_failures)
    ? progress.content_failures
        .map(taskItemFailure)
        .filter((detail): detail is string => detail !== null)
    : [];
  // Local validation rejects a provider answer after the call is billed, so these
  // rows cost money and carry no verdict.
  const unreviewed = itemRows(progress.validation_failures, "failure_code", "No detail returned.");
  // A skip reason such as a dismissal is complete without a detail.
  const skippedRows = itemRows(progress.skipped_rows, "skipped", "");
  const tallies = countRows(progress.so_far);
  const soFar = tallies
    ?.map(([key, count]) => namedProgressLine(key, count, new Set()))
    .filter((line): line is string => line !== null);
  // Per-step position, its tallies, and the row id are shown as the step lines, not the payload.
  // Stored source.continue progress from earlier builds still carries "continued".
  // A batch's results repeat the counts it already names.
  const hidden = new Set([
    "current",
    "total",
    "so_far",
    "phase",
    "pending_discovery_id",
    "job_posting_id",
    "continued",
    "results",
  ]);
  const readable = Object.entries(progress).filter(
    ([key]) =>
      key !== "failures" &&
      key !== "content_failures" &&
      key !== "validation_failures" &&
      key !== "skipped_rows" &&
      !hidden.has(key) &&
      !(continuation && CONTINUATION_KEYS.has(key)),
  );
  const uncounted = uncountedLists(readable);
  const named = readable
    .map(([key, value]) => namedProgressLine(key, value, uncounted))
    .filter((line): line is string => line !== null);
  // A batch step carries its tallies under so_far, so its other lines describe one item,
  // and a stopped or requeued batch keeps the last item it reached.
  const itemScope = step !== null && tallies ? (running ? "Current item" : "Last item") : null;
  const contents = readable.flatMap(([key, value]) =>
    Array.isArray(value) && !uncounted.has(key) ? (listContentsLine(key, value) ?? []) : [],
  );
  const groups = countGroups(readable);
  const rest = readable.flatMap(([key, value]) => {
    const entry = payloadEntry(key, value, uncounted);
    return entry ? [entry] : [];
  });
  const shown =
    step ||
    lines.length ||
    named.length ||
    contents.length ||
    groups.length ||
    failures.length ||
    contentFailures.length ||
    unreviewed.length ||
    skippedRows.length ||
    rest.length;
  if (error && !shown) {
    return <p className="task-error">{error}</p>;
  }
  if (!error && !shown) {
    return <p>Waiting for progress</p>;
  }
  return (
    <>
      {error && <p className="task-error">{error}</p>}
      {step && <p>Step {step}</p>}
      {soFar && soFar.length > 0 && <p>{`So far: ${soFar.join(", ")}`}</p>}
      {lines.map((line) => (
        <p key={line}>{line}</p>
      ))}
      {named.length > 0 && (
        <p>{itemScope ? `${itemScope} - ${named.join(", ")}` : named.join(", ")}</p>
      )}
      {contents.map((line) => (
        <p key={line}>{line}</p>
      ))}
      {groups.map((group) => (
        <div className="table-wrap" key={group.key}>
          <table aria-label={group.label} className="data-table">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Count</th>
              </tr>
            </thead>
            <tbody>
              {group.rows.map(([name, count]) => (
                <tr key={name}>
                  <th scope="row">{progressLabel(name)}</th>
                  <td>{count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
      {failures.length > 0 && (
        <ul aria-label="Task item failures" className="field-error">
          {failures.map((detail) => (
            <li key={detail}>{detail}</li>
          ))}
        </ul>
      )}
      {contentFailures.length > 0 && (
        <>
          <p className="field-note">
            Posting text could not be read. The publisher outcome for these rows stands.
          </p>
          <ul aria-label="Posting text read failures" className="field-note">
            {contentFailures.map((detail) => (
              <li key={detail}>{detail}</li>
            ))}
          </ul>
        </>
      )}
      {unreviewed.length > 0 && (
        <>
          <p className="field-note">
            These rows were not reviewed. The model call was paid for and returned nothing usable
            for them, so no verdict was recorded.
          </p>
          <ItemRowsTable label="Rows left unreviewed" rows={unreviewed} />
        </>
      )}
      {skippedRows.length > 0 && (
        <>
          <p className="field-note">These rows were not evaluated.</p>
          <ItemRowsTable label="Skipped rows" rows={skippedRows} />
        </>
      )}
      {rest.length > 0 && (
        <details className="task-payload">
          <summary>Full payload</summary>
          <dl>
            {rest.map(([key, value]) => (
              <div key={key}>
                <dt>{key}</dt>
                <dd className="path-value">{formatProgressValue(value)}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
    </>
  );
}
