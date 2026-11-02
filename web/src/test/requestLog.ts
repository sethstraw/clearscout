type RecordedRequest = { method: string; url: URL; body: unknown };

/**
 * Collects the exact wire requests an MSW handler sees, for the tests whose
 * contract is the request rather than what the component renders.
 */
export function requestLog() {
  const requests: RecordedRequest[] = [];
  const record = async (request: Request) => {
    const text = request.method === "GET" ? "" : await request.clone().text();
    requests.push({
      method: request.method,
      url: new URL(request.url),
      body: text ? (JSON.parse(text) as unknown) : null,
    });
  };
  const reads = (url: URL) => requests.push({ method: "GET", url, body: null });
  return { record, reads, requests };
}
