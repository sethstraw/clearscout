import { text } from "./json";

type OpenApiSchema = {
  $ref?: string;
  type?: string;
  format?: string;
  description?: string;
  default?: unknown;
  example?: unknown;
  enum?: unknown[];
  properties?: Record<string, OpenApiSchema>;
  required?: string[];
  items?: OpenApiSchema;
  allOf?: OpenApiSchema[];
  anyOf?: OpenApiSchema[];
  oneOf?: OpenApiSchema[];
};

type OpenApiParameter = {
  $ref?: string;
  name: string;
  in: "path" | "query" | "header" | "cookie";
  description?: string;
  required?: boolean;
  schema?: OpenApiSchema;
};

type OpenApiOperationShape = {
  operationId?: string;
  summary?: string;
  description?: string;
  tags?: string[];
  parameters?: OpenApiParameter[];
  requestBody?: {
    required?: boolean;
    content?: Record<string, { schema?: OpenApiSchema }>;
  };
  responses?: Record<string, { content?: Record<string, { schema?: OpenApiSchema }> }>;
};

type OpenApiPathItem = Partial<Record<HttpMethod, OpenApiOperationShape>> & {
  parameters?: OpenApiParameter[];
};

export type OpenApiDocument = {
  info: { title: string; version: string; description?: string };
  paths: Record<string, OpenApiPathItem>;
  components?: {
    schemas?: Record<string, OpenApiSchema>;
    parameters?: Record<string, OpenApiParameter>;
  };
};

type HttpMethod = "get" | "post" | "put" | "patch" | "delete";

export type ApiOperation = {
  key: string;
  method: HttpMethod;
  path: string;
  shape: OpenApiOperationShape;
  pathParameters: OpenApiParameter[];
  tag: string;
};

const HTTP_METHODS: readonly HttpMethod[] = ["get", "post", "put", "patch", "delete"];

export function operationsFrom(document: OpenApiDocument): ApiOperation[] {
  return Object.entries(document.paths)
    .flatMap(([path, item]) =>
      HTTP_METHODS.flatMap((method) => {
        const shape = item[method];
        return shape
          ? [
              {
                key: `${method}:${path}`,
                method,
                path,
                shape,
                pathParameters: item.parameters ?? [],
                tag: shape.tags?.[0] ?? "other",
              },
            ]
          : [];
      }),
    )
    .sort(
      (left, right) => left.tag.localeCompare(right.tag) || left.path.localeCompare(right.path),
    );
}

export function parametersFor(operation: ApiOperation): OpenApiParameter[] {
  return [...operation.pathParameters, ...(operation.shape.parameters ?? [])];
}

export function requestMediaType(operation: ApiOperation): string | null {
  const types = Object.keys(operation.shape.requestBody?.content ?? {});
  return types.includes("application/json") ? "application/json" : (types[0] ?? null);
}

export function requestSchema(operation: ApiOperation): OpenApiSchema | null {
  const mediaType = requestMediaType(operation);
  return mediaType ? (operation.shape.requestBody?.content?.[mediaType]?.schema ?? null) : null;
}

export function sampleFromSchema(
  schema: OpenApiSchema | null,
  document: OpenApiDocument,
  workspaceId = "",
  propertyName = "",
  depth = 0,
): unknown {
  if (!schema || depth > 8) return null;
  const resolved = resolveSchema(schema, document);
  if (resolved.example !== undefined) return resolved.example;
  if (resolved.default !== undefined) return resolved.default;
  if (resolved.enum?.length) return resolved.enum[0];
  if (propertyName === "workspace_id" && workspaceId) return workspaceId;
  const variants = resolved.oneOf ?? resolved.anyOf;
  if (variants?.length) {
    const candidate = variants.find((item) => item.type !== "null") ?? variants[0];
    return sampleFromSchema(candidate ?? null, document, workspaceId, propertyName, depth + 1);
  }
  if (resolved.allOf?.length) {
    const parts = resolved.allOf.map((item) =>
      sampleFromSchema(item, document, workspaceId, propertyName, depth + 1),
    );
    return Object.assign({}, ...parts.filter(isRecord));
  }
  if (resolved.type === "object" || resolved.properties) {
    return Object.fromEntries(
      Object.entries(resolved.properties ?? {}).map(([name, value]) => [
        name,
        sampleFromSchema(value, document, workspaceId, name, depth + 1),
      ]),
    );
  }
  if (resolved.type === "array") return [];
  if (resolved.type === "boolean") return false;
  if (resolved.type === "integer" || resolved.type === "number") return 0;
  if (resolved.format === "date-time") return new Date().toISOString();
  if (resolved.format === "date") return new Date().toISOString().slice(0, 10);
  return "";
}

