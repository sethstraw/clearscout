import { useMemo, useState } from "react";
import type { KeyboardEvent } from "react";
import { Link } from "react-router-dom";

import {
  useClearRequirementRuling,
  useMasterCvRequirementGaps,
  useMasterCvRequirementRulings,
  useSetRequirementRuling,
} from "../../api/hooks";
import type { RequirementImportance, RulingValue } from "../../api/client/types";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";

const IMPORTANCE_LEVELS = [
  "must_have",
  "nice_to_have",
  "responsibility",
] as const satisfies readonly RequirementImportance[];

// Adding a value to RequirementImportance without extending IMPORTANCE_LEVELS
// leaves this line unable to resolve to `true` and the build refuses.
const _importanceLevelsAreExhaustive: [
  Exclude<RequirementImportance, (typeof IMPORTANCE_LEVELS)[number]>,
] extends [never]
  ? true
  : never = true;
void _importanceLevelsAreExhaustive;

type DisplayRulingValue = RulingValue | "reversed";

const RULING_ACTIONS = [
  { value: "met", label: "Mark met", description: null },
  { value: "not_applicable", label: "Not applicable", description: null },
  {
    value: "disputed_fit",
    label: "Disputed (fit)",
    description: "A paid fit re-match checks this requirement against the current master CV.",
  },
  {
    value: "disputed_extraction",
    label: "Disputed (extraction)",
    description: "A paid re-extraction checks how this requirement was read from the posting.",
  },
] as const satisfies readonly {
  value: RulingValue;
  label: string;
  description: string | null;
}[];

// Adding a value to RulingValue without extending RULING_ACTIONS leaves this
// line unable to resolve to `true` and the build refuses.
const _rulingActionsAreExhaustive: [
  Exclude<RulingValue, (typeof RULING_ACTIONS)[number]["value"]>,
] extends [never]
  ? true
  : never = true;
void _rulingActionsAreExhaustive;

function importanceLabel(value: RequirementImportance) {
  return value.replaceAll("_", " ");
}

type Draft = { key: string; ruling: RulingValue; note: string };

function RulingControls({
  workspaceId,
  requirementKey,
  requirementText,
  currentRuling,
  storedNote,
  draft,
  onDraftChange,
}: {
  workspaceId: string;
  requirementKey: string;
  requirementText: string;
  currentRuling: DisplayRulingValue | null | undefined;
  storedNote: string | null;
  draft: Draft | null;
  onDraftChange: (next: Draft | null) => void;
}) {
  const setRuling = useSetRequirementRuling(workspaceId);
  const clearRuling = useClearRequirementRuling(workspaceId);
  const isActive = draft?.key === requirementKey;
  const busy = setRuling.isPending || clearRuling.isPending;

  const commit = async () => {
    if (!isActive || !draft) return;
    const note = draft.note.trim();
    await setRuling.mutateAsync({
      requirementKey,
      body: { ruling: draft.ruling, note: note ? note : null },
    });
    onDraftChange(null);
  };

  const handleNoteKey = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") {
      event.preventDefault();
      void commit();
    } else if (event.key === "Escape") {
      event.preventDefault();
      onDraftChange(null);
    }
  };

  const disputeKind =
    currentRuling === "disputed_fit"
      ? "fit"
      : currentRuling === "disputed_extraction"
        ? "extraction"
        : null;
  const disputedBadge = disputeKind ? (
    <span className="badge" aria-label={`Disputed (${disputeKind}) by you: ${requirementText}`}>
      Disputed ({disputeKind}) by you
    </span>
  ) : null;

  return (
    <div className="button-row">
      {disputedBadge}
      {storedNote ? <span className="field-note">Note: {storedNote}</span> : null}
      {RULING_ACTIONS.map((action) => {
        const descriptionId = `${requirementKey}-${action.value}-description`;
        return (
          <span key={action.value}>
            <button
              aria-describedby={action.description ? descriptionId : undefined}
              className="button button--quiet"
              disabled={busy}
              onClick={() => onDraftChange({ key: requirementKey, ruling: action.value, note: "" })}
              type="button"
            >
              {action.label}
            </button>
            {action.description ? (
              <small className="field-note" id={descriptionId}>
                {action.description}
              </small>
            ) : null}
          </span>
        );
      })}
      {currentRuling ? (
        <button
          className="button button--quiet"
          disabled={busy}
          onClick={() => {
            onDraftChange(null);
            void clearRuling.mutateAsync(requirementKey);
          }}
          type="button"
        >
          Clear ruling
        </button>
      ) : null}
      {isActive && draft ? (
        <>
          <input
            aria-label={`Note for ${requirementText}`}
            className="compact-field"
            disabled={busy}
            onChange={(event) => onDraftChange({ ...draft, note: event.target.value })}
            onKeyDown={handleNoteKey}
            placeholder="Optional note"
            type="text"
            value={draft.note}
          />
          <button
            className="button button--primary"
            disabled={busy}
            onClick={() => void commit()}
            type="button"
          >
            Save
          </button>
          <button
            className="button button--quiet"
            disabled={busy}
            onClick={() => onDraftChange(null)}
            type="button"
          >
            Cancel
          </button>
        </>
      ) : null}
    </div>
  );
}

