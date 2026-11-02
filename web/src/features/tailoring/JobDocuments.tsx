import { useState } from "react";
import { Link } from "react-router-dom";

import { useArtifacts } from "../../api/hooks";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { JobDocumentFiles } from "./JobDocumentFiles";
import { JobDocumentViewer } from "./JobDocumentViewer";

export function JobDocuments({ jobId, workspaceId }: { jobId: string; workspaceId: string }) {
  // Evaluation, tailoring and posting checks each file against their own run, so
  // asking only for the job itself returns almost nothing.
  const artifacts = useArtifacts(workspaceId, "job_posting", jobId, true);
  const [selectedId, setSelectedId] = useState("");
  if (artifacts.isLoading) return <LoadingState label="Opening documents" />;
  if (artifacts.isError) return <ErrorState error={artifacts.error} />;
  const selected = artifacts.data?.find((item) => item.id === selectedId) ?? artifacts.data?.[0];
  return (
    <section className="documents-stack">
      <JobDocumentFiles
        artifacts={artifacts.data ?? []}
        jobId={jobId}
        onSelect={setSelectedId}
        selectedId={selected?.id ?? ""}
        workspaceId={workspaceId}
      />
      {selected ? (
        <JobDocumentViewer key={selected.id} artifact={selected} workspaceId={workspaceId} />
      ) : (
        <article className="panel">
          <h2>Job files</h2>
          <EmptyState>Generate a package in Studio, or upload a related file above.</EmptyState>
          <Link className="button button--quiet" to={`/jobs/${jobId}?tab=studio`}>
            Open Studio
          </Link>
        </article>
      )}
    </section>
  );
}
