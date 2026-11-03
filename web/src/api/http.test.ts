import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, requestIntentJson, requestJson, requestText, requestVoid } from "./http";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/** Typed so an assertion can read back the RequestInit the helper built. */
function stubFetch(response: Response) {
  return vi
    .fn<(input: string, init?: RequestInit) => Promise<Response>>()
    .mockResolvedValue(response);
}

describe("HTTP helpers", () => {
  it("applies the versioned prefix and JSON headers consistently", async () => {
    const fetch = stubFetch(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetch);
    await expect(
      requestJson<{ ok: boolean }>("/health", {
        method: "POST",
        headers: { "Idempotency-Key": "stable-1" },
        body: JSON.stringify({ value: 1 }),
      }),
    ).resolves.toEqual({ ok: true });
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/health",
      expect.objectContaining({
        method: "POST",
      }),
    );
    const headers = new Headers(fetch.mock.calls[0]?.[1]?.headers);
    expect(headers.get("Accept")).toBe("application/json");
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.get("Idempotency-Key")).toBe("stable-1");
    expect(headers.get("X-ClearScout-Request")).toBe("browser");
  });

  it("reuses one key and body for the same active intent, then retires both on success", async () => {
    const fetch = vi
      .fn<(input: string, init?: RequestInit) => Promise<Response>>()
      .mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ ok: true }))));
    vi.stubGlobal("fetch", fetch);
    vi.spyOn(crypto, "randomUUID")
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000001")
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000002")
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000003");
    const firstBody = JSON.stringify({ value: 1, occurred_at: "first" });

    await Promise.all([
      requestIntentJson("/items", "same-intent", firstBody),
      requestIntentJson(
        "/items",
        "same-intent",
        JSON.stringify({ value: 1, occurred_at: "second" }),
      ),
    ]);
    await requestIntentJson("/items", "same-intent", firstBody);
    await requestIntentJson("/items", "different-intent", firstBody);

    const keys = fetch.mock.calls.map((call) =>
      new Headers(call[1]?.headers).get("Idempotency-Key"),
    );
    expect(keys).toEqual([
      "00000000-0000-4000-8000-000000000001",
      "00000000-0000-4000-8000-000000000001",
      "00000000-0000-4000-8000-000000000002",
      "00000000-0000-4000-8000-000000000003",
    ]);
    expect(fetch.mock.calls[1]?.[1]?.body).toBe(firstBody);
  });

  it("keeps a failed intent stable for an explicit retry", async () => {
    const fetch = vi
      .fn<(input: string, init?: RequestInit) => Promise<Response>>()
      .mockRejectedValueOnce(new TypeError("connection lost"))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true })));
    vi.stubGlobal("fetch", fetch);
    const randomUUID = vi
      .spyOn(crypto, "randomUUID")
      .mockReturnValue("00000000-0000-4000-8000-000000000004");
    const firstBody = JSON.stringify({ status: "applied", occurred_at: "first" });

    await expect(requestIntentJson("/events", "same-intent", firstBody)).rejects.toThrow(
      "connection lost",
    );
    await expect(
      requestIntentJson(
        "/events",
        "same-intent",
        JSON.stringify({ status: "applied", occurred_at: "second" }),
      ),
    ).resolves.toEqual({ ok: true });

    expect(randomUUID).toHaveBeenCalledTimes(1);
    expect(
      fetch.mock.calls.map((call) => new Headers(call[1]?.headers).get("Idempotency-Key")),
    ).toEqual(Array(2).fill("00000000-0000-4000-8000-000000000004"));
    expect(fetch.mock.calls.map((call) => call[1]?.body)).toEqual(Array(2).fill(firstBody));
  });

  it("expires a failed intent after five minutes", async () => {
    const fetch = vi
      .fn<(input: string, init?: RequestInit) => Promise<Response>>()
      .mockRejectedValueOnce(new TypeError("connection lost"))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true })));
    vi.stubGlobal("fetch", fetch);
    vi.spyOn(Date, "now").mockReturnValueOnce(0).mockReturnValueOnce(0).mockReturnValue(300_001);
    vi.spyOn(crypto, "randomUUID")
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000005")
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000006");

    await expect(requestIntentJson("/expired", "same-intent", "first-body")).rejects.toThrow();
    await expect(requestIntentJson("/expired", "same-intent", "second-body")).resolves.toEqual({
      ok: true,
    });

    expect(
      fetch.mock.calls.map((call) => new Headers(call[1]?.headers).get("Idempotency-Key")),
    ).toEqual(["00000000-0000-4000-8000-000000000005", "00000000-0000-4000-8000-000000000006"]);
    expect(fetch.mock.calls[1]?.[1]?.body).toBe("second-body");
  });

  it("surfaces safe problem detail and correlation IDs", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: "The record changed.",
            correlation_id: "problem-42",
          }),
          { status: 409, headers: { "Content-Type": "application/problem+json" } },
        ),
      ),
    );
    const error = await requestVoid("/jobs/1", { method: "DELETE" }).catch(
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      message: "The record changed.",
      status: 409,
      correlationId: "problem-42",
    });
  });

  it("preserves an explicit binary content type", async () => {
    const fetch = stubFetch(
      new Response(JSON.stringify({ id: "file-1" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetch);
    await requestJson("/artifacts/upload", {
      method: "POST",
      headers: { "Content-Type": "application/pdf" },
      body: new Blob(["synthetic"], { type: "application/pdf" }),
    });
    expect(new Headers(fetch.mock.calls[0]?.[1]?.headers).get("Content-Type")).toBe(
      "application/pdf",
    );
  });

  it("resolves without parsing an empty successful response body", async () => {
    const fetch = stubFetch(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetch);
    await expect(requestJson<void>("/sources/1", { method: "DELETE" })).resolves.toBeUndefined();
  });

  it("falls back safely when an error body is not JSON", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("not json", {
          status: 503,
          headers: { "X-Correlation-ID": "header-9" },
        }),
      ),
    );
    await expect(requestText("/report")).rejects.toMatchObject({
      message: "ClearScout request failed (503).",
      correlationId: "header-9",
    });
  });
});
