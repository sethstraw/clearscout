import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { InvalidateQueryFilters } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { createElement } from "react";
import type { PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Task } from "../client";
import { MAX_TASK_EVENT_STREAMS, useTaskEventStreams } from "./taskEvents";

class FakeEventSource {
  static instances: FakeEventSource[] = [];

  readonly close = vi.fn();
  readonly listeners = new Map<string, Set<() => void>>();
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(name: string, listener: () => void) {
    const listeners = this.listeners.get(name) ?? new Set();
    listeners.add(listener);
    this.listeners.set(name, listeners);
  }

  emit(name: string) {
    for (const listener of this.listeners.get(name) ?? []) listener();
  }
}

function task(overrides: Partial<Task> = {}): Task {
  return {
    attempt_count: 0,
    job_posting_ids: [],
    pending_discovery_ids: [],
    source_ids: [],
    cancellation_requested_at: null,
    completed_at: null,
    created_at: "2026-08-10T12:00:00Z",
    error_category: null,
    error_message: null,
    id: "task-1",
    progress: {},
    started_at: null,
    status: "running",
    task_type: "workflow.tailor",
    updated_at: "2026-08-10T12:00:00Z",
    workspace_id: "workspace-hooks",
    ...overrides,
  };
}

function setup() {
  const invalidations: InvalidateQueryFilters[] = [];
  const client = new QueryClient();
  vi.spyOn(client, "invalidateQueries").mockImplementation((filters?: InvalidateQueryFilters) => {
    invalidations.push(filters ?? {});
    return Promise.resolve();
  });
  const wrapper = ({ children }: PropsWithChildren) =>
    createElement(QueryClientProvider, { client }, children);
  return { invalidations, wrapper };
}

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("task event streams", () => {
  it("refreshes every workspace query when a task reaches a terminal state", () => {
    const { invalidations, wrapper } = setup();
    const { unmount } = renderHook(
      () => useTaskEventStreams("workspace-hooks", [task({ task_type: "posting.refresh" })]),
      { wrapper },
    );

    const stream = FakeEventSource.instances[0];
    expect(stream?.url).toBe("/api/v1/tasks/task-1/events?workspace_id=workspace-hooks");
    act(() => stream?.emit("task.completed"));

    expect(invalidations.map((filters) => filters.queryKey)).toContainEqual([
      "tasks",
      "workspace-hooks",
    ]);
    expect(invalidations.map((filters) => filters.queryKey)).toContainEqual([
      "task",
      "workspace-hooks",
      "task-1",
    ]);
    const workspaceInvalidation = invalidations.find((filters) => filters.predicate);
    expect(
      workspaceInvalidation?.predicate?.({ queryKey: ["artifacts", "workspace-hooks"] } as never),
    ).toBe(true);
    expect(
      workspaceInvalidation?.predicate?.({ queryKey: ["artifacts", "another-workspace"] } as never),
    ).toBe(false);

    unmount();
    expect(stream?.close).toHaveBeenCalledOnce();
  });

  it("keeps progress refresh scoped to its task", () => {
    const { invalidations, wrapper } = setup();
    renderHook(() => useTaskEventStreams("workspace-hooks", [task()]), { wrapper });

    act(() => FakeEventSource.instances[0]?.emit("task.progress"));

    expect(invalidations).toHaveLength(2);
    expect(invalidations.some((filters) => typeof filters.predicate === "function")).toBe(false);
  });

  it("opens only a bounded number of workspace task streams", () => {
    const { wrapper } = setup();
    const tasks = Array.from({ length: MAX_TASK_EVENT_STREAMS + 1 }, (_, index) =>
      task({ id: `task-${index}` }),
    );

    renderHook(() => useTaskEventStreams("workspace-hooks", tasks), { wrapper });

    expect(FakeEventSource.instances).toHaveLength(MAX_TASK_EVENT_STREAMS);
  });

  it("closes streams and stops polling while the tab is hidden", () => {
    vi.useFakeTimers();
    const { invalidations, wrapper } = setup();
    const { rerender } = renderHook(
      () => useTaskEventStreams("workspace-hooks", [task(), task({ id: "task-2" })]),
      { wrapper },
    );
    const stream = FakeEventSource.instances[0];

    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    rerender();
    expect(stream?.close).toHaveBeenCalledOnce();

    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    expect(invalidations).toEqual([]);
  });

  it("polls active tasks that exceed streaming capacity", () => {
    vi.useFakeTimers();
    const { invalidations, wrapper } = setup();
    const tasks = Array.from({ length: MAX_TASK_EVENT_STREAMS + 1 }, (_, index) =>
      task({ id: `task-${index}` }),
    );

    renderHook(() => useTaskEventStreams("workspace-hooks", tasks), { wrapper });
    act(() => {
      vi.advanceTimersByTime(5_000);
    });

    expect(invalidations.map((filters) => filters.queryKey)).toContainEqual([
      "task",
      "workspace-hooks",
      `task-${MAX_TASK_EVENT_STREAMS}`,
    ]);
  });

  it("refreshes every workspace query when polling observes an unstreamed terminal task", () => {
    const { invalidations, wrapper } = setup();
    const active = Array.from({ length: MAX_TASK_EVENT_STREAMS + 1 }, (_, index) =>
      task({ id: `task-${index}` }),
    );
    const { rerender } = renderHook(
      ({ tasks }: { tasks: Task[] }) => useTaskEventStreams("workspace-hooks", tasks),
      {
        initialProps: { tasks: active },
        wrapper,
      },
    );

    rerender({
      tasks: active.map((item) =>
        item.id === `task-${MAX_TASK_EVENT_STREAMS}` ? { ...item, status: "completed" } : item,
      ),
    });

    const workspaceInvalidation = invalidations.find((filters) => filters.predicate);
    expect(
      workspaceInvalidation?.predicate?.({ queryKey: ["jobs", "workspace-hooks"] } as never),
    ).toBe(true);
  });

  it("polls while SSE is down and stops polling when it reconnects", () => {
    vi.useFakeTimers();
    const { invalidations, wrapper } = setup();
    renderHook(() => useTaskEventStreams("workspace-hooks", [task()]), { wrapper });
    const stream = FakeEventSource.instances[0];

    act(() => stream?.onerror?.());
    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    expect(invalidations.map((filters) => filters.queryKey)).toContainEqual([
      "tasks",
      "workspace-hooks",
    ]);
    expect(invalidations.map((filters) => filters.queryKey)).toContainEqual([
      "task",
      "workspace-hooks",
      "task-1",
    ]);

    invalidations.length = 0;
    act(() => stream?.onopen?.());
    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    expect(invalidations).toEqual([]);
  });
});
