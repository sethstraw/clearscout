import { useState } from "react";

import { api, type Task } from "../../api/client";
import {
  taskBillingSummary,
  useEnqueueTask,
  useTaskBilling,
  useTaskCosts,
  useTasks,
} from "../../api/hooks";
import { text } from "../../api/json";
import { taskKindOf } from "../../api/taskNames";
import { PaidAction } from "../../components/PaidAction";
import { EmptyState } from "../../components/States";
import { StatusBadge } from "../../components/StatusBadge";
import { formatUsd } from "../../components/text";
import { useConfirmation } from "../../confirmations";

const confirmationPoints = [
  "the exact factual wording",
  "the metrics, including that there are none",
  "the ownership level",
  "the scope",
  "the evidence category",
  "the job gaps it relates to",
  "that its source is user-confirmed discovery",
  "what remains ambiguous",
] as const;

type DiscoveryPlacement = {
  roles?: { key: string; label: string }[];
  sections?: string[];
};

type RunTask = (taskType: string, payload: Record<string, unknown>) => void;

function TailoringActions({
  billingSummary,
  jobId,
  workspaceId,
  discoveryBusy,
  enqueueError,
  enqueuePending,
  run,
  startDiscovery,
  taskStateError,
  taskStateMessage,
  tailoringUnavailable,
}: {
  billingSummary: string;
  jobId: string;
  workspaceId: string;
  discoveryBusy: boolean;
  enqueueError: string;
  enqueuePending: boolean;
  run: RunTask;
  startDiscovery: () => void;
  taskStateError: boolean;
  taskStateMessage: string;
  tailoringUnavailable: boolean;
}) {
  const costs = useTaskCosts(workspaceId);
  const confirm = useConfirmation();
  const [preflightPending, setPreflightPending] = useState(false);
  const [preflightError, setPreflightError] = useState("");
  const confirmTailoring = async () => {
    setPreflightPending(true);
    setPreflightError("");
    try {
      const preflight = await api.tailoringPreflight(workspaceId, jobId);
      const approved = await confirm({
        title: preflight.reuse_available ? "Reuse this exact package?" : "Generate this package?",
        description: preflight.reuse_available
          ? `Exact completed package found. Reusing it does not call the provider and adds no provider charge. Preflight cost: ${formatUsd(preflight.cost_usd ?? 0)}. ${costs.confirmation("workflow.tailor")}`
          : `No exact completed package is available. Preflight did not find a reusable package cost. ${costs.confirmation("workflow.tailor")}`,
        confirmLabel: preflight.reuse_available ? "Reuse package" : "Generate package",
      });
      if (approved)
        run("workflow.tailor", { job_posting_id: jobId, cover_letter: true, regenerate: false });
    } catch (error) {
      setPreflightError(
        error instanceof Error ? error.message : "Tailoring cost could not be checked.",
      );
    } finally {
      setPreflightPending(false);
    }
  };
  return (
    <article className="panel studio-actions">
      <h2>Tailoring Studio</h2>
      <p>Every action runs as durable work. Closing this page does not erase its progress.</p>
      <p className="callout">
        Re-evaluation stores a new evaluation version against the current posting, prompts,
        parameters, and master CV.
      </p>
      {billingSummary && (
        <p aria-label="Latest tailoring task billing" className="field-note" role="status">
          {billingSummary}
        </p>
      )}
      {taskStateMessage && (
        <p
          className={taskStateError ? "field-error" : "field-note"}
          role={taskStateError ? "alert" : "status"}
        >
          {taskStateMessage}
        </p>
      )}
      <div className="button-row">
        <PaidAction
          evaluation={{ workspaceId, selection: { job_posting_ids: [jobId] } }}
          confirmation={{
            title: "Re-evaluate this job?",
            description: "Store a new evaluation version for this tracked posting.",
            confirmLabel: "Confirm re-evaluation",
          }}
          costs={costs}
          disabled={enqueuePending || !costs.loaded}
          label="Re-evaluate job"
          onAct={() => run("discovery.evaluate", { job_posting_id: jobId, fresh: true })}
          taskType="discovery.evaluate"
        />
        <PaidAction
          costs={costs}
          disabled={enqueuePending || preflightPending || tailoringUnavailable}
          label={preflightPending ? "Checking reuse" : "Generate or reuse the CV and letter"}
          onPrepare={() => {
            void confirmTailoring();
          }}
          taskType="workflow.tailor"
        />
        <PaidAction
          confirmation={{
            title: "Regenerate this package?",
            description: `Regeneration skips completed-run reuse. ${costs.confirmation("workflow.tailor")}`,
            confirmLabel: "Regenerate package",
          }}
          costs={costs}
          disabled={enqueuePending || tailoringUnavailable}
          label="Regenerate new version"
          onAct={() =>
            run("workflow.tailor", { job_posting_id: jobId, cover_letter: true, regenerate: true })
          }
          taskType="workflow.tailor"
        />
        <div>
          <button
            className="button button--quiet"
            disabled={discoveryBusy}
            onClick={startDiscovery}
            type="button"
          >
            Explore a gap
          </button>
          <small className="free-action">{costs.cost("workflow.discovery")}</small>
        </div>
      </div>
      {preflightError && <p className="field-error">{preflightError}</p>}
      {enqueueError && <p className="field-error">{enqueueError}</p>}
    </article>
  );
}

