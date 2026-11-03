import { Link } from "react-router-dom";

import type { JobDetail } from "../../api/client";
import {
  evaluationReview,
  evaluationReviewMessage,
  evidenceIntegrity,
} from "../../components/evaluationReview";
import { ConfirmedActionButton } from "../../components/ConfirmedActionButton";
import { Icons } from "../../components/Icons";
import { StatusBadge } from "../../components/StatusBadge";
import { reviewScores, type ReviewScore } from "../../components/reviewScores";

function relativeAge(date: Date): string {
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMinutes = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMinutes / 60);
  const diffDays = Math.floor(diffHours / 24);
  const diffWeeks = Math.floor(diffDays / 7);

  if (diffMinutes < 60) return diffMinutes === 1 ? "1 minute ago" : `${diffMinutes} minutes ago`;
  if (diffHours < 24) return diffHours === 1 ? "1 hour ago" : `${diffHours} hours ago`;
  if (diffDays < 7) return diffDays === 1 ? "1 day ago" : `${diffDays} days ago`;
  if (diffWeeks < 5) return diffWeeks === 1 ? "1 week ago" : `${diffWeeks} weeks ago`;
  return date.toLocaleDateString();
}

function Score({
  label,
  value,
  source,
}: {
  label: string;
  value: number | null;
  source?: ReviewScore["source"];
}) {
  return (
    <div className="hero-score">
      <span>{label}</span>
      <strong>
        {value ?? "not stated"}
        {source === "review" && <small className="hero-score__pill">your review</small>}
        {source === "minimum" && <small className="hero-score__pill">minimum</small>}
      </strong>
    </div>
  );
}

function CompensationScore({
  value,
  components,
  standingSource,
  source,
}: {
  value: number | null;
  components?: JobDetail["compensation_components"];
  standingSource?: "compared" | "extracted" | null;
  source?: ReviewScore["source"];
}) {
  const formatRange = (comp: JobDetail["compensation_components"][number]) => {
    const min = comp.amount_min !== null ? Math.round(comp.amount_min).toLocaleString() : null;
    const max = comp.amount_max !== null ? Math.round(comp.amount_max).toLocaleString() : null;
    if (min && max) return `${comp.currency} ${min}-${max}`;
    if (min) return `${comp.currency} ${min}+`;
    if (max) return `${comp.currency} up to ${max}`;
    return null;
  };
  const formatAnnualized = (comp: JobDetail["compensation_components"][number]) => {
    if (comp.basis === "annual") return null;
    const min =
      comp.annualized_min !== null && comp.annualized_min !== undefined
        ? Math.round(comp.annualized_min).toLocaleString()
        : null;
    const max =
      comp.annualized_max !== null && comp.annualized_max !== undefined
        ? Math.round(comp.annualized_max).toLocaleString()
        : null;
    if (min && max) return `${comp.currency} ${min}-${max} annualized`;
    if (min) return `${comp.currency} ${min}+ annualized`;
    if (max) return `${comp.currency} up to ${max} annualized`;
    return null;
  };
  /* Show distinct compensation amounts; the Posting tab contains all statements. */
  const stated = [
    ...new Map(
      (components ?? [])
        .map((comp) => ({
          comp,
          range: formatRange(comp),
          annualized: formatAnnualized(comp),
        }))
        .filter(
          (item): item is { comp: typeof item.comp; range: string; annualized: string | null } =>
            item.range !== null,
        )
        .map((item) => [`${item.range} ${item.comp.basis}`, item] as const),
    ).values(),
  ];

  return (
    <div className="hero-score hero-score--compensation">
      <span>Compensation</span>
      <strong>
        {value ?? "not stated"}
        {source === "review" && <small className="hero-score__pill">your review</small>}
        {standingSource === "extracted" && (
          <small
            className="hero-score__pill"
            title="Extracted from the posting; no comparable band, so this score is a placeholder."
          >
            shown
          </small>
        )}
      </strong>
      {stated.length > 0 && (
        <div>
          {stated.map(({ comp, range, annualized }) => (
            <small key={`${range} ${comp.basis}`}>
              {range} {comp.basis}
              {annualized ? ` (${annualized})` : ""}
            </small>
          ))}
        </div>
      )}
    </div>
  );
}