export function parameterDefaults(
  operation: ApiOperation,
  document: OpenApiDocument,
  workspaceId: string,
): Record<string, string> {
  return Object.fromEntries(
    parametersFor(operation).map((parameter) => {
      const resolved = resolveParameter(parameter, document);
      const sample = sampleFromSchema(
        resolved.schema ?? null,
        document,
        workspaceId,
        resolved.name,
      );
      const value =
        resolved.name.toLocaleLowerCase() === "idempotency-key"
          ? crypto.randomUUID()
          : text(sample);
      return [parameterKey(resolved), value];
    }),
  );
}

export function parameterKey(parameter: Pick<OpenApiParameter, "in" | "name">): string {
  return `${parameter.in}:${parameter.name}`;
}

export function buildRequest(
  operation: ApiOperation,
  document: OpenApiDocument,
  values: Record<string, string>,
  body: string,
): { url: string; init: RequestInit } {
  let path = operation.path;
  const query = new URLSearchParams();
  const headers = new Headers({ Accept: "application/json" });
  for (const parameter of parametersFor(operation)) {
    const resolved = resolveParameter(parameter, document);
    const value = values[parameterKey(resolved)]?.trim() ?? "";
    if (resolved.required && !value) {
      throw new Error(`${resolved.name} is required.`);
    }
    if (!value) continue;
    if (resolved.in === "path")
      path = path.replace(`{${resolved.name}}`, encodeURIComponent(value));
    if (resolved.in === "query") query.append(resolved.name, value);
    if (resolved.in === "header") headers.set(resolved.name, value);
  }
  if (/\{[^}]+\}/.test(path)) throw new Error("Every path parameter is required.");
  const mediaType = requestMediaType(operation);
  const init: RequestInit = { method: operation.method.toUpperCase(), headers };
  if (operation.method !== "get") headers.set("X-ClearScout-Request", "browser");
  if (operation.shape.requestBody) {
    if (mediaType !== "application/json") {
      throw new Error(
        "Binary and multipart request bodies use the file panels, not this playground.",
      );
    }
    try {
      JSON.parse(body);
    } catch (error) {
      throw new Error("The request body must be valid JSON.", { cause: error });
    }
    headers.set("Content-Type", mediaType);
    init.body = body;
  }
  const suffix = query.toString();
  return { url: suffix ? `${path}?${suffix}` : path, init };
}

export function isStreamingOperation(operation: ApiOperation): boolean {
  return Object.values(operation.shape.responses ?? {}).some((response) =>
    Object.keys(response.content ?? {}).includes("text/event-stream"),
  );
}

function resolveSchema(schema: OpenApiSchema, document: OpenApiDocument): OpenApiSchema {
  if (!schema.$ref) return schema;
  const name = schema.$ref.split("/").at(-1) ?? "";
  return document.components?.schemas?.[name] ?? schema;
}

function resolveParameter(
  parameter: OpenApiParameter,
  document: OpenApiDocument,
): OpenApiParameter {
  if (!parameter.$ref) return parameter;
  const name = parameter.$ref.split("/").at(-1) ?? "";
  return document.components?.parameters?.[name] ?? parameter;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