export function TailoringStudio({ jobId, workspaceId }: { jobId: string; workspaceId: string }) {
  const enqueue = useEnqueueTask(workspaceId);
  const taskQuery = useTasks(workspaceId);
  const [submitted, setSubmitted] = useState<Task[]>([]);
  const [answer, setAnswer] = useState("");
  const [replies, setReplies] = useState<string[]>([]);
  const [claim, setClaim] = useState("");
  const [scope, setScope] = useState("");
  const [placement, setPlacement] = useState("");
  const [ambiguities, setAmbiguities] = useState("");
  const [confirmed, setConfirmed] = useState<Set<string>>(() => new Set());
  // Work recorded for this job, not only what this visit started.
  const recorded = (taskQuery.data ?? []).filter((task) => task.job_posting_ids.includes(jobId));
  const pending = submitted.filter((task) => !recorded.some((current) => current.id === task.id));
  const tasks = [...recorded, ...pending].sort((left, right) =>
    right.created_at.localeCompare(left.created_at),
  );
  const run: RunTask = (task_type, payload) => {
    enqueue.mutate(
      { workspace_id: workspaceId, task_type, payload },
      { onSuccess: (task) => setSubmitted((current) => [...current, task]) },
    );
  };
  const latestTailor = tasks.find((task) => task.task_type === "workflow.tailor");
  const tailorTerminal = ["completed", "failed", "cancelled"].includes(latestTailor?.status ?? "");
  const tailorBusy = Boolean(latestTailor && !tailorTerminal);
  const tailorBilling = useTaskBilling(workspaceId, latestTailor?.id ?? "", tailorTerminal);

  const tailorBillingSummary = tailorTerminal
    ? taskBillingSummary(tailorBilling.data, tailorBilling.isError)
    : "";
  const discoveryTasks = tasks.filter((task) => task.task_type === "workflow.discovery");
  const latestDiscovery = discoveryTasks[0];
  const discoveryState = discoveryTasks.find(
    (task) => "next_question" in task.progress || "proposal" in task.progress,
  );
  const nextQuestion = discoveryState?.progress.next_question as
    { id: string; text: string; angle: string } | null | undefined;
  const answered = Boolean(discoveryState?.progress.answered);
  const closed = text(discoveryState?.progress.closed);
  const placements = (discoveryState?.progress.placements ?? {}) as DiscoveryPlacement;
  const proposal = discoveryState?.progress.proposal as string[] | undefined;
  const written = discoveryState?.progress.written as
    { entry_ids?: string[]; journal?: string } | null | undefined;
  const proposalPayload = () => ({
    text: claim,
    scope,
    role_key: placement.startsWith("role:") ? placement.slice(5) : "",
    section: placement.startsWith("section:") ? placement.slice(8) : "",
    ambiguities: ambiguities
      .split("\n")
      .map((item) => item.trim())
      .filter(Boolean),
  });
  const discoveryBusy = ["queued", "running", "committing"].includes(latestDiscovery?.status ?? "");
  const startDiscovery = () => {
    setReplies([]);
    setClaim("");
    setScope("");
    setPlacement("");
    setAmbiguities("");
    setConfirmed(new Set());
    run("workflow.discovery", { job_posting_id: jobId, replies: [] });
  };
  return (
    <section className="studio-grid">
      <TailoringActions
        billingSummary={tailorBillingSummary}
        discoveryBusy={discoveryBusy}
        enqueueError={enqueue.isError ? enqueue.error.message : ""}
        enqueuePending={enqueue.isPending}
        jobId={jobId}
        run={run}
        startDiscovery={startDiscovery}
        taskStateError={taskQuery.isError}
        taskStateMessage={
          taskQuery.isPending
            ? "Checking current tailoring work before enabling paid actions."
            : taskQuery.isError
              ? "Current tailoring work is unavailable. Reload or review Tasks before starting paid work."
              : ""
        }
        tailoringUnavailable={taskQuery.isPending || taskQuery.isError || tailorBusy}
        workspaceId={workspaceId}
      />
      <article className="panel">
        <h2>Current work</h2>
        {taskQuery.isSuccess && !tasks.length && (
          <EmptyState>No work has run for this job yet.</EmptyState>
        )}
        <ol className="task-mini-list">
          {tasks.map((task) => (
            <li key={task.id}>
              <StatusBadge value={task.status} />
              <span>{taskKindOf(task).name}</span>
              <small>
                {task.status === "failed"
                  ? task.error_message
                  : text(
                      task.progress.message ?? task.progress.outcome,
                      "Progress appears on the Tasks page.",
                    )
                      .split("\n")
                      .map((line, index) => <span key={index}>{line}</span>)}
              </small>
            </li>
          ))}
        </ol>
        {nextQuestion && !discoveryBusy && (
          <form
            className="discovery-question"
            onSubmit={(event) => {
              event.preventDefault();
              const next = [...replies, answer];
              setReplies(next);
              setAnswer("");
              run("workflow.discovery", { job_posting_id: jobId, replies: next });
            }}
          >
            <span>{nextQuestion.angle}</span>
            <strong>{nextQuestion.text}</strong>
            <textarea
              aria-label="Your answer"
              onChange={(event) => setAnswer(event.target.value)}
              required
              rows={4}
              value={answer}
            />
            <p>Only your typed words can become a claim. "No" is accepted as a complete answer.</p>
            <button className="button button--primary" type="submit">
              Save answer and continue
            </button>
          </form>
        )}
        {!nextQuestion && answered && !proposal && !discoveryBusy && (
          <form
            className="discovery-question"
            onSubmit={(event) => {
              event.preventDefault();
              run("workflow.discovery", {
                job_posting_id: jobId,
                replies,
                proposal: proposalPayload(),
              });
            }}
          >
            <span>Person-authored evidence</span>
            <strong>Draft the exact claim to validate</strong>
            <label>
              Exact factual wording
              <textarea
                onChange={(event) => setClaim(event.target.value)}
                required
                rows={4}
                value={claim}
              />
            </label>
            <label>
              Scope
              <input
                onChange={(event) => setScope(event.target.value)}
                placeholder="What this covered"
                value={scope}
              />
            </label>
            <label>
              Placement
              <select
                onChange={(event) => setPlacement(event.target.value)}
                required
                value={placement}
              >
                <option value="">Choose a role or section</option>
                {placements.roles?.map((role) => (
                  <option key={role.key} value={`role:${role.key}`}>
                    {role.label || role.key}
                  </option>
                ))}
                {placements.sections?.map((section) => (
                  <option key={section} value={`section:${section}`}>
                    {section}
                  </option>
                ))}
              </select>
            </label>
            <label>
              What remains ambiguous
              <textarea
                onChange={(event) => setAmbiguities(event.target.value)}
                placeholder="One item per line, or leave blank"
                rows={3}
                value={ambiguities}
              />
            </label>
            <p>
              Use only words supported by your answers above. TailorCV will reject invented wording,
              numbers, or ownership.
            </p>
            <button className="button button--primary" type="submit">
              Validate and preview
            </button>
          </form>
        )}
        {!nextQuestion && !answered && closed && (
          <p className="discovery-closed">
            Gap exploration stopped safely: {closed}. Nothing was added to the master CV.
          </p>
        )}
        {proposal && !written && (
          <section className="discovery-confirmation">
            <h3>Exact proposed addition</h3>
            <pre>{proposal.join("\n")}</pre>
            <p>
              Confirm each point separately. The master CV remains untouched until all eight are
              checked and you use the final button.
            </p>
            <div>
              {confirmationPoints.map((point) => (
                <label key={point}>
                  <input
                    checked={confirmed.has(point)}
                    onChange={(event) =>
                      setConfirmed((current) => {
                        const next = new Set(current);
                        if (event.target.checked) next.add(point);
                        else next.delete(point);
                        return next;
                      })
                    }
                    type="checkbox"
                  />
                  {point}
                </label>
              ))}
            </div>
            <button
              className="button button--primary"
              disabled={confirmed.size !== confirmationPoints.length || discoveryBusy}
              onClick={() =>
                run("workflow.discovery", {
                  job_posting_id: jobId,
                  replies,
                  proposal: proposalPayload(),
                  confirmed_points: [...confirmationPoints],
                  write_confirmed: true,
                })
              }
              type="button"
            >
              Confirm all eight and write to master CV
            </button>
          </section>
        )}
        {written && (
          <p className="refresh-result" role="status">
            Added {written.entry_ids?.length ?? 0} confirmed record
            {written.entry_ids?.length === 1 ? "" : "s"} to the master CV. Journal:{" "}
            {written.journal}.
          </p>
        )}
      </article>
    </section>
  );
}
