import { useEffect, useId, useRef, useState } from "react";

import {
  useAddSource,
  useArchiveSource,
  useEditSource,
  usePreviewSource,
  useRevalidateSource,
  useSources,
} from "../../api/hooks";
import { safeHttpUrl } from "../../api/safeUrl";
import { ApiError } from "../../api/http";
import { ConfirmedActionButton } from "../../components/ConfirmedActionButton";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { Toggletip } from "../../components/Toggletip";
import type { Source, SourceEdit } from "../../api/client";
import type { SourcePreview } from "../../api/client/types";
import type { components } from "../../api/schema";
import { statusEntry } from "../../components/statuses";
import { scanDay } from "./scanDay";

type SourceKind = components["schemas"]["SourceKind"];

const SOURCE_KIND = {
  employer_board: "Employer board",
  aggregator: "Aggregator",
} satisfies Record<SourceKind, string>;

function lastRead(source: Source) {
  if (!source.last_status || !source.last_scanned_on) return "Never read.";
  return `${statusEntry(source.last_status).label} on ${scanDay(source.last_scanned_on)}`;
}

function platformNote(source: Source) {
  return source.platform
    ? `Recognized platform: ${source.platform}. Check the last read result for permission and coverage.`
    : "No board platform recognized. Check the last read result; an individual posting link may still be readable.";
}

/** Ids that tie a control to its note and, once a save is refused, to the refusal. */
function useFieldDescription(refused: boolean) {
  const noteId = useId();
  const errorId = useId();
  return {
    noteId,
    errorId,
    describedBy: refused ? `${noteId} ${errorId}` : noteId,
    invalid: refused || undefined,
  };
}

interface SourceTextFieldProps {
  source: Source;
  workspaceId: string;
  label: string;
  note: string;
  saveLabel: string;
  /** The stored value; callers key the field on it so a refetch replaces the draft. */
  stored: string;
  change: (value: string) => SourceEdit;
  /** Whether an empty save is a change, which clears the stored value. */
  clearable?: boolean;
  type?: "url";
}

function SourceTextField({
  source,
  workspaceId,
  label,
  note,
  saveLabel,
  stored,
  change,
  clearable = false,
  type,
}: SourceTextFieldProps) {
  const [value, setValue] = useState(stored);
  const edit = useEditSource(workspaceId);
  const fieldId = useId();
  const description = useFieldDescription(edit.isError);
  const trimmed = value.trim();
  const changed = trimmed !== stored && (clearable || trimmed !== "");
  return (
    <div className="source-field">
      <label htmlFor={fieldId}>{label}</label>
      <p className="field-note" id={description.noteId}>
        {note}
      </p>
      <div className="source-field__control">
        <input
          aria-describedby={description.describedBy}
          aria-invalid={description.invalid}
          id={fieldId}
          onChange={(event) => setValue(event.target.value)}
          type={type}
          value={value}
        />
        <button
          className="button button--quiet"
          disabled={!changed || edit.isPending}
          onClick={() => edit.mutate({ sourceId: source.id, ...change(trimmed) })}
          type="button"
        >
          {saveLabel}
        </button>
      </div>
      {edit.isError && (
        <p className="field-error" id={description.errorId} role="alert">
          {edit.error.message}
        </p>
      )}
    </div>
  );
}

function TermsFields({ source, workspaceId }: { source: Source; workspaceId: string }) {
  const [condition, setCondition] = useState(source.terms.condition);
  const [termsUrl, setTermsUrl] = useState(source.terms.url);
  const edit = useEditSource(workspaceId);
  const conditionId = useId();
  const termsUrlId = useId();
  const conditionNote = useFieldDescription(edit.isError);
  const termsUrlNoteId = useId();
  const changed =
    condition.trim() !== source.terms.condition || termsUrl.trim() !== source.terms.url;
  return (
    <div className="source-field">
      <label htmlFor={conditionId}>Condition on reading it</label>
      <p className="field-note" id={conditionNote.noteId}>
        What the operator published about automated reading. A condition that refuses reading is a
        reason not to sweep this board; ClearScout asks you to confirm one before it turns the sweep
        on.
      </p>
      <textarea
        aria-describedby={conditionNote.describedBy}
        aria-invalid={conditionNote.invalid}
        id={conditionId}
        onChange={(event) => setCondition(event.target.value)}
        rows={3}
        value={condition}
      />
      <label htmlFor={termsUrlId}>Where that condition is published</label>
      <p className="field-note" id={termsUrlNoteId}>
        The page the wording above was read from.
      </p>
      <div className="source-field__control">
        <input
          aria-describedby={
            edit.isError ? `${termsUrlNoteId} ${conditionNote.errorId}` : termsUrlNoteId
          }
          aria-invalid={conditionNote.invalid}
          id={termsUrlId}
          onChange={(event) => setTermsUrl(event.target.value)}
          value={termsUrl}
        />
        <button
          className="button button--quiet"
          disabled={!changed || edit.isPending}
          onClick={() =>
            edit.mutate({
              sourceId: source.id,
              terms: { condition: condition.trim(), url: termsUrl.trim() },
            })
          }
          type="button"
        >
          Save condition
        </button>
      </div>
      {edit.isError && (
        <p className="field-error" id={conditionNote.errorId} role="alert">
          {edit.error.message}
        </p>
      )}
    </div>
  );
}

