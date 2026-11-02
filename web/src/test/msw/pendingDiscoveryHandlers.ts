import { HttpResponse, delay, http, type HttpHandler } from "msw";

import type {
  DismissedDiscovery,
  DispositionPreview,
  DispositionSelection,
  Job,
  MasterCvVersion,
  PendingDisposition,
  PendingDiscovery,
  PendingDiscoveryChanges,
  PendingDiscoveryPage,
  PendingDuplicateCandidate,
  RelevanceReviewConfiguration,
  Task,
} from "../../api/client/types";
import { jobList } from "../../features/job-ledger/jobFixtures";
import { TASK_TYPE_CATALOG } from "../taskTypes";
import {
  dispositionPreview,
  pendingDiscoveryChanges,
  queueFacets,
  queueFilterFailures,
  tally,
} from "../../features/job-search/pendingDiscoveryFixtures";

const API = "*/api/v1";
const WORKSPACE = `${API}/workspaces/:workspaceId`;

/** A whole page, or bare rows the handler splits into lanes by retained content. */
type QueuePageBody = PendingDiscoveryPage | PendingDiscovery[];

export interface PendingQueueReads {
  /** Served through the route's filters; a bare array is also split into lanes and counted. */
  page?: QueuePageBody | ((url: URL) => QueuePageBody);
  /** Answer the page route directly, for a status the queue must recover from. */
  onPageRequest?: (url: URL) => Response | undefined;
  onRequest?: (url: URL) => void;
  changes?: PendingDiscoveryChanges | (() => PendingDiscoveryChanges);
  closedHistory?: PendingDiscovery[];
  masterCvVersions?: MasterCvVersion[];
  relevanceReviewConfiguration?: RelevanceReviewConfiguration;
  tasks?: Task[];
}

const ANTHROPIC_REVIEW_CONFIGURATION = {
  provider: "anthropic",
  model_choice: { model: "claude-review", effort: null, service_tier: "standard" },
  batch_rows: 20,
} satisfies RelevanceReviewConfiguration;

/** The current master CV, so an embedded review reads as up to date. */
export const CURRENT_MASTER_CV_VERSIONS = [
  {
    id: "cv-current",
    is_current: true,
    byte_size: 1024,
    content_sha256: "c".repeat(64),
    effective_at: "2026-08-01T00:00:00Z",
    source_uri: "file:///synthetic/master-cv.md",
  },
] satisfies MasterCvVersion[];

function resolve<T>(value: T | (() => T) | undefined, fallback: T): T {
  if (value === undefined) return fallback;
  return typeof value === "function" ? (value as () => T)() : value;
}

function servedEmployers(rows: readonly PendingDiscovery[], params: URLSearchParams) {
  const counted = rows.filter((row) =>
    [...queueFilterFailures(row, params)].every((item) => item === "employer"),
  );
  return tally(counted.map((row) => row.employer)).map(({ value, count }) => ({
    name: value,
    count,
  }));
}

/** Serve a queue body as the page route would under this request's filters. */
function servedPage(body: QueuePageBody, url: URL): PendingDiscoveryPage {
  const params = url.searchParams;
  const page: PendingDiscoveryPage = Array.isArray(body)
    ? {
        snapshot_sha256: "",
        snapshot_before: "",
        queue_total: body.length,
        ready: lane(
          body.filter((item) => Boolean(item.retained_content_id)),
          url,
        ),
        attention: lane(
          body.filter((item) => !item.retained_content_id),
          url,
        ),
        facets: queueFacets(body, params),
        employers: servedEmployers(body, params),
      }
    : body;
  const narrowed = (current: PendingDiscoveryPage["ready"]) => {
    const items = current.items.filter((row) => queueFilterFailures(row, params).size === 0);
    return { ...current, items, total: current.total - (current.items.length - items.length) };
  };
  return { ...page, ready: narrowed(page.ready), attention: narrowed(page.attention) };
}

function lane(items: PendingDiscovery[], url: URL): PendingDiscoveryPage["ready"] {
  const limit = Number(url.searchParams.get("limit") ?? 50);
  return { items, total: items.length, offset: 0, limit, has_more: false };
}

/**
 * Every read the pending-discovery panel makes on mount, in one registration.
 * Register these first; a later `server.use` in the test overrides one route.
 */