export function RequirementGaps({ workspaceId }: { workspaceId: string }) {
  const [minRaisedText, setMinRaisedText] = useState("1");
  const [importance, setImportance] = useState<readonly RequirementImportance[]>([]);
  const [draft, setDraft] = useState<Draft | null>(null);

  const parsedMinRaised = Number.parseInt(minRaisedText, 10);
  const minRaised =
    Number.isFinite(parsedMinRaised) && parsedMinRaised > 1 ? parsedMinRaised : undefined;

  const gaps = useMasterCvRequirementGaps(workspaceId, { minRaised, importance });
  const rulings = useMasterCvRequirementRulings(workspaceId);
  const notesByKey = useMemo(() => {
    const table = new Map<string, string>();
    for (const row of rulings.data?.rulings ?? []) {
      if (row.note) table.set(row.requirement_key, row.note);
    }
    return table;
  }, [rulings.data]);
  const filterActive = minRaised !== undefined || importance.length > 0;
  const toggleImportance = (value: RequirementImportance) =>
    setImportance((current) =>
      current.includes(value) ? current.filter((item) => item !== value) : [...current, value],
    );
  const importanceSet = useMemo(() => new Set(importance), [importance]);

  if (gaps.isLoading) return <LoadingState label="Reading unanswered requirements" />;
  if (gaps.isError) return <ErrorState error={gaps.error} />;
  const summary = gaps.data;
  if (!summary) return null;

  return (
    <article className="panel">
      <header>
        <div>
          <h2>Unanswered requirements</h2>
          <p>
            Requirements the analysis settled and found no evidence for in this CV, read from the
            current evaluation of {summary.evaluations_read} posting
            {summary.evaluations_read === 1 ? "" : "s"}. Nothing here is a ruling and nothing is
            stored.
          </p>
        </div>
      </header>
      <div className="panel-body">
        <form
          aria-label="Filter unanswered requirements"
          className="settings-grid"
          onSubmit={(event) => event.preventDefault()}
        >
          <label className="compact-field">
            Raised by at least
            <input
              aria-label="Minimum postings"
              min={1}
              onChange={(event) => setMinRaisedText(event.target.value)}
              type="number"
              value={minRaisedText}
            />
            <small className="field-note">
              Hide groups raised by fewer than this many postings.
            </small>
          </label>
          <fieldset className="compact-field">
            <legend>Importance</legend>
            {IMPORTANCE_LEVELS.map((level) => (
              <label className="compact-field" key={level}>
                <input
                  checked={importanceSet.has(level)}
                  onChange={() => toggleImportance(level)}
                  type="checkbox"
                />
                {importanceLabel(level)}
              </label>
            ))}
            <small className="field-note">
              Narrow raised-by rows to these importance levels. A group with no matching row is
              dropped.
            </small>
          </fieldset>
        </form>
        {summary.gap_count === 0 ? (
          <EmptyState>
            {filterActive
              ? "No requirements match these filters."
              : summary.evaluations_read === 0
                ? "No posting has been evaluated against this CV yet, so there is nothing to compare."
                : "Every requirement these evaluations settled was answered from this CV."}
          </EmptyState>
        ) : (
          <>
            <p className="field-note">{summary.grouping_rule}</p>
            {summary.omitted_count > 0 && (
              <p className="field-note">
                Listing {summary.gaps.length} of {summary.gap_count}, most raised first.{" "}
                {summary.omitted_count} more are not shown here, so this list is not the whole of
                what these evaluations left unanswered.
              </p>
            )}
            <ul className="record-list">
              {summary.gaps.map((gap) => (
                <li key={gap.text}>
                  <strong>{gap.text}</strong>
                  <span>
                    {gap.raised_by.length} posting{gap.raised_by.length === 1 ? "" : "s"}
                  </span>
                  <ul className="record-list">
                    {gap.raised_by.map((source, index) => (
                      <li key={`${source.job_posting_id}-${index}`}>
                        <Link
                          className="button button--quiet"
                          to={`/jobs/${source.job_posting_id}?tab=fit-report`}
                        >
                          {source.employer}: {source.title}
                        </Link>
                        <span>
                          {importanceLabel(source.importance)}, read on{" "}
                          {new Date(source.evaluated_at).toLocaleDateString()}
                        </span>
                        <RulingControls
                          currentRuling={source.ruling}
                          draft={draft}
                          onDraftChange={setDraft}
                          requirementKey={source.requirement_key}
                          requirementText={gap.text}
                          storedNote={notesByKey.get(source.requirement_key) ?? null}
                          workspaceId={workspaceId}
                        />
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </article>
  );
}
