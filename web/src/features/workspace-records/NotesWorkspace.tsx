import { useState } from "react";

import { api } from "../../api/client";
import type { Note } from "../../api/client";
import { useCreateNote, useNotes, useWorkspaceMutation } from "../../api/hooks";
import { MarkdownView } from "../../components/MarkdownView";
import { NoteComposer } from "../../components/NoteComposer";
import { ErrorState, LoadingState } from "../../components/States";
import { ArchiveButton, EditableRecordRow, RecordSection } from "./RecordComponents";

export function NotesWorkspace({ workspaceId }: { workspaceId: string }) {
  const [search, setSearch] = useState("");
  const { title, setTitle, body, setBody, submit } = useCreateNote(
    workspaceId,
    { entityType: "workspace", entityId: null },
    { successMessage: "Note saved." },
  );
  const notes = useNotes(workspaceId, "", "", search);
  if (notes.isLoading) return <LoadingState label="Searching notes" />;
  if (notes.isError) return <ErrorState error={notes.error} />;
  return (
    <section className="detail-grid">
      <article className="panel">
        <h2>Add workspace note</h2>
        <aside className="non-evidence-note">
          <strong>Not evidence</strong>
          <p>Nothing written here can justify a score or enter a generated resume.</p>
        </aside>
        <NoteComposer
          body={body}
          onBody={setBody}
          onSubmit={submit}
          onTitle={setTitle}
          title={title}
        />
      </article>
      <RecordSection
        empty={
          search
            ? "No note matches that search."
            : "No notes yet. Write one on the left, or attach one to a job from its Notes tab."
        }
        hasRecords={Boolean(notes.data?.length)}
        header={
          <header>
            <div>
              <h2>All notes</h2>
              <p>Search and manage workspace and attached notes.</p>
            </div>
            <label className="compact-field">
              Search
              <input
                onChange={(event) => setSearch(event.target.value)}
                type="search"
                value={search}
              />
            </label>
          </header>
        }
        listClassName="note-list"
        ordered
      >
        {notes.data?.map((note) => (
          <NoteRow key={note.id} note={note} workspaceId={workspaceId} />
        ))}
      </RecordSection>
    </section>
  );
}

function NoteRow({ note, workspaceId }: { note: Note; workspaceId: string }) {
  const [title, setTitle] = useState(note.title);
  const [body, setBody] = useState(note.body);
  const [pinned, setPinned] = useState(note.pinned);
  const save = useWorkspaceMutation(
    workspaceId,
    () => api.updateNote(note.id, { workspace_id: workspaceId, title, body, pinned }),
    { successMessage: "Note changes saved." },
  );
  const archive = useWorkspaceMutation(workspaceId, () => api.archiveNote(note.id, workspaceId), {
    successMessage: "Note archived.",
  });
  return (
    <EditableRecordRow
      editLabel="Edit details"
      editor={
        <form
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate(undefined);
          }}
        >
          <label>
            Title
            <input onChange={(event) => setTitle(event.target.value)} required value={title} />
          </label>
          <label>
            Markdown note
            <textarea
              onChange={(event) => setBody(event.target.value)}
              required
              rows={6}
              value={body}
            />
          </label>
          <label>
            <input
              checked={pinned}
              onChange={(event) => setPinned(event.target.checked)}
              type="checkbox"
            />{" "}
            Pin this note
          </label>
          <div className="button-row">
            <button className="button button--primary" type="submit">
              Save changes
            </button>
            <ArchiveButton
              className="button button--quiet"
              label="note"
              onArchive={() => archive.mutate(undefined)}
            />
          </div>
        </form>
      }
    >
      <header>
        <div>
          <strong>
            {note.pinned ? "Pinned: " : ""}
            {note.title}
          </strong>
          <small>
            {note.entity_type} - {new Date(note.updated_at).toLocaleString()}
          </small>
        </div>
      </header>
      <MarkdownView>{note.body}</MarkdownView>
    </EditableRecordRow>
  );
}
