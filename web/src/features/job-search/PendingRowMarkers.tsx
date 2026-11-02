import { useEffect, useRef } from "react";
import type { MouseEvent } from "react";

import { KEEP_NOTE_MAX, type RowMarkers } from "./usePendingRowMarkers";

// A pressed toggle leaves focus in the note, so a mid-edit click sends one request
// carrying the typed note instead of racing a blur save.
const keepFocus = (event: MouseEvent<HTMLButtonElement>) => event.preventDefault();

/** Favorite and Keep toggles for the role's title line. */
export function PendingRowMarkerToggles({ markers }: { markers: RowMarkers }) {
  const { favorite, kept, label, toggle } = markers;
  return (
    <span className="pending-row-markers">
      <button
        aria-label={`Favorite ${label}`}
        aria-pressed={favorite}
        className="button button--quiet"
        onClick={() => void toggle({ favorite: !favorite, kept })}
        onMouseDown={keepFocus}
        type="button"
      >
        {favorite ? "Favorited" : "Favorite"}
      </button>
      <button
        aria-label={`Keep ${label}`}
        aria-pressed={kept}
        className="button button--quiet"
        onClick={() => void toggle({ favorite, kept: !kept })}
        onMouseDown={keepFocus}
        type="button"
      >
        {kept ? "Kept" : "Keep"}
      </button>
      {markers.error ? (
        <span className="field-error" role="alert">
          Marker not saved: {markers.error.message}
        </span>
      ) : null}
    </span>
  );
}

/** A kept row's one-line note, read as text and edited in place on click. */
export function PendingRowKeepNote({ markers }: { markers: RowMarkers }) {
  const { cancelEditing, editing, finishEditing, kept, label, note, setNote, startEditing } =
    markers;
  const inputRef = useRef<HTMLInputElement | null>(null);
  const shownRef = useRef<HTMLButtonElement | null>(null);
  const refocus = useRef(false);
  // Escape unmounts the focused input, and the blur that can follow must not save.
  const cancelled = useRef(false);
  useEffect(() => {
    if (editing) {
      cancelled.current = false;
      inputRef.current?.focus();
    } else if (refocus.current) {
      refocus.current = false;
      shownRef.current?.focus();
    }
  }, [editing]);
  if (!kept) return null;
  return (
    <p className="field-note pending-keep-note">
      {editing ? (
        <input
          aria-label={`Keep note for ${label}`}
          maxLength={KEEP_NOTE_MAX}
          onBlur={() => {
            if (!cancelled.current) finishEditing();
          }}
          onChange={(event) => setNote(event.target.value.replace(/[\r\n]+/g, " "))}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              cancelled.current = true;
              refocus.current = true;
              cancelEditing();
              return;
            }
            if (event.key !== "Enter") return;
            refocus.current = true;
            // Blurring first saves once; unmounting a focused input can fire a second blur.
            event.currentTarget.blur();
          }}
          placeholder="Why you are keeping it"
          ref={inputRef}
          type="text"
          value={note}
        />
      ) : (
        // The name leads with the visible note, so a spoken "Kept" command finds it.
        <button
          className={note.trim() ? "pending-keep-note__text" : "button button--quiet"}
          onClick={startEditing}
          ref={shownRef}
          type="button"
        >
          Kept: {note.trim() || "add a note"}
          <span className="visually-hidden">. Edit keep note for {label}</span>
        </button>
      )}
    </p>
  );
}
