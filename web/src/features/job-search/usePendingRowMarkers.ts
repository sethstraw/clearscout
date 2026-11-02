import { useState } from "react";

import type { PendingDiscovery, PendingMarkers } from "../../api/client";
import { useSetPendingMarkers } from "../../api/hooks";
import { useConfirmation } from "../../confirmations";

type Flags = { favorite: boolean; kept: boolean };

// The server refuses a longer keep note.
export const KEEP_NOTE_MAX = 300;

/** One row's Favorite and Keep state, shared by its title-line toggles and its keep note. */
export function usePendingRowMarkers(item: PendingDiscovery, label: string, workspaceId: string) {
  const mutation = useSetPendingMarkers(workspaceId);
  const confirm = useConfirmation();
  // The markers last sent stand in for the row until the refetch lands, so a second
  // click builds on the first rather than on the stale row.
  const shown: PendingMarkers =
    mutation.isPending && mutation.variables
      ? mutation.variables.body
      : { favorite: Boolean(item.favorite), kept: Boolean(item.kept), keep_note: item.keep_note };
  const { favorite, kept } = shown;
  const serverNote = item.keep_note ?? "";
  const [note, setNote] = useState(serverNote);
  const [syncedNote, setSyncedNote] = useState(serverNote);
  const [editing, setEditing] = useState(false);
  if (serverNote !== syncedNote && !editing && !mutation.isPending) {
    setSyncedNote(serverNote);
    setNote(serverNote);
  }

  const save = (next: Flags) => {
    const keepNote = next.kept ? note.trim() || null : null;
    if (!next.kept) setNote("");
    mutation.mutate({ pendingId: item.id, body: { ...next, keep_note: keepNote } });
  };
  const saveNote = () => {
    if (!kept || note.trim() === (shown.keep_note ?? "")) return;
    save({ favorite, kept });
  };
  const finishEditing = () => {
    setEditing(false);
    setSyncedNote(serverNote);
    saveNote();
  };
  const cancelEditing = () => {
    setEditing(false);
    setNote(shown.keep_note ?? "");
  };
  // A withdrawn row is listed only while a marker holds it.
  const leavesQueue = (next: Flags) => Boolean(item.withdrawn_at) && !next.favorite && !next.kept;
  const confirmRemoval = (next: Flags) => {
    const deletesNote = kept && !next.kept && note.trim() !== "";
    const leaves = leavesQueue(next);
    if (!deletesNote && !leaves) return Promise.resolve(true);
    const marker = next.favorite !== favorite ? "favorite" : "keep marker";
    return confirm({
      title: `Remove the ${marker} from ${label}?`,
      description: [
        deletesNote ? "This deletes the keep note." : "",
        leaves
          ? "This posting is no longer listed, so it leaves the pending queue once the marker is gone."
          : "",
      ]
        .filter(Boolean)
        .join(" "),
      confirmLabel: `Remove ${marker}`,
      danger: true,
    });
  };
  const toggle = async (next: Flags) => {
    if (await confirmRemoval(next)) save(next);
  };

  return {
    cancelEditing,
    editing,
    error: mutation.error,
    favorite,
    finishEditing,
    kept,
    label,
    note,
    setNote,
    startEditing: () => setEditing(true),
    toggle,
  };
}

export type RowMarkers = ReturnType<typeof usePendingRowMarkers>;