export function pendingQueueHandlers(reads: PendingQueueReads = {}): HttpHandler[] {
  const seen = ({ request }: { request: Request }) => reads.onRequest?.(new URL(request.url));
  return [
    http.get(`${API}/tasks/types`, () => HttpResponse.json(TASK_TYPE_CATALOG)),
    http.get(`${API}/tasks`, () => HttpResponse.json(reads.tasks ?? [])),
    http.get(`${WORKSPACE}/spend-history`, () => HttpResponse.json([])),
    http.get(`${API}/jobs`, () => HttpResponse.json({ items: [], total: 0 })),
    http.get(`${WORKSPACE}/master-cv/versions`, () =>
      HttpResponse.json(reads.masterCvVersions ?? []),
    ),
    http.get(`${WORKSPACE}/job-search-parameters`, () =>
      HttpResponse.json({ id: "parameters-current", configured: true }),
    ),
    // The saved-parameter read is workspace-scoped by query string, not by path.
    http.get(`${API}/job-search-parameters`, () =>
      HttpResponse.json({ id: "parameters-current", configured: true }),
    ),
    http.get(`${WORKSPACE}/relevance-review-configuration`, () =>
      HttpResponse.json(reads.relevanceReviewConfiguration ?? ANTHROPIC_REVIEW_CONFIGURATION),
    ),
    http.get(`${WORKSPACE}/pending-dispositions/dismissed`, () => HttpResponse.json([])),
    http.get(`${WORKSPACE}/pending-discoveries/changes`, (info) => {
      seen(info);
      return HttpResponse.json(resolve(reads.changes, pendingDiscoveryChanges()));
    }),
    http.get(`${WORKSPACE}/pending-discoveries/page`, (info) => {
      seen(info);
      const url = new URL(info.request.url);
      const answered = reads.onPageRequest?.(url);
      if (answered) return answered;
      const body = typeof reads.page === "function" ? reads.page(url) : (reads.page ?? []);
      return HttpResponse.json(servedPage(body, url));
    }),
    http.get(`${WORKSPACE}/pending-discoveries`, (info) => {
      seen(info);
      return HttpResponse.json(reads.closedHistory ?? []);
    }),
  ];
}

export interface DispositionReads {
  dismissed?: DismissedDiscovery[];
  /** Hold the dismissed-list response open, for the loading state. */
  dismissedPending?: boolean;
  duplicateCandidates?: PendingDuplicateCandidate[];
  history?: PendingDisposition[];
  jobs?: Job[];
  /** The exact rows the preview reports for one selection. */
  targetsFor?: (selection: DispositionSelection) => DispositionPreview["targets"];
  snapshotSha256?: string;
  /** Favorite or kept rows the preview reports it left out. */
  skippedMarkedIds?: string[];
  /** Withdrawn rows a restore preview reports it left dismissed. */
  skippedWithdrawnIds?: string[];
  onPreview?: (request: Request) => Promise<void>;
  onApply?: (request: Request) => Promise<void>;
}

/** Every read and write the dismissal and restore flows make. */
export function dispositionHandlers(reads: DispositionReads = {}): HttpHandler[] {
  return [
    http.get(`${API}/jobs`, () => HttpResponse.json(jobList(reads.jobs ?? []))),
    http.get(`${WORKSPACE}/pending-dispositions/dismissed`, async () => {
      if (reads.dismissedPending) await delay("infinite");
      return HttpResponse.json(reads.dismissed ?? []);
    }),
    http.get(`${WORKSPACE}/pending-dispositions/duplicate-candidates`, () =>
      HttpResponse.json(reads.duplicateCandidates ?? []),
    ),
    http.get(`${WORKSPACE}/pending-discoveries/:pendingId/dispositions`, () =>
      HttpResponse.json(reads.history ?? []),
    ),
    http.post(`${WORKSPACE}/pending-dispositions/preview`, async ({ request }) => {
      await reads.onPreview?.(request.clone());
      const selection = (await request.json()) as DispositionSelection;
      return HttpResponse.json(
        dispositionPreview(selection, {
          skipped_marked_ids: reads.skippedMarkedIds,
          skipped_withdrawn_ids: reads.skippedWithdrawnIds,
          snapshot_sha256: reads.snapshotSha256,
          targets: reads.targetsFor?.(selection),
        }),
      );
    }),
    http.post(`${WORKSPACE}/pending-dispositions/apply`, async ({ request }) => {
      await reads.onApply?.(request.clone());
      return HttpResponse.json([]);
    }),
  ];
}
