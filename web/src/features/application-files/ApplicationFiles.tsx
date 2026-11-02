import { useQuery } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api } from "../../api/client";
import type { Artifact } from "../../api/client";
import {
  ARTIFACT_LOGICAL_TYPES,
  documentName,
  filedBy,
  type ArtifactLogicalType,
} from "../../api/documentNames";
import { useArtifacts, useTags, useWorkspaceMutation } from "../../api/hooks";
import { text } from "../../api/json";
import { ConfirmedActionButton } from "../../components/ConfirmedActionButton";
import { EntityTags } from "../../components/EntityTags";
import { Page } from "../../components/Page";
import { SortableHeader } from "../../components/SortableHeader";
import { compareValues, nextSort, type SortDirection } from "../../components/sorting";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { titleCase } from "../../components/text";
import { useWorkspace } from "../../providers";

type ArtifactSort =
  | "logical_type"
  | "filed_by"
  | "byte_size"
  | "sha256"
  | "integrity_status"
  | "created_at"
  | "provenance";

export function ApplicationFilesPage() {
  const { applicationId = "" } = useParams();
  const { workspaceId } = useWorkspace();
  const [sortKey, setSortKey] = useState<ArtifactSort>("created_at");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const [submissionRole, setSubmissionRole] = useState<
    "resume" | "cover_letter" | "other" | "related"
  >("related");
  const options = useQuery({
    queryKey: ["application-options", workspaceId],
    queryFn: () => api.applicationOptions(workspaceId),
  });
  // A document names the run that produced it, not the application, so this asks
  // for the whole job rather than the few files uploaded here by hand.
  const artifacts = useArtifacts(workspaceId, "application", applicationId, true);
  const tags = useTags(workspaceId);
  const upload = useWorkspaceMutation(workspaceId, (file: File) =>
    api.uploadArtifact(
      workspaceId,
      "application",
      applicationId,
      "user.attachment",
      file,
      submissionRole === "related" ? undefined : submissionRole,
    ),
  );
  const action = useWorkspaceMutation(
    workspaceId,
    async ({ artifactId, kind }: { artifactId: string; kind: "verify" | "archive" }) =>
      kind === "verify"
        ? api.enqueueTask({
            workspace_id: workspaceId,
            task_type: "artifact.verify",
            payload: { artifact_id: artifactId },
          })
        : api.archiveArtifact(artifactId, workspaceId),
  );
  if (options.isLoading || artifacts.isLoading)
    return <LoadingState label="Opening application files" />;
  if (options.isError || artifacts.isError)
    return <ErrorState error={options.error ?? artifacts.error} />;
  const application = options.data?.find((item) => item.id === applicationId);
  const rows = [...(artifacts.data ?? [])].sort((left, right) =>
    compareValues(
      artifactSortValue(left, sortKey),
      artifactSortValue(right, sortKey),
      sortDirection,
    ),
  );
  const submittedRows = rows.filter((artifact) => isSubmittedArtifact(artifact, applicationId));
  const relatedRows = rows.filter((artifact) => !isSubmittedArtifact(artifact, applicationId));
  const sortBy = (column: ArtifactSort) => {
    const next = nextSort(sortKey, sortDirection, column);
    setSortKey(next.key);
    setSortDirection(next.direction);
  };
  return (
    <Page
      action={
        <Link className="button button--quiet" to="/applications">
          Back to applications
        </Link>
      }
      eyebrow="Apply"
      title={application?.title ?? "Application"}
      description="Submitted files and related drafts are shown separately, with each file's stored SHA-256."
    >
      <EntityTags
        entityId={applicationId}
        entityType="application"
        tags={tags.data ?? []}
        workspaceId={workspaceId}
      />
      <section className="panel application-files">
        <header>
          <div>
            <h2>Application files</h2>
            <p>Choose Submitted only for the exact file you sent with this application.</p>
          </div>
          <div className="file-actions">
            <label>
              File role
              <select
                onChange={(event) => setSubmissionRole(event.target.value as typeof submissionRole)}
                value={submissionRole}
              >
                <option value="related">Related file, not submitted</option>
                <option value="resume">Submitted resume</option>
                <option value="cover_letter">Submitted cover letter</option>
                <option value="other">Other submitted file</option>
              </select>
            </label>
            <label className="button button--quiet">
              {upload.isPending ? "Uploading" : "Upload file"}
              <input
                className="visually-hidden"
                disabled={upload.isPending}
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) upload.mutate(file);
                  event.target.value = "";
                }}
                type="file"
              />
            </label>
          </div>
        </header>
        {rows.length === 0 ? (
          <EmptyState>
            No files yet. Evaluation and tailoring file their documents here, and Upload file adds
            one you already have.
          </EmptyState>
        ) : (
          <div className="table-wrap">
            <table aria-label="Application files">
              <thead>
                <tr>
                  <SortableHeader
                    activeKey={sortKey}
                    column="logical_type"
                    direction={sortDirection}
                    label="Document"
                    onSort={sortBy}
                  />
                  <SortableHeader
                    activeKey={sortKey}
                    column="filed_by"
                    direction={sortDirection}
                    label="Filed by"
                    onSort={sortBy}
                  />
                  <SortableHeader
                    activeKey={sortKey}
                    column="byte_size"
                    direction={sortDirection}
                    label="Size"
                    onSort={sortBy}
                  />
                  <SortableHeader
                    activeKey={sortKey}
                    column="sha256"
                    direction={sortDirection}
                    label="SHA-256"
                    onSort={sortBy}
                  />
                  <SortableHeader
                    activeKey={sortKey}
                    column="integrity_status"
                    direction={sortDirection}
                    label="Verification"
                    onSort={sortBy}
                  />
                  <SortableHeader
                    activeKey={sortKey}
                    column="created_at"
                    direction={sortDirection}
                    label="Created"
                    onSort={sortBy}
                  />
                  <SortableHeader
                    activeKey={sortKey}
                    column="provenance"
                    direction={sortDirection}
                    label="Origin"
                    onSort={sortBy}
                  />
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {submittedRows.length > 0 && (
                  <tr>
                    <th colSpan={8} scope="rowgroup">
                      Submitted files
                    </th>
                  </tr>
                )}
                {[...submittedRows, ...relatedRows].map((artifact, index) => (
                  <Fragment key={artifact.id}>
                    {index === submittedRows.length && relatedRows.length > 0 && (
                      <tr>
                        <th colSpan={8} scope="rowgroup">
                          Related files and drafts
                        </th>
                      </tr>
                    )}
                    <tr key={artifact.id}>
                      <td title={artifact.logical_type}>{submittedName(artifact)}</td>
                      <td>{filedBy(artifact.related_entity_type)}</td>
                      <td>{artifact.byte_size.toLocaleString()} bytes</td>
                      <td className="path-value" title={artifact.sha256}>
                        {artifact.sha256.slice(0, 12)}
                      </td>
                      <td>{titleCase(artifact.integrity_status)}</td>
                      <td>{new Date(artifact.created_at).toLocaleString()}</td>
                      <td>{artifactProvenance(artifact)}</td>
                      <td>
                        <div className="file-actions">
                          <a href={api.artifactUrl(artifact.id, workspaceId)}>Open</a>
                          <a
                            download
                            href={api.artifactUrl(artifact.id, workspaceId, "attachment")}
                          >
                            Download
                          </a>
                          <button
                            onClick={() =>
                              action.mutate({ artifactId: artifact.id, kind: "verify" })
                            }
                            type="button"
                          >
                            Verify
                          </button>
                          <ConfirmedActionButton
                            onConfirm={() =>
                              action.mutate({ artifactId: artifact.id, kind: "archive" })
                            }
                            options={{
                              title: "Archive this file?",
                              description:
                                "It will leave active application files but remain in durable local history.",
                              confirmLabel: "Archive file",
                              danger: true,
                            }}
                          >
                            Archive
                          </ConfirmedActionButton>
                        </div>
                        <Relabel artifact={artifact} workspaceId={workspaceId} />
                      </td>
                    </tr>
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </Page>
  );
}

function artifactProvenance(artifact: Artifact) {
  const origin = text(artifact.provenance.source ?? artifact.provenance.mode);
  return [origin, artifact.author_type].filter(Boolean).map(titleCase).join(", ") || "Not recorded";
}

function isSubmittedArtifact(artifact: Artifact, applicationId: string): boolean {
  const role = artifact.provenance.submission_role;
  return (
    artifact.related_entity_type === "application" &&
    artifact.related_entity_id === applicationId &&
    (role === "resume" || role === "cover_letter" || role === "other")
  );
}

function submittedName(artifact: Artifact): string {
  const role = artifact.provenance.submission_role;
  if (role === "resume") return "Submitted resume";
  if (role === "cover_letter") return "Submitted cover letter";
  if (role === "other") return "Other submitted file";
  return documentName(artifact.logical_type);
}

function artifactSortValue(artifact: Artifact, sort: ArtifactSort) {
  if (sort === "created_at") return Date.parse(artifact.created_at);
  if (sort === "provenance") return artifactProvenance(artifact);
  if (sort === "logical_type") return documentName(artifact.logical_type);
  if (sort === "filed_by") return filedBy(artifact.related_entity_type);
  return artifact[sort];
}

function Relabel({ artifact, workspaceId }: { artifact: Artifact; workspaceId: string }) {
  const [value, setValue] = useState(artifact.logical_type);
  const mutation = useWorkspaceMutation(workspaceId, () =>
    api.relabelArtifact(artifact.id, { workspace_id: workspaceId, logical_type: value }),
  );
  return (
    <details className="relabel-details">
      <summary>Relabel</summary>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          mutation.mutate(undefined);
        }}
      >
        <select
          aria-label="New file label"
          onChange={(event) => setValue(event.target.value as ArtifactLogicalType)}
          value={value}
        >
          {ARTIFACT_LOGICAL_TYPES.map((type) => (
            <option key={type} value={type}>
              {documentName(type)}
            </option>
          ))}
        </select>
        <button type="submit">Save</button>
      </form>
    </details>
  );
}
