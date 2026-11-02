import { HttpResponse, http, type HttpHandler } from "msw";

import { searchParametersState } from "../search-parameters/searchParameterFixtures";
import type {
  GuardrailVersion,
  GuardrailsSaved,
  GuardrailsState,
  Health,
  RunCostHistory,
  SearchParameterOptions,
  SearchParametersState,
  SpendSummary,
  Workspace,
} from "../../api/client/types";

const API = "*/api/v1";

const HEALTHY = {
  status: "ok",
  version: "0.1.0",
  database_reachable: true,
  schema_current: true,
  current_revision: "0001",
  head_revision: "0001",
  database_exists: true,
  worker_available: false,
  free_mode: false,
} satisfies Health;

const WORKSPACES = [
  {
    id: "workspace-settings",
    slug: "synthetic",
    name: "Synthetic Search",
    root_path: null,
    provider: "openai",
    created_at: "2026-08-01T12:00:00Z",
  },
] satisfies Workspace[];

const NO_RULES = {
  protected: [],
  required: [],
  forbidden: [],
  work_authorization: null,
  distinctions: [],
  first_person: false,
  ascii_only: true,
} satisfies GuardrailsState["rules"];

const UNCONFIGURED_GUARDRAILS = {
  workspace_id: "workspace-settings",
  version: 0,
  content_sha256: "",
  effective_at: null,
  configured: false,
  rules: NO_RULES,
} satisfies GuardrailsState;

const NO_PARAMETERS = searchParametersState({ workspace_id: "workspace-settings" });

const PARAMETER_OPTIONS = {
  arrangements: [],
  levels: [],
  lanes: [],
  bases: [],
  eligibility: [],
  employment_types: [],
  acceptance: [],
  caps: { titles: 8, archetypes: 6, capabilities: 5, domains: 5, cluster_terms: 10 },
} satisfies SearchParameterOptions;

const NO_SPEND = {
  currency: "USD",
  total_usd: 0,
  call_count: 0,
  unpriced_calls: 0,
  by_stage: [],
  by_model: [],
  recent: [],
} satisfies SpendSummary;

export function guardrails(overrides: Partial<GuardrailsState> = {}): GuardrailsState {
  return { ...UNCONFIGURED_GUARDRAILS, ...overrides };
}

export function guardrailRules(
  overrides: Partial<GuardrailsState["rules"]> = {},
): GuardrailsState["rules"] {
  return { ...NO_RULES, ...overrides };
}

export function savedGuardrails(overrides: Partial<GuardrailsSaved> = {}): GuardrailsSaved {
  const base = {
    id: "guardrail-version-1",
    workspace_id: "workspace-settings",
    version: 1,
    content_sha256: "saved-digest",
    effective_at: "2026-08-10T22:00:00Z",
    supersedes_id: null,
    unchanged: false,
    configured: true,
    rules: NO_RULES,
  } satisfies GuardrailsSaved;
  return { ...base, ...overrides };
}

export function spendSummary(overrides: Partial<SpendSummary> = {}): SpendSummary {
  return { ...NO_SPEND, ...overrides };
}

export interface SettingsReads {
  health?: Health;
  onHealthRequest?: () => Response | undefined;
  guardrails?: GuardrailsState;
  guardrailHistory?: GuardrailVersion[] | (() => GuardrailVersion[]);
  onSaveGuardrails?: (request: Request) => Promise<void>;
  savedGuardrails?: GuardrailsSaved;
  parameters?: SearchParametersState;
  spend?: SpendSummary;
  spendHistory?: RunCostHistory[];
}

/** Every read the settings page makes on mount, plus the guardrail save. */
export function settingsHandlers(reads: SettingsReads = {}): HttpHandler[] {
  return [
    http.put(`${API}/workspaces/:workspaceId/guardrails`, async ({ request }) => {
      await reads.onSaveGuardrails?.(request.clone());
      return HttpResponse.json(reads.savedGuardrails ?? savedGuardrails());
    }),
    http.get(`${API}/health`, () => {
      const refused = reads.onHealthRequest?.();
      return refused ?? HttpResponse.json(reads.health ?? HEALTHY);
    }),
    http.get(`${API}/workspaces`, () => HttpResponse.json(WORKSPACES)),
    http.get(`${API}/workspaces/:workspaceId/guardrails/history`, () => {
      const history =
        typeof reads.guardrailHistory === "function"
          ? reads.guardrailHistory()
          : reads.guardrailHistory;
      return HttpResponse.json(history ?? []);
    }),
    http.get(`${API}/workspaces/:workspaceId/guardrails`, () =>
      HttpResponse.json(reads.guardrails ?? UNCONFIGURED_GUARDRAILS),
    ),
    http.get(`${API}/job-search-parameters/options`, () => HttpResponse.json(PARAMETER_OPTIONS)),
    http.get(`${API}/job-search-parameters`, () =>
      HttpResponse.json(reads.parameters ?? NO_PARAMETERS),
    ),
    http.get(`${API}/workspaces/:workspaceId/spend-history`, () =>
      HttpResponse.json(reads.spendHistory ?? []),
    ),
    http.get(`${API}/workspaces/:workspaceId/spend`, () =>
      HttpResponse.json(reads.spend ?? NO_SPEND),
    ),
    http.get(`${API}/workspaces/:workspaceId/sources`, () => HttpResponse.json({ items: [] })),
    http.get(`${API}/providers`, () => HttpResponse.json([])),
    http.get(`${API}/workspaces/:workspaceId/master-cv`, () => HttpResponse.json(null)),
    http.get(`${API}/tasks/types`, () => HttpResponse.json([])),
    http.get(`${API}/tasks`, () => HttpResponse.json([])),
  ];
}
