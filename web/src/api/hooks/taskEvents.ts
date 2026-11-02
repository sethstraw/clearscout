import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import type { Task } from "../client";
import { queryKeys } from "./keys";

// Keep long-lived HTTP/1.1 requests from starving route and API traffic.
export const MAX_TASK_EVENT_STREAMS = 1;

export function useTaskEventStreams(workspaceId: string, tasks: Task[]) {
  const client = useQueryClient();
  const [failedTaskIds, setFailedTaskIds] = useState<Set<string>>(() => new Set());
  const [pageVisible, setPageVisible] = useState(
    () => typeof document === "undefined" || document.visibilityState === "visible",
  );
  const previousTasks = useRef<Task[]>([]);
  const activeTaskIds = tasks
    .filter((task) => ["queued", "running", "needs_review", "committing"].includes(task.status))
    .map((task) => task.id)
    .sort();
  const streamedTaskIds = pageVisible ? activeTaskIds.slice(0, MAX_TASK_EVENT_STREAMS) : [];
  const active = activeTaskIds.join(",");
  const streamed = streamedTaskIds.join(",");
  useEffect(() => {
    const updateVisibility = () => setPageVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", updateVisibility);
    return () => document.removeEventListener("visibilitychange", updateVisibility);
  }, []);
  useEffect(() => {
    const previouslyActive = new Set(
      previousTasks.current
        .filter((task) => ["queued", "running", "needs_review", "committing"].includes(task.status))
        .map((task) => task.id),
    );
    const completedSinceLastRead = tasks.some(
      (task) =>
        previouslyActive.has(task.id) && ["completed", "failed", "cancelled"].includes(task.status),
    );
    previousTasks.current = tasks;
    if (completedSinceLastRead) {
      void client.invalidateQueries({
        predicate: (query) => query.queryKey[1] === workspaceId,
      });
    }
  }, [client, tasks, workspaceId]);
  useEffect(() => {
    if (!streamed) return undefined;
    if (typeof EventSource === "undefined") return undefined;
    const streams = streamed.split(",").map((taskId) => {
      const stream = new EventSource(
        `/api/v1/tasks/${encodeURIComponent(taskId)}/events?workspace_id=${encodeURIComponent(workspaceId)}`,
      );
      const update = () => {
        void client.invalidateQueries({ queryKey: queryKeys.tasks(workspaceId) });
        void client.invalidateQueries({ queryKey: queryKeys.task(workspaceId, taskId) });
      };
      const complete = () => {
        update();
        void client.invalidateQueries({
          predicate: (query) => query.queryKey[1] === workspaceId,
        });
      };
      for (const eventName of [
        "task.queued",
        "task.started",
        "task.recovered",
        "task.progress",
        "task.cancellation_requested",
        "task.completed",
        "task.failed",
        "task.cancelled",
      ]) {
        stream.addEventListener(
          eventName,
          ["task.completed", "task.failed", "task.cancelled"].includes(eventName)
            ? complete
            : update,
        );
      }
      stream.onopen = () =>
        setFailedTaskIds((current) => {
          const next = new Set(current);
          next.delete(taskId);
          return next;
        });
      stream.onerror = () => setFailedTaskIds((current) => new Set(current).add(taskId));
      return stream;
    });
    return () => streams.forEach((stream) => stream.close());
  }, [client, streamed, workspaceId]);
  useEffect(() => {
    const activeIds = active ? active.split(",") : [];
    const streamedIds = streamed ? streamed.split(",") : [];
    const needsPolling =
      activeIds.length > streamedIds.length ||
      streamedIds.some((taskId) => failedTaskIds.has(taskId));
    if (!pageVisible || !needsPolling || !activeIds.length) return undefined;
    const timer = window.setInterval(() => {
      void client.invalidateQueries({ queryKey: queryKeys.tasks(workspaceId) });
      for (const taskId of activeIds) {
        void client.invalidateQueries({ queryKey: queryKeys.task(workspaceId, taskId) });
      }
    }, 5_000);
    return () => window.clearInterval(timer);
  }, [active, client, failedTaskIds, pageVisible, streamed, workspaceId]);
}
