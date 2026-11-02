import { useMutation, useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { api, type EvaluationSelection, type TaskBilling } from "../client";
import {
  guidedCampaignConfigurationDescription,
  relevanceReviewConfigurationDescription,
  searchVocabularyConfigurationDescription,
} from "../providerDetails";
import { formatUsd, UNKNOWN_AMOUNT } from "../../components/text";
import { queryKeys } from "./keys";
import { useWorkspaceMutation } from "./records";
import { useRunCostHistory } from "./workspaces";

export function useEvaluationPreflight(workspaceId: string) {
  return useMutation({
    mutationFn: (selection: EvaluationSelection) => api.evaluationPreflight(workspaceId, selection),
  });
}

export type SearchProviderConfiguration = {
  description: string;
  isError: boolean;
  loaded: boolean;
  /** Which of the two ways paid work is held, so a panel can say which rather than guess. */
  heldBecause: "unreachable" | "undescribable" | null;
};

// A configuration that answers but cannot be described still holds paid work:
// approving a run means seeing the provider and model it will use.
function providerConfigurationState(
  isError: boolean,
  loaded: boolean,
  description: string,
  enabled: boolean,
): Pick<SearchProviderConfiguration, "isError" | "loaded" | "heldBecause"> {
  const undescribable = enabled && loaded && !description;
  return {
    isError: isError || undescribable,
    loaded,
    heldBecause: isError ? "unreachable" : undescribable ? "undescribable" : null,
  };
}

/** Read the effective provider choices for a search-campaign task, deriving its description. */
export function useGuidedCampaignProviderConfiguration(
  workspaceId: string,
  enabled = true,
): SearchProviderConfiguration {
  const configuration = useQuery({
    queryKey: queryKeys.guidedCampaignConfiguration(workspaceId),
    queryFn: () => api.guidedCampaignConfiguration(workspaceId),
    enabled: Boolean(workspaceId && enabled),
  });
  const loaded = !enabled || configuration.data !== undefined;
  const description = guidedCampaignConfigurationDescription(configuration.data);
  return {
    description,
    ...providerConfigurationState(configuration.isError, loaded, description, enabled),
  };
}

/** Read the effective provider choices for a search-vocabulary task, deriving its description. */
export function useSearchVocabularyProviderConfiguration(
  workspaceId: string,
  enabled = true,
): SearchProviderConfiguration {
  const configuration = useQuery({
    queryKey: queryKeys.searchVocabularyConfiguration(workspaceId),
    queryFn: () => api.searchVocabularyConfiguration(workspaceId),
    enabled: Boolean(workspaceId && enabled),
  });
  const loaded = !enabled || configuration.data !== undefined;
  const description = searchVocabularyConfigurationDescription(configuration.data);
  return {
    description,
    ...providerConfigurationState(configuration.isError, loaded, description, enabled),
  };
}

/** Read the effective provider choices for a relevance-review task, deriving its description. */
export function useRelevanceReviewProviderConfiguration(
  workspaceId: string,
  enabled = true,
): SearchProviderConfiguration & { batchRows: number | undefined } {
  const configuration = useQuery({
    queryKey: queryKeys.relevanceReviewConfiguration(workspaceId),
    queryFn: () => api.relevanceReviewConfiguration(workspaceId),
    enabled: Boolean(workspaceId && enabled),
  });
  const loaded = !enabled || configuration.data !== undefined;
  const description = relevanceReviewConfigurationDescription(configuration.data);
  return {
    batchRows: configuration.data?.batch_rows,
    description,
    ...providerConfigurationState(configuration.isError, loaded, description, enabled),
  };
}

/**
 * The registry is product data rather than workspace data, so one fetch answers
 * for the whole session.
 */
function useTaskTypes() {
  return useQuery({
    queryKey: queryKeys.taskTypes,
    queryFn: api.taskTypes,
    staleTime: Infinity,
  });
}

export type TaskCosts = {
  loaded: boolean;
  isError: boolean;
  isPaid: (taskType: string) => boolean;
  cost: (taskType: string) => string;
  inline: (taskType: string, count?: number) => string;
  summary: (taskType: string, count?: number) => string;
  confirmation: (taskType: string, count?: number) => string;
};

/**
 * Cost history is advisory; only the task registry is required.
 */
export function useTaskCosts(workspaceId = ""): TaskCosts {
  const taskTypes = useTaskTypes();
  const history = useRunCostHistory(workspaceId);
  return useMemo(() => {
    const byType = new Map(
      (taskTypes.data ?? []).map((entry) => [entry.task_type as string, entry]),
    );
    const rows = Array.isArray(history.data) ? history.data : [];
    const historyAbsent = history.isError
      ? "Cost history is unavailable."
      : workspaceId && history.data === undefined
        ? "Reading cost history."
        : "No completed runs yet.";
    const pluralize = (amount: number, singular: string) =>
      `${amount} ${amount === 1 ? singular : `${singular}s`}`;
    // The history prices one unit of work, so the figure a person sees is the
    // units they selected times that price, with the measured spread around it.
    const priced = (taskType: string, units: number) => {
      const row = rows.find((item) => item.task_type === taskType);
      if (!row || row.per_unit_median === null || row.per_unit_median === undefined) {
        return historyAbsent;
      }
      const each = Number(row.per_unit_median);
      const low = Number(row.per_unit_low ?? row.per_unit_median);
      const high = Number(row.per_unit_high ?? row.per_unit_median);
      return `About ${formatUsd(each * units, { places: 2 })} for ${pluralize(units, row.unit)} (range ${formatUsd(low * units, { places: 2 })} to ${formatUsd(high * units, { places: 2 })}, from ${pluralize(row.measured_runs, "run")}).`;
    };
    return {
      loaded: taskTypes.data !== undefined,
      isError: taskTypes.isError,
      isPaid: (taskType: string) => byType.get(taskType)?.paid ?? false,
      cost: (taskType: string) => byType.get(taskType)?.cost ?? "",
      // The measured spread stays in confirmation(); this is the button-face line.
      inline: (taskType: string, count = 1) => {
        if (!byType.get(taskType)?.paid) return byType.get(taskType)?.cost ?? "";
        const row = rows.find((item) => item.task_type === taskType);
        if (!row || row.per_unit_median === null || row.per_unit_median === undefined) {
          return "cost shown before you run";
        }
        const each = Number(row.per_unit_median);
        return count > 1
          ? `about ${formatUsd(each * count, { places: 2 })} total`
          : `about ${formatUsd(each, { places: 2 })} per ${row.unit}`;
      },
      // The compact face sits beside the Paid pill, so it carries only the figure.
      summary: (taskType: string, count = 1) => {
        if (!byType.get(taskType)?.paid) return byType.get(taskType)?.cost ?? "";
        const row = rows.find((item) => item.task_type === taskType);
        if (!row || row.per_unit_median === null || row.per_unit_median === undefined) {
          return "cost shown before you run";
        }
        return `about ${formatUsd(Number(row.per_unit_median) * count, { places: 2 })}`;
      },
      confirmation: (taskType: string, count = 1) =>
        `${priced(taskType, count)} Charges may be higher and nothing stops the task once you confirm.`,
    };
  }, [history.data, history.isError, taskTypes.data, taskTypes.isError, workspaceId]);
}

export function useTasks(workspaceId: string) {
  return useQuery({
    queryKey: queryKeys.tasks(workspaceId),
    // One board scan's continuation chain alone can fill the route's default of 50.
    queryFn: () => api.tasks(workspaceId, { limit: LATEST_SEARCH_TASK_WINDOW }),
    enabled: Boolean(workspaceId),
  });
}

// The list endpoint has no task_type filter and caps at 250 rows per read; we
// request the wider window so a caller's latest task of one type stays visible
// after dozens of unrelated tasks accumulate. Callers still verify a hit before trusting it.
export const LATEST_SEARCH_TASK_WINDOW = 250;

export function useTask(workspaceId: string, taskId: string) {
  return useQuery({
    queryKey: queryKeys.task(workspaceId, taskId),
    queryFn: () => api.task(taskId, workspaceId),
    enabled: Boolean(workspaceId && taskId),
  });
}

export function useTaskBilling(workspaceId: string, taskId: string, terminal: boolean) {
  return useQuery({
    queryKey: queryKeys.taskBilling(workspaceId, taskId),
    queryFn: () => api.taskBilling(taskId, workspaceId),
    enabled: Boolean(workspaceId && taskId && terminal),
    staleTime: Infinity,
  });
}

function resentNote(billing: TaskBilling): string {
  const count = billing.resent_on_standard_count;
  if (!count) return "";
  const [calls, them] = count === 1 ? ["1 call was", "it"] : [`${count} calls were`, "them"];
  return ` ${calls} resent on the standard tier after the Flex tier refused ${them}.`;
}

export function taskBillingSummary(
  billing: TaskBilling | undefined,
  billingError: boolean,
  taskStarted = true,
): string {
  if (!taskStarted) return "No task was started, so no model charge was recorded.";
  if (billing?.status === "no_model_calls") return "No model charge was recorded for this task.";
  if (billing?.status === "priced") {
    return `Recorded cost for this task: ${formatUsd(billing.actual_cost_usd, { unknown: UNKNOWN_AMOUNT })} across ${billing.call_count} model call${billing.call_count === 1 ? "" : "s"}.${resentNote(billing)}`;
  }
  if (billing?.status === "unpriced") {
    const calls = `${billing.call_count} model call${billing.call_count === 1 ? "" : "s"}`;
    const verb = billing.call_count === 1 ? "was" : "were";
    return `${calls} ${verb} recorded for this task, but the exact cost is unavailable.${resentNote(billing)}`;
  }
  return billingError
    ? "Recorded cost for this task is unavailable; review Spend before retrying."
    : "Checking recorded cost for this task.";
}

export function useCancelTask(workspaceId: string) {
  return useWorkspaceMutation(
    workspaceId,
    (taskId: string) => api.cancelTask(taskId, workspaceId),
    { successMessage: "Cancellation requested." },
  );
}

export function useRetryTask(workspaceId: string) {
  return useWorkspaceMutation(workspaceId, (taskId: string) => api.retryTask(taskId, workspaceId), {
    successMessage: "Task queued again.",
  });
}

export function useResumeTask(workspaceId: string) {
  return useWorkspaceMutation(
    workspaceId,
    (taskId: string) => api.resumeTask(taskId, workspaceId),
    {
      successMessage: "Retained source work queued to resume.",
    },
  );
}

export function useEnqueueTask(workspaceId: string) {
  return useWorkspaceMutation(workspaceId, api.enqueueTask, {
    successMessage: "Task queued.",
  });
}
