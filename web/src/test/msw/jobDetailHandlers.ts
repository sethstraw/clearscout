import { HttpResponse, http, type HttpHandler } from "msw";

import type {
  FitReport,
  JobDetail,
  SearchParametersState,
  Task,
  TaskBilling,
} from "../../api/client/types";
import { TASK_TYPE_CATALOG } from "../taskTypes";
import { jobDetail } from "../../features/job-detail/jobDetailFixtures";

const API = "*/api/v1";

/** One saved place, so an eligibility reading has a workspace location to name. */
const SAVED_PARAMETERS = {
  workspace_id: "workspace-jobs",
  id: "parameters-1",
  version: 1,
  configured: true,
  content_sha256: "a".repeat(64),
  effective_at: "2026-08-01T12:00:00Z",
  parameters: {
    opportunity_thesis: "Synthetic health-data product leadership.",
    score_weight_set: "ClearScout default",
    working_day_shift_hours: 0,
    locations: [
      {
        key: "canada_remote",
        country: "Canada",
        region: "Ontario",
        metro: "",
        eligibility: "authorized",
        basis: "Stated at setup",
        aliases: [],
        arrangement: null,
        resident: false,
      },
    ],
  },
} satisfies SearchParametersState;

export const NO_SAVED_PLACES = {
  ...SAVED_PARAMETERS,
  parameters: {
    opportunity_thesis: "Synthetic health-data product leadership.",
    score_weight_set: "ClearScout default",
    working_day_shift_hours: 0,
    locations: [],
  },
} satisfies SearchParametersState;

export interface JobDetailReads {
  detail?: JobDetail | (() => JobDetail);
  onDetailRequest?: (url: URL) => Response | undefined;
  parameters?: SearchParametersState;
  task?: Task | (() => Promise<Task>);
  billing?: TaskBilling | (() => Promise<TaskBilling>);
  onBillingRequest?: () => Response | undefined;
  evidenceSource?: unknown;
  fitReport?: FitReport;
  notes?: unknown[];
  onRequest?: (url: URL) => void;
  onEnqueue?: (request: Request) => Promise<void>;
  enqueued?: Task;
  onEnqueueRefused?: () => Response | undefined;
  onApplicationEvent?: (request: Request) => Promise<void>;
}

/** Every read the job page makes on mount, plus the writes its actions send. */
export function jobDetailHandlers(reads: JobDetailReads = {}): HttpHandler[] {
  const seen = ({ request }: { request: Request }) => reads.onRequest?.(new URL(request.url));
  return [
    http.post(`${API}/jobs/:jobId/application-events`, async ({ request }) => {
      await reads.onApplicationEvent?.(request.clone());
      return HttpResponse.json({ id: "event-2", status: "applied" });
    }),
    http.post(`${API}/tasks`, async ({ request }) => {
      await reads.onEnqueue?.(request.clone());
      const refused = reads.onEnqueueRefused?.();
      return refused ?? HttpResponse.json(reads.enqueued ?? { id: "task-42", status: "queued" });
    }),
    http.get(`${API}/tasks/types`, () => HttpResponse.json(TASK_TYPE_CATALOG)),
    http.get(`${API}/tasks/:taskId/billing`, async (info) => {
      seen(info);
      const refused = reads.onBillingRequest?.();
      if (refused) return refused;
      const billing = typeof reads.billing === "function" ? await reads.billing() : reads.billing;
      return HttpResponse.json(billing ?? null);
    }),
    http.get(`${API}/tasks/:taskId`, async (info) => {
      seen(info);
      const task = typeof reads.task === "function" ? await reads.task() : reads.task;
      return HttpResponse.json(task ?? null);
    }),
    http.get(`${API}/tasks`, () => HttpResponse.json([])),
    http.get(`${API}/evidence/:evidenceId/source`, (info) => {
      seen(info);
      return HttpResponse.json(reads.evidenceSource ?? null);
    }),
    http.get(`${API}/job-search-parameters`, () =>
      HttpResponse.json(reads.parameters ?? SAVED_PARAMETERS),
    ),
    http.get(`${API}/jobs/:jobId/fit-report`, () =>
      reads.fitReport
        ? HttpResponse.json(reads.fitReport)
        : HttpResponse.json({ detail: "No fit report is stored." }, { status: 404 }),
    ),
    http.get(`${API}/jobs/:jobId`, (info) => {
      seen(info);
      const answered = reads.onDetailRequest?.(new URL(info.request.url));
      if (answered) return answered;
      const body = typeof reads.detail === "function" ? reads.detail() : reads.detail;
      return HttpResponse.json(body ?? jobDetail());
    }),
    http.get(`${API}/jobs`, () => HttpResponse.json({ items: [], total: 0 })),
    http.get(`${API}/notes`, () => HttpResponse.json(reads.notes ?? [])),
    http.get(`${API}/tags`, () => HttpResponse.json([])),
    http.get(`${API}/workspaces/:workspaceId/spend-history`, () => HttpResponse.json([])),
  ];
}
