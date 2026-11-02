import { useState } from "react";

import type { RelevanceReview as RelevanceReviewData } from "../../api/client";
import { useRelevanceDispute } from "../../api/hooks";
import {
  modelRelevanceKey,
  relevanceLabel,
  relevanceReviewIsOutOfDate,
  STALE_INPUT_ORDER,
  staleInputLabel,
  staleInputTone,
} from "./relevanceReviewModel";

function classLabel(value: string | null): string {
  return value ? value.replaceAll("_", " ") : "not recorded";
}

export function RelevanceReview({
  review,
  workspaceId,
}: {
  review: RelevanceReviewData;
  workspaceId: string;
}) {
  const [noteOpen, setNoteOpen] = useState(false);
  const [note, setNote] = useState("");
  const dispute = useRelevanceDispute(workspaceId);
  const isOutOfDate = relevanceReviewIsOutOfDate(review);
  const staleInputs = STALE_INPUT_ORDER.filter((input) =>
    review.out_of_date_because.includes(input),
  );
  const judged = review.outcome === "judged";
  const disputed = Boolean(review.disputed_at);
  const noteFieldId = `relevance-dispute-note-${review.id}`;

  const setAside = async () => {
    await dispute
      .mutateAsync({
        pendingId: review.pending_discovery_id,
        body: { relevance_review_id: review.id, note: note.trim() || null },
      })
      .catch(() => undefined);
    setNoteOpen(false);
    setNote("");
  };

  return (
    <div className="relevance-review">
      {isOutOfDate ? (
        <>
          <p className="field-note" role="status">
            <strong>Review is out of date.</strong>
          </p>
          <ul aria-label="Changed since this review" className="chip-row">
            {staleInputs.map((input) => (
              <li className={`status status--tone-${staleInputTone(input)}`} key={input}>
                {staleInputLabel(input)}
              </li>
            ))}
          </ul>
        </>
      ) : null}
      <p className="field-note">
        <strong>Automated relevance review: {relevanceLabel(modelRelevanceKey(review))}</strong>.{" "}
        {review.summary}
      </p>
      {review.matched_exclusion ? (
        <p className="field-note">Matches your excluded condition: {review.matched_exclusion}</p>
      ) : null}
      <div className="relevance-review__dispute">
        {disputed ? (
          <>
            <p className="field-note" role="status">
              <strong>Disputed by you.</strong> This row is back in triage, ranked below the
              verdicts the model raised itself.
            </p>
            {review.dispute_note ? <p className="field-note">{review.dispute_note}</p> : null}
            <button
              className="button button--quiet"
              disabled={dispute.isPending}
              onClick={() =>
                void dispute
                  .mutateAsync({ pendingId: review.pending_discovery_id, body: null })
                  .catch(() => undefined)
              }
              type="button"
            >
              Restore the model verdict
            </button>
          </>
        ) : noteOpen ? (
          <>
            <label htmlFor={noteFieldId}>Why this verdict is wrong (optional)</label>
            <textarea
              id={noteFieldId}
              onChange={(event) => setNote(event.target.value)}
              rows={2}
              value={note}
            />
            <button
              className="button"
              disabled={dispute.isPending}
              onClick={() => void setAside()}
              type="button"
            >
              Set this verdict aside
            </button>
            <button
              className="button button--quiet"
              onClick={() => setNoteOpen(false)}
              type="button"
            >
              Cancel
            </button>
          </>
        ) : (
          <button className="button button--quiet" onClick={() => setNoteOpen(true)} type="button">
            Dispute this verdict
          </button>
        )}
        {dispute.isError ? (
          <p className="field-error" role="alert">
            That dispute did not save. The verdict shown may have been replaced by a newer review.
          </p>
        ) : null}
      </div>
      <details>
        <summary>Review relevance evidence and unresolved questions</summary>
        {judged ? (
          <dl className="relevance-review__basis">
            <div>
              <dt>Decisive basis</dt>
              <dd>{classLabel(review.decisive_basis)}</dd>
            </div>
            <div>
              <dt>Core work</dt>
              <dd>{classLabel(review.core_work_alignment)}</dd>
            </div>
            <div>
              <dt>Core qualifications</dt>
              <dd>{classLabel(review.core_qualification_alignment)}</dd>
            </div>
            <div>
              <dt>Hard conditions</dt>
              <dd>{classLabel(review.hard_condition)}</dd>
            </div>
          </dl>
        ) : (
          <p className="field-note">
            The posting text was too short to judge, so no relevance class was recorded.
          </p>
        )}
        <p>
          <strong>Posting evidence</strong>
        </p>
        {review.posting_citations.length > 0 ? (
          <ul>
            {review.posting_citations.map((citation) => (
              <li key={citation.span_id}>{citation.text}</li>
            ))}
          </ul>
        ) : (
          <p className="field-note">No posting citation was retained.</p>
        )}
        <p>
          <strong>Master CV evidence IDs</strong>:{" "}
          {review.master_cv_evidence_ids.join(", ") || "None cited"}
        </p>
        {review.unresolved_questions.length > 0 ? (
          <>
            <p>
              <strong>Questions a full evaluation must resolve</strong>
            </p>
            <ul>
              {review.unresolved_questions.map((question) => (
                <li key={question}>{question}</li>
              ))}
            </ul>
          </>
        ) : null}
        <p className="field-note">
          This automated relevance review is not a full fit evaluation. Check the cited posting
          evidence before dismissing the role.
        </p>
      </details>
    </div>
  );
}
