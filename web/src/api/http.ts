type Problem = { detail?: string; correlation_id?: string };
type IntentRequest = { body: string; expiresAt: number; key: string; pending: number };

// Failed requests keep their original key briefly so a retry cannot become a
// second write. A successful request retires the intent immediately.
const RETRY_WINDOW_MS = 5 * 60 * 1000;
const UNSAFE_METHODS = new Set(["DELETE", "PATCH", "POST", "PUT"]);
const intentRequests = new Map<string, IntentRequest>();

export class ApiError extends Error {
  status: number;
  correlationId?: string;

  constructor(message: string, status: number, correlationId?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.correlationId = correlationId;
  }
}

async function responseError(response: Response): Promise<ApiError> {
  const problem = (await response.json().catch(() => null)) as Problem | null;
  return new ApiError(
    problem?.detail ?? `ClearScout request failed (${response.status}).`,
    response.status,
    problem?.correlation_id ?? response.headers?.get("X-Correlation-ID") ?? undefined,
  );
}

function requestHeaders(init?: RequestInit): Headers {
  const headers = new Headers(init?.headers);
  if (!headers.has("Accept")) headers.set("Accept", "application/json");
  if (typeof init?.body === "string" && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (init?.method && UNSAFE_METHODS.has(init.method.toUpperCase())) {
    headers.set("X-ClearScout-Request", "browser");
  }
  return headers;
}

export async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    ...init,
    headers: requestHeaders(init),
  });
  if (!response.ok) throw await responseError(response);
  if (!hasJsonBody(response)) return undefined as T;
  return (await response.json()) as T;
}

function hasJsonBody(response: Response): boolean {
  if (response.status === 204 || response.status === 205) return false;
  const length = response.headers?.get("Content-Length");
  if (length !== null && length !== undefined && Number.parseInt(length, 10) === 0) return false;
  return true;
}

export async function requestIntentJson<T>(path: string, intent: string, body: string): Promise<T> {
  const now = Date.now();
  for (const [id, request] of intentRequests) {
    if (request.pending === 0 && request.expiresAt <= now) intentRequests.delete(id);
  }
  const intentId = `${path}\n${intent}`;
  const existing = intentRequests.get(intentId);
  const entry = existing
    ? existing
    : { body, expiresAt: Number.POSITIVE_INFINITY, key: crypto.randomUUID(), pending: 0 };
  entry.pending += 1;
  intentRequests.set(intentId, entry);
  try {
    const result = await requestJson<T>(path, {
      method: "POST",
      headers: { "Idempotency-Key": entry.key },
      body: entry.body,
    });
    entry.pending -= 1;
    if (intentRequests.get(intentId)?.key === entry.key && entry.pending === 0) {
      intentRequests.delete(intentId);
    }
    return result;
  } catch (error) {
    entry.pending -= 1;
    if (intentRequests.get(intentId)?.key === entry.key) {
      entry.expiresAt =
        entry.pending === 0 ? Date.now() + RETRY_WINDOW_MS : Number.POSITIVE_INFINITY;
    }
    throw error;
  }
}

export async function requestText(path: string): Promise<string> {
  const response = await fetch(`/api/v1${path}`, { headers: { Accept: "text/plain" } });
  if (!response.ok) throw await responseError(response);
  return response.text();
}

export async function requestVoid(path: string, init?: RequestInit): Promise<void> {
  const response = await fetch(`/api/v1${path}`, {
    ...init,
    headers: requestHeaders(init),
  });
  if (!response.ok) throw await responseError(response);
}
