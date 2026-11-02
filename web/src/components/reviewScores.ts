import type { Job } from "../api/client";

export type ReviewScore = {
  value: number | null;
  source: "review" | "evaluation" | "minimum" | "none";
};

export function reviewScoreSource(score: ReviewScore): string {
  if (score.source === "review") return "From your review";
  if (score.source === "evaluation") return "From TailorCV evaluation";
  if (score.source === "minimum") return "Conservative minimum from TailorCV evaluation";
  return "No score recorded";
}

function chooseScore(recorded: number | null | undefined, evaluated: number | null): ReviewScore {
  if (recorded != null) return { value: recorded, source: "review" };
  if (evaluated != null) return { value: evaluated, source: "evaluation" };
  return { value: null, source: "none" };
}

export function reviewScores(job: Job): {
  fit: ReviewScore;
  compensation: ReviewScore;
  overall: ReviewScore;
} {
  const overall = chooseScore(job.recorded_overall_score, job.overall_score);
  return {
    fit: chooseScore(job.recorded_fit_score, job.fit_score),
    compensation: chooseScore(job.recorded_compensation_score, job.compensation_score),
    overall:
      overall.value != null
        ? overall
        : job.overall_score_lower_bound != null
          ? { value: job.overall_score_lower_bound, source: "minimum" }
          : overall,
  };
}
