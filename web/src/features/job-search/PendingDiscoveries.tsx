import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";

import type { PendingDiscoveryChanges } from "../../api/client";
import {
  usePendingDiscoveries,
  usePendingDiscoveryChanges,
  usePendingDiscoveryPage,
} from "../../api/hooks";
import { queryKeys } from "../../api/hooks/keys";
import { ApiError } from "../../api/http";
import { postingAddress } from "../../components/aggregatorHosts";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { useDebouncedValue } from "../../hooks/useDebouncedValue";
import { DiscoveryTable } from "./DiscoveryTable";
import {
  DEFAULT_QUEUE_FILTERS,
  pendingQueueQuery,
  type PendingQueueFilters,
  type QueuePageSize,
} from "./pendingDiscoveryFilterModel";
import { relevanceReviewsByPending } from "./relevanceReviewModel";
import { PendingDiscoveryQueue } from "./PendingDiscoveryQueue";
import { RefusalPatternProposals } from "./RefusalPatternProposals";

// Default page size, and the bound on the closed-posting history below.
export const PENDING_DISCOVERIES_LIMIT = 50;
// Mirrors the page route's own ceiling, so "All" asks for the whole lane and
// mounts every row's remediation controls at once.
const PENDING_DISCOVERIES_ALL_LIMIT = 2000;
const PAGE_SIZE_KEY = "clearscout.pendingPageSize";
// A link to /find?tab=review&constraint=excluded_only opens the queue on Screened out.
const SCREENED_OUT_PARAM = "constraint";
const STRICT_UNCERTAINTY_KEY = "clearscout.pendingStrictUncertainty.";

function readStrictUncertainty(workspaceId: string): boolean {
  try {
    return window.localStorage.getItem(`${STRICT_UNCERTAINTY_KEY}${workspaceId}`) === "true";
  } catch {
    return false;
  }
}

function pageLimit(pageSize: QueuePageSize): number {
  return pageSize === "all" ? PENDING_DISCOVERIES_ALL_LIMIT : Number(pageSize);
}

function readPageSize(): QueuePageSize {
  try {
    const stored = window.localStorage.getItem(PAGE_SIZE_KEY);
    if (stored === "50" || stored === "100" || stored === "all") return stored;
  } catch {
    // A blocked localStorage still loads the default page.
  }
  return "50";
}

function laneRange(offset: number, itemCount: number, total: number) {
  if (!total) return "no rows";
  // A lane past the requested limit is never called whole, whatever was asked for.
  if (offset === 0 && itemCount < total && itemCount >= PENDING_DISCOVERIES_ALL_LIMIT) {
    return `first ${itemCount} of ${total} matching`;
  }
  return `rows ${offset + 1}-${offset + itemCount} of ${total} matching`;
}

