import { useState } from "react";

import { api } from "../../api/client";
import type { Artifact } from "../../api/client";
import {
  ARTIFACT_LOGICAL_TYPES,
  documentName,
  filedBy,
  storedFileName,
  type ArtifactLogicalType,
} from "../../api/documentNames";
import { useWorkspaceMutation } from "../../api/hooks";
import { ConfirmedActionButton } from "../../components/ConfirmedActionButton";
import { formatArtifactSize } from "./jobDocumentFormat";

export function JobDocumentFiles({
  artifacts,
  jobId,
  onSelect,
  selectedId,
  workspaceId,
}: {
  artifacts: Artifact[];
  jobId: string;
  onSelect: (id: string) => void;
  selectedId: string;
  workspaceId: string;
}) {
  const mutate = useWorkspaceMutation(
    workspaceId,
    async ({ action, artifact }: { action: string; artifact: Artifact }) => {
      if (action === "verify")
        return api.enqueueTask({
          workspace_id: workspaceId,
          task_type: "artifact.verify",
          payload: { artifact_id: artifact.id },
        });
      if (action === "archive") return api.archiveArtifact(artifact.id, workspaceId);
      return api.enqueueTask({
        workspace_id: workspaceId,
        task_type: "document.render",
        payload: { artifact_id: artifact.id },
      });
    },
  );
  const upload = useWorkspaceMutation(
    workspaceId,
    ({ file, logicalType }: { file: File; logicalType: ArtifactLogicalType }) =>
      api.uploadArtifact(workspaceId, "job_posting", jobId, logicalType, file),
  );
  const relabel = useWorkspaceMutation(
    workspaceId,
    ({ artifactId, value }: { artifactId: string; value: ArtifactLogicalType }) =>
      api.relabelArtifact(artifactId, { workspace_id: workspaceId, logical_type: value }),
  );
  const [logicalType, setLogicalType] = useState<ArtifactLogicalType>("user.attachment");
  const [relabelId, setRelabelId] = useState("");
  const [newLabel, setNewLabel] = useState<ArtifactLogicalType>("user.attachment");
  const current = artifacts.find((artifact) => artifact.id === selectedId);
  return (
    <>
      <section className="panel document-toolbar">
        <div className="document-add">
          <label className="compact-field">
            Add a related file
            <select
              onChange={(event) => setLogicalType(event.target.value as ArtifactLogicalType)}
              value={logicalType}
            >
              <option value="user.attachment">Attachment</option>
              <option value="posting.source.pdf">Job description PDF</option>
              <option value="recruiter.email">Recruiter email</option>
              <option value="take_home">Take-home exercise</option>
            </select>
          </label>
          <label className="button button--quiet upload-button">
            Choose a file
            <input
              className="visually-hidden"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) upload.mutate({ file, logicalType });
              }}
              type="file"
            />
          </label>
        </div>
        {upload.isError && <p className="field-error">{upload.error.message}</p>}
        <div className="table-wrap">
          <table aria-label="Files held for this job">
            <thead>
              <tr>
                <th scope="col">Document</th>
                <th scope="col">Kind</th>
                <th scope="col">Filed under</th>
                <th scope="col">Added</th>
                <th scope="col">Size</th>
                <th scope="col">Integrity</th>
              </tr>
            </thead>
            <tbody>
              {artifacts.map((artifact) => (
                <tr
                  className={selectedId === artifact.id ? "document-row selected" : "document-row"}
                  key={artifact.id}
                >
                  <th scope="row">
                    <button
                      aria-current={selectedId === artifact.id}
                      className="document-open"
                      onClick={() => onSelect(artifact.id)}
                      type="button"
                    >
                      {storedFileName(artifact.provenance) || documentName(artifact.logical_type)}
                    </button>
                  </th>
                  <td>{documentName(artifact.logical_type)}</td>
                  <td>{filedBy(artifact.related_entity_type)}</td>
                  <td>{new Date(artifact.created_at).toLocaleString()}</td>
                  <td>{formatArtifactSize(artifact.byte_size)}</td>
                  <td>{artifact.integrity_status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      {current && (
        <section className="panel document-actions">
          <div className="row-actions">
            <a
              className="button button--quiet"
              download
              href={api.artifactUrl(current.id, workspaceId, "attachment")}
            >
              Download
            </a>
            <button
              className="button button--quiet"
              onClick={() => mutate.mutate({ action: "verify", artifact: current })}
              type="button"
            >
              Verify
            </button>
            <button
              className="button button--quiet"
              onClick={() => {
                setRelabelId(current.id);
                setNewLabel(current.logical_type);
              }}
              type="button"
            >
              Relabel
            </button>
            {current.mime_type === "text/markdown" && (
              <button
                className="button button--quiet"
                onClick={() => mutate.mutate({ action: "render", artifact: current })}
                type="button"
              >
                Save as PDF or text
              </button>
            )}
            <ConfirmedActionButton
              className="button button--quiet"
              onConfirm={() => mutate.mutate({ action: "archive", artifact: current })}
              options={{
                title: "Archive this document?",
                description:
                  "It will leave active job documents but remain in durable local history.",
                confirmLabel: "Archive document",
                danger: true,
              }}
            >
              Archive
            </ConfirmedActionButton>
          </div>
          {current.mime_type === "text/markdown" && (
            <p className="field-note">
              Saving writes the file to this machine. ClearScout does not typeset a resume; the
              sibling Pressume application does that.
            </p>
          )}
          {relabelId === current.id && (
            <form
              className="relabel-form"
              onSubmit={(event) => {
                event.preventDefault();
                relabel.mutate(
                  { artifactId: current.id, value: newLabel },
                  { onSuccess: () => setRelabelId("") },
                );
              }}
            >
              <select
                aria-label="New file label"
                onChange={(event) => setNewLabel(event.target.value as ArtifactLogicalType)}
                value={newLabel}
              >
                {ARTIFACT_LOGICAL_TYPES.map((type) => (
                  <option key={type} value={type}>
                    {documentName(type)}
                  </option>
                ))}
              </select>
              <button className="button button--quiet" type="submit">
                Save label
              </button>
            </form>
          )}
        </section>
      )}
    </>
  );
}
