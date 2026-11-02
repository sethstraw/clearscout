import { useState } from "react";

import {
  LATEST_SEARCH_TASK_WINDOW,
  useEnqueueTask,
  useTasks,
  useRetryTask,
  useSearchArchetypeProposals,
  useSearchClusterProposals,
  useSearchParameters,
  useSearchTitleProposals,
  useSearchVocabularyProviderConfiguration,
  useTaskCosts,
  useWorkspaceSpend,
} from "../../api/hooks";
import { PaidAction } from "../../components/PaidAction";
import { formatUsd, UNKNOWN_AMOUNT } from "../../components/text";
import { SearchSuggestions } from "../search-parameters/SearchSuggestions";
import { SLOW_RUN_NOTICE, useSearchPanelTask, useSlowNotice } from "./useSearchPanelTask";

function nonnegativeInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

/** The vocabulary task reports one phase, "proposing_vocabulary", while it plans terms. */
function VocabularyRunStatus({
  progress,
  updatedAt,
}: {
  progress: Record<string, unknown>;
  updatedAt: string;
}) {
  const slow = useSlowNotice(updatedAt);
  const label =
    progress.phase === "proposing_vocabulary"
      ? "Suggesting CV-grounded vocabulary"
      : "Preparing search vocabulary";
  const completed = nonnegativeInteger(progress.completed);
  const total = nonnegativeInteger(progress.total);
  const itemLabel = typeof progress.item_label === "string" ? progress.item_label.trim() : "";
  return (
    <div className="search-vocabulary-progress" role="status">
      <p>
        <strong>{label}</strong>
      </p>
      {completed !== null && total !== null && total > 0 ? (
        <>
          <progress
            aria-label={`${label} progress`}
            max={total}
            value={Math.min(completed, total)}
          />
          <p>
            {completed} of {total} finished.
            {itemLabel ? ` Now: ${itemLabel}.` : ""}
          </p>
        </>
      ) : (
        <p>{itemLabel ? `Now: ${itemLabel}.` : "Waiting for the next progress update."}</p>
      )}
      {slow && <p className="field-note">{SLOW_RUN_NOTICE}</p>}
    </div>
  );
}

/**
 * Proposes CV-grounded search vocabulary with one text-only model call; finds no jobs itself.
 */