function pluralCount(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

function changesBannerText(changes: PendingDiscoveryChanges): string {
  const parts: string[] = [];
  const followUp = changes.follow_up;
  if (followUp.active_sources > 0) {
    parts.push(
      `Reading ${pluralCount(followUp.active_sources, "source")}; ` +
        `${pluralCount(followUp.frontier_completed_count, "publisher link")} read; ` +
        `${pluralCount(followUp.automatic_pending_count, "known link")} queued. ` +
        "Final remaining total unknown.",
    );
  }
  if (changes.newer_visible.ready > 0 || changes.newer_visible.attention > 0) {
    parts.push(
      `New since this list loaded: ${changes.newer_visible.ready} ready, ` +
        `${changes.newer_visible.attention} need attention.`,
    );
  }
  return parts.join(" ");
}

export function PendingDiscoveries({ workspaceId }: { workspaceId: string }) {
  return <PendingDiscoveryWorkspace key={workspaceId} workspaceId={workspaceId} />;
}

function PendingDiscoveryWorkspace({ workspaceId }: { workspaceId: string }) {
  const queryClient = useQueryClient();
  const [historyRequested, setHistoryRequested] = useState(false);
  const [readyOffset, setReadyOffset] = useState(0);
  const [attentionOffset, setAttentionOffset] = useState(0);
  const [snapshotSha256, setSnapshotSha256] = useState("");
  const [snapshotBefore, setSnapshotBefore] = useState("");
  const [pagingNotice, setPagingNotice] = useState("");
  const [pageSize, setPageSize] = useState<QueuePageSize>(readPageSize);
  const [strictUncertainty, setStrictUncertainty] = useState(() =>
    readStrictUncertainty(workspaceId),
  );
  const [searchParams, setSearchParams] = useSearchParams();
  const screenedOutLinked = searchParams.get(SCREENED_OUT_PARAM) === "excluded_only";
  const [filters, setFilters] = useState<PendingQueueFilters>(() => ({
    ...DEFAULT_QUEUE_FILTERS,
    constraint: screenedOutLinked
      ? "excluded_only"
      : strictUncertainty
        ? "pursue_only"
        : DEFAULT_QUEUE_FILTERS.constraint,
  }));
  const [searchText, setSearchText] = useState("");
  const debouncedSearch = useDebouncedValue(searchText, 250);
  const query = useMemo(() => pendingQueueQuery(filters), [filters]);
  const limit = pageLimit(pageSize);
  const paging = readyOffset > 0 || attentionOffset > 0;
  const expectedSnapshotSha256 = paging ? snapshotSha256 : "";
  const discoveryPage = usePendingDiscoveryPage(
    workspaceId,
    limit,
    readyOffset,
    attentionOffset,
    expectedSnapshotSha256,
    snapshotBefore,
    query,
  );
  const receivedPage = discoveryPage.data;
  const keptFromPreviousRead = discoveryPage.isPlaceholderData;
  const snapshotConflict =
    paging && discoveryPage.error instanceof ApiError && discoveryPage.error.status === 409;
  const orderDrift = Boolean(
    expectedSnapshotSha256 &&
    receivedPage &&
    receivedPage.snapshot_sha256 !== expectedSnapshotSha256,
  );
  // A read that has already failed an attempt is still pending while it retries,
  // and its held-over rows would be rows nobody just read.
  const heldOverAfterFailure = keptFromPreviousRead && discoveryPage.failureCount > 0;
  const page = orderDrift || heldOverAfterFailure ? undefined : receivedPage;
  // Paging and the snapshot pin also hold the previous page, but only a filter change alters its counts.
  const [pageQuery, setPageQuery] = useState(query);
  if (receivedPage && !keptFromPreviousRead && pageQuery !== query) setPageQuery(query);
  const countPending = pageQuery !== query;
  const discoveryHistory = usePendingDiscoveries(
    workspaceId,
    PENDING_DISCOVERIES_LIMIT,
    true,
    historyRequested,
  );
  const discoveries = useMemo(
    () => [...(page?.ready.items ?? []), ...(page?.attention.items ?? [])],
    [page],
  );
  const laneStatus = page
    ? {
        ready: `Ready to evaluate: ${laneRange(page.ready.offset, page.ready.items.length, page.ready.total)}.`,
        // A readiness filter can empty this lane while the callout still counts the whole queue.
        attention:
          page.attention.total > 0
            ? `Needs attention: ${laneRange(page.attention.offset, page.attention.items.length, page.attention.total)}.`
            : null,
      }
    : null;
  const reviewsByPending = relevanceReviewsByPending(
    discoveries.flatMap((item) => (item.relevance_review ? [item.relevance_review] : [])),
  );
  // A favorite or kept closed posting stays in the queue, so it is not history yet.
  const closedDiscoveries = (discoveryHistory.data ?? []).filter(
    (discovery) => !discovery.favorite && !discovery.kept,
  );
  const [pendingFocus, setPendingFocus] = useState<string | null>(null);
  const clearPendingFocus = useCallback(() => setPendingFocus(null), []);
  const restartPaging = useCallback((notice = "") => {
    setReadyOffset(0);
    setAttentionOffset(0);
    setSnapshotSha256("");
    setSnapshotBefore("");
    setPagingNotice(notice);
  }, []);
  // A restart reads the first page unpinned and then pinned to its before mark,
  // so every cached first page must go or the old rows come back without a request.
  const removeFirstPage = useCallback(
    () =>
      queryClient.removeQueries({
        queryKey: queryKeys.pendingQueuePrefix(workspaceId),
        predicate: ({ queryKey }) =>
          queryKey[2] === "page" && queryKey[4] === 0 && queryKey[5] === 0,
      }),
    [queryClient, workspaceId],
  );
  const changePageSize = useCallback(
    (value: QueuePageSize) => {
      setPageSize(value);
      removeFirstPage();
      restartPaging();
      try {
        window.localStorage.setItem(PAGE_SIZE_KEY, value);
      } catch {
        // A blocked localStorage still applies the size for this session.
      }
    },
    [removeFirstPage, restartPaging],
  );
  // Filters join the page key and narrow the set before paging, so a change reads from the top.
  const changeFilters = useCallback(
    (next: PendingQueueFilters) => {
      setFilters(next);
      removeFirstPage();
      restartPaging();
      const screenedOut = next.constraint === "excluded_only";
      setSearchParams(
        (current) => {
          if ((current.get(SCREENED_OUT_PARAM) === "excluded_only") === screenedOut) return current;
          const params = new URLSearchParams(current);
          if (screenedOut) params.set(SCREENED_OUT_PARAM, "excluded_only");
          else params.delete(SCREENED_OUT_PARAM);
          return params;
        },
        { replace: true },
      );
    },
    [removeFirstPage, restartPaging, setSearchParams],
  );
  // Following a Screened out link while the queue is open switches the filter.
  const [linkedSeen, setLinkedSeen] = useState(screenedOutLinked);
  if (linkedSeen !== screenedOutLinked) {
    setLinkedSeen(screenedOutLinked);
    if (screenedOutLinked !== (filters.constraint === "excluded_only")) {
      setFilters({
        ...filters,
        constraint: screenedOutLinked
          ? "excluded_only"
          : strictUncertainty
            ? "pursue_only"
            : DEFAULT_QUEUE_FILTERS.constraint,
      });
      removeFirstPage();
      restartPaging();
    }
  }
  const changeStrictUncertainty = useCallback(
    (value: boolean) => {
      setStrictUncertainty(value);
      changeFilters({ ...filters, constraint: value ? "pursue_only" : "not_excluded" });
      try {
        window.localStorage.setItem(`${STRICT_UNCERTAINTY_KEY}${workspaceId}`, String(value));
      } catch {
        // Blocked storage still permits changing the current workspace view.
      }
    },
    [changeFilters, filters, workspaceId],
  );
  const changeSearchText = useCallback(
    (value: string) => {
      setSearchText(value);
      // A cleared box applies at once, so a reset never waits on the typing pause.
      if (!value.trim() && filters.search) changeFilters({ ...filters, search: "" });
    },
    [changeFilters, filters],
  );
  useEffect(() => {
    // Only a settled value commits, so a reset while typing is not undone by the pause ending.
    if (debouncedSearch !== searchText || debouncedSearch === filters.search) return undefined;
    const timer = window.setTimeout(
      () => changeFilters({ ...filters, search: debouncedSearch }),
      0,
    );
    return () => window.clearTimeout(timer);
  }, [changeFilters, debouncedSearch, filters, searchText]);
  const panelHeadingRef = useRef<HTMLHeadingElement>(null);
  // Only the Refresh-list notice should clear itself once a fresh snapshot lands;
  // the snapshot-conflict notice must stay visible, so track which one is pending.
  const noticeClearsOnSnapshot = useRef(false);
  const changes = usePendingDiscoveryChanges(workspaceId, snapshotBefore, query);
  const changesData = changes.data;
  const followUpActive = Boolean(changesData && changesData.follow_up.active_sources > 0);
  const hasNewRows = Boolean(
    changesData && (changesData.newer_visible.ready > 0 || changesData.newer_visible.attention > 0),
  );
  const showChangesBanner = !pagingNotice && (followUpActive || hasNewRows);
  const [changesAnnouncement, setChangesAnnouncement] = useState("");
  const previousFollowUpActive = useRef<boolean | null>(null);
  const previousHasNewRows = useRef(false);
  useEffect(() => {
    if (!changesData) return;
    if (previousFollowUpActive.current === null) {
      previousFollowUpActive.current = followUpActive;
      previousHasNewRows.current = hasNewRows;
      return;
    }
    if (followUpActive !== previousFollowUpActive.current) {
      setChangesAnnouncement(
        followUpActive
          ? "Publisher follow-up started reading more sources."
          : "Publisher follow-up finished.",
      );
    } else if (hasNewRows && !previousHasNewRows.current) {
      setChangesAnnouncement("New pending discoveries are available.");
    }
    previousFollowUpActive.current = followUpActive;
    previousHasNewRows.current = hasNewRows;
  }, [changesData, followUpActive, hasNewRows]);
  const refreshFromChanges = useCallback(() => {
    removeFirstPage();
    noticeClearsOnSnapshot.current = true;
    restartPaging("The list was refreshed from the beginning, so the order may have changed.");
    panelHeadingRef.current?.focus();
  }, [removeFirstPage, restartPaging]);

  useEffect(() => {
    if (
      readyOffset > 0 ||
      attentionOffset > 0 ||
      !receivedPage?.snapshot_sha256 ||
      snapshotBefore ||
      // The held-over page belongs to the previous read, so its marks are stale.
      keptFromPreviousRead
    ) {
      return undefined;
    }
    const timer = window.setTimeout(() => {
      setSnapshotSha256(receivedPage.snapshot_sha256);
      setSnapshotBefore(receivedPage.snapshot_before);
      if (noticeClearsOnSnapshot.current) {
        noticeClearsOnSnapshot.current = false;
        setPagingNotice("");
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, [attentionOffset, keptFromPreviousRead, readyOffset, receivedPage, snapshotBefore]);

  useEffect(() => {
    if (!snapshotConflict && !orderDrift) return undefined;
    const timer = window.setTimeout(() => {
      removeFirstPage();
      restartPaging(
        "The queue order changed, so paging restarted from the beginning and your selection was cleared.",
      );
    }, 0);
    return () => window.clearTimeout(timer);
  }, [orderDrift, removeFirstPage, restartPaging, snapshotConflict]);

  useEffect(() => {
    if (!page || keptFromPreviousRead) return undefined;
    const lastPageOffset = (total: number) =>
      total > 0 ? Math.floor((total - 1) / limit) * limit : 0;
    const nextReadyOffset =
      readyOffset >= page.ready.total && readyOffset > 0
        ? lastPageOffset(page.ready.total)
        : readyOffset;
    const nextAttentionOffset =
      attentionOffset >= page.attention.total && attentionOffset > 0
        ? lastPageOffset(page.attention.total)
        : attentionOffset;
    if (nextReadyOffset === readyOffset && nextAttentionOffset === attentionOffset)
      return undefined;
    const timer = window.setTimeout(() => {
      setReadyOffset(nextReadyOffset);
      setAttentionOffset(nextAttentionOffset);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [attentionOffset, keptFromPreviousRead, limit, page, readyOffset]);

  return (
    <article className="panel pending-discoveries" data-tour="find-review">
      <header>
        <div>
          <h2 ref={panelHeadingRef} tabIndex={-1}>
            Pending discoveries
          </h2>
        </div>
        {showChangesBanner && changesData ? (
          <p className="field-note discovery-changes">
            {changesBannerText(changesData)}{" "}
            <button className="button button--primary" onClick={refreshFromChanges} type="button">
              Refresh list
            </button>
          </p>
        ) : null}
      </header>
      <div className="panel-body">
        <span aria-live="polite" className="visually-hidden" role="status">
          {changesAnnouncement}
        </span>
        {discoveryPage.isLoading && <LoadingState label="Reading discoveries" />}
        {discoveryPage.isError && !snapshotConflict && <ErrorState error={discoveryPage.error} />}
        {pagingNotice ? (
          <p className="field-note" role="status">
            {pagingNotice}
          </p>
        ) : null}
        <PendingDiscoveryQueue
          countPending={countPending}
          discoveries={discoveries}
          employers={page?.employers ?? []}
          facets={page?.facets}
          filters={filters}
          focusPendingId={pendingFocus}
          laneStatus={laneStatus}
          matching={page ? page.ready.total + page.attention.total : 0}
          relevanceReviews={reviewsByPending}
          loaded={page !== undefined}
          pageSize={pageSize}
          selectionScope={JSON.stringify([
            workspaceId,
            query,
            page?.snapshot_sha256 || snapshotSha256,
          ])}
          settled={!keptFromPreviousRead}
          strictUncertainty={strictUncertainty}
          onAdded={(pendingId) => {
            restartPaging();
            setPendingFocus(pendingId);
          }}
          onFiltersChange={changeFilters}
          onFocusRestored={clearPendingFocus}
          onPageSizeChange={changePageSize}
          onSearchTextChange={changeSearchText}
          onStrictUncertaintyChange={changeStrictUncertainty}
          queueTotal={page?.queue_total ?? 0}
          searchText={searchText}
          workspaceId={workspaceId}
        />
        {page && (
          <nav aria-label="Pending discovery pages" className="button-row">
            <button
              className="button button--quiet"
              disabled={readyOffset === 0}
              onClick={() => {
                const nextOffset = Math.max(0, readyOffset - limit);
                setReadyOffset(nextOffset);
                setPagingNotice("");
                if (nextOffset === 0 && attentionOffset === 0) restartPaging();
              }}
              type="button"
            >
              Previous ready page
            </button>
            <button
              className="button button--quiet"
              disabled={!page.ready.has_more || !snapshotSha256}
              onClick={() => {
                setPagingNotice("");
                setReadyOffset(readyOffset + limit);
              }}
              type="button"
            >
              Next ready page
            </button>
            <button
              className="button button--quiet"
              disabled={attentionOffset === 0}
              onClick={() => {
                const nextOffset = Math.max(0, attentionOffset - limit);
                setAttentionOffset(nextOffset);
                setPagingNotice("");
                if (nextOffset === 0 && readyOffset === 0) restartPaging();
              }}
              type="button"
            >
              Previous attention page
            </button>
            <button
              className="button button--quiet"
              disabled={!page.attention.has_more || !snapshotSha256}
              onClick={() => {
                setPagingNotice("");
                setAttentionOffset(attentionOffset + limit);
              }}
              type="button"
            >
              Next attention page
            </button>
          </nav>
        )}
        {filters.constraint === "excluded_only" ? (
          <div className="callout">
            <RefusalPatternProposals workspaceId={workspaceId} />
          </div>
        ) : null}
        <details
          className="field-note"
          onToggle={(event) => {
            if (event.currentTarget.open) setHistoryRequested(true);
          }}
        >
          <summary>
            Closed posting history
            {discoveryHistory.data ? ` (${closedDiscoveries.length})` : ""}
          </summary>
          {historyRequested ? (
            <>
              {discoveryHistory.data?.length === PENDING_DISCOVERIES_LIMIT ? (
                <p className="field-note" role="status">
                  Showing at most {PENDING_DISCOVERIES_LIMIT} recent records. Older closed postings
                  may not be shown.
                </p>
              ) : null}
              {discoveryHistory.isLoading ? <LoadingState label="Reading closed postings" /> : null}
              {discoveryHistory.isError ? <ErrorState error={discoveryHistory.error} /> : null}
              {discoveryHistory.data && closedDiscoveries.length === 0 ? (
                <EmptyState>No closed postings have been recorded.</EmptyState>
              ) : null}
              {closedDiscoveries.length > 0 ? (
                <div className="table-wrap">
                  <DiscoveryTable
                    discoveries={closedDiscoveries.map((discovery) => ({
                      employer: discovery.employer,
                      location: discovery.location,
                      note: discovery.content_note || "Closure recorded",
                      title: discovery.title,
                      url: postingAddress(discovery),
                      creditUrl: discovery.discovery_url,
                    }))}
                  />
                </div>
              ) : null}
            </>
          ) : null}
        </details>
      </div>
    </article>
  );
}
