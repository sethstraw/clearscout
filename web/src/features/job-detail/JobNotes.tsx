import { api } from "../../api/client";
import { useCreateNote, useNotes, useWorkspaceMutation } from "../../api/hooks";
import { ConfirmedActionButton } from "../../components/ConfirmedActionButton";
import { MarkdownView } from "../../components/MarkdownView";
import { NoteComposer } from "../../components/NoteComposer";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";

export function JobNotes({ jobId, workspaceId }: { jobId: string; workspaceId: string }) {
  const notes = useNotes(workspaceId, "job_posting", jobId);
  const {
    title,
    setTitle,
    body,
    setBody,
    mutation: create,
    submit,
  } = useCreateNote(workspaceId, { entityType: "job_posting", entityId: jobId });
  const update = useWorkspaceMutation(
    workspaceId,
    ({ id, pinned }: { id: string; pinned: boolean }) => {
      const note = notes.data?.find((item) => item.id === id);
      if (!note) throw new Error("Note no longer exists.");
      return api.updateNote(id, {
        workspace_id: workspaceId,
        title: note.title,
        body: note.body,
        pinned,
      });
    },
  );
  const archive = useWorkspaceMutation(workspaceId, (id: string) =>
    api.archiveNote(id, workspaceId),
  );
  if (notes.isLoading) return <LoadingState label="Opening notes" />;
  if (notes.isError) return <ErrorState error={notes.error} />;
  return (
    <section className="notes-layout">
      <article className="panel">
        <h2>Add note</h2>
        <aside className="non-evidence-note">
          <strong>Notes are not evidence</strong>
          <p>
            They never feed a score, match, or generated claim. Use Explore a gap to confirm a
            career fact and add it to the master CV.
          </p>
        </aside>
        <NoteComposer
          body={body}
          error={create.isError ? create.error.message : null}
          onBody={setBody}
          onSubmit={submit}
          onTitle={setTitle}
          rows={10}
          saving={create.isPending}
          title={title}
        />
      </article>
      <article className="panel">
        <h2>Job timeline notes</h2>
        {!notes.data?.length && <EmptyState>No notes attached to this job.</EmptyState>}
        <ol className="note-list">
          {notes.data?.map((note) => (
            <li key={note.id}>
              <header>
                <div>
                  {note.pinned && <small>Pinned</small>}
                  <strong>{note.title}</strong>
                  <time>{new Date(note.updated_at).toLocaleString()}</time>
                </div>
                <div>
                  <button
                    onClick={() => update.mutate({ id: note.id, pinned: !note.pinned })}
                    type="button"
                  >
                    {note.pinned ? "Unpin" : "Pin"}
                  </button>
                  <ConfirmedActionButton
                    onConfirm={() => archive.mutate(note.id)}
                    options={{
                      title: "Archive this note?",
                      description:
                        "It will leave the active job timeline but remain in durable local history.",
                      confirmLabel: "Archive note",
                      danger: true,
                    }}
                  >
                    Archive
                  </ConfirmedActionButton>
                </div>
              </header>
              <MarkdownView>{note.body}</MarkdownView>
            </li>
          ))}
        </ol>
      </article>
    </section>
  );
}
