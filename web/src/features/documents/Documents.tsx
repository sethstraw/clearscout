import { Link } from "react-router-dom";

import { api } from "../../api/client";
import type { Artifact } from "../../api/client";
import { documentName, storedFileName, storedPath } from "../../api/documentNames";
import { useApplicationPipeline, useArtifacts } from "../../api/hooks";
import { Page } from "../../components/Page";
import { StatusBadge } from "../../components/StatusBadge";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { useWorkspace } from "../../providers";

/** The stored types this page counts as a document belonging to a role. */
const TAILORED_DOCUMENTS: ReadonlySet<string> = new Set([
  "resume.markdown",
  "cover_letter.markdown",
  "document.rendered.pdf",
  "document.rendered.text",
  "user.attachment",
]);

/** Every job is asked for, because a document exists whether or not the role is pursued. */
const EVERY_STATUS: readonly string[] = [];

/** List tailored documents across jobs. */
export function DocumentsPage() {
  const { workspaceId } = useWorkspace();
  const jobs = useApplicationPipeline(workspaceId, EVERY_STATUS);
  const artifacts = useArtifacts(workspaceId);

  if (jobs.isLoading || artifacts.isLoading) return <LoadingState label="Finding your documents" />;
  if (jobs.isError) return <ErrorState error={jobs.error} />;
  if (artifacts.isError) return <ErrorState error={artifacts.error} />;
  const documented = new Set(
    (artifacts.data ?? [])
      .filter((artifact) => TAILORED_DOCUMENTS.has(artifact.logical_type))
      .map((artifact) => artifact.related_entity_id),
  );
  const items = (jobs.data?.items ?? []).filter(
    (job) =>
      documented.has(job.job_posting_id) ||
      (job.application_id !== null && documented.has(job.application_id)),
  );

  return (
    <Page
      eyebrow="Apply"
      title="Documents"
      description="Tailored CVs and cover letters for each role."
    >
      {items.length ? (
        <div className="table-wrap">
          <table aria-label="Documents by job">
            <thead>
              <tr>
                <th scope="col">Employer</th>
                <th scope="col">Role</th>
                <th scope="col">Status</th>
                <th scope="col">Files</th>
              </tr>
            </thead>
            <tbody>
              {items.map((job) => (
                <tr key={job.job_posting_id}>
                  <th scope="row">{job.employer}</th>
                  <td>{job.title}</td>
                  <td>
                    <StatusBadge value={job.application_status} />
                  </td>
                  <td>
                    <div className="row-actions">
                      <Link
                        className="button button--quiet"
                        to={`/jobs/${job.job_posting_id}?tab=documents`}
                      >
                        Open job files
                      </Link>
                      {job.application_id && (
                        <Link
                          className="button button--quiet"
                          to={`/applications/${job.application_id}`}
                        >
                          Open application files
                        </Link>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState>
          No tailored documents yet. A job gains them when you tailor a CV or a cover letter for it.
        </EmptyState>
      )}
      <UnattachedFiles workspaceId={workspaceId} />
    </Page>
  );
}

/** Display the source folder recorded during import. */
function importFolder(path: string): string {
  const parts = path.split("/");
  return parts.length > 1 ? parts.slice(0, -1).join("/") : "Loose files";
}

/** Keep imported files with no identified job reachable. */
function UnattachedFiles({ workspaceId }: { workspaceId: string }) {
  const artifacts = useArtifacts(workspaceId);
  if (artifacts.isLoading) return <LoadingState label="Finding unattached imported files" />;
  if (artifacts.isError) return <ErrorState error={artifacts.error} />;
  const orphans = (artifacts.data ?? []).filter((artifact) => !artifact.related_entity_type);
  const folders = new Map<string, Artifact[]>();
  for (const artifact of orphans) {
    const folder = importFolder(storedPath(artifact.provenance));
    folders.set(folder, [...(folders.get(folder) ?? []), artifact]);
  }
  if (!orphans.length) return null;
  return (
    <details className="panel advanced-view">
      <summary>Imported files not tied to a job ({orphans.length})</summary>
      <header>
        <div>
          <p>
            Brought in from an earlier tool without a stored link to a job, so they appear nowhere
            else. They are grouped by the folder they were imported from, which is a record of how
            they were filed, not a claim about which job they belong to.
          </p>
        </div>
      </header>
      {[...folders].map(([folder, files]) => (
        <details className="import-folder" key={folder}>
          <summary>
            <span className="path-value">{folder}</span> ({files.length})
          </summary>
          <div className="table-wrap">
            <table aria-label={`Imported files from ${folder}`}>
              <thead>
                <tr>
                  <th scope="col">File</th>
                  <th scope="col">Size</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {files.map((artifact) => (
                  <tr key={artifact.id}>
                    <th className="path-value" scope="row">
                      {storedFileName(artifact.provenance) || documentName(artifact.logical_type)}
                    </th>
                    <td>{artifact.byte_size.toLocaleString()} bytes</td>
                    <td>
                      <div className="row-actions">
                        <a
                          className="button button--quiet"
                          href={api.artifactUrl(artifact.id, workspaceId)}
                        >
                          Open
                        </a>
                        <a
                          className="button button--quiet"
                          download
                          href={api.artifactUrl(artifact.id, workspaceId, "attachment")}
                        >
                          Download
                        </a>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      ))}
    </details>
  );
}
