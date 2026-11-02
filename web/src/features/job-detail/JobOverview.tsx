import { useState } from "react";
import {
  manualReviewFromJob,
  type JobDetail,
  type SearchLocation,
  type ManualJobReview,
} from "../../api/client";
import {
  useSaveClosingDate,
  useSaveManualJobReview,
  useSearchParameters,
  useTags,
} from "../../api/hooks";
import { ClosingDates } from "../../components/ClosingDates";
import { EntityTags } from "../../components/EntityTags";
import { StatusBadge } from "../../components/StatusBadge";
import { employmentTypeLabel } from "../../components/text";

/** Name a place from the words its owner wrote, never from its internal key. */
function placeName(place: SearchLocation): string {
  if (place.metro) return place.metro;
  if (place.region) return `${place.region}, ${place.country}`;
  return place.country;
}

/**
 * Show one row per distinct posting reading.
 *
 * A stored reading holds the location key the workspace used at the time. A key
 * the workspace still states is named in words; any other key is shown as
 * stored.
 */
function EligibilityRecords({
  records,
  statedPlaces,
}: {
  records: NonNullable<JobDetail["eligibility_records"]>;
  statedPlaces: readonly SearchLocation[];
}) {
  const distinct = [
    ...new Map(records.map((record) => [JSON.stringify(record), record] as const)).values(),
  ];
  const readAs = (key: string) => {
    const stated = statedPlaces.find((place) => place.key === key);
    return stated ? placeName(stated) : key;
  };
  return (
    <>
      {distinct.map((record, index) => (
        <div key={index}>
          <h4>
            {record.workspace_location_key
              ? `Against ${readAs(record.workspace_location_key)}`
              : `Reading ${index + 1}`}
          </h4>
          <dl className="fact-list">
            {record.region_rule && (
              <div>
                <dt>Region rule</dt>
                <dd>{record.region_rule}</dd>
              </div>
            )}
            {record.arrangement && (
              <div>
                <dt>Arrangement</dt>
                <dd>{record.arrangement}</dd>
              </div>
            )}
            {record.details && (
              <div>
                <dt>Details</dt>
                <dd className="fact-value">{record.details}</dd>
              </div>
            )}
          </dl>
        </div>
      ))}
    </>
  );
}

const PLACE_LIMIT = 80;

/** Withholds a stored place no place can be: over PLACE_LIMIT, or carrying prose. */
function statedPlace(value: string | null | undefined): string {
  const stated = value?.trim() ?? "";
  if (!stated) return "Not stated";
  if (stated.length > PLACE_LIMIT || /[.!?]\s/.test(stated)) return "Not recorded readably";
  return stated;
}

/**
 * Show the latest reading; the Posting tab contains the full sequence.
 */
function LatestObservation({
  observation,
  observationCount,
  onOpenPosting,
}: {
  observation: JobDetail["latest_observation"];
  observationCount: number;
  onOpenPosting: () => void;
}) {
  if (!observation) return <p className="field-note">No posting has been read yet.</p>;
  const earlier = observationCount - 1;
  return (
    <>
      <dl className="fact-list">
        <div>
          <dt>Status</dt>
          <dd>
            <StatusBadge value={observation.posting_status} />
          </dd>
        </div>
        <div>
          <dt>Location</dt>
          <dd className="fact-value">{statedPlace(observation.location)}</dd>
        </div>
        <div>
          <dt>Residence</dt>
          <dd>{observation.residence_rule.trim() || "Not stated"}</dd>
        </div>
        <div>
          <dt>Employment type</dt>
          <dd>
            {observation.employment_type
              ? employmentTypeLabel(observation.employment_type)
              : "Not stated"}
          </dd>
        </div>
        <div>
          <dt>Payroll arrangement</dt>
          <dd>{observation.payroll_arrangement || "Not stated"}</dd>
        </div>
        <div>
          <dt>Observed</dt>
          <dd>{new Date(observation.observed_at).toLocaleDateString()}</dd>
        </div>
      </dl>
      {earlier > 0 && (
        <p className="field-note">
          {earlier} earlier reading{earlier === 1 ? "" : "s"} of this posting.{" "}
          <button className="text-button" onClick={onOpenPosting} type="button">
            Read them on the Posting tab
          </button>
        </p>
      )}
    </>
  );
}

