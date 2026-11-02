import { HttpResponse, http, type HttpHandler } from "msw";

import type { EvaluationPreflight, TaskTypeInfo } from "../api/client/types";

const UNPAID = "No provider charge.";

/** What GET /tasks/types answers, shared by every test that renders a paid button. */
export const TASK_TYPE_CATALOG: TaskTypeInfo[] = [
  { task_type: "artifact.verify", paid: false, cost: UNPAID },
  { task_type: "posting.refresh", paid: false, cost: UNPAID },
  { task_type: "posting.refresh_batch", paid: false, cost: UNPAID },
  {
    task_type: "posting.recover_batch",
    paid: true,
    cost: "One model-token charge and up to two hosted-search charges per selected posting.",
  },
  {
    task_type: "posting.refresh_with_reextraction",
    paid: true,
    cost:
      "One extraction-tier model-token charge per invocation to re-check every " +
      "disputed extraction requirement. No search fee.",
  },
  {
    task_type: "posting.refresh_with_rematch",
    paid: true,
    cost:
      "One match-stage model-token charge per invocation to re-check every disputed fit " +
      "requirement. No search fee.",
  },
  {
    task_type: "discovery.evaluate",
    paid: true,
    cost: "Each evaluation may incur provider model-token charges.",
  },
  {
    task_type: "discovery.evaluate_batch",
    paid: true,
    cost:
      "Evaluates each selected discovery or tracked posting. Provider model-token " +
      "charges apply; cost estimates do not stop the task.",
  },
  {
    task_type: "workflow.tailor",
    paid: true,
    cost:
      "A matching completed package is reused at no additional provider cost unless " +
      "regeneration is requested. Otherwise it may call the configured provider and " +
      "incur model-token charges.",
  },
  {
    task_type: "workflow.approve",
    paid: true,
    cost:
      "A matching completed approved package is reused at no additional provider cost. " +
      "Otherwise it may call the configured provider and incur model-token charges.",
  },
  {
    task_type: "workflow.batch",
    paid: true,
    cost: "Provider model-token charges may scale with the number of postings compared.",
  },
  { task_type: "workflow.discovery", paid: false, cost: UNPAID },
  { task_type: "document.render", paid: false, cost: UNPAID },
  { task_type: "discovery.rescreen_queue", paid: false, cost: UNPAID },
  { task_type: "source.scan", paid: false, cost: UNPAID },
  { task_type: "source.continue", paid: false, cost: UNPAID },
  {
    task_type: "search.guided_campaign",
    paid: true,
    cost:
      "Provider charges may include model tokens, search fees, and a web-content-retrieval " +
      "charge. Costs are estimates and do not stop an approved task.",
  },
  { task_type: "search.guided_campaign_retry", paid: false, cost: UNPAID },
  {
    task_type: "search.vocabulary",
    paid: true,
    cost: "One provider model-token charge per invocation. No search fee.",
  },
  {
    task_type: "discovery.relevance_review",
    paid: true,
    cost: "Provider model-token charges may apply per reviewed batch.",
  },
];

/** The catalog read every page makes before offering a paid action. */
export function taskTypesHandler(): HttpHandler {
  return http.get("*/api/v1/tasks/types", () => HttpResponse.json(TASK_TYPE_CATALOG));
}

export function evaluationPreflightHandler(onRequest?: (request: Request) => void): HttpHandler {
  return http.post(
    "*/api/v1/workspaces/:workspaceId/evaluation-preflight",
    ({ params, request }) => {
      onRequest?.(request);
      return HttpResponse.json({
        workspace_id: String(params.workspaceId),
        currency: "USD",
        advisory: true,
        generated_at: "2026-10-08T12:00:00Z",
        items: [],
        expected_cost_usd: "0.20",
        range_low_usd: "0",
        range_high_usd: "0.50",
        basis: "Synthetic current configured rates and retained inputs.",
        limitations: ["Actual charges can exceed this range."],
      } satisfies EvaluationPreflight);
    },
  );
}
