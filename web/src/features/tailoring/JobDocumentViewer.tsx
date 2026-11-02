import { api } from "../../api/client";
import type { Artifact } from "../../api/client";
import { documentName } from "../../api/documentNames";
import { useArtifactText } from "../../api/hooks";
import { MarkdownView } from "../../components/MarkdownView";
import { ErrorState, LoadingState } from "../../components/States";
import { JobDocumentMarkdownEditor } from "./JobDocumentMarkdownEditor";
import { ArtifactHeader } from "./JobDocumentPresentation";

export function JobDocumentViewer({
  artifact,
  workspaceId,
}: {
  artifact: Artifact;
  workspaceId: string;
}) {
  const isText =
    artifact.mime_type.startsWith("text/") || artifact.mime_type === "application/json";
  const text = useArtifactText(workspaceId, artifact.id, isText);
  if (artifact.mime_type === "application/pdf")
    return (
      <article className="panel artifact-viewer">
        <ArtifactHeader artifact={artifact} />
        <object
          aria-label={documentName(artifact.logical_type)}
          data={api.artifactUrl(artifact.id, workspaceId)}
          type="application/pdf"
        >
          <a href={api.artifactUrl(artifact.id, workspaceId, "attachment")}>Download PDF</a>
        </object>
      </article>
    );
  if (artifact.mime_type.startsWith("image/") && artifact.inline_viewable)
    return (
      <article className="panel artifact-viewer">
        <ArtifactHeader artifact={artifact} />
        <img
          alt={documentName(artifact.logical_type)}
          src={api.artifactUrl(artifact.id, workspaceId)}
        />
      </article>
    );
  if (!isText)
    return (
      <article className="panel artifact-viewer">
        <ArtifactHeader artifact={artifact} />
        <p>This file type is available as a verified download.</p>
      </article>
    );
  if (text.isLoading) return <LoadingState label="Verifying file" />;
  if (text.isError || text.data === undefined) return <ErrorState error={text.error} />;
  if (artifact.editable && artifact.mime_type === "text/markdown")
    return (
      <JobDocumentMarkdownEditor
        artifact={artifact}
        initial={text.data}
        workspaceId={workspaceId}
      />
    );
  return (
    <article className="panel artifact-viewer">
      <ArtifactHeader artifact={artifact} />
      {artifact.logical_type.endsWith("markdown") ? (
        <MarkdownView>{text.data ?? ""}</MarkdownView>
      ) : artifact.mime_type === "application/json" ? (
        <JsonDocument content={text.data} />
      ) : (
        <pre>{text.data}</pre>
      )}
    </article>
  );
}

function JsonDocument({ content }: { content: string }) {
  if (content.length > 250_000)
    return (
      <p className="json-document">
        This JSON document is too large to render here. Download it to inspect the complete file.
      </p>
    );
  let value: unknown;
  try {
    value = JSON.parse(content) as unknown;
  } catch {
    return (
      <p className="json-document" role="alert">
        This stored JSON document is invalid. Download it to inspect the complete file.
      </p>
    );
  }
  return <JsonNode depth={0} label="Document" value={value} />;
}

function JsonNode({ depth, label, value }: { depth: number; label: string; value: unknown }) {
  if (depth >= 12)
    return (
      <span className="json-value">
        <span className="json-key">{label}</span>
        Nested content is omitted from this view.
      </span>
    );
  if (Array.isArray(value)) {
    const items = value as unknown[];
    return (
      <details className="json-document json-node" open={depth === 0}>
        <summary>
          <span className="json-key">{label}</span>
          <span className="json-type">Array ({items.length})</span>
        </summary>
        <ul>
          {items.map((item, index) => (
            <li key={index}>
              <JsonNode depth={depth + 1} label={`[${index}]`} value={item} />
            </li>
          ))}
        </ul>
      </details>
    );
  }
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    return (
      <details className="json-document json-node" open={depth === 0}>
        <summary>
          <span className="json-key">{label}</span>
          <span className="json-type">Object ({entries.length})</span>
        </summary>
        <ul>
          {entries.map(([key, child]) => (
            <li key={key}>
              <JsonNode depth={depth + 1} label={key} value={child} />
            </li>
          ))}
        </ul>
      </details>
    );
  }
  return (
    <span className="json-value">
      <span className="json-key">{label}</span>
      {typeof value === "string" ? JSON.stringify(value) : String(value)}
    </span>
  );
}
