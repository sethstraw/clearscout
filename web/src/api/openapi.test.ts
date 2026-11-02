import { describe, expect, it, vi } from "vitest";

import {
  buildRequest,
  isStreamingOperation,
  operationsFrom,
  parameterDefaults,
  requestSchema,
  sampleFromSchema,
} from "./openapi";
import type { ApiOperation, OpenApiDocument } from "./openapi";

const document: OpenApiDocument = {
  info: { title: "Synthetic API", version: "0.2.0" },
  paths: {
    "/workspaces/{workspace_id}/items": {
      get: {
        summary: "List items",
        tags: ["items"],
        parameters: [
          { name: "workspace_id", in: "path", required: true, schema: { type: "string" } },
          { name: "search", in: "query", schema: { type: "string", default: "open" } },
        ],
        responses: { "200": { content: { "application/json": { schema: { type: "object" } } } } },
      },
      post: {
        summary: "Create item",
        tags: ["items"],
        parameters: [
          { name: "workspace_id", in: "path", required: true, schema: { type: "string" } },
          { name: "Idempotency-Key", in: "header", required: true, schema: { type: "string" } },
        ],
        requestBody: {
          required: true,
          content: { "application/json": { schema: { $ref: "#/components/schemas/CreateItem" } } },
        },
        responses: { "201": { content: { "application/json": { schema: { type: "object" } } } } },
      },
    },
    "/tasks/{task_id}/events": {
      get: {
        tags: ["tasks"],
        responses: { "200": { content: { "text/event-stream": { schema: { type: "string" } } } } },
      },
    },
  },
  components: {
    schemas: {
      CreateItem: {
        type: "object",
        required: ["workspace_id", "title"],
        properties: {
          workspace_id: { type: "string" },
          title: { type: "string", default: "Draft" },
          enabled: { type: "boolean" },
          labels: { type: "array", items: { type: "string" } },
        },
      },
    },
  },
};

function operation(method: "get" | "post", path = "/workspaces/{workspace_id}/items") {
  return operationsFrom(document).find(
    (item) => item.method === method && item.path === path,
  ) as ApiOperation;
}

describe("OpenAPI playground utilities", () => {
  it("enumerates and groups the live contract without inventing operations", () => {
    const operations = operationsFrom(document);
    expect(operations.map((item) => item.key)).toEqual([
      "get:/workspaces/{workspace_id}/items",
      "post:/workspaces/{workspace_id}/items",
      "get:/tasks/{task_id}/events",
    ]);
  });

  it("builds a schema-derived JSON sample with the active workspace", () => {
    const create = operation("post");
    expect(sampleFromSchema(requestSchema(create), document, "workspace-7")).toEqual({
      workspace_id: "workspace-7",
      title: "Draft",
      enabled: false,
      labels: [],
    });
  });

  it("builds encoded paths, query strings, headers, and JSON bodies", () => {
    vi.spyOn(crypto, "randomUUID").mockReturnValue("00000000-0000-4000-8000-000000000001");
    const create = operation("post");
    const defaults = parameterDefaults(create, document, "workspace / 7");
    const request = buildRequest(create, document, defaults, '{"title":"Draft"}');
    expect(request.url).toBe("/workspaces/workspace%20%2F%207/items");
    expect(new Headers(request.init.headers).get("Idempotency-Key")).toBe(
      "00000000-0000-4000-8000-000000000001",
    );
    expect(request.init.body).toBe('{"title":"Draft"}');

    const list = operation("get");
    expect(
      buildRequest(list, document, parameterDefaults(list, document, "workspace-7"), "").url,
    ).toBe("/workspaces/workspace-7/items?search=open");
  });

  it("rejects missing required parameters and malformed JSON before fetch", () => {
    const create = operation("post");
    expect(() => buildRequest(create, document, {}, "{}")).toThrow("workspace_id is required");
    expect(() =>
      buildRequest(
        create,
        document,
        parameterDefaults(create, document, "workspace-7"),
        "not json",
      ),
    ).toThrow("valid JSON");
  });

  it("identifies streams so the playground can defer to reconnect-aware task UI", () => {
    expect(isStreamingOperation(operation("get", "/tasks/{task_id}/events"))).toBe(true);
    expect(isStreamingOperation(operation("get"))).toBe(false);
  });
});
