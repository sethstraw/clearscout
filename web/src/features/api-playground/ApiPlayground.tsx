import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import {
  buildRequest,
  isStreamingOperation,
  operationsFrom,
  parameterDefaults,
  parameterKey,
  parametersFor,
  requestMediaType,
  requestSchema,
  sampleFromSchema,
} from "../../api/openapi";
import type { ApiOperation, OpenApiDocument } from "../../api/openapi";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { Page } from "../../components/Page";
import { titleCase } from "../../components/text";
import { useWorkspace } from "../../providers";

type PlaygroundResponse = { status: string; contentType: string; body: string };

async function loadOpenApi(): Promise<OpenApiDocument> {
  const response = await fetch("/api/v1/openapi.json", { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`OpenAPI schema could not be loaded (${response.status}).`);
  return (await response.json()) as OpenApiDocument;
}

export function ApiPlaygroundPage() {
  const { workspaceId } = useWorkspace();
  const schema = useQuery({ queryKey: ["openapi"], queryFn: loadOpenApi, staleTime: Infinity });
  const operations = useMemo(() => (schema.data ? operationsFrom(schema.data) : []), [schema.data]);
  const [search, setSearch] = useState("");
  const [selectedKey, setSelectedKey] = useState("");
  const selected = operations.find((item) => item.key === selectedKey) ?? operations[0];
  const filtered = operations.filter((item) =>
    `${item.method} ${item.path} ${item.shape.summary ?? ""} ${item.tag}`
      .toLocaleLowerCase()
      .includes(search.trim().toLocaleLowerCase()),
  );
  if (schema.isLoading) return <LoadingState label="Reading the generated API contract" />;
  if (schema.isError || !schema.data) return <ErrorState error={schema.error} />;
  return (
    <Page
      action={
        <a className="button button--quiet" href="/api/v1/openapi.json">
          Open raw schema
        </a>
      }
      description="Inspect and debug this local integration surface from its generated OpenAPI schema. Normal job-search work belongs in the guided web workflow, including evaluating discoveries into ledger jobs."
      eyebrow={`OpenAPI ${schema.data.info.version}`}
      title="API Playground"
    >
      <aside className="playground-warning" role="note">
        <strong>Local data boundary</strong>
        <p>
          Requests stay on this loopback TailorCV server. Mutating operations require an extra
          acknowledgement and may append or archive local records. This playground is for
          integration work and debugging, not the normal path for creating a job.
        </p>
      </aside>
      <div className="playground-layout">
        <section className="panel endpoint-browser" aria-label="API operations">
          <label className="search-field">
            <span>Filter operations</span>
            <input
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Method, path, summary, or tag"
              type="search"
              value={search}
            />
          </label>
          <p className="endpoint-count" aria-live="polite">
            {filtered.length} of {operations.length} operations
          </p>
          <nav>
            {groupOperations(filtered).map(([tag, items]) => (
              <section key={tag}>
                <h2>{titleCase(tag)}</h2>
                {items.map((operation) => (
                  <button
                    aria-current={operation.key === selected?.key ? "page" : undefined}
                    className={operation.key === selected?.key ? "selected" : ""}
                    key={operation.key}
                    onClick={() => setSelectedKey(operation.key)}
                    type="button"
                  >
                    <code className={`http-method http-method--${operation.method}`}>
                      {operation.method.toUpperCase()}
                    </code>
                    <span>{operation.path}</span>
                    <small>{operation.shape.summary ?? operation.shape.operationId}</small>
                  </button>
                ))}
              </section>
            ))}
          </nav>
          {!filtered.length && <EmptyState>No operations match that filter.</EmptyState>}
        </section>
        {selected && (
          <OperationPanel
            document={schema.data}
            key={selected.key}
            operation={selected}
            workspaceId={workspaceId}
          />
        )}
      </div>
    </Page>
  );
}

function OperationPanel({
  document,
  operation,
  workspaceId,
}: {
  document: OpenApiDocument;
  operation: ApiOperation;
  workspaceId: string;
}) {
  const [values, setValues] = useState(() => parameterDefaults(operation, document, workspaceId));
  const [body, setBody] = useState(() =>
    JSON.stringify(sampleFromSchema(requestSchema(operation), document, workspaceId), null, 2),
  );
  const [acknowledged, setAcknowledged] = useState(false);
  const [result, setResult] = useState<PlaygroundResponse | null>(null);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const mutates = operation.method !== "get";
  const mediaType = requestMediaType(operation);
  const streaming = isStreamingOperation(operation);
  const binaryRequest = Boolean(operation.shape.requestBody && mediaType !== "application/json");
  const execute = async () => {
    setError("");
    setResult(null);
    setRunning(true);
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 30_000);
    try {
      const request = buildRequest(operation, document, values, body);
      const response = await fetch(request.url, { ...request.init, signal: controller.signal });
      const contentType = response.headers.get("content-type") ?? "not stated";
      const responseBody = contentType.includes("json")
        ? JSON.stringify(await response.json(), null, 2)
        : contentType.startsWith("text/")
          ? await response.text()
          : `[${(await response.arrayBuffer()).byteLength.toLocaleString()} binary bytes]`;
      setResult({
        status: `${response.status} ${response.statusText}`,
        contentType,
        body: responseBody,
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The request failed.");
    } finally {
      window.clearTimeout(timeout);
      setRunning(false);
    }
  };
  return (
    <section className="panel operation-panel">
      <header>
        <div>
          <p className="operation-path">
            <code className={`http-method http-method--${operation.method}`}>
              {operation.method.toUpperCase()}
            </code>{" "}
            <code>{operation.path}</code>
          </p>
          <h2>{operation.shape.summary ?? operation.shape.operationId ?? "API operation"}</h2>
          {operation.shape.description && <p>{operation.shape.description}</p>}
        </div>
        <span className="operation-tag">{operation.tag}</span>
      </header>
      <div className="operation-form">
        {parametersFor(operation).map((parameter) => (
          <label key={parameterKey(parameter)}>
            <span>
              {parameter.name}{" "}
              <small>
                {parameter.in}
                {parameter.required ? " - required" : ""}
              </small>
            </span>
            <input
              onChange={(event) =>
                setValues((current) => ({
                  ...current,
                  [parameterKey(parameter)]: event.target.value,
                }))
              }
              required={parameter.required}
              value={values[parameterKey(parameter)] ?? ""}
            />
            {parameter.description && <small>{parameter.description}</small>}
          </label>
        ))}
        {operation.shape.requestBody && (
          <label>
            <span>
              Request body <small>{mediaType ?? "unknown media type"}</small>
            </span>
            <textarea
              disabled={binaryRequest}
              onChange={(event) => setBody(event.target.value)}
              rows={16}
              spellCheck={false}
              value={
                binaryRequest
                  ? "Binary request bodies are available through the file panels."
                  : body
              }
            />
          </label>
        )}
        {mutates && (
          <label className="mutation-acknowledgement">
            <input
              checked={acknowledged}
              onChange={(event) => setAcknowledged(event.target.checked)}
              type="checkbox"
            />
            I understand this operation may change durable local data.
          </label>
        )}
        {streaming && (
          <p className="field-note">
            SSE streams are documented here but run from the Tasks page, which handles reconnection
            and Last-Event-ID.
          </p>
        )}
        {binaryRequest && (
          <p className="field-note">
            Binary uploads are documented here and executed from bounded job/application file
            panels.
          </p>
        )}
        {error && (
          <p className="field-error" role="alert">
            {error}
          </p>
        )}
        <button
          className="button button--primary"
          disabled={running || streaming || binaryRequest || (mutates && !acknowledged)}
          onClick={() => {
            void execute();
          }}
          type="button"
        >
          {running ? "Sending..." : "Send request"}
        </button>
      </div>
      {result && (
        <section className="playground-response" aria-live="polite">
          <header>
            <strong>Response {result.status}</strong>
            <span>{result.contentType}</span>
          </header>
          <pre>{result.body || "[empty response]"}</pre>
        </section>
      )}
    </section>
  );
}

function groupOperations(operations: ApiOperation[]): Array<[string, ApiOperation[]]> {
  const groups = new Map<string, ApiOperation[]>();
  for (const operation of operations) {
    groups.set(operation.tag, [...(groups.get(operation.tag) ?? []), operation]);
  }
  return [...groups.entries()];
}
