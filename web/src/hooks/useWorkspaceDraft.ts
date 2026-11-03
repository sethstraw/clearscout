import { useState } from "react";

type SavedDraft<T> = { value: T; version: number };

function readDraft<T>(key: string): SavedDraft<T> | null {
  try {
    const value: unknown = JSON.parse(window.sessionStorage.getItem(key) ?? "null");
    if (
      value !== null &&
      typeof value === "object" &&
      "version" in value &&
      Number.isInteger(value.version) &&
      Number(value.version) >= 0 &&
      "value" in value &&
      value.value !== null &&
      typeof value.value === "object"
    )
      return value as SavedDraft<T>;
  } catch {
    // An unavailable browser store leaves this form usable in memory.
  }
  return null;
}

/** Keep unsaved workspace forms and their original concurrency version in this tab. */
export function useWorkspaceDraft<T>(workspaceId: string, form: string, version: number) {
  const key = `clearscout.draft.v1.${workspaceId}.${form}`;
  const [drafts, setDrafts] = useState<Record<string, SavedDraft<T> | null>>({});
  const [storageError, setStorageError] = useState(false);
  const edits = key in drafts ? (drafts[key] ?? null) : readDraft<T>(key);
  const setEdits = (value: T | null, reviewedVersion?: number) => {
    const next =
      value === null ? null : { value, version: reviewedVersion ?? edits?.version ?? version };
    setDrafts((current) => ({ ...current, [key]: next }));
    try {
      if (next === null) window.sessionStorage.removeItem(key);
      else window.sessionStorage.setItem(key, JSON.stringify(next));
      setStorageError(false);
    } catch {
      setStorageError(true);
    }
  };
  return {
    edits: edits?.value ?? null,
    expectedVersion: edits?.version ?? version,
    setEdits,
    storageError,
  };
}
