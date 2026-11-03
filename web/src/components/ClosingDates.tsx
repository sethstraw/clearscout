import { useId, useRef, useState } from "react";

import type { components } from "../api/schema";
import { safeHttpUrl } from "../api/safeUrl";

type PublisherDate = components["schemas"]["PublisherClosingDateResponse"];

function calendarDate(now: Date): string {
  return `${now.getFullYear().toString().padStart(4, "0")}-${(now.getMonth() + 1).toString().padStart(2, "0")}-${now.getDate().toString().padStart(2, "0")}`;
}

function validDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith("0000-")) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

function publisherDatePassed(publisher: PublisherDate | null | undefined, now: Date): boolean {
  const value = publisher?.normalized_value;
  if (!value || publisher?.needs_clarification) return false;
  if (publisher.precision === "date") return validDate(value) && value < calendarDate(now);
  if (!publisher.timezone || !/(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return false;
  const instant = Date.parse(value);
  return Number.isFinite(instant) && instant < now.getTime();
}

// A board's own end of listing withdraws the row once it passes, unlike a closing date.
function statesListingEnd(publisher: PublisherDate | null | undefined): boolean {
  return Boolean(publisher?.statements?.some((item) => item.basis === "listed_until"));
}

function publisherLabel(publisher: PublisherDate | null | undefined): string {
  if (!publisher?.statements?.length) return "Not stated";
  if (publisher.needs_clarification) return "Needs clarification";
  return publisher.normalized_value || publisher.statements.map((item) => item.wording).join("; ");
}

export function ClosingDates({
  publisher,
  override,
  onSave,
  now = new Date(),
  label = "Closing dates",
  compact = false,
}: {
  publisher?: PublisherDate | null;
  override?: string | null;
  onSave?: (value: string | null) => Promise<void>;
  now?: Date;
  label?: string;
  compact?: boolean;
}) {
  const fieldId = useId();
  const [draft, setDraft] = useState(override || "");
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const details = useRef<HTMLDetailsElement>(null);
  const summary = useRef<HTMLElement>(null);
  const source = safeHttpUrl(publisher?.source_url || "");
  const currentDraft = dirty ? draft : override || "";
  const listingEnd = statesListingEnd(publisher);
  const publisherHeading = listingEnd ? "Listed on the board until" : "Publisher closing date";
  const passedNote = listingEnd
    ? "The board listing ended."
    : "Closing date passed; verify availability.";

  const save = async (value: string | null) => {
    if (!onSave || busyRef.current) return;
    if (value !== null && !validDate(value)) {
      setError("Enter a valid date as YYYY-MM-DD, or choose Use publisher date.");
      return;
    }
    busyRef.current = true;
    setBusy(true);
    setError("");
    setStatus("");
    try {
      await onSave(value);
      const ownsFocus = details.current?.contains(document.activeElement);
      setDirty(false);
      if (details.current) details.current.open = false;
      if (ownsFocus) summary.current?.focus();
      setStatus(
        value === null ? "Using the publisher closing date." : "Your closing date was saved.",
      );
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Date save failed. Try again.");
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  if (compact) {
    if (!override && !publisher?.statements?.length) return null;
    const value =
      override ||
      (publisher?.needs_clarification
        ? "Needs clarification"
        : publisher?.normalized_value || "Stated without a date");
    return (
      <section role="group" aria-label={label}>
        <p className="field-note">
          {override ? "Your closing date" : publisherHeading}: {value}
          {!override && publisher?.precision === "timestamp" && (
            <span> {publisher.timezone || "Timezone not stated"}</span>
          )}
        </p>
        {publisherDatePassed(publisher, now) && <p className="field-note">{passedNote}</p>}
        {override && validDate(override) && override < calendarDate(now) && (
          <p className="field-note">Your closing date has passed. It is a tracking reminder.</p>
        )}
      </section>
    );
  }

  return (
    <section role="group" aria-label={label}>
      <dl className="fact-list">
        <div>
          <dt>{publisherHeading}</dt>
          <dd>
            {publisherLabel(publisher)}
            {publisher?.precision === "timestamp" && (
              <span className="field-note"> {publisher.timezone || "Timezone not stated"}</span>
            )}
          </dd>
        </div>
        <div>
          <dt>Your closing date</dt>
          <dd>{override || "Not set; using publisher information"}</dd>
        </div>
      </dl>
      {publisherDatePassed(publisher, now) && <p className="field-note">{passedNote}</p>}
      {override && validDate(override) && override < calendarDate(now) && (
        <p className="field-note">Your closing date has passed. It is a tracking reminder.</p>
      )}
      {!!publisher?.statements?.length && (
        <details>
          <summary>Publisher date evidence</summary>
          <ul>
            {publisher.statements.map((statement, index) => (
              <li key={index}>
                {statement.wording}
                {statement.clarification && <p>{statement.clarification}</p>}
              </li>
            ))}
          </ul>
          {source && <a href={source}>Publisher date source</a>}
          {publisher.observed_at && <p className="field-note">Observed {publisher.observed_at}</p>}
        </details>
      )}
      {onSave && (
        <details ref={details}>
          <summary ref={summary}>Edit closing date</summary>
          <p className="field-note">
            Your date only helps tracking. It never closes or reopens a posting.
          </p>
          <form
            noValidate
            onSubmit={(event) => {
              event.preventDefault();
              void save(currentDraft);
            }}
          >
            <div className="field">
              <label htmlFor={fieldId}>Your closing date</label>
              <input
                id={fieldId}
                type="date"
                value={currentDraft}
                disabled={busy}
                aria-invalid={!!error}
                aria-describedby={error ? `${fieldId}-error` : undefined}
                onChange={(event) => {
                  setDraft(event.target.value);
                  setDirty(true);
                  setError("");
                }}
              />
            </div>
            {error && (
              <p id={`${fieldId}-error`} className="field-error" role="alert">
                {error}
              </p>
            )}
            <button
              type="submit"
              className="button button--quiet"
              aria-disabled={busy}
              aria-busy={busy || undefined}
            >
              Save date
            </button>
            <button
              type="button"
              className="button button--quiet"
              aria-disabled={busy}
              onClick={() => void save(null)}
            >
              Use publisher date
            </button>
          </form>
        </details>
      )}
      {status && <p role="status">{status}</p>}
    </section>
  );
}