/** A source stating a condition is not swept until that condition has been shown. */
function SweptControl({ source, workspaceId }: { source: Source; workspaceId: string }) {
  const [asking, setAsking] = useState(false);
  const edit = useEditSource(workspaceId);
  const set = (enabled: boolean) => {
    setAsking(false);
    edit.mutate({ sourceId: source.id, enabled });
  };
  return (
    <>
      <label className="source-swept">
        <input
          aria-label={`Sweep ${source.name}`}
          checked={source.enabled}
          onChange={(event) => {
            if (event.target.checked && source.terms.condition) {
              setAsking(true);
              return;
            }
            set(event.target.checked);
          }}
          type="checkbox"
        />
        {source.enabled ? "Swept" : "Not swept"}
      </label>
      {edit.isError && <p className="field-error">{edit.error.message}</p>}
      {asking && (
        <div className="source-consent">
          <p className="field-note">{source.terms.condition}</p>
          <div className="button-row">
            <button
              className="button button--primary"
              disabled={edit.isPending}
              onClick={() => set(true)}
              type="button"
            >
              Turn on {source.name}
            </button>
            <button className="button button--quiet" onClick={() => setAsking(false)} type="button">
              Cancel
            </button>
          </div>
        </div>
      )}
    </>
  );
}

function SourceCard({ source, workspaceId }: { source: Source; workspaceId: string }) {
  const archive = useArchiveSource(workspaceId);
  const revalidate = useRevalidateSource(workspaceId);
  const headingId = useId();
  const link = safeHttpUrl(source.url);
  return (
    <li aria-labelledby={headingId} className="source-card">
      <div className="source-card__header">
        <div>
          <h3 id={headingId}>
            {link ? (
              <a href={link} rel="noreferrer" target="_blank">
                {source.name}
              </a>
            ) : (
              source.name
            )}
          </h3>
          <p className="field-note">
            {SOURCE_KIND[source.source_type]}. Priority {source.priority}. {platformNote(source)}
          </p>
        </div>
        <div className="source-card__actions">
          <SweptControl source={source} workspaceId={workspaceId} />
          <button
            className="button button--quiet"
            disabled={revalidate.isPending}
            onClick={() => revalidate.mutate(source.id)}
            type="button"
          >
            {revalidate.isPending ? "Revalidating" : "Revalidate"}
          </button>
          <ConfirmedActionButton
            className="button button--quiet"
            disabled={archive.isPending}
            onConfirm={() => archive.mutate(source.id)}
            options={{
              title: `Retire ${source.name}?`,
              description:
                "No scan reads it again and it leaves this list. The scans it has already recorded are kept, and adding the same name and address again brings the source back with its history.",
              confirmLabel: "Retire source",
              danger: true,
            }}
          >
            Retire
          </ConfirmedActionButton>
        </div>
      </div>
      <div className="source-card__fields">
        <SourceTextField
          change={(name) => ({ name })}
          key={`name:${source.name}`}
          label="Name"
          note="What this board and the employer behind it are called here. Discoveries staged under the old name take the new one. A name another source or employer already holds is refused rather than merged. A rename is refused while a search or evaluation is working on one of those discoveries; try again when it finishes."
          saveLabel="Save name"
          source={source}
          stored={source.name}
          workspaceId={workspaceId}
        />
        <SourceTextField
          change={(url) => ({ url })}
          key={`url:${source.url}`}
          label="Board or careers address"
          note="Where a scan looks for this employer's openings."
          saveLabel="Save address"
          source={source}
          stored={source.url}
          workspaceId={workspaceId}
        />
        {source.employer !== null && (
          <SourceTextField
            change={(careers_url) => ({ careers_url })}
            clearable
            key={`careers:${source.careers_url ?? ""}`}
            label="Company careers page"
            note="The employer's own careers page, which research reads to describe the company in a cover letter. A job board or applicant tracking system address is refused, and an empty save removes the page. Research reads it; scans read the address above."
            saveLabel="Save careers page"
            source={source}
            stored={source.careers_url ?? ""}
            type="url"
            workspaceId={workspaceId}
          />
        )}
        <TermsFields
          key={`terms:${source.terms.condition}\u0000${source.terms.url}`}
          source={source}
          workspaceId={workspaceId}
        />
      </div>
      <p className="source-card__read">
        Last read: {lastRead(source)}
        {source.last_notes && <span className="field-note"> {source.last_notes}</span>}
      </p>
      {archive.isError && <p className="field-error">{archive.error.message}</p>}
      {revalidate.isError && <p className="field-error">{revalidate.error.message}</p>}
    </li>
  );
}

