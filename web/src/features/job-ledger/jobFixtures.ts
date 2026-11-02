import type { Job } from "../../api/client/types";

function baseJob(): Job {
  return {
    job_posting_id: "job-1",
    employer: "Globex",
    title: "Data Steward",
    evaluation_id: "evaluation-1",
    evaluation_version: 1,
    decision: "investigate",
    rationale: "Evidence-grounded evaluation.",
    scope_summary: "Data platform scope.",
    qualifications_summary: "Stored qualifications.",
    first_seen_at: "2026-08-01T12:00:00Z",
    evaluated_at: "2026-08-05T12:00:00Z",
    manual_note: "",
    recorded_fit_score: null,
    recorded_compensation_score: null,
    recorded_overall_score: null,
    recorded_compensation: "",
    recorded_work_model: "",
    recorded_eligibility: "",
    next_action: "",
    recorded_deadline: "",
    recorded_source: "",
    application_id: null,
    application_status: "not_applied",
    application_event_count: 0,
    compensation_components: [],
    compensation_score: null,
    completeness: { status: "incomplete", missing: ["posting_body"], checks: [] },
    eligibility_records: [],
    eligibility_score: null,
    evidence_score: null,
    fit_score: null,
    opportunity_score: null,
    overall_score: null,
    overall_score_lower_bound: null,
    unscored_weighted_component_count: 0,
  } satisfies Job;
}

export function jobSummary(overrides: Partial<Job> = {}): Job {
  return { ...baseJob(), ...overrides };
}

export function jobList(items: Job[] = []): { items: Job[]; total: number } {
  return { items, total: items.length };
}
