import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw/http";
import { describe, expect, it } from "vitest";

import type { OpenApiDocument } from "../../api/openapi";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { ApiPlaygroundPage } from "./ApiPlayground";

const schema = {
  info: { title: "Synthetic ClearScout API", version: "0.2.0" },
  paths: {
    "/health": {
      get: {
        summary: "Read health",
        tags: ["health"],
        responses: { "200": { content: { "application/json": { schema: { type: "object" } } } } },
      },
    },
    "/tasks": {
      post: {
        summary: "Create task",
        tags: ["tasks"],
        requestBody: {
          required: true,
          content: {
            "application/json": {
              schema: { type: "object", properties: { task_type: { type: "string" } } },
            },
          },
        },
        responses: { "202": { content: { "application/json": { schema: { type: "object" } } } } },
      },
    },
  },
} satisfies OpenApiDocument;

describe("ApiPlaygroundPage", () => {
  it("executes safe reads and requires acknowledgement before mutations", async () => {
    const reads: string[] = [];
    const taskRequestHeaders: (string | null)[] = [];
    server.use(
      http.get("*/api/v1/openapi.json", () => HttpResponse.json(schema)),
      http.get("*/health", ({ request }) => {
        reads.push(new URL(request.url).pathname);
        return HttpResponse.json({ status: "healthy", version: "0.2.0" });
      }),
      http.post("*/tasks", ({ request }) => {
        taskRequestHeaders.push(request.headers.get("X-ClearScout-Request"));
        return HttpResponse.json({ id: "task-1" }, { status: 202 });
      }),
    );
    render(
      <AppProviders>
        <ApiPlaygroundPage />
      </AppProviders>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Send request" }));
    await waitFor(() => expect(reads).toEqual(["/health"]));
    expect(await screen.findByText("Response 200 OK")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /POST.*\/tasks.*Create task/ }));
    const send = screen.getByRole("button", { name: "Send request" });
    expect(send.hasAttribute("disabled")).toBe(true);
    fireEvent.click(
      screen.getByLabelText("I understand this operation may change durable local data."),
    );
    expect(send.hasAttribute("disabled")).toBe(false);
    fireEvent.click(send);
    await waitFor(() => expect(taskRequestHeaders).toEqual(["browser"]));
  });
});
