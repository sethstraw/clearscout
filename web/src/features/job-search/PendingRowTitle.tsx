import { useEffect, useRef, useState, type KeyboardEvent } from "react";

import { ApiError } from "../../api/client";
import { useCorrectPendingDiscoveryTitle } from "../../api/hooks";

type Props = {
  pendingId: string;
  workspaceId: string;
  currentTitle: string;
  displayTitle: string;
  url: string | null;
};

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  const raw = error as { message?: unknown; status?: unknown } | null;
  const message = typeof raw?.message === "string" ? raw.message.slice(0, 120) : "";
  const status = typeof raw?.status === "number" ? ` (HTTP ${raw.status})` : "";
  return message ? `${message}${status}` : "Could not save the title. Try again.";
}

export function PendingRowTitle({
  pendingId,
  workspaceId,
  currentTitle,
  displayTitle,
  url,
}: Props) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(currentTitle);
  const [errorMessage, setErrorMessage] = useState("");
  const submittingRef = useRef(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const editButtonRef = useRef<HTMLButtonElement | null>(null);
  const mutation = useCorrectPendingDiscoveryTitle(workspaceId);

  useEffect(() => {
    if (editing && inputRef.current) inputRef.current.focus();
  }, [editing]);

  const restoreFocus = () => {
    if (editButtonRef.current) editButtonRef.current.focus();
  };

  const openEditor = () => {
    setDraft(currentTitle);
    setErrorMessage("");
    setEditing(true);
  };

  const cancel = () => {
    setEditing(false);
    setErrorMessage("");
    submittingRef.current = false;
    restoreFocus();
  };

  const submit = () => {
    const value = draft.trim();
    if (!value) {
      setErrorMessage("A title is required.");
      return;
    }
    if (value === currentTitle) {
      cancel();
      return;
    }
    submittingRef.current = true;
    mutation.mutate(
      { pendingId, body: { title: value } },
      {
        onSuccess: () => {
          setEditing(false);
          setErrorMessage("");
          submittingRef.current = false;
          restoreFocus();
        },
        onError: (error) => {
          setErrorMessage(errorText(error));
          submittingRef.current = false;
        },
      },
    );
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") {
      event.preventDefault();
      event.stopPropagation();
      submit();
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      cancel();
    }
  };

  const onBlur = () => {
    if (submittingRef.current) return;
    cancel();
  };

  if (!editing) {
    if (!currentTitle) {
      return (
        <div className="pending-row-title__missing">
          <button
            aria-label={`Add title for ${displayTitle}`}
            className="pending-row-title__prompt"
            onClick={openEditor}
            ref={editButtonRef}
            type="button"
          >
            Add title...
          </button>
          <span className="field-note">{displayTitle}</span>
        </div>
      );
    }
    return (
      <>
        {url ? (
          <a href={url} rel="noreferrer" target="_blank">
            {displayTitle}
          </a>
        ) : (
          displayTitle
        )}{" "}
        <button
          className="button button--quiet"
          onClick={openEditor}
          ref={editButtonRef}
          type="button"
        >
          Edit title
        </button>
      </>
    );
  }

  return (
    <div className="pending-row-title__editor">
      <label className="visually-hidden" htmlFor={`pending-title-${pendingId}`}>
        Correct the title
      </label>
      <input
        aria-invalid={Boolean(errorMessage) || undefined}
        disabled={mutation.isPending}
        id={`pending-title-${pendingId}`}
        onBlur={onBlur}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={onKeyDown}
        ref={inputRef}
        type="text"
        value={draft}
      />
      <button
        className="button button--quiet"
        disabled={mutation.isPending}
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => {
          submittingRef.current = true;
          submit();
        }}
        type="button"
      >
        Save
      </button>
      <button
        className="button button--quiet"
        disabled={mutation.isPending}
        onMouseDown={(event) => event.preventDefault()}
        onClick={cancel}
        type="button"
      >
        Cancel
      </button>
      {errorMessage ? (
        <p className="field-error" role="alert">
          {errorMessage}
        </p>
      ) : null}
    </div>
  );
}
