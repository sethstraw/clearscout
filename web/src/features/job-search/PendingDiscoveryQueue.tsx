import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  api,
  ApiError,
  type PendingDiscovery,
  type PendingDiscoveryFacets,
  type PendingDiscoveryPage,
  type RelevanceReview,
  type Task,
} from "../../api/client";
import {
  useEnqueueTask,
  useFreeMode,
  usePendingQueueRescreen,
  useRelevanceReviewProviderConfiguration,
  useResetPendingQueue,
  useTaskCosts,
  useTasks,
  useTrackPendingDiscovery,
} from "../../api/hooks";
import { useConfirmation } from "../../confirmations";
import { FREE_MODE_REASON } from "../../components/freeMode";
import { PaidAction } from "../../components/PaidAction";
import { EvaluationCostDetails } from "../../components/EvaluationCostDetails";
import { EmptyState, ErrorState } from "../../components/States";
import { focusIsParked, returnFocus } from "../../providers/focusReturn";
import { DiscoveryDispositionManager, DismissSelection } from "./DiscoveryDispositions";
import { useDispositionAction } from "./useDispositionAction";
import { usePendingQueueKeyboard } from "./usePendingQueueKeyboard";
import { discoveryRelevanceKey, relevanceRank } from "./relevanceReviewModel";
import type { RelevanceKey } from "./relevanceReviewModel";
import {
  evaluationPendingDiscoveryIds,
  pendingDiscoveryIsEvaluated,
} from "./evaluationTaskOutcome";
import type {
  PendingQueueFilters,
  QueuePageSize,
  QueueSortMode,
} from "./pendingDiscoveryFilterModel";
import {
  ATTENTION_READINESS,
  CONTENT_READINESS_LABELS,
  MAX_SELECTION_SIZE,
  UNFILTERED_QUEUE,
  contentNeedsRecovery,
  contentReadiness,
  markerRank,
  neverReadDiscoveryIds,
  selectAllRefusal,
} from "./pendingDiscoveryFilterModel";
import { AssistedRecoveryAction } from "./AssistedRecoveryAction";
import { PendingDiscoveryFilters } from "./PendingDiscoveryFilters";
import { PendingDiscoveryIntake } from "./PendingDiscoveryIntake";
import { copiesWorthKeepingInstead } from "./pendingDuplicateSuggestionModel";
import { type ListingKind, PendingDiscoveryRow } from "./PendingDiscoveryRow";
import { PublisherFactsRefresh } from "./PublisherFactsRefresh";
import { SavedClosureAction } from "./SavedClosureAction";
import { SkippedMarkedNote } from "./SkippedMarkedNote";

function count(progress: Record<string, unknown>, key: string): number {
  return typeof progress[key] === "number" ? progress[key] : 0;
}

/** The running count of a whole-queue re-screen, then what it did to the queue. */
function RescreenStatus({ task, finished }: { task?: Task; finished?: Task }) {
  const progress: Record<string, unknown> = (finished ?? task)?.progress ?? {};
  if (task?.status === "running" && count(progress, "total") > 0) {
    return (
      <p role="status">
        Re-screening {count(progress, "current")} of {count(progress, "total")} discoveries;{" "}
        {count(progress, "held_out")} held out so far.
      </p>
    );
  }
  if (!finished) return null;
  if (finished.status === "failed") {
    return (
      <p className="field-error" role="alert">
        Re-screen failed: {finished.error_message}
      </p>
    );
  }
  if (finished.status === "cancelled") {
    return <p role="status">Re-screen cancelled; rows it had not reached are unchanged.</p>;
  }
  const others = [
    [count(progress, "vanished"), "left the queue first"],
    [count(progress, "skipped_for_paid_task"), "left alone under an active paid task"],
    [count(progress, "failed"), "could not be re-screened"],
  ]
    .filter(([amount]) => Number(amount) > 0)
    .map(([amount, label]) => `${amount} ${label}`);
  return (
    <p role="status">
      Re-screened {count(progress, "screened")} discoveries and held out{" "}
      {count(progress, "held_out")}.{others.length ? ` ${others.join("; ")}.` : ""}
    </p>
  );
}

function matchCount(count: number): string {
  return `${count} ${count === 1 ? "matches" : "match"}`;
}

/** Where rows the current view hides do match, read from the counts behind each filter. */
function hiddenMatchesHint(
  filters: PendingQueueFilters,
  readinessTotal: number,
  constraintTotal: number,
  strictUncertainty: boolean,
): string {
  const places: string[] = [];
  if (filters.readiness !== "all" && readinessTotal > 0) {
    places.push(
      `${matchCount(readinessTotal)} in another content state; choose Content readiness > All content states.`,
    );
  }
  if (filters.constraint !== "all" && constraintTotal > 0) {
    places.push(
      strictUncertainty
        ? `${matchCount(constraintTotal)} on another constraint route; open Unconfirmed matches for uncertain postings, or Screened out for postings that conflict with your rules.`
        : `${matchCount(constraintTotal)} on another constraint route; choose Screened out, or More filters > Constraint route to inspect unconfirmed matches or all confidence routes.`,
    );
  }
  return places.join(" ");
}

function taskIsInFlight(task: Task | undefined): boolean {
  return task?.status === "queued" || task?.status === "running";
}

function evaluationTasksByPending(tasks: readonly Task[] | undefined): Map<string, Task> {
  const map = new Map<string, Task>();
  for (const task of tasks ?? []) {
    for (const pendingId of evaluationPendingDiscoveryIds(task)) {
      const existing = map.get(pendingId);
      if (!existing || task.updated_at > existing.updated_at) map.set(pendingId, task);
    }
  }
  return map;
}

const SELECTION_BOUND_ID = "pending-selection-bound";