export function JobOverview({
  data,
  hasCorrection,
  jobId,
  onOpenApplicationHistory,
  onOpenPosting,
  workspaceId,
}: {
  data: JobDetail;
  hasCorrection: boolean;
  jobId: string;
  onOpenApplicationHistory: () => void;
  onOpenPosting: () => void;
  workspaceId: string;
}) {
  const parameters = useSearchParameters(workspaceId);
  const tags = useTags(workspaceId);
  const saveReview = useSaveManualJobReview(workspaceId, jobId);
  const saveClosingDate = useSaveClosingDate(workspaceId);
  const [review, setReview] = useState<ManualJobReview>(() => manualReviewFromJob(data));
  return (
    <>
      {hasCorrection && (
        <aside className="reconciliation-note" role="note">
          <div>
            <strong>Application status reconciled</strong>
            <p>
              The current pipeline status comes from application history. Evaluation prose may
              describe an earlier state.
            </p>
          </div>
          <button className="button button--quiet" onClick={onOpenApplicationHistory} type="button">
            View correction
          </button>
        </aside>
      )}
      <EntityTags
        entityId={jobId}
        entityType="job_posting"
        tags={tags.data ?? []}
        workspaceId={workspaceId}
      />
      <section className="panel">
        <h2>Closing dates</h2>
        <ClosingDates
          publisher={data.publisher_closing_date}
          override={data.closing_date_override}
          onSave={async (value) => {
            await saveClosingDate.mutateAsync({ jobId, body: { closing_date_override: value } });
          }}
        />
      </section>
      <section className="panel">
        <h2>Your review</h2>
        <p className="field-note">
          Your recorded values stay separate from TailorCV evaluations. Blank fields mean no value
          was recorded; changing one does not run an evaluation.
        </p>
        <form
          className="event-form"
          onSubmit={(event) => {
            event.preventDefault();
            saveReview.mutate(review);
          }}
        >
          <label>
            Your priority
            <select
              onChange={(event) =>
                setReview({
                  ...review,
                  priority: (event.target.value as ManualJobReview["priority"]) || null,
                })
              }
              value={review.priority ?? ""}
            >
              <option value="">Not set</option>
              <option value="high">High</option>
              <option value="medium">Medium</option>
              <option value="low">Low</option>
            </select>
          </label>
          <label>
            Your fit judgment
            <select
              onChange={(event) =>
                setReview({
                  ...review,
                  fit: (event.target.value as ManualJobReview["fit"]) || null,
                })
              }
              value={review.fit ?? ""}
            >
              <option value="">Not set</option>
              <option value="strong">Strong</option>
              <option value="possible">Possible</option>
              <option value="weak">Weak</option>
            </select>
          </label>
          <h3>Recorded scores</h3>
          <p className="field-note">
            A score you enter here takes precedence in the tracked-postings review. Leave it blank
            to use the TailorCV evaluation score. Pay alone does not supply a compensation score.
          </p>
          <div className="manual-review__fields">
            {(
              [
                ["recorded_fit_score", "Fit score"],
                ["recorded_compensation_score", "Compensation score"],
                ["recorded_overall_score", "Overall score"],
              ] as const
            ).map(([field, label]) => (
              <label key={field}>
                {label}
                <input
                  min={0}
                  max={100}
                  onChange={(event) =>
                    setReview({
                      ...review,
                      [field]: event.target.value === "" ? null : Number(event.target.value),
                    })
                  }
                  step={1}
                  type="number"
                  value={review[field] ?? ""}
                />
              </label>
            ))}
          </div>
          <h3>Recorded source and follow-up</h3>
          <div className="manual-review__fields">
            {(
              [
                ["recorded_compensation", "Compensation information", 500],
                ["recorded_work_model", "Recorded work model", 500],
                ["recorded_eligibility", "Recorded eligibility assessment", 500],
                ["next_action", "Next action", 500],
                ["recorded_deadline", "Recorded deadline", 200],
                ["recorded_source", "Recorded source", 500],
              ] as const
            ).map(([field, label, maxLength]) => (
              <label key={field}>
                {label}
                <input
                  maxLength={maxLength}
                  onChange={(event) => setReview({ ...review, [field]: event.target.value })}
                  type="text"
                  value={review[field] ?? ""}
                />
              </label>
            ))}
          </div>
          <p className="field-note">
            Recorded source names the original record. Later edits are your corrections, not new
            claims from that source.
          </p>
          <label>
            Your note
            <textarea
              maxLength={2000}
              onChange={(event) => setReview({ ...review, note: event.target.value })}
              rows={3}
              value={review.note}
            />
          </label>
          <button className="button button--primary" disabled={saveReview.isPending} type="submit">
            {saveReview.isPending ? "Saving" : "Save your review"}
          </button>
        </form>
      </section>
      <section className="detail-grid">
        <article className="panel">
          <h2>{data.evaluation_id ? "Why this decision" : "Model evaluation"}</h2>
          <p className="reading-copy">
            {data.rationale ||
              "Not evaluated. You can track and apply for this job without a model review."}
          </p>
          <h3>Qualifications</h3>
          <p>{data.qualifications_summary || "No separate qualification summary."}</p>
          {data.scope_summary && (
            <>
              <h3>What the posting asks for</h3>
              <p className="reading-copy">{data.scope_summary}</p>
            </>
          )}
        </article>
        <article className="panel">
          <h2>Latest posting facts</h2>
          <LatestObservation
            observation={data.latest_observation}
            observationCount={data.observation_count}
            onOpenPosting={onOpenPosting}
          />
          {data.eligibility_records && data.eligibility_records.length > 0 && (
            <>
              <h3>Eligibility</h3>
              <EligibilityRecords
                records={data.eligibility_records}
                statedPlaces={parameters.data?.parameters.locations ?? []}
              />
            </>
          )}
        </article>
      </section>
    </>
  );
}