export function SearchVocabularyPanel({ workspaceId }: { workspaceId: string }) {
  const [selectedTaskId, setSelectedTaskId] = useState("");
  const [termsOpen, setTermsOpen] = useState(false);
  const costs = useTaskCosts(workspaceId);
  const vocabularyConfiguration = useSearchVocabularyProviderConfiguration(workspaceId, true);
  const search = useEnqueueTask(workspaceId);
  const retry = useRetryTask(workspaceId);
  const tasks = useTasks(workspaceId);
  const parameters = useSearchParameters(workspaceId);
  const titleProposals = useSearchTitleProposals(workspaceId);
  const archetypeProposals = useSearchArchetypeProposals(workspaceId);
  const clusterProposals = useSearchClusterProposals(workspaceId);
  const workspaceSpend = useWorkspaceSpend(workspaceId);
  const taskRows = tasks.data ?? [];
  const vocabularyRows = taskRows.filter((item) => item.task_type === "search.vocabulary");
  const latestVocabulary = vocabularyRows
    .slice()
    .sort((left, right) => right.created_at.localeCompare(left.created_at))[0];
  const vocabularyOutOfView = !latestVocabulary && taskRows.length >= LATEST_SEARCH_TASK_WINDOW;
  const taskId = selectedTaskId || latestVocabulary?.id || "";
  const { task, queued, running, failedTask, terminal, taskCost } = useSearchPanelTask(
    workspaceId,
    taskId,
  );
  const queueing = search.isPending;
  const retrying = retry.isPending;
  const progress = task.data?.progress ?? {};
  const scopeReady = [parameters, titleProposals, archetypeProposals].every(
    (query) => query.data !== undefined && !query.isError,
  );
  const proposedTitleCount = (titleProposals.data?.items ?? []).filter(
    (item) => item.state === "proposed",
  ).length;
  const proposedArchetypeCount = (archetypeProposals.data?.items ?? []).filter(
    (item) => item.state === "proposed",
  ).length;
  const proposedClusterCount = (clusterProposals.data?.items ?? []).filter(
    (item) => item.state === "proposed",
  ).length;
  const proposalCountsReady = [titleProposals, archetypeProposals, clusterProposals].every(
    (query) => query.data !== undefined && !query.isError,
  );
  const fanOutLine =
    "One paid text-only call proposes CV-grounded search vocabulary. No postings or new employer boards are discovered. Review suggestions, then scan registered sources for free.";
  const vocabularySpend = workspaceSpend.data?.by_stage.find(
    (item) => item.name === "search_vocabulary",
  );
  const busy = queueing || retrying || queued || running;
  const inFlightLabel = (idle: string) =>
    queueing
      ? "Queueing paid search"
      : queued
        ? "Paid search queued"
        : running
          ? "Paid search running"
          : idle;
  const vocabularyAction = (
    <PaidAction
      compact
      confirmation={{
        title: "Propose new search terms?",
        description: `${vocabularyConfiguration.description} ${fanOutLine} ${costs.confirmation("search.vocabulary")}`,
        confirmLabel: "Propose search terms",
      }}
      costs={costs}
      disabled={
        busy ||
        !costs.loaded ||
        !vocabularyConfiguration.loaded ||
        vocabularyConfiguration.isError ||
        !scopeReady
      }
      label={
        !retrying && (queueing || queued || running)
          ? inFlightLabel("Propose new search terms")
          : "Propose new search terms"
      }
      onAct={() => {
        search.mutate(
          {
            workspace_id: workspaceId,
            task_type: "search.vocabulary",
            payload: {},
          },
          { onSuccess: (nextTask) => setSelectedTaskId(nextTask.id) },
        );
      }}
      taskType="search.vocabulary"
    />
  );
  return (
    <div className="search-vocabulary-layout">
      <details
        className="panel search-vocabulary-terms"
        open={termsOpen}
        onToggle={(event) => setTermsOpen(event.currentTarget.open)}
      >
        <summary>
          Search terms and suggestions
          {proposalCountsReady
            ? ` (${proposedTitleCount + proposedArchetypeCount + proposedClusterCount} to review)`
            : ""}
        </summary>
        <div className="panel-body">
          <p>Optional: widen future searches with CV-backed terms. This action finds no jobs.</p>
          {vocabularyAction}
          {vocabularyConfiguration.isError && (
            <p className="field-error" role="alert">
              The configured provider, extraction model, and effort could not be read, so
              search-term proposals cannot start. Check Settings, then reload.
            </p>
          )}
          {!vocabularyConfiguration.loaded && !vocabularyConfiguration.isError && (
            <p aria-label="Search vocabulary provider status" role="status">
              Reading the provider model and effort before payment.
            </p>
          )}
          {search.isError && <p className="field-error">{search.error.message}</p>}
          {retry.isError && <p className="field-error">{retry.error.message}</p>}
          {queueing ? (
            <p aria-label="Search vocabulary queue status" role="status">
              Queueing paid search.
            </p>
          ) : retrying ? (
            <p aria-label="Search vocabulary queue status" role="status">
              Queueing retry.
            </p>
          ) : queued ? (
            <p aria-label="Search vocabulary queue status" role="status">
              Queued - waiting for the local worker.
            </p>
          ) : running ? (
            <VocabularyRunStatus
              key={task.data?.updated_at}
              progress={progress}
              updatedAt={task.data?.updated_at ?? new Date().toISOString()}
            />
          ) : null}
          {terminal && (
            <>
              {failedTask && !retrying && (
                <div className="field-error" role="alert">
                  <p>
                    Search failed: {failedTask.error_message || "No failure detail was returned."}
                  </p>
                  <p>Review the failure and recorded cost before retrying.</p>
                </div>
              )}
              <p aria-label="Task billing status" className="field-note" role="status">
                {taskCost}
              </p>
              {failedTask && !retrying && (
                <PaidAction
                  compact
                  confirmation={{
                    title: "Retry proposing search terms?",
                    description: `${taskCost} This repeats the failed search-vocabulary payload and may reach the provider again. ${vocabularyConfiguration.description} ${costs.confirmation("search.vocabulary")}`,
                    confirmLabel: "Retry search vocabulary",
                  }}
                  costs={costs}
                  disabled={
                    busy ||
                    !costs.loaded ||
                    !vocabularyConfiguration.loaded ||
                    vocabularyConfiguration.isError
                  }
                  label={retrying ? "Queueing retry" : "Retry this search"}
                  onContinue={() => {
                    retry.mutate(failedTask.id, {
                      onSuccess: (nextTask) => setSelectedTaskId(nextTask.id),
                    });
                  }}
                  taskType="search.vocabulary"
                />
              )}
              {task.data?.status === "completed" && (
                <p aria-label="Search vocabulary result" role="status">
                  Search vocabulary suggested. No postings or new employer boards were discovered.{" "}
                  <a href="#search-suggestions-heading" onClick={() => setTermsOpen(true)}>
                    Review search proposals
                  </a>{" "}
                  below.
                </p>
              )}
            </>
          )}
          {vocabularyOutOfView && (
            <p aria-label="Search vocabulary history status" role="status">
              Earlier recorded search-vocabulary runs may be out of view; only the most recent{" "}
              {LATEST_SEARCH_TASK_WINDOW} tasks were read.
            </p>
          )}
          <SearchSuggestions workspaceId={workspaceId} />
          {workspaceSpend.isError ? (
            <p className="field-error">Recorded search-vocabulary spend could not be read.</p>
          ) : workspaceSpend.data ? (
            <p>
              Spent on search vocabulary:{" "}
              {formatUsd(vocabularySpend ? vocabularySpend.amount_usd : 0, {
                unknown: UNKNOWN_AMOUNT,
              })}{" "}
              across {vocabularySpend?.calls ?? 0} recorded provider calls.
              {(vocabularySpend?.unpriced_calls ?? 0) > 0
                ? ` ${vocabularySpend?.unpriced_calls} call${vocabularySpend?.unpriced_calls === 1 ? " has" : "s have"} no configured price; a missing total is not zero.`
                : ""}
            </p>
          ) : null}
        </div>
      </details>
    </div>
  );
}
