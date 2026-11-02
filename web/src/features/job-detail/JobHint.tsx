import { useState } from "react";

import { api } from "../../api/client";
import { useWorkspaceMutation } from "../../api/hooks";
import { ErrorState } from "../../components/States";

export function JobHint({
  jobId,
  workspaceId,
  hint,
  enabled,
}: {
  jobId: string;
  workspaceId: string;
  hint: string | null;
  enabled: boolean;
}) {
  const [draft, setDraft] = useState(hint ?? "");
  const [lastServerHint, setLastServerHint] = useState(hint);
  if (hint !== lastServerHint) {
    setLastServerHint(hint);
    setDraft(hint ?? "");
  }
  const save = useWorkspaceMutation(workspaceId, (next: string) =>
    api.setPostingHint({ jobId, workspaceId, hint: next.trim() ? next : null }),
  );
  if (!enabled) return null;
  const dirty = (draft.trim() || null) !== (hint ?? null);
  return (
    <section aria-labelledby="hint-heading" className="job-notes">
      <h3 id="hint-heading">Tailor hint</h3>
      <p className="hint-caveat">
        Context for the model, not evidence. The tailor and cover letter use this to shape framing;
        no line in the output cites it, and the fit report never asserts it.
      </p>
      <label className="hint-editor" htmlFor="posting-hint-body">
        <span className="hint-label">
          What should the model know when writing this application?
        </span>
        <textarea
          id="posting-hint-body"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          rows={4}
          spellCheck
          maxLength={4000}
          placeholder="e.g. Emphasise the payments platform work. Skip the volunteer section."
        />
      </label>
      <div className="hint-actions">
        <button
          type="button"
          disabled={!dirty || save.isPending}
          onClick={() => save.mutate(draft)}
        >
          {save.isPending ? "Saving..." : "Save"}
        </button>
        <button
          type="button"
          disabled={draft === "" || save.isPending}
          onClick={() => {
            setDraft("");
            save.mutate("");
          }}
        >
          Clear
        </button>
      </div>
      {save.isError ? <ErrorState error={save.error} /> : null}
    </section>
  );
}