type SelectedDiscovery = {
  item: PendingDiscovery;
  relevanceReview: RelevanceReview | undefined;
};

type Selection = {
  entries: ReadonlyMap<string, SelectedDiscovery>;
  scope: string;
};

export function PendingDiscoveryQueue({
  countPending,
  discoveries,
  employers,
  facets,
  filters,
  focusPendingId,
  laneStatus,
  matching,
  relevanceReviews,
  loaded,
  pageSize,
  queueTotal,
  searchText,
  selectionScope,
  settled,
  strictUncertainty,
  onAdded,
  onFiltersChange,
  onFocusRestored,
  onPageSizeChange,
  onSearchTextChange,
  onStrictUncertaintyChange,
  workspaceId,
}: {
  /** True while the counts on screen were read under filters other than the current ones. */
  countPending: boolean;
  discoveries: PendingDiscovery[];
  employers: PendingDiscoveryPage["employers"];
  facets: PendingDiscoveryFacets | undefined;
  filters: PendingQueueFilters;
  focusPendingId: string | null;
  laneStatus: { ready: string; attention: string | null } | null;
  matching: number;
  relevanceReviews: ReadonlyMap<string, RelevanceReview>;
  loaded: boolean;
  pageSize: QueuePageSize;
  queueTotal: number;
  searchText: string;
  selectionScope: string;
  /** False while the shown rows are held over from the previous read. */
  settled: boolean;
  strictUncertainty: boolean;
  onAdded: (pendingDiscoveryId: string) => void;
  onFiltersChange: (next: PendingQueueFilters) => void;
  onFocusRestored: () => void;
  onPageSizeChange: (value: QueuePageSize) => void;
  onSearchTextChange: (value: string) => void;
  onStrictUncertaintyChange: (value: boolean) => void;
  workspaceId: string;
}) {
  const costs = useTaskCosts(workspaceId);
  const freeMode = useFreeMode();
  const relevanceReviewConfiguration = useRelevanceReviewProviderConfiguration(workspaceId, true);
  const enqueue = useEnqueueTask(workspaceId);
  const track = useTrackPendingDiscovery(workspaceId);
  const resetQueue = useResetPendingQueue(workspaceId);
  const rescreen = usePendingQueueRescreen(workspaceId);
  const confirm = useConfirmation();
  const dispositions = useDispositionAction(workspaceId);
  const [undo, setUndo] = useState<{ id: string; label: string } | null>(null);
  const [undoHold, setUndoHold] = useState(false);
  const [announce, setAnnounce] = useState("");
  const evaluationPreparing = useRef(false);
  const trackingBusy = useRef(false);
  const [tracking, setTracking] = useState(false);
  const [trackingOutcomes, setTrackingOutcomes] = useState<
    ReadonlyMap<string, { item: PendingDiscovery; error: string | null }>
  >(new Map());
  // Held here because the bulk dismissal that sets it also clears the selection bar.
  const [bulkSkippedMarked, setBulkSkippedMarked] = useState(0);
  const tasks = useTasks(workspaceId);
  const evaluationTasks = useMemo(() => evaluationTasksByPending(tasks.data), [tasks.data]);
  const [selection, setSelection] = useState<Selection>(() => ({
    entries: new Map(),
    scope: selectionScope,
  }));
  const [queued, setQueued] = useState<Set<string>>(new Set());
  const [sortMode, setSortMode] = useState<QueueSortMode>("relevance");
  const [dismissalRowId, setDismissalRowId] = useState<string | null>(null);
  const [openListing, setOpenListing] = useState<{ id: string; kind: ListingKind } | null>(null);
  const [detailsRowId, setDetailsRowId] = useState<string | null>(null);
  const [dismissalPendingId, setDismissalPendingId] = useState<string | null>(null);
  const selectAllRef = useRef<HTMLInputElement | null>(null);
  const rowCheckboxes = useRef(new Map<string, HTMLInputElement>());
  const rowDismissButtons = useRef(new Map<string, HTMLButtonElement>());
  const dismissalFocusId = useRef<string | null>(null);

  const evaluationInFlight = (id: string) =>
    queued.has(id) || taskIsInFlight(evaluationTasks.get(id));
  // Consults the live task map, not only the item's own snapshot, so an off-page
  // selection reflects a completed evaluation the stale snapshot cannot show.
  const discoveryIsEvaluated = useCallback(
    (item: PendingDiscovery) => pendingDiscoveryIsEvaluated(item, evaluationTasks.get(item.id)),
    [evaluationTasks],
  );

  const clearSelection = useCallback(() => {
    setSelection({ entries: new Map(), scope: selectionScope });
  }, [selectionScope]);

  const selectionEntry = useCallback(
    (item: PendingDiscovery): SelectedDiscovery => ({
      item,
      relevanceReview: relevanceReviews.get(item.id) ?? item.relevance_review ?? undefined,
    }),
    [relevanceReviews],
  );

  const selectionEntries = useMemo(
    () =>
      selection.scope === selectionScope ? selection.entries : new Map<string, SelectedDiscovery>(),
    [selection, selectionScope],
  );
  const selectedIds = new Set(selectionEntries.keys());
  // Rows hold one stable select handler, so the selection cap is read from the
  // committed selection rather than from a closure rebuilt for each row.
  const selectionEntriesRef = useRef(selectionEntries);
  useEffect(() => {
    selectionEntriesRef.current = selectionEntries;
  }, [selectionEntries]);

  // The mutation object is rebuilt each render while its bound mutateAsync is not,
  // so naming the function keeps every row's evaluate handler stable.
  const enqueueTask = enqueue.mutateAsync;
  const evaluate = useCallback(
    async (ids: string[]) => {
      if (ids.length === 0) return;
      try {
        // One batch task evaluates every selected row under a single shared spend
        // ledger, so the whole queue is one action rather than one task per row.
        await enqueueTask({
          workspace_id: workspaceId,
          task_type: "discovery.evaluate_batch",
          payload: { pending_discovery_ids: ids },
        });
        setQueued((current) => {
          const next = new Set(current);
          for (const id of ids) next.add(id);
          return next;
        });
        clearSelection();
      } catch {
        // A failed enqueue leaves the selection intact for an explicit retry.
      }
    },
    [clearSelection, enqueueTask, workspaceId],
  );

  const toggleDetails = useCallback((id: string) => {
    setDetailsRowId((current) => (current === id ? null : id));
  }, []);
  const toggleListing = useCallback((id: string, kind: ListingKind) => {
    setOpenListing((current) =>
      current?.id === id && current.kind === kind ? null : { id, kind },
    );
  }, []);
  const toggleDismissal = useCallback((id: string) => {
    setDismissalRowId((current) => (current === id ? null : id));
  }, []);
  const closeDismissal = useCallback((id: string) => {
    setDismissalRowId(null);
    setDismissalPendingId(null);
    dismissalFocusId.current = id;
  }, []);
  const changeDismissalPending = useCallback((id: string, pending: boolean) => {
    setDismissalPendingId(pending ? id : null);
  }, []);
  const registerCheckbox = useCallback((id: string, node: HTMLInputElement | null) => {
    if (node) rowCheckboxes.current.set(id, node);
    else rowCheckboxes.current.delete(id);
  }, []);
  const registerDismissButton = useCallback((id: string, node: HTMLButtonElement | null) => {
    if (node) rowDismissButtons.current.set(id, node);
    else rowDismissButtons.current.delete(id);
  }, []);
  // The sticky header row sits under the sticky batch bar, whose height follows the selection.
  const trackBatchBarHeight = useCallback((node: HTMLDivElement | null) => {
    const host = node?.parentElement;
    if (!node || !host || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() =>
      host.style.setProperty("--pending-batch-bar-height", `${node.offsetHeight}px`),
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  const toggleRowSelection = useCallback(
    (item: PendingDiscovery) => {
      if (countPending) return;
      const entries = selectionEntriesRef.current;
      setBulkSkippedMarked(0);
      if (!entries.has(item.id) && entries.size === MAX_SELECTION_SIZE) {
        setAnnounce(`Select no more than ${MAX_SELECTION_SIZE} postings.`);
        return;
      }
      setSelection((current) => {
        const next = new Map(current.scope === selectionScope ? current.entries : undefined);
        if (next.has(item.id)) next.delete(item.id);
        else if (next.size < MAX_SELECTION_SIZE) next.set(item.id, selectionEntry(item));
        return { entries: next, scope: selectionScope };
      });
    },
    [countPending, selectionEntry, selectionScope],
  );

  const rowLabel = (item: PendingDiscovery) =>
    `${item.title || "this discovery"} at ${item.employer || "the employer"}`;

  // Keyboard evaluate and review reach the same paid confirmation the buttons use.
  // An ineligible key announces why, matching what a disabled mouse button shows.
  const evaluateOne = async (item: PendingDiscovery) => {
    if (freeMode) return setAnnounce(FREE_MODE_REASON);
    if (evaluationPreparing.current)
      return setAnnounce("Evaluation confirmation already in progress");
    if (!costs.loaded) return setAnnounce("Costs still loading");
    if (evaluationInFlight(item.id)) return setAnnounce("Evaluation in progress");
    if (discoveryIsEvaluated(item)) return setAnnounce("Already evaluated");
    if (item.current_posting_status === "closed") {
      return setAnnounce("Mark this posting closed from saved publisher evidence");
    }
    if (contentReadiness(item) === "screening_refused") {
      return setAnnounce(
        "This posting conflicts with your search rules. Review your rules or dismiss it.",
      );
    }
    if (contentReadiness(item) !== "ready") {
      return setAnnounce("Refresh this posting first.");
    }
    evaluationPreparing.current = true;
    setAnnounce("Preparing cost estimate");
    let estimate;
    try {
      estimate = await api.evaluationPreflight(workspaceId, { pending_discovery_ids: [item.id] });
    } catch (error) {
      setAnnounce(error instanceof Error ? error.message : "Cost estimate unavailable. Try again.");
      evaluationPreparing.current = false;
      return;
    }
    const retry = evaluationTasks.get(item.id)?.status === "failed";
    const betterCopies = copiesWorthKeepingInstead(item.id, item.duplicate_suggestions ?? []);
    const duplicateWarning =
      betterCopies > 0
        ? `This may be the same opening as ${betterCopies} other found ${betterCopies === 1 ? "job" : "jobs"} suggested to keep instead. Evaluating this one may pay for the same opening twice. `
        : "";
    const approved = await confirm({
      title: `${retry ? "Retry evaluation for" : "Fully evaluate"} ${rowLabel(item)}?`,
      description: `${duplicateWarning}Review the estimate before you confirm.`,
      details: <EvaluationCostDetails estimate={estimate} />,
      confirmLabel: retry ? "Retry evaluation" : "Run full evaluation",
    });
    evaluationPreparing.current = false;
    setAnnounce("");
    if (approved) await evaluate([item.id]);
  };
  // Memoized rows need one handler that never changes, so it reads the latest evaluateOne.
  const evaluateOneRef = useRef(evaluateOne);
  useEffect(() => {
    evaluateOneRef.current = evaluateOne;
  });
  const evaluateRow = useCallback((item: PendingDiscovery) => {
    void evaluateOneRef.current(item);
  }, []);

  const reviewOne = async (item: PendingDiscovery) => {
    if (freeMode) return setAnnounce(FREE_MODE_REASON);
    if (!costs.loaded) return setAnnounce("Costs still loading");
    if (!relevanceReviewConfiguration.loaded) return setAnnounce("Provider configuration loading");
    if (relevanceReviewConfiguration.isError)
      return setAnnounce(
        relevanceReviewConfiguration.heldBecause === "undescribable"
          ? "Provider configuration could not be read, so paid review is held"
          : "Provider configuration unavailable",
      );
    if (contentReadiness(item) === "screening_refused") {
      return setAnnounce(
        "This posting conflicts with your search rules. Review your rules or dismiss it.",
      );
    }
    if (contentReadiness(item) !== "ready") {
      return setAnnounce("Refresh this posting first.");
    }
    if (item.current_posting_status === "closed") {
      return setAnnounce("Mark this posting closed from saved publisher evidence");
    }
    setAnnounce("");
    const approved = await confirm({
      title: `Review relevance of ${rowLabel(item)}?`,
      description: `Reviews this discovery against your CV and search parameters. No full evaluation. ${relevanceReviewConfiguration.description} ${costs.confirmation("discovery.relevance_review", 1)}`,
      confirmLabel: "Review selected relevance",
    });
    if (!approved) return;
    await enqueue
      .mutateAsync({
        workspace_id: workspaceId,
        task_type: "discovery.relevance_review",
        payload: { pending_discovery_ids: [item.id] },
      })
      .catch(() => undefined);
  };

  // Dismiss applies at once and offers undo, rather than opening the reason modal;
  // a favorite or kept row asks first, since dismissing it clears its markers.
  const dismissOne = async (item: PendingDiscovery) => {
    if (evaluationInFlight(item.id)) return setAnnounce("Evaluation in progress; not dismissed");
    setAnnounce("");
    const marked = Boolean(item.favorite || item.kept);
    const applied = await dispositions.act(
      {
        action: "dismiss",
        category: "other",
        dismiss_marked: marked,
        pending_discovery_ids: [item.id],
        reason: "Dismissed from keyboard triage.",
        selection_mode: "explicit",
      },
      { skipConfirm: !marked },
    );
    if (applied) {
      setSelection((current) => {
        if (current.scope !== selectionScope || !current.entries.has(item.id)) return current;
        const entries = new Map(current.entries);
        entries.delete(item.id);
        return { entries, scope: selectionScope };
      });
      // A restore cannot reach a row its source no longer lists, so it gets no undo.
      if (item.withdrawn_at) return setAnnounce(`Dismissed ${rowLabel(item)}.`);
      setUndo({ id: item.id, label: rowLabel(item) });
      setAnnounce(`Dismissed ${rowLabel(item)}. Press u to undo.`);
    }
  };

  const undoDismiss = async () => {
    if (!undo) return setAnnounce("Nothing to undo");
    if (dispositions.pending) return;
    const { id, label } = undo;
    let failure = "the restore failed.";
    let refused = false;
    const restored = await dispositions.act(
      {
        action: "restore",
        category: "other",
        pending_discovery_ids: [id],
        reason: "Restore dismissal: Dismissed from keyboard triage.",
        selection_mode: "explicit",
      },
      {
        skipConfirm: true,
        onError: (error) => {
          if (!(error instanceof ApiError)) return;
          failure = error.message;
          // A conflict means a retry would meet the same refusal.
          refused = error.status === 409;
        },
      },
    );
    if (restored || refused) setUndo(null);
    setAnnounce(restored ? `Restored ${label}` : `Not restored: ${failure}`);
  };

  // Counts, badges, and order read the stored verdict whether or not it is stale.
  const storedRelevanceReview = (item: PendingDiscovery): RelevanceReview | undefined =>
    relevanceReviews.get(item.id) ?? item.relevance_review ?? undefined;
  const relevanceKeyOf = (item: PendingDiscovery): RelevanceKey =>
    discoveryRelevanceKey(storedRelevanceReview(item), item.relevance_review_rejection);
  // Counted by the server under every other filter, so attention shows before any select.
  // A route's count is what choosing it lists, favorite and kept rows included.
  const attentionEntries = ATTENTION_READINESS.flatMap((readiness) => {
    const entry = facets?.readiness.find((item) => item.value === readiness);
    return entry?.holding ? [{ readiness, count: entry.count, holding: entry.holding }] : [];
  });
  const attentionTotal = attentionEntries.reduce((sum, entry) => sum + entry.holding, 0);
  const readinessTotal = (facets?.readiness ?? []).reduce((sum, entry) => sum + entry.holding, 0);
  const emptyViewHint = hiddenMatchesHint(
    filters,
    readinessTotal,
    facets?.constraint_total ?? 0,
    strictUncertainty,
  );
  // Favorites, then kept rows, lead under either sort, each group listing open rows
  // before no-longer-listed ones, as the server orders them. Then the default view leads
  // with the strongest relevance verdicts, tie-broken by item.rank; "rank" uses that alone.
  const ordered = [...discoveries].sort((a, b) => {
    const byMarker = markerRank(a) - markerRank(b);
    if (byMarker !== 0) return byMarker;
    const byListing = Number(Boolean(a.withdrawn_at)) - Number(Boolean(b.withdrawn_at));
    if (byListing !== 0) return byListing;
    if (sortMode === "rank") return (b.rank ?? 0) - (a.rank ?? 0);
    const byRelevance = relevanceRank(relevanceKeyOf(a)) - relevanceRank(relevanceKeyOf(b));
    return byRelevance !== 0 ? byRelevance : (b.rank ?? 0) - (a.rank ?? 0);
  });

  const itemById = new Map(ordered.map((item) => [item.id, item] as const));
  const keyboard = usePendingQueueKeyboard({
    ids: ordered.map((item) => item.id),
    onDismiss: (id) => {
      const item = itemById.get(id);
      if (item) void dismissOne(item);
    },
    onEvaluate: (id) => {
      const item = itemById.get(id);
      if (item) void evaluateOne(item);
    },
    onReview: (id) => {
      const item = itemById.get(id);
      if (item) void reviewOne(item);
    },
    onUndo: () => void undoDismiss(),
  });
  const { containerRef } = keyboard;
  // The row's Dismiss button stays disabled until the cleared pending state commits.
  useEffect(() => {
    const id = dismissalFocusId.current;
    if (id === null) return;
    dismissalFocusId.current = null;
    const from = {
      origin: rowDismissButtons.current.get(id) ?? null,
      landing: containerRef.current,
    };
    if (focusIsParked(from)) returnFocus(from);
  });

  const selectableVisibleIds = ordered
    .filter((item) => !evaluationInFlight(item.id) && !discoveryIsEvaluated(item))
    .map((item) => item.id);
  const discoveriesById = new Map(discoveries.map((item) => [item.id, item] as const));
  const selectedEntries = [
    ...discoveries.flatMap((item) => {
      const selected = selectionEntries.get(item.id);
      return selected
        ? [
            {
              item,
              relevanceReview:
                relevanceReviews.get(item.id) ?? item.relevance_review ?? selected.relevanceReview,
            },
          ]
        : [];
    }),
    ...[...selectionEntries].flatMap(([id, selected]) =>
      discoveriesById.has(id) ? [] : [selected],
    ),
  ].filter((selected) => !selected.item.withdrawn_at && !discoveryIsEvaluated(selected.item));
  const selectedDiscoveries = selectedEntries.map((selected) => selected.item);
  const selectedList = selectedDiscoveries.map((item) => item.id);
  const selectedCount = selectedList.length;
  const trackingSelectionRef = useRef({ ids: selectedList, scope: selectionScope, settled });
  useEffect(() => {
    trackingSelectionRef.current = { ids: selectedList, scope: selectionScope, settled };
  }, [selectedList, selectionScope, settled]);
  // A review is billed per model call, and the server groups the selection into
  // batches of at most batchRows. Posting length can split a batch further, so
  // this is a floor on the calls rather than an exact count.
  const batchRows = relevanceReviewConfiguration.batchRows ?? selectedCount;
  const reviewCallCount = Math.max(1, Math.ceil(selectedCount / Math.max(1, batchRows)));
  const recoverableContentIds = selectedDiscoveries
    .filter((item) => contentNeedsRecovery(item))
    .map((item) => item.id);
  const savedClosureIds = selectedDiscoveries
    .filter((item) => item.current_posting_status === "closed")
    .map((item) => item.id);
  const evaluationBlockedCount = selectedDiscoveries.filter(
    (item) => contentReadiness(item) !== "ready" || item.current_posting_status === "closed",
  ).length;
  const screeningRefusedCount = selectedDiscoveries.filter(
    (item) => contentReadiness(item) === "screening_refused",
  ).length;
  const refreshBlockedCount = evaluationBlockedCount - screeningRefusedCount;
  const evaluationSelectionAllowed = settled && selectedCount > 0 && evaluationBlockedCount === 0;
  const reviewSelectionAllowed = settled && selectedCount > 0 && evaluationBlockedCount === 0;
  const allVisibleSelected =
    selectableVisibleIds.length > 0 && selectableVisibleIds.every((id) => selectedIds.has(id));
  const someVisibleSelected = selectableVisibleIds.some((id) => selectedIds.has(id));
  const wouldAddToSelection = selectableVisibleIds.filter((id) => !selectedIds.has(id)).length;
  const selectAllBlockedBecause = allVisibleSelected
    ? ""
    : selectAllRefusal(selectedCount, wouldAddToSelection);
  const selectAllLabel = "Select eligible discoveries on this page";

  const trackSelected = async () => {
    if (trackingBusy.current || !settled || selectedCount === 0) return;
    const scope = selectionScope;
    const eligibleById = new Map(selectedDiscoveries.map((item) => [item.id, item]));
    const items = [...selectionEntries.keys()].flatMap((id) => {
      const item = eligibleById.get(id);
      return item ? [item] : [];
    });
    const offPageCount = items.filter((item) => !discoveriesById.has(item.id)).length;
    trackingBusy.current = true;
    setTracking(true);
    try {
      const approved = await confirm({
        title: `Track ${items.length} selected ${items.length === 1 ? "job" : "jobs"}?`,
        description: `Adds these ${items.length} selected jobs to tracked postings, including ${offPageCount} selected on other pages. No provider key or evaluation is needed. Each job is tracked separately; failed jobs stay selected for retry.`,
        details: (
          <ul>
            {items.map((item) => (
              <li key={item.id}>
                <strong>{rowLabel(item)}</strong>
                <div>{item.canonical_url || item.discovery_url}</div>
              </li>
            ))}
          </ul>
        ),
        confirmLabel: `Track ${items.length} ${items.length === 1 ? "job" : "jobs"}`,
      });
      if (!approved) return;
      const current = trackingSelectionRef.current;
      if (
        current.scope !== scope ||
        !current.settled ||
        current.ids.length !== items.length ||
        items.some((item) => !current.ids.includes(item.id))
      ) {
        setAnnounce("Selection changed. Select the jobs again before tracking.");
        return;
      }
      for (const item of items) {
        let error: string | null = null;
        try {
          await track.mutateAsync(item.id);
          setSelection((selection) => {
            const entries = new Map(selection.entries);
            entries.delete(item.id);
            return { ...selection, entries };
          });
        } catch (failure) {
          error = failure instanceof Error ? failure.message : "Tracking failed. Try again.";
        }
        setTrackingOutcomes((outcomes) => new Map(outcomes).set(item.id, { item, error }));
      }
    } finally {
      trackingBusy.current = false;
      setTracking(false);
    }
  };

  const resetSelectionSurface = () => {
    clearSelection();
    setDismissalRowId(null);
  };

  const requestQueueReset = async () => {
    const approved = await confirm({
      title: "Reset the pending queue?",
      description: `This forgets machine decisions for all ${discoveries.length} discoveries shown and any others still pending. It keeps your disposition choices, favorite and kept discoveries, rows under a running paid task, and rows that no board scan can find; those source-less rows are re-screened in place. The next scan re-stages the remaining active openings. Jobs and their evaluation history are unchanged.`,
      confirmLabel: "Reset pending queue",
      danger: true,
    });
    if (!approved) return;
    clearSelection();
    const result = await resetQueue.mutateAsync().catch(() => undefined);
    if (result) {
      setAnnounce(
        `Reset archived ${result.archived_count}, kept ${result.kept_count}, and re-screened ${result.rescreened_count} discoveries.`,
      );
    }
  };

  const requestQueueRescreen = async () => {
    const approved = await confirm({
      title: "Re-screen the pending queue?",
      description:
        "Re-screens every active discovery against the current screening rules, using only " +
        "publisher facts already stored on each row. This fetches nothing from any " +
        "publisher and costs nothing. A row the current rules now refuse gets one more " +
        "individually restorable hold-out; nothing else changes.",
      confirmLabel: "Re-screen queue",
    });
    if (!approved) return;
    await rescreen.start().catch(() => undefined);
  };

  const changeFilters = (next: PendingQueueFilters) => {
    onFiltersChange(next);
    resetSelectionSurface();
  };

  const revealAddedDiscovery = (pendingDiscoveryId: string) => {
    onSearchTextChange("");
    changeFilters(UNFILTERED_QUEUE);
    onAdded(pendingDiscoveryId);
  };

  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = someVisibleSelected && !allVisibleSelected;
    }
  }, [allVisibleSelected, someVisibleSelected]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setSelection((current) => {
        if (current.scope !== selectionScope) {
          return { entries: new Map(), scope: selectionScope };
        }
        let next: Map<string, SelectedDiscovery> | undefined;
        for (const item of discoveries) {
          const selected = current.entries.get(item.id);
          if (!selected) continue;
          if (item.withdrawn_at || discoveryIsEvaluated(item)) {
            next ??= new Map(current.entries);
            next.delete(item.id);
          } else {
            const relevanceReview =
              relevanceReviews.get(item.id) ?? item.relevance_review ?? undefined;
            if (selected.item !== item || selected.relevanceReview !== relevanceReview) {
              next ??= new Map(current.entries);
              next.set(item.id, { item, relevanceReview });
            }
          }
        }
        return next ? { entries: next, scope: selectionScope } : current;
      });
      setDismissalRowId(null);
      setOpenListing(null);
      setDismissalPendingId(null);
      setDetailsRowId(null);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [discoveries, discoveryIsEvaluated, evaluationTasks, relevanceReviews, selectionScope]);

  useEffect(() => {
    if (!focusPendingId) return;
    const node = rowCheckboxes.current.get(focusPendingId);
    if (node) {
      node.focus();
      onFocusRestored();
    }
  }, [discoveries, focusPendingId, onFocusRestored]);

  useEffect(() => {
    // Hold the timer while the toast is hovered or focused, so a slow or keyboard
    // user is not cut off mid-reach; it restarts once they leave.
    if (!undo || undoHold) return undefined;
    const timer = window.setTimeout(() => setUndo(null), 8000);
    return () => window.clearTimeout(timer);
  }, [undo, undoHold]);

  return (
    <>
      {enqueue.isError && <ErrorState error={enqueue.error} />}
      {resetQueue.isError && <ErrorState error={resetQueue.error} />}
      {rescreen.error && (
        <p className="field-error" role="alert">
          {rescreen.error.message}
        </p>
      )}
      <RescreenStatus task={rescreen.task} finished={rescreen.finished} />
      {trackingOutcomes.size > 0 && (
        <section aria-label="Tracking outcomes">
          <ul>
            {[...trackingOutcomes].map(([id, outcome]) => (
              <li key={id}>
                <p
                  className={outcome.error ? "field-error" : undefined}
                  role={outcome.error ? "alert" : "status"}
                >
                  {outcome.error
                    ? `Not tracked: ${rowLabel(outcome.item)}. ${outcome.error}`
                    : `Tracked ${rowLabel(outcome.item)}.`}
                </p>
              </li>
            ))}
          </ul>
        </section>
      )}
      <div className="discovery-toolbar">
        {/* Mounted even with no rows, so an empty, loading, or failed read still
            offers Rows per page as the way back from a page size that cannot load. */}
        <PendingDiscoveryFilters
          countPending={countPending}
          employers={employers}
          facets={facets}
          filters={filters}
          // The attention callout carries its own lane's window whenever it shows.
          laneStatus={
            laneStatus &&
            (attentionTotal > 0 || !laneStatus.attention
              ? laneStatus.ready
              : `${laneStatus.ready} ${laneStatus.attention}`)
          }
          matching={matching}
          onChange={changeFilters}
          onPageSizeChange={onPageSizeChange}
          onSearchTextChange={(value) => {
            onSearchTextChange(value);
            resetSelectionSurface();
          }}
          onSortChange={setSortMode}
          onStrictUncertaintyChange={(value) => {
            onStrictUncertaintyChange(value);
            resetSelectionSurface();
          }}
          pageSize={pageSize}
          queueTotal={queueTotal}
          searchText={searchText}
          sortMode={sortMode}
          strictUncertainty={strictUncertainty}
        />
        <div className="discovery-toolbar__actions">
          <PendingDiscoveryIntake onAdded={revealAddedDiscovery} workspaceId={workspaceId} />
          <details className="menu-disclosure">
            <summary className="button button--quiet">Queue maintenance</summary>
            <div className="menu-disclosure__panel discovery-maintenance">
              <button
                className="button button--quiet"
                disabled={rescreen.running}
                onClick={() => void requestQueueRescreen()}
                type="button"
              >
                {rescreen.running ? "Re-screening queue" : "Re-screen queue"}
              </button>
              <button
                className="button button--danger"
                disabled={resetQueue.isPending}
                onClick={() => void requestQueueReset()}
                type="button"
              >
                Reset pending queue
              </button>
            </div>
          </details>
        </div>
      </div>
      {loaded && queueTotal > 0 ? (
        <>
          {attentionTotal > 0 && (
            <div
              aria-label="Postings needing attention"
              className="discovery-attention"
              role="region"
            >
              <strong>
                {attentionTotal} of {readinessTotal} matching in any content state need attention
              </strong>
              <div className="discovery-attention__routes">
                {attentionEntries.map((entry) => (
                  <button
                    className="button button--quiet discovery-attention__route"
                    key={entry.readiness}
                    onClick={() => changeFilters({ ...filters, readiness: entry.readiness })}
                    type="button"
                  >
                    {CONTENT_READINESS_LABELS[entry.readiness]} ({entry.count})
                  </button>
                ))}
              </div>
              {laneStatus?.attention ? (
                <span className="field-note">{laneStatus.attention}</span>
              ) : null}
            </div>
          )}
          <div
            aria-label="Selected discovery actions"
            className="discovery-batch-bar"
            ref={trackBatchBarHeight}
            role="region"
          >
            <span className="discovery-batch-bar__count">
              <strong>{selectedCount} selected</strong>.{" "}
              <span
                className={`field-note${selectAllBlockedBecause ? " field-note--blocked" : ""}`}
                id={SELECTION_BOUND_ID}
                role={selectAllBlockedBecause ? "status" : undefined}
              >
                {selectAllBlockedBecause || `Select up to ${MAX_SELECTION_SIZE}.`}
              </span>
            </span>
            {selectedCount > 0 && (
              <>
                <button
                  aria-busy={tracking || undefined}
                  aria-disabled={tracking || !settled}
                  className="button button--quiet"
                  onClick={() => void trackSelected()}
                  type="button"
                >
                  {tracking ? "Tracking selected jobs" : `Track selected (${selectedCount})`}
                </button>
                <PublisherFactsRefresh
                  pendingDiscoveryIds={selectedList}
                  workspaceId={workspaceId}
                />
                <PaidAction
                  confirmation={{
                    title: `Review relevance of ${selectedCount} ${selectedCount === 1 ? "discovery" : "discoveries"}?`,
                    description: `Reviews all ${selectedCount} against your CV and search parameters, including rows already reviewed. These are the ${selectedCount} rows you selected, including any selected on other pages. No full evaluations. ${relevanceReviewConfiguration.description} ${costs.confirmation("discovery.relevance_review", reviewCallCount)}`,
                    confirmLabel: "Review selected relevance",
                  }}
                  costs={costs}
                  disabled={
                    !reviewSelectionAllowed ||
                    enqueue.isPending ||
                    !relevanceReviewConfiguration.loaded ||
                    relevanceReviewConfiguration.isError
                  }
                  count={reviewCallCount}
                  label={`Review relevance (${selectedCount})`}
                  onAct={() => {
                    void enqueue
                      .mutateAsync({
                        workspace_id: workspaceId,
                        task_type: "discovery.relevance_review",
                        payload: { pending_discovery_ids: selectedList },
                      })
                      .then(clearSelection)
                      .catch(() => undefined);
                  }}
                  taskType="discovery.relevance_review"
                />
                <PaidAction
                  confirmation={{
                    title: `Fully evaluate ${selectedCount} ${selectedCount === 1 ? "discovery" : "discoveries"}?`,
                    description: `One task evaluates all ${selectedCount} selected discoveries with advisory cost estimates. These are the ${selectedCount} rows you selected, including any selected on other pages.`,
                    confirmLabel: `Run ${selectedCount} full ${selectedCount === 1 ? "evaluation" : "evaluations"}`,
                  }}
                  costs={costs}
                  evaluation={{ workspaceId, selection: { pending_discovery_ids: selectedList } }}
                  count={selectedCount}
                  disabled={!evaluationSelectionAllowed || enqueue.isPending || !costs.loaded}
                  label={`Full evaluation (${selectedCount})`}
                  onAct={() => void evaluate(selectedList)}
                  taskType="discovery.evaluate_batch"
                />
                {screeningRefusedCount > 0 && (
                  <span className="field-note">
                    {`${screeningRefusedCount} selected ${screeningRefusedCount === 1 ? "row conflicts" : "rows conflict"} with your search rules; review your rules before either paid action.`}
                  </span>
                )}
                {refreshBlockedCount > 0 && (
                  <span className="field-note">
                    {`${refreshBlockedCount} selected ${refreshBlockedCount === 1 ? "row needs" : "rows need"} a refresh before either paid action.`}
                  </span>
                )}
                {relevanceReviewConfiguration.isError ? (
                  <span className="field-error" role="alert">
                    {relevanceReviewConfiguration.heldBecause === "undescribable"
                      ? "Relevance review answered, but its provider and model could not be read, so paid review is held until they can be shown. Review the provider settings before reviewing relevance."
                      : "Relevance-review provider configuration is unavailable. Reload this page or review the provider settings before reviewing relevance."}
                  </span>
                ) : !relevanceReviewConfiguration.loaded ? (
                  <span className="field-note" role="status">
                    Reading the relevance-review provider and model before payment.
                  </span>
                ) : null}
                {recoverableContentIds.length > 0 && (
                  <AssistedRecoveryAction
                    pendingDiscoveryIds={recoverableContentIds}
                    workspaceId={workspaceId}
                  />
                )}
                <SavedClosureAction
                  pendingDiscoveryIds={savedClosureIds}
                  workspaceId={workspaceId}
                />
                <div className="discovery-batch-bar__end">
                  <DismissSelection
                    key={selectedList.join("\u0000")}
                    label={`Dismiss selected (${selectedCount})`}
                    neverReadIds={neverReadDiscoveryIds(discoveries)}
                    onSkippedMarkedChange={setBulkSkippedMarked}
                    pendingDiscoveryIds={selectedList}
                    workspaceId={workspaceId}
                  />
                  <button className="button button--quiet" onClick={clearSelection} type="button">
                    Clear
                  </button>
                </div>
              </>
            )}
          </div>
          <p aria-live="polite" className="pending-kbd-status" role="status">
            {announce}
          </p>
          <SkippedMarkedNote count={bulkSkippedMarked} />
          {undo && (
            <div
              className="pending-undo"
              onBlur={(event) => {
                if (!event.currentTarget.contains(event.relatedTarget)) setUndoHold(false);
              }}
              onFocus={() => setUndoHold(true)}
              onMouseEnter={() => setUndoHold(true)}
              onMouseLeave={() => setUndoHold(false)}
            >
              <span>Dismissed {undo.label}.</span>
              <button
                className="button button--quiet"
                onClick={() => void undoDismiss()}
                type="button"
              >
                Undo
              </button>
              <button
                aria-label="Dismiss undo notification"
                className="button button--quiet"
                onClick={() => setUndo(null)}
                type="button"
              >
                Close
              </button>
            </div>
          )}
          <div
            aria-label="Pending queue keyboard triage: press j and k to move the cursor, e to evaluate, r to review, x to dismiss the focused row, u to undo."
            className="table-wrap pending-queue-keyboard"
            role="group"
            {...keyboard.containerProps}
          >
            {filters.constraint === "needs_clarification" ? <h3>Unconfirmed matches</h3> : null}
            {filters.constraint === "excluded_only" ? <h3>Screened out</h3> : null}
            <table aria-label="Pending job discoveries">
              <thead>
                <tr>
                  <th scope="col">
                    <input
                      aria-describedby={selectAllBlockedBecause ? SELECTION_BOUND_ID : undefined}
                      aria-label={selectAllLabel}
                      checked={allVisibleSelected}
                      disabled={
                        countPending ||
                        selectableVisibleIds.length === 0 ||
                        Boolean(selectAllBlockedBecause)
                      }
                      onChange={() => {
                        if (countPending) return;
                        const selectableItems = selectableVisibleIds.flatMap((id) => {
                          const item = itemById.get(id);
                          return item ? [item] : [];
                        });
                        setSelection((current) => {
                          const next = new Map(
                            current.scope === selectionScope ? current.entries : undefined,
                          );
                          if (selectableItems.every((item) => next.has(item.id))) {
                            for (const item of selectableItems) {
                              next.delete(item.id);
                            }
                          } else {
                            for (const item of selectableItems) {
                              next.set(item.id, selectionEntry(item));
                            }
                          }
                          return { entries: next, scope: selectionScope };
                        });
                      }}
                      ref={selectAllRef}
                      type="checkbox"
                    />
                  </th>
                  <th scope="col">Employer</th>
                  <th scope="col">Role</th>
                  <th scope="col">Place</th>
                  <th scope="col">Relevance review</th>
                  <th scope="col">Action</th>
                </tr>
              </thead>
              <tbody>
                {ordered.map((item, index) => {
                  const evaluationTask = evaluationTasks.get(item.id);
                  const isInFlight = evaluationInFlight(item.id);
                  return (
                    <PendingDiscoveryRow
                      costs={costs}
                      keyboardFocused={index === keyboard.activeIndex}
                      detailsOpen={detailsRowId === item.id}
                      dismissalOpen={dismissalRowId === item.id}
                      dismissalPending={dismissalPendingId === item.id}
                      enqueuePending={enqueue.isPending}
                      evaluationTask={evaluationTask}
                      isInFlight={isInFlight}
                      isSelected={selectedIds.has(item.id)}
                      item={item}
                      key={item.id}
                      openListing={openListing?.id === item.id ? openListing.kind : null}
                      onDetailsToggle={toggleDetails}
                      onDismissalClose={closeDismissal}
                      onDismissalPendingChange={changeDismissalPending}
                      onDismissalToggle={toggleDismissal}
                      onEvaluate={evaluateRow}
                      onListingToggle={toggleListing}
                      onSelect={toggleRowSelection}
                      registerCheckbox={registerCheckbox}
                      registerDismissButton={registerDismissButton}
                      relevanceReview={storedRelevanceReview(item)}
                      workspaceId={workspaceId}
                    />
                  );
                })}
                {ordered.length === 0 && (
                  <tr>
                    <td colSpan={6}>
                      No discoveries match these filters.
                      {emptyViewHint && ` ${emptyViewHint}`}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      ) : loaded ? (
        <EmptyState>
          <p>No openings are waiting. Scan your saved boards to find new postings.</p>
          <a className="button button--primary" href="?tab=boards">
            Scan your boards
          </a>
        </EmptyState>
      ) : null}
      {loaded && (
        <DiscoveryDispositionManager
          discoveries={discoveries}
          // Held-over rows belong to a view the person has left, so no bulk action takes them.
          visibleDiscoveries={settled ? discoveries : []}
          workspaceId={workspaceId}
        />
      )}
    </>
  );
}
