import type { Artifact } from "../../api/client";
import { documentName, filedBy } from "../../api/documentNames";
import { StatusBadge } from "../../components/StatusBadge";
import { formatArtifactSize } from "./jobDocumentFormat";

export function ArtifactHeader({ artifact }: { artifact: Artifact }) {
  return (
    <header className="artifact-header">
      <div>
        <h2>{documentName(artifact.logical_type)}</h2>
        <p>
          {filedBy(artifact.related_entity_type)}, {formatArtifactSize(artifact.byte_size)}, SHA-256{" "}
          <span title={artifact.sha256}>{artifact.sha256.slice(0, 12)}</span>,{" "}
          {artifact.integrity_status}
        </p>
      </div>
      <StatusBadge value={artifact.author_type} />
    </header>
  );
}