export function JobSummary({
  data,
  onRetainRecorded,
  retainingRecorded = false,
}: {
  data: JobDetail;
  onRetainRecorded?: () => void;
  retainingRecorded?: boolean;
}) {
  const review = evaluationReview(data.evaluation_inputs, data.evaluation_review);
  const reviewMessage = evaluationReviewMessage(review);
  const reviewReason = review.status === "needs_review" ? review.reason : null;
  const integrity = evidenceIntegrity(data.completeness);
  const scores = reviewScores(data);
  const hasReviewScores =
    scores.fit.value !== null ||
    scores.compensation.value !== null ||
    scores.overall.value !== null;
  const evaluatedObservationId = data.evaluation_inputs?.find(
    (input) => input.name === "posting",
  )?.recorded;
  return (
    <>
      {reviewMessage && (
        <aside className="currentness-review" aria-label="Evaluation review">
          <div>
            <strong>
              {reviewReason === "posting_changed" ? "Posting changed" : "Criteria changed"}
            </strong>
            <p>
              Compare the change first. Re-run the evaluation only if it could change your decision;
              otherwise mark this review complete.
            </p>
          </div>
          <div className="currentness-review__actions">
            {review.status === "needs_review" &&
              review.reason === "posting_changed" &&
              evaluatedObservationId && (
                <Link
                  className="button button--quiet"
                  to={`?tab=posting&compare=${encodeURIComponent(evaluatedObservationId)}`}
                >
                  Compare versions
                </Link>
              )}
            {review.status === "needs_review" && review.reason === "criteria_changed" && (
              <Link className="button button--quiet" to="?tab=evaluation#evaluation-inputs">
                Inspect criteria
              </Link>
            )}
            {review.status === "needs_review" && onRetainRecorded && (
              <ConfirmedActionButton
                className="button button--quiet"
                disabled={retainingRecorded}
                onConfirm={onRetainRecorded}
                options={{
                  title: "Keep the recorded evaluation?",
                  description:
                    "You reviewed the current changes and decided they do not warrant a new evaluation. The recorded score and evidence remain historical. ClearScout will ask for review again if the posting or decision criteria change further.",
                  confirmLabel: "Mark reviewed",
                }}
              >
                {retainingRecorded ? "Recording review" : "Keep recorded evaluation"}
              </ConfirmedActionButton>
            )}
          </div>
        </aside>
      )}
      <section className="job-hero">
        <div>
          <span>
            {data.evaluation_id
              ? `Decision (evaluation v${data.evaluation_version})`
              : "Model evaluation"}
          </span>
          {data.decision ? <StatusBadge value={data.decision} /> : <strong>Not evaluated</strong>}
        </div>
        {(data.evaluation_id || hasReviewScores) && (
          <>
            <Score label="Fit" value={scores.fit.value} source={scores.fit.source} />
            <CompensationScore
              value={scores.compensation.value}
              components={data.compensation_components}
              standingSource={
                scores.compensation.source === "evaluation"
                  ? data.compensation_standing_source
                  : null
              }
              source={scores.compensation.source}
            />
            {data.evaluation_id && (
              <>
                <Score label="Eligibility" value={data.eligibility_score} />
                <Score label="Evidence" value={data.evidence_score} />
                <Score label="Opportunity" value={data.opportunity_score} />
              </>
            )}
            {scores.overall.value !== null && (
              <Score label="Overall" value={scores.overall.value} source={scores.overall.source} />
            )}
          </>
        )}
        <div>
          <span>Your priority</span>
          <strong>{data.manual_priority ?? "Not set"}</strong>
        </div>
        <div>
          <span>Fit judgment</span>
          <strong>{data.manual_fit ?? "Not set"}</strong>
        </div>
        <div>
          <span>Application</span>
          <StatusBadge value={data.application_status} />
        </div>
        <div>
          <span>Posting</span>
          <StatusBadge value={data.posting_status} />
        </div>
      </section>
      {data.evaluation_id && (
        <aside className={`completeness-card completeness-card--${data.completeness.status}`}>
          <header>
            <div>
              <strong>Evidence integrity</strong>
              <p>Checks whether the saved evaluation has the records needed to support it.</p>
            </div>
            <span
              className={`status status--tone-${data.completeness.status === "complete" ? "positive" : "warning"}`}
            >
              {integrity.label}
            </span>
          </header>
          <ul>
            {data.completeness.checks.map((item) => (
              <li key={item.code}>
                {item.complete ? (
                  <Icons.check height={14} width={14} />
                ) : (
                  <Icons.circle height={14} width={14} />
                )}
                <span>{item.label}</span>
                {!item.complete && <Link to={item.action}>Open</Link>}
              </li>
            ))}
          </ul>
          {data.last_verified_at && (
            <footer>
              <small>Last verified {relativeAge(new Date(data.last_verified_at))}</small>
            </footer>
          )}
        </aside>
      )}
    </>
  );
}
