import { useCallback, useEffect, useRef, useState } from "react";
import type { FocusEvent, KeyboardEvent } from "react";

import { focusIsParked, returnFocus } from "../../providers/focusReturn";

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}

function aDialogIsOpen(): boolean {
  return document.querySelector('[role="dialog"]') !== null;
}

/**
 * Keyboard triage for the rendered pending rows. Scoped to the container it is
 * spread onto: it never touches window, ignores keys while a field is focused or
 * a modal is open, and exposes the cursor only while the container holds focus.
 * Focus on a row that leaves the view falls back to the container.
 */
export function usePendingQueueKeyboard({
  ids,
  onDismiss,
  onEvaluate,
  onReview,
  onUndo,
}: {
  ids: readonly string[];
  onDismiss: (id: string) => void;
  onEvaluate: (id: string) => void;
  onReview: (id: string) => void;
  onUndo: () => void;
}) {
  const [focusedIndex, setFocusedIndex] = useState(0);
  const [engaged, setEngaged] = useState(false);
  const last = ids.length - 1;
  const containerRef = useRef<HTMLDivElement | null>(null);
  const lastFocused = useRef<HTMLElement | null>(null);

  // A row that leaves the view takes its focused control with it, so focus lands on the region.
  useEffect(() => {
    const origin = lastFocused.current;
    const region = containerRef.current;
    if (!origin || origin.isConnected || !region) return;
    lastFocused.current = null;
    const from = { origin, landing: region };
    if (focusIsParked(from)) returnFocus(from);
  });

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLElement>) => {
      // Act only when the region container itself holds focus: a descendant
      // control's keydown also bubbles here, and acting then would fire the paid
      // action on the cursor row rather than the focused one.
      if (event.target !== event.currentTarget) return;
      if (aDialogIsOpen() || isTypingTarget(event.target) || last < 0) return;
      // Clamp the stored index against the current list before moving, so a row
      // count that shrank does not strand the cursor past the end.
      const id = ids[Math.min(focusedIndex, last)];
      switch (event.key) {
        case "j":
          setFocusedIndex((current) => Math.min(Math.min(current, last) + 1, last));
          event.preventDefault();
          break;
        case "k":
          setFocusedIndex((current) => Math.max(Math.min(current, last) - 1, 0));
          event.preventDefault();
          break;
        case "x":
          if (id) onDismiss(id);
          event.preventDefault();
          break;
        case "e":
          if (id) onEvaluate(id);
          event.preventDefault();
          break;
        case "r":
          if (id) onReview(id);
          event.preventDefault();
          break;
        case "u":
          onUndo();
          event.preventDefault();
          break;
        default:
          break;
      }
    },
    [focusedIndex, ids, last, onDismiss, onEvaluate, onReview, onUndo],
  );

  // Engage the cursor only when the container itself is focused, not when focus
  // bubbles up from a descendant control, so the cursor and the real focus agree.
  const onFocus = useCallback((event: FocusEvent<HTMLElement>) => {
    lastFocused.current = event.target;
    if (event.target === event.currentTarget) setEngaged(true);
  }, []);

  const onBlur = useCallback((event: FocusEvent<HTMLElement>) => {
    if (event.currentTarget.contains(event.relatedTarget)) return;
    setEngaged(false);
    if (event.relatedTarget) {
      lastFocused.current = null;
      return;
    }
    // A blur to nowhere is a press on blank space unless the control was removed with its row.
    const origin = event.target;
    queueMicrotask(() => {
      if (origin.isConnected && lastFocused.current === origin) lastFocused.current = null;
    });
  }, []);

  return {
    // -1 while the container is not focused, so no row carries the cursor at rest.
    activeIndex: engaged ? Math.min(focusedIndex, last) : -1,
    containerRef,
    containerProps: {
      ref: containerRef,
      onKeyDown,
      onFocus,
      onBlur,
      tabIndex: 0,
    },
  };
}