interface SourceDraft {
  id: number;
  name: string;
  url: string;
  sourceType: SourceKind;
  condition: string;
  termsUrl: string;
  preview: SourcePreview | null;
  outcome: "added" | "duplicate" | "unsupported" | "failed" | null;
  detail: string;
}

function emptySourceDraft(id: number): SourceDraft {
  return {
    id,
    name: "",
    url: "",
    sourceType: "employer_board",
    condition: "",
    termsUrl: "",
    preview: null,
    outcome: null,
    detail: "",
  };
}

function SourceDraftFields({
  draft,
  number,
  busy,
  pending,
  change,
  validate,
  add,
  nameInputRef,
}: {
  draft: SourceDraft;
  number: number;
  busy: boolean;
  pending: "validate" | "add" | null;
  change: (update: Partial<SourceDraft>) => void;
  validate: () => void;
  add: (beforeSaved: () => void) => void;
  nameInputRef: (element: HTMLInputElement | null) => void;
}) {
  const id = useId();
  const resultId = `${id}-result`;
  const resultDescription = draft.outcome ? resultId : undefined;
  const resultRef = useRef<HTMLDivElement>(null);
  const focusResult = useRef(false);
  const saved = draft.outcome === "added" || draft.outcome === "unsupported";
  useEffect(() => {
    if (saved && focusResult.current) {
      resultRef.current?.focus();
      focusResult.current = false;
    }
  }, [saved]);
  const resultLabel = {
    added: "Added",
    duplicate: "Duplicate",
    unsupported: "Saved; board reader not recognized",
    failed: "Failed",
  };
  return (
    <fieldset disabled={saved}>
      <legend>Source {number}</legend>
      <p className="source-add__step-label">Step 1: Validate the board address</p>
      <div className="source-add__fields">
        <div className="compact-field">
          <label htmlFor={`${id}-url`}>Board or careers address</label>
          <input
            id={`${id}-url`}
            disabled={busy}
            aria-describedby={`${id}-url-note ${resultDescription ?? ""}`}
            value={draft.url}
            onChange={(event) => change({ url: event.target.value })}
          />
          <p className="field-note" id={`${id}-url-note`}>
            Where the openings are listed. A careers page is read once to find the board behind it.
          </p>
        </div>
        <div className="compact-field">
          <label htmlFor={`${id}-kind`}>Kind</label>
          <select
            id={`${id}-kind`}
            disabled={busy}
            aria-describedby={`${id}-kind-note ${resultDescription ?? ""}`}
            value={draft.sourceType}
            onChange={(event) => change({ sourceType: event.target.value as SourceKind })}
          >
            {Object.entries(SOURCE_KIND).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
          <p className="field-note" id={`${id}-kind-note`}>
            An employer board lists one employer. An aggregator lists many, and each opening keeps
            the employer it names.
          </p>
        </div>
      </div>
      <button
        className="button button--quiet"
        disabled={!draft.url.trim() || saved}
        aria-disabled={busy || saved}
        aria-busy={pending === "validate"}
        onClick={validate}
        type="button"
      >
        {pending === "validate" ? "Validating" : "Validate"}
      </button>
      {draft.preview && (
        <p className="field-note" role="status">
          {draft.preview.note}
        </p>
      )}
      {draft.preview && (
        <fieldset className="source-add__details">
          <legend>Step 2: Name the board</legend>
          <div className="source-add__details-grid">
            <div className="compact-field">
              <label htmlFor={`${id}-name`}>Name</label>
              <input
                autoComplete="organization"
                id={`${id}-name`}
                ref={nameInputRef}
                disabled={busy}
                aria-describedby={resultDescription}
                value={draft.name}
                onChange={(event) => change({ name: event.target.value })}
              />
            </div>
            <details className="scan-options source-add__terms">
              <summary>Advanced: reading condition</summary>
              <p className="field-note source-add__evidence-note" id={`${id}-evidence-note`}>
                Record any published condition on automated reading and where you found it. Leave
                both blank if you have not checked. You confirm a stated condition before the board
                is swept.
              </p>
              <div className="compact-field">
                <label htmlFor={`${id}-condition`}>Condition on reading it</label>
                <textarea
                  id={`${id}-condition`}
                  disabled={busy}
                  aria-describedby={`${id}-evidence-note ${resultDescription ?? ""}`}
                  rows={2}
                  value={draft.condition}
                  onChange={(event) => change({ condition: event.target.value })}
                />
              </div>
              <div className="compact-field">
                <label htmlFor={`${id}-terms`}>Published at</label>
                <input
                  id={`${id}-terms`}
                  disabled={busy}
                  aria-describedby={`${id}-evidence-note ${resultDescription ?? ""}`}
                  inputMode="url"
                  type="url"
                  placeholder="robots.txt or terms page"
                  value={draft.termsUrl}
                  onChange={(event) => change({ termsUrl: event.target.value })}
                />
              </div>
            </details>
            <button
              className="button button--primary source-add__submit"
              disabled={!draft.name.trim() || saved || draft.outcome === "duplicate"}
              aria-disabled={busy || saved}
              aria-busy={pending === "add"}
              onClick={(event) => {
                const launcher = event.currentTarget;
                add(() => {
                  focusResult.current = document.activeElement === launcher;
                });
              }}
              type="button"
            >
              {pending === "add" ? "Adding source" : "Add source"}
            </button>
          </div>
        </fieldset>
      )}
      {draft.outcome && (
        <div
          id={resultId}
          ref={resultRef}
          tabIndex={-1}
          className={draft.outcome === "failed" ? "field-error" : "field-note"}
          role={draft.outcome === "failed" ? "alert" : "status"}
        >
          <strong>{resultLabel[draft.outcome]}</strong>
          <p>{draft.detail}</p>
        </div>
      )}
    </fieldset>
  );
}

/** Add, correct, turn off, and retire the sources the sweep reads. */
export function SourceManager({ workspaceId }: { workspaceId: string }) {
  const sources = useSources(workspaceId);
  const add = useAddSource(workspaceId);
  const preview = usePreviewSource(workspaceId);
  const [drafts, setDrafts] = useState<SourceDraft[]>(() => [emptySourceDraft(0)]);
  const [pending, setPending] = useState<{ id: number; action: "validate" | "add" } | null>(null);
  const busy = useRef(false);
  const nextId = useRef(1);
  const nameInputs = useRef(new Map<number, HTMLInputElement>());
  const nameFocus = useRef<{ id: number; launcher: HTMLElement } | null>(null);
  const items = sources.data?.items ?? [];
  useEffect(() => {
    const target = nameFocus.current;
    if (pending === null && target) {
      if (document.activeElement === target.launcher) nameInputs.current.get(target.id)?.focus();
      nameFocus.current = null;
    }
  }, [pending]);

  const updateDraft = (id: number, update: Partial<SourceDraft>) => {
    setDrafts((current) =>
      current.map((draft) => (draft.id === id ? { ...draft, ...update } : draft)),
    );
  };

  const changeDraft = (id: number, update: Partial<SourceDraft>) => {
    if (busy.current) return;
    updateDraft(id, {
      ...update,
      outcome: null,
      detail: "",
      ...(update.url !== undefined || update.sourceType !== undefined ? { preview: null } : {}),
    });
  };

  const runPreview = async (rows: SourceDraft[], launcher?: HTMLElement) => {
    if (busy.current || !rows.length) return;
    busy.current = true;
    let firstName: number | null = null;
    try {
      for (const draft of rows) {
        setPending({ id: draft.id, action: "validate" });
        try {
          const result = await preview.mutateAsync({
            url: draft.url.trim(),
            source_type: draft.sourceType,
          });
          updateDraft(draft.id, { preview: result, outcome: null, detail: "" });
          firstName ??= draft.id;
        } catch (error) {
          updateDraft(draft.id, {
            preview: null,
            outcome: "failed",
            detail: error instanceof Error ? error.message : "The source could not be checked.",
          });
        }
      }
    } finally {
      if (launcher && firstName !== null) nameFocus.current = { id: firstName, launcher };
      busy.current = false;
      setPending(null);
    }
  };

  const submit = async (rows: SourceDraft[], beforeSaved?: () => void) => {
    if (busy.current || !rows.length) return;
    busy.current = true;
    const seen = new Set(items.map((source) => source.url));
    try {
      for (const draft of rows) {
        setPending({ id: draft.id, action: "add" });
        const url = draft.url.trim();
        const resolvedUrl = draft.preview!.resolved_url;
        if (seen.has(url) || seen.has(resolvedUrl)) {
          updateDraft(draft.id, {
            outcome: "duplicate",
            detail: "This board is already saved. Its history and settings are kept.",
          });
          continue;
        }
        try {
          const source = await add.mutateAsync({
            name: draft.name.trim(),
            url,
            source_type: draft.sourceType,
            terms: { condition: draft.condition.trim(), url: draft.termsUrl.trim() },
          });
          seen.add(url);
          seen.add(resolvedUrl);
          seen.add(source.url);
          beforeSaved?.();
          updateDraft(draft.id, {
            outcome: source.platform ? "added" : "unsupported",
            detail: source.platform
              ? "Source saved. Check its read result for permission and coverage."
              : "Saved for tracking without a recognized board reader. Try an individual posting link from Add a posting; published reading policy still applies.",
          });
        } catch (error) {
          updateDraft(draft.id, {
            outcome: error instanceof ApiError && error.status === 409 ? "duplicate" : "failed",
            detail: error instanceof Error ? error.message : "The source could not be added.",
          });
        }
      }
    } finally {
      busy.current = false;
      setPending(null);
    }
  };

  const editable = drafts.filter(
    (draft) => draft.outcome !== "added" && draft.outcome !== "unsupported",
  );
  const unchecked = editable.filter((draft) => draft.url.trim() && !draft.preview);
  const ready = editable.filter(
    (draft) => draft.name.trim() && draft.preview && draft.outcome !== "duplicate",
  );

  if (sources.isLoading) return <LoadingState label="Reading board sources" />;
  if (sources.isError) return <ErrorState error={sources.error} />;

  return (
    <section className="panel" aria-labelledby="source-manager-heading">
      <header>
        <div>
          <h2 id="source-manager-heading">Your boards</h2>
          <p>
            Each scan reads the boards you save here.{" "}
            <Toggletip label="When a board cannot be read">
              A recognized hiring platform does not promise permission or full coverage. Check each
              board&apos;s last read and reading condition. A board that cannot be read stays
              listed; add its postings one link at a time from Review instead.
            </Toggletip>
          </p>
        </div>
      </header>
      <section aria-labelledby="source-add-heading" className="panel-body source-add">
        <h3 id="source-add-heading">Add a board</h3>
        <p className="field-note">Validate the address, then name the board and add it.</p>
        <p className="field-note" id="source-ready-help">
          Only validated rows with a name are ready. Saved and duplicate rows are skipped.
        </p>
        {drafts.map((draft, index) => (
          <SourceDraftFields
            key={draft.id}
            draft={draft}
            number={index + 1}
            busy={pending !== null}
            pending={pending?.id === draft.id ? pending.action : null}
            change={(update) => changeDraft(draft.id, update)}
            validate={() => void runPreview([draft])}
            add={(beforeSaved) => void submit([draft], beforeSaved)}
            nameInputRef={(element) => {
              if (element) nameInputs.current.set(draft.id, element);
              else nameInputs.current.delete(draft.id);
            }}
          />
        ))}
        <div className="button-row">
          <button
            className="button button--quiet"
            type="button"
            disabled={pending !== null}
            onClick={() => {
              const id = nextId.current++;
              setDrafts((current) => [...current, emptySourceDraft(id)]);
            }}
          >
            Add another source
          </button>
          {drafts.length > 1 && (
            <>
              <button
                className="button button--quiet"
                type="button"
                aria-disabled={pending !== null || !unchecked.length}
                aria-busy={pending?.action === "validate"}
                onClick={(event) => void runPreview(unchecked, event.currentTarget)}
              >
                Validate sources
              </button>
              <button
                className="button button--primary"
                type="button"
                aria-describedby="source-ready-help"
                aria-disabled={pending !== null || !ready.length}
                aria-busy={pending?.action === "add"}
                onClick={() => void submit(ready)}
              >
                Add ready sources ({ready.length})
              </button>
            </>
          )}
        </div>
      </section>
      {items.length ? (
        <ul className="source-list">
          {items.map((source) => (
            <SourceCard key={source.id} source={source} workspaceId={workspaceId} />
          ))}
        </ul>
      ) : (
        <EmptyState>No boards yet.</EmptyState>
      )}
    </section>
  );
}
