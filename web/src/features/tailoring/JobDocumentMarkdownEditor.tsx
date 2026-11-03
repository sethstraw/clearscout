import { useEffect, useMemo, useState } from "react";

import { api } from "../../api/client";
import type { Artifact, CitationCheck } from "../../api/client";
import { useArtifactVersions, useWorkspaceMutation } from "../../api/hooks";
import { text } from "../../api/json";
import { ConfirmedActionButton } from "../../components/ConfirmedActionButton";
import { MarkdownWorkspace } from "../../components/MarkdownWorkspace";
import { formatUsd } from "../../components/text";
import { UnifiedDiff } from "../../components/UnifiedDiff";
import { JobDocumentApprovalReview } from "./JobDocumentApprovalReview";
import { ArtifactHeader } from "./JobDocumentPresentation";

export function JobDocumentMarkdownEditor({
  artifact,
  initial,
  workspaceId,
}: {
  artifact: Artifact;
  initial: string;
  workspaceId: string;
}) {
  const [content, setContent] = useState(initial);
  const [citation, setCitation] = useState<CitationCheck | null>(null);
  const [overrideReason, setOverrideReason] = useState("");
  // The older version is the "from" side, or every change reads reversed. The
  // version number rides along so the result can say which pair it is.
  const [diff, setDiff] = useState<{ version: number; text: string } | null>(null);
  const versions = useArtifactVersions(workspaceId, artifact.id);
  const dirty = content !== initial;
  const activeCitation = dirty ? citation : null;
  const save = useWorkspaceMutation(workspaceId, () =>
    api.saveArtifact(artifact.id, {
      workspace_id: workspaceId,
      content,
      author_type: "human",
      override_reason: overrideReason,
    }),
  );
  const revert = useWorkspaceMutation(workspaceId, (targetId: string) =>
    api.revertArtifact(artifact.id, { workspace_id: workspaceId, target_artifact_id: targetId }),
  );
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (dirty) event.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  useEffect(() => {
    if (!dirty) return undefined;
    const timer = window.setTimeout(() => {
      void api.citationCheck(artifact.id, { workspace_id: workspaceId, content }).then(setCitation);
    }, 500);
    return () => window.clearTimeout(timer);
  }, [artifact.id, content, dirty, workspaceId]);
  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        if (dirty && (!activeCitation?.requires_override || overrideReason.trim()))
          save.mutate(undefined);
      }
    };
    window.addEventListener("keydown", shortcut);
    return () => window.removeEventListener("keydown", shortcut);
    // Without this list the effect rebinds a window listener on every render.
  }, [activeCitation?.requires_override, dirty, overrideReason, save]);
  const backed = useMemo(() => {
    const value = artifact.provenance.line_citations;
    return Array.isArray(value) ? value.length : 0;
  }, [artifact.provenance]);
  return (
    <article className="panel markdown-editor">
      <ArtifactHeader artifact={artifact} />
      <div className="editor-toolbar">
        <span>
          {backed} evidence-backed line{backed === 1 ? "" : "s"}
        </span>
        <span>{dirty ? "Unsaved changes" : "Saved"}</span>
        <button
          className="button button--primary"
          disabled={
            !dirty ||
            save.isPending ||
            Boolean(activeCitation?.requires_override && !overrideReason.trim())
          }
          onClick={() => save.mutate(undefined)}
          type="button"
        >
          {save.isPending ? "Saving" : "Save as new version"}
        </button>
      </div>
      {activeCitation?.requires_override && (
        <aside className="citation-warning" role="alert">
          <strong>
            {activeCitation.broken.length} citation-backed line
            {activeCitation.broken.length === 1 ? "" : "s"} changed
          </strong>
          <ul>
            {activeCitation.broken.map((item) => (
              <li key={String(item.line_number)}>
                Line {String(item.line_number)}: {String(item.text)}
              </li>
            ))}
          </ul>
          <label>
            Reason for the human override
            <textarea
              onChange={(event) => setOverrideReason(event.target.value)}
              required
              rows={3}
              value={overrideReason}
            />
          </label>
          <p>Saving records a digest-bound ReviewProposal and ApprovalDecision.</p>
        </aside>
      )}
      {save.isError && <p className="field-error">{save.error.message}</p>}
      <MarkdownWorkspace content={content} onChange={setContent} />
      <section className="version-history">
        <h3>Version history</h3>
        <ul>
          {versions.data?.map((version) => (
            <li key={version.id}>
              <span>
                v{version.version_number}, {version.author_type},{" "}
                {new Date(version.created_at).toLocaleString()}
              </span>
              <small>
                {text(version.provenance.prompt_suite) || "No prompt fingerprint"}, CV{" "}
                {text(version.provenance.master_cv_hash) || "not recorded"}, model{" "}
                {Object.values(version.provenance.served_models ?? {}).join(", ") || "not recorded"}
                , cost{" "}
                {formatUsd(text(version.provenance.cost_usd) || null, {
                  unknown: "not stated",
                })}
              </small>
              <div>
                <button
                  onClick={() => {
                    void api
                      .artifactDiff(version.id, artifact.id, workspaceId)
                      .then((value) =>
                        setDiff({ version: version.version_number ?? 0, text: value.diff }),
                      );
                  }}
                  type="button"
                >
                  Diff
                </button>
                <ConfirmedActionButton
                  disabled={version.id === artifact.id}
                  onConfirm={() => revert.mutate(version.id)}
                  options={{
                    title: `Restore version ${version.version_number}?`,
                    description:
                      "ClearScout will preserve the current content and append the selected content as a new version.",
                    confirmLabel: "Restore as new version",
                  }}
                >
                  Revert as new version
                </ConfirmedActionButton>
              </div>
            </li>
          ))}
        </ul>
        {diff && (
          <UnifiedDiff diff={diff.text} label={`Changes from v${diff.version} to this version.`} />
        )}
      </section>
      {artifact.tailoring_run_id && (
        <JobDocumentApprovalReview
          jobId={String(artifact.related_entity_id)}
          runId={artifact.tailoring_run_id}
          workspaceId={workspaceId}
        />
      )}
    </article>
  );
}
