import type { Job } from "../api/client";

type EvaluationInputs = Job["evaluation_inputs"];

const evidenceLabels: Record<string, string> = {
  posting_body: "posting body",
  score_evidence: "score evidence",
  fit_report: "fit report",
  observed_status_eligibility: "current status and eligibility",
  normalized_identity: "normalized identity",
};

export function evidenceIntegrity(completeness: Job["completeness"]) {
  if (completeness.status === "complete") return { label: "Evidence complete", reason: "" };
  const missing = completeness.missing.map((code) => evidenceLabels[code] ?? code).join(", ");
  return { label: "Evidence gap", reason: missing };
}

export type EvaluationReview =
  | { status: "current" }
  | { status: "needs_review"; reason: "posting_changed" | "criteria_changed" }
  | { status: "reviewed"; reason: "posting_changed" | "criteria_changed" }
  | { status: "unknown" };

/** Classify a saved assessment separately from durable record-integrity checks. */
export function evaluationReview(
  inputs: EvaluationInputs,
  persisted?: Job["evaluation_review"],
): EvaluationReview {
  if (persisted?.status === "reviewed" && persisted.reason) {
    return { status: "reviewed", reason: persisted.reason };
  }
  if (!inputs?.length) return { status: "unknown" };
  const posting = inputs.find((input) => input.name === "posting");
  if (posting?.status === "changed") return { status: "needs_review", reason: "posting_changed" };
  if (
    inputs.some(
      (input) =>
        (input.name === "cv" || input.name === "search_parameters") && input.status === "changed",
    )
  ) {
    return { status: "needs_review", reason: "criteria_changed" };
  }
  return posting?.status === "unknown" ? { status: "unknown" } : { status: "current" };
}

export function evaluationReviewMessage(review: EvaluationReview): string | null {
  if (review.status !== "needs_review") return null;
  return review.reason === "posting_changed"
    ? "The posting changed since this evaluation. Compare it with the version evaluated, then re-run the evaluation only if the difference could change your decision."
    : "Your CV or search criteria changed since this evaluation. Re-run the evaluation if those changes could change your decision.";
}
