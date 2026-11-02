/**
 * How the background work is named and explained, so a task reads the same way
 * wherever it is started, listed or reported.
 */
import type { components } from "./schema";

/** The generated task vocabulary that task labels are keyed by. */
export type TaskType = components["schemas"]["TaskCreate"]["task_type"];

type TaskKind = {
  name: string;
  detail: string;
};

export const TASK_KINDS: Record<TaskType, TaskKind> = {
  "posting.refresh": {
    name: "Refresh a posting",
    detail: "Read the posting again and record what changed, including whether it closed.",
  },
  "posting.refresh_batch": {
    name: "Refresh tracked postings",
    detail: "Read the selected tracked postings again and record what changed in each.",
  },
  "posting.refresh_with_reextraction": {
    name: "Refresh with dispute re-extraction",
    detail:
      "Read the posting again and ask the model to re-check disputed extraction requirements. A reversed ruling stands only when new cited text is found in the fresh body.",
  },
  "posting.refresh_with_rematch": {
    name: "Refresh with fit re-match",
    detail:
      "Read the current posting and master CV, then ask the model to re-check every fit dispute. A ruling reverses only when the match cites a CV entry that the previous match did not cite for that requirement.",
  },
  "posting.recover_batch": {
    name: "Assisted posting recovery",
    detail:
      "Use bounded hosted search to locate an exact publisher source, then retain content only after TailorCV reads and verifies it deterministically.",
  },
  "discovery.evaluate": {
    name: "Evaluate a discovery",
    detail:
      "Read the posting against your master CV and add its scores, decision, fit report, and cited evidence to evaluated jobs.",
  },
  "discovery.evaluate_batch": {
    name: "Evaluate discoveries",
    detail: "Evaluate each selected discovery. Costs are estimates and do not stop the task.",
  },
  "discovery.refresh_batch": {
    name: "Refresh queued postings",
    detail:
      "Re-read the publisher's facts, liveness, and posting text for each selected discovery.",
  },
  "discovery.rescreen_queue": {
    name: "Re-screen the pending queue",
    detail:
      "Re-judge every active discovery against the current screening rules from facts already stored. Fetches nothing.",
  },
  "discovery.relevance_review": {
    name: "Review discovery relevance",
    detail: "Review selected readable discoveries against the current CV and search parameters.",
  },
  "workflow.tailor": {
    name: "Tailor a package",
    detail: "Draft a resume, and a cover letter if asked, from evidence this posting selected.",
  },
  "workflow.approve": {
    name: "Apply a review",
    detail: "Rebuild the documents from your accept and reject decisions.",
  },
  "workflow.batch": {
    name: "Compare postings",
    detail: "Screen each posting on its own, then report what they ask for in common.",
  },
  "workflow.discovery": {
    name: "Explore a gap",
    detail:
      "Ask about work your CV does not record. Nothing here calls a model; the words stay yours.",
  },
  "search.guided_campaign": {
    name: "Search for new jobs",
    detail: "Search the web for new postings and boards beyond your saved sources.",
  },
  "search.guided_campaign_retry": {
    name: "Retry campaign processing",
    detail:
      "Process captured campaign results that failed locally. Reads publisher pages again; makes no model call.",
  },
  "search.vocabulary": {
    name: "Suggest search vocabulary",
    detail: "Propose CV-grounded search terms for you to review before they affect later runs.",
  },
  "source.scan": {
    name: "Scan for openings",
    detail:
      "Scan the boards you already track for current openings. Finds postings; does not evaluate them.",
  },
  "source.continue": {
    name: "Continue reading a source",
    detail:
      "Read the next saved publisher-link batch, retain every outcome, and continue automatically while unread links remain.",
  },
  "artifact.verify": {
    name: "Verify a file",
    detail: "Re-hash a stored file and confirm it matches what was recorded.",
  },
  "document.render": {
    name: "Render a document",
    detail:
      "Ask the renderer on your PATH to convert a document. Absent renderer costs the PDF and nothing else.",
  },
};

// The ledger queues both evaluation tasks over tracked postings as well as discoveries.
const TRACKED_POSTING_EVALUATIONS: Partial<Record<TaskType, TaskKind>> = {
  "discovery.evaluate": {
    name: "Re-evaluate a tracked posting",
    detail:
      "Evaluate the tracked posting again against your current master CV and rules, and add the new version to evaluated jobs.",
  },
  "discovery.evaluate_batch": {
    name: "Re-evaluate tracked postings",
    detail:
      "Evaluate each selected tracked posting again against your current master CV and rules. Costs are estimates and do not stop the task.",
  },
};

/** The kind one queued task is, where its payload's subjects change what it works on. */
export function taskKindOf(task: {
  task_type: TaskType;
  job_posting_ids: readonly string[];
}): TaskKind {
  const tracked =
    task.job_posting_ids.length > 0 ? TRACKED_POSTING_EVALUATIONS[task.task_type] : undefined;
  return tracked ?? TASK_KINDS[task.task_type];
}

// Copied verbatim from the cost each paid task registers on the server.
const SPEND_DISCLOSURES: Partial<Record<TaskType, string>> = {
  "search.guided_campaign":
    "Provider charges may include model tokens, search fees, and a web-content-retrieval " +
    "charge. Costs are estimates and do not stop an approved task.",
  "search.vocabulary": "One provider model-token charge per invocation. No search fee.",
  "discovery.relevance_review": "Provider model-token charges may apply per reviewed batch.",
};

export function taskSpendDisclosure(taskType: TaskType): string {
  return SPEND_DISCLOSURES[taskType] ?? "";
}

/** Name how many times a task has been tried, counting the first try. */
export function attemptCount(attempts: number): string {
  return `${attempts} attempt${attempts === 1 ? "" : "s"}`;
}
