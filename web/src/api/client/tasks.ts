import { requestIntentJson, requestJson as request } from "../http";
import type {
  EvaluationPreflight,
  EvaluationSelection,
  Task,
  TaskBilling,
  TaskTypeInfo,
} from "./types";

export const tasksApi = {
  evaluationPreflight: (workspaceId: string, selection: EvaluationSelection) =>
    request<EvaluationPreflight>(
      `/workspaces/${encodeURIComponent(workspaceId)}/evaluation-preflight`,
      { method: "POST", body: JSON.stringify(selection) },
    ),
  tasks: (workspaceId: string, options: { limit?: number } = {}) => {
    const query = [`workspace_id=${encodeURIComponent(workspaceId)}`];
    if (options.limit !== undefined) query.push(`limit=${options.limit}`);
    return request<Task[]>(`/tasks?${query.join("&")}`);
  },
  taskTypes: () => request<TaskTypeInfo[]>("/tasks/types"),
  task: (taskId: string, workspaceId: string) =>
    request<Task>(
      `/tasks/${encodeURIComponent(taskId)}?workspace_id=${encodeURIComponent(workspaceId)}`,
    ),
  taskBilling: (taskId: string, workspaceId: string) =>
    request<TaskBilling>(
      `/tasks/${encodeURIComponent(taskId)}/billing?workspace_id=${encodeURIComponent(workspaceId)}`,
    ),
  enqueueTask: (body: {
    workspace_id: string;
    task_type: string;
    payload: Record<string, unknown>;
  }) => {
    const intent = JSON.stringify(body);
    return requestIntentJson<Task>("/tasks", intent, intent);
  },
  cancelTask: (taskId: string, workspaceId: string) =>
    request<Task>(
      `/tasks/${encodeURIComponent(taskId)}/cancel?workspace_id=${encodeURIComponent(workspaceId)}`,
      { method: "POST" },
    ),
  retryTask: (taskId: string, workspaceId: string) =>
    request<Task>(
      `/tasks/${encodeURIComponent(taskId)}/retry?workspace_id=${encodeURIComponent(workspaceId)}`,
      { method: "POST" },
    ),
  resumeTask: (taskId: string, workspaceId: string) =>
    requestIntentJson<Task>(
      `/tasks/${encodeURIComponent(taskId)}/resume?workspace_id=${encodeURIComponent(workspaceId)}`,
      taskId,
      "",
    ),
};
