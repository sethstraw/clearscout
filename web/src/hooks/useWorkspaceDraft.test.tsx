import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";

import { useWorkspaceDraft } from "./useWorkspaceDraft";

afterEach(() => window.sessionStorage.clear());

it("retains answers when explicitly rebasing a draft onto a reviewed saved version", () => {
  const draft = renderHook(() => useWorkspaceDraft<{ thesis: string }>("one", "search", 2));
  act(() => draft.result.current.setEdits({ thesis: "Technical direction" }));
  act(() => draft.result.current.setEdits({ thesis: "Technical direction" }, 3));
  expect(draft.result.current.edits).toEqual({ thesis: "Technical direction" });
  expect(draft.result.current.expectedVersion).toBe(3);
  draft.unmount();
  const restored = renderHook(() => useWorkspaceDraft<{ thesis: string }>("one", "search", 3));
  expect(restored.result.current.expectedVersion).toBe(3);
  expect(restored.result.current.edits).toEqual({ thesis: "Technical direction" });
});

it("restores edits after remount, isolates workspaces, and retains the original save version", () => {
  const first = renderHook(() => useWorkspaceDraft<{ thesis: string }>("one", "search", 2));
  act(() => first.result.current.setEdits({ thesis: "Technical direction" }));
  first.unmount();
  const restored = renderHook(
    ({ workspaceId, version }) =>
      useWorkspaceDraft<{ thesis: string }>(workspaceId, "search", version),
    {
      initialProps: { workspaceId: "one", version: 3 },
    },
  );
  expect(restored.result.current.edits).toEqual({ thesis: "Technical direction" });
  expect(restored.result.current.expectedVersion).toBe(2);
  restored.rerender({ workspaceId: "two", version: 7 });
  expect(restored.result.current.edits).toBeNull();
  act(() => restored.result.current.setEdits({ thesis: "Product work" }));
  restored.rerender({ workspaceId: "one", version: 3 });
  expect(restored.result.current.edits).toEqual({ thesis: "Technical direction" });
  act(() => restored.result.current.setEdits(null));
  restored.unmount();
  const cleared = renderHook(() => useWorkspaceDraft<{ thesis: string }>("one", "search", 3));
  expect(cleared.result.current.edits).toBeNull();
  expect(cleared.result.current.expectedVersion).toBe(3);
});
